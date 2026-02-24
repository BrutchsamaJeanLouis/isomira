#!/usr/bin/env python3
"""
Isomira -- Single-model TDD orchestrator (dual-profile).
Phases A-D complete. One file. One loop. Tests decide when it's done.
Phase B: Command sandboxing (write-path, sudo allowlist, foreground blocking).
Uses one model (Devstral 24B) with different temperature/prompt profiles for planning vs coding.
"""

import ast
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

# =============================================
# PUBLIC API — entry points and key functions
# =============================================
__all__ = [
    # Entry point
    "run",
    # Config
    "CONFIG", "PROFILES",
    # Model interface
    "call_model",
    # Command execution (independently testable)
    "sandbox_check", "execute_command",
    # Parsers
    "parse_json_output", "parse_file_blocks", "parse_cmd_blocks",
    "normalize_plan", "extract_review_code",
    # Codebase analysis
    "summarise_codebase",
    # Test runner
    "run_tests",
]

# ---------------------------------------------
# CONFIG
# ---------------------------------------------

CONFIG = {
    "planner_model": "mistralai_devstral-small-2-24b-instruct-2512",
    "implementer_model": "mistralai_devstral-small-2-24b-instruct-2512",
    "consultant_model": "mistralai_ministral-3-14b-reasoning-2512",
    "lmstudio_url": "http://localhost:1234/v1",
    "workspace": "./workspace",
    "max_context_tokens": 16000,
    "consultant_max_context_tokens": 61440,
    "cmd_timeout_default": 30,
    "cmd_timeout_install": 300,
}

# Dual-profile tuning: same model, different sampling parameters.
# Planner profile runs hotter for broader test/plan exploration.
# Implementer profile runs tighter for precise code generation.
# Conservative fallback used on retry-after-failure.
PROFILES = {
    "planner": {
        "temperature": 0.6,
        "top_p": 0.95,
        "top_k": 0,
        "min_p": 0.05,
        "repeat_penalty": 1.05,
        "max_tokens": 2048,
    },
    "implementer": {
        "temperature": 0.4,
        "top_p": 0.85,
        "top_k": 0,
        "min_p": 0.05,
        "repeat_penalty": 1.05,
        "max_tokens": 4096,
    },
    "conservative": {
        "temperature": 0.2,
        "top_p": 0.85,
        "top_k": 0,
        "min_p": 0.05,
        "repeat_penalty": 1.05,
        "max_tokens": 4096,
    },
    "consultant": {
        "temperature": 0.3,
        "top_p": 0.9,
        "top_k": 0,
        "min_p": 0.05,
        "repeat_penalty": 1.05,
        "max_tokens": 8192,
    },
}

# ---------------------------------------------
# TOKEN ESTIMATION
# ---------------------------------------------

def estimate_tokens(text: str) -> int:
    """Rough token count. ~3 chars per token for code-heavy content.
    Biased toward overcounting (compress early, not late)."""
    return len(text) // 3


def strip_think_blocks(text: str) -> str:
    """Remove <think>...</think> reasoning blocks from model output.
    Reasoning models (Ministral) emit chain-of-thought in <think> tags
    before the actual response. Strip these before parsing JSON."""
    return re.sub(r"<think>[\s\S]*?</think>", "", text, flags=re.DOTALL).strip()


# ---------------------------------------------
# MODEL CALLING
# ---------------------------------------------

def call_model(model_name: str, system_prompt: str, user_prompt: str,
               profile: str = "implementer") -> str:
    """
    Call LMStudio API. Blocking. Model autoswap handled by LMStudio.
    Profile selects sampling parameters: "planner", "implementer", or "conservative".
    """
    import requests

    params = PROFILES.get(profile, PROFILES["implementer"])
    url = f"{CONFIG['lmstudio_url']}/chat/completions"
    payload = {
        "model": model_name,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": params["temperature"],
        "top_k": params["top_k"],
        "min_p": params["min_p"],
        "top_p": params["top_p"],
        "repeat_penalty": params["repeat_penalty"],
        "max_tokens": params["max_tokens"],
        "stream": False,
    }

    log(f"  -> Calling {model_name} ({estimate_tokens(system_prompt + user_prompt)} est. tokens in)")

    last_err = None
    for attempt in range(4):  # 1 initial + 3 retries
        try:
            response = requests.post(url, json=payload, timeout=300)
            response.raise_for_status()
            content = response.json()["choices"][0]["message"]["content"]
            log(f"  <- Got {estimate_tokens(content)} est. tokens back")
            return content
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
            last_err = e
            if attempt < 3:
                delay = [2, 8, 32][attempt]
                log(f"  Model call failed (attempt {attempt + 1}/4): {e}")
                log(f"  Retrying in {delay}s (LMStudio may be swapping models)...")
                time.sleep(delay)
            else:
                fatal(f"Cannot connect to LMStudio after 4 attempts: {last_err}")
        except Exception as e:
            fatal(f"Model call failed: {e}")


# ---------------------------------------------
# LOGGING
# ---------------------------------------------

LOG_FILE = None

def log(msg: str):
    """Print and optionally write to log file."""
    ts = time.strftime("%H:%M:%S")
    line = f"[{ts}] {msg}"
    # Safe print: replace unencodable chars on Windows consoles (cp1252 etc.)
    print(line.encode(sys.stdout.encoding or "utf-8", errors="replace").decode(sys.stdout.encoding or "utf-8", errors="replace"))
    if LOG_FILE:
        LOG_FILE.write(line + "\n")
        LOG_FILE.flush()


def fatal(msg: str):
    """Log error, beep, and exit."""
    log(f"FATAL: {msg}")
    print("\a", end="", flush=True)
    sys.exit(1)


# ---------------------------------------------
# PHASE 1: SUMMARISE (no model -- AST parsing)
# ---------------------------------------------

def summarise_codebase(workspace: Path) -> str:
    """
    Generate a compressed codebase summary from the workspace.
    Python files: parsed via ast for function/class signatures.
    Other files: listed with line counts.
    """
    if not workspace.exists():
        return "(empty workspace)"

    lines = ["# Codebase Summary\n"]

    # File tree
    all_files = sorted(workspace.rglob("*"))
    all_files = [f for f in all_files if f.is_file()
                 and "__pycache__" not in str(f)
                 and ".pytest_cache" not in str(f)]

    if not all_files:
        return "(empty workspace)"

    lines.append("## File Tree")
    for f in all_files:
        rel = f.relative_to(workspace)
        lc = sum(1 for _ in open(f, errors="ignore"))
        lines.append(f"  {rel} ({lc} lines)")

    # Python file details
    py_files = [f for f in all_files if f.suffix == ".py"]
    if py_files:
        lines.append("\n## Python Signatures")
        for f in py_files:
            rel = f.relative_to(workspace)
            lines.append(f"\n### {rel}")
            try:
                source = f.read_text(errors="ignore")
                tree = ast.parse(source)
                for node in ast.walk(tree):
                    if isinstance(node, ast.FunctionDef):
                        args = ", ".join(a.arg for a in node.args.args)
                        lines.append(f"  def {node.name}({args})")
                    elif isinstance(node, ast.ClassDef):
                        lines.append(f"  class {node.name}")
            except SyntaxError:
                lines.append("  (syntax error -- could not parse)")

    # Import graph
    if py_files:
        lines.append("\n## Imports")
        for f in py_files:
            rel = f.relative_to(workspace)
            try:
                source = f.read_text(errors="ignore")
                tree = ast.parse(source)
                imports = []
                for node in ast.walk(tree):
                    if isinstance(node, ast.Import):
                        imports.extend(a.name for a in node.names)
                    elif isinstance(node, ast.ImportFrom):
                        imports.append(node.module or "")
                if imports:
                    lines.append(f"  {rel}: {', '.join(imports)}")
            except SyntaxError:
                pass

    return "\n".join(lines)


# ---------------------------------------------
# OUTPUT PARSERS
# ---------------------------------------------

def extract_review_code(review_data: dict) -> str:
    """
    Extract corrected code snippets from review plan entries.
    The planner often returns plan entries with a "code" key containing
    the exact corrected function. Collect these so the implementer can use them.
    Returns a formatted string of all code corrections, or empty string.
    """
    corrections = []
    plan_entries = review_data.get("plan", [])
    if not isinstance(plan_entries, list):
        return ""

    for entry in plan_entries:
        if not isinstance(entry, dict):
            continue
        code = entry.get("code", "")
        desc = entry.get("description", entry.get("rationale",
               entry.get("reason", entry.get("action", ""))))
        if code and len(code.strip()) > 10:
            header = f"# Fix: {desc}" if desc else "# Correction from review"
            corrections.append(f"{header}\n{code.strip()}")

    if not corrections:
        return ""
    return "\n\n".join(corrections)


def count_test_functions(content: str) -> int:
    """Count the number of test functions (def test_...) in a string."""
    return len(re.findall(r"^def test_", content, re.MULTILINE))


def parse_json_output(text: str) -> dict:
    """
    Extract JSON from model output. Handles markdown fences.
    """
    # Try stripping markdown code fences
    cleaned = re.sub(r"^```(?:json)?\s*\n?", "", text.strip(), flags=re.MULTILINE)
    cleaned = re.sub(r"\n?```\s*$", "", cleaned.strip(), flags=re.MULTILINE)

    # Try direct parse
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass

    # Try finding JSON object in the text
    match = re.search(r"\{[\s\S]*\}", text)
    if match:
        try:
            return json.loads(match.group())
        except json.JSONDecodeError:
            pass

    raise ValueError(f"Could not parse JSON from model output:\n{text[:500]}")


def parse_file_blocks(text: str) -> list[dict]:
    """
    Extract ===FILE: path=== ... ===END FILE=== blocks.
    Returns list of {"path": str, "content": str}.
    """
    blocks = []
    pattern = r"===FILE:\s*(.+?)===\s*\n([\s\S]*?)===END FILE==="
    for match in re.finditer(pattern, text):
        blocks.append({
            "path": match.group(1).strip(),
            "content": match.group(2),
        })
    return blocks


def parse_cmd_blocks(text: str) -> list[str]:
    """
    Extract ===CMD=== ... ===END CMD=== blocks.
    """
    cmds = []
    pattern = r"===CMD===\s*\n([\s\S]*?)===END CMD==="
    for match in re.finditer(pattern, text):
        cmds.append(match.group(1).strip())
    return cmds


def normalize_plan(plan: list, fallback_file: str = "") -> list:
    """
    Normalize plan entries from model output so keys are consistent.
    Models return wildly varying schemas -- this tries hard to extract
    a usable plan entry from whatever structure the model invented.

    fallback_file: when set, entries with no detectable file path
    inherit this file. Used when review plans describe function-level
    fixes without naming the target file (single-file tasks).
    """
    FILE_KEYS = ("file", "filename", "filepath", "path", "file_path",
                 "target", "source", "module", "target_file", "source_file")
    ACTION_KEYS = ("action", "operation", "type", "mode")
    PY_FILE_RE = re.compile(r"[\w/\\.\-]+\.py\b")

    normalized = []
    for entry in plan:
        if not isinstance(entry, dict):
            # If entry is a string that looks like a filename, wrap it
            if isinstance(entry, str) and PY_FILE_RE.search(entry):
                entry = {"file": PY_FILE_RE.search(entry).group()}
            else:
                continue
        out = dict(entry)

        # Canonicalize file key -- check known key names
        if "file" not in out:
            for k in FILE_KEYS:
                if k in out and isinstance(out[k], str) and "." in out[k]:
                    out["file"] = out.pop(k)
                    break

        # Last resort: scan ALL string values for a .py path
        if "file" not in out:
            for k, v in out.items():
                if isinstance(v, str):
                    m = PY_FILE_RE.search(v)
                    if m:
                        out["file"] = m.group()
                        break

        # Final fallback: use the file from the existing plan
        if "file" not in out and fallback_file:
            out["file"] = fallback_file

        # Canonicalize action key
        if "action" not in out:
            for k in ACTION_KEYS:
                if k in out:
                    out["action"] = out.pop(k)
                    break
            if "action" not in out:
                out["action"] = "modify"

        # Strip workspace/ prefix if model included it
        if "file" in out:
            f = out["file"]
            for prefix in ("workspace/", "workspace\\", "./", ".\\"):
                if f.startswith(prefix):
                    f = f[len(prefix):]
            out["file"] = f

        # Ensure functions list exists
        if "functions" not in out:
            out["functions"] = []

        if "file" in out:
            normalized.append(out)

    return normalized


# ---------------------------------------------
# COMMAND EXECUTOR (Phase B: sandboxed)
# ---------------------------------------------

# Sudo subcommands that are safe for unattended use.
# Anything not on this list gets blocked.
SUDO_ALLOWLIST = frozenset({
    "apt", "apt-get", "dpkg",
    "systemctl", "service",
    "kill", "killall", "pkill",
    "lsof", "fuser",
    "ufw",
    "netstat", "ss",
})

# Commands/patterns that run forever or require interactive input.
# Block these unconditionally.
FOREGROUND_PATTERNS = [
    r"\btail\s+-f\b",
    r"\bwatch\b",
    r"\bpython\s+-m\s+http\.server\b",
    r"\bnpm\s+run\s+dev\b",
    r"\bnpm\s+start\b",
    r"\bnode\s+.*--watch\b",
    r"\bflask\s+run\b",
    r"\buvicorn\b",
    r"\bgunicorn\b",
    r"\bjupyter\b",
    r"\bless\b",
    r"\bmore\b",
    r"\bvi\b",
    r"\bvim\b",
    r"\bnano\b",
    r"\btop\b",
    r"\bhtop\b",
]

# Shell operators and commands that produce file output.
_WRITE_INDICATORS = re.compile(
    r"(?:"
    r"\s>\s|\s>>\s"           # redirect operators (space-padded to avoid false positives on >> in heredocs)
    r"|>(?!/dev/null)"        # bare redirect (but allow /dev/null)
    r"|\btee\s"               # tee command
    r"|\bmv\s|\bcp\s"         # move/copy
    r"|\brm\s|\brmdir\s"      # remove
    r"|\bmkdir\s"             # create dir
    r"|\btouch\s"             # create file
    r"|\bchmod\s|\bchown\s"   # permission changes
    r"|\bln\s"                # symlinks
    r"|\binstall\s"           # coreutils install
    r"|\bdd\s"                # disk dump
    r"|\bwget\s|\bcurl\s.*-o" # download to file
    r")"
)


def _resolve_write_targets(cmd: str) -> list[str]:
    """
    Best-effort extraction of paths a command might write to.
    Returns a list of path strings found after write-producing operators.
    Not foolproof -- defence in depth with workspace cwd.
    """
    targets = []

    # Redirects: anything after > or >>
    for m in re.finditer(r">{1,2}\s*(\S+)", cmd):
        targets.append(m.group(1))

    # tee targets
    for m in re.finditer(r"\btee\s+(?:-a\s+)?(\S+)", cmd):
        targets.append(m.group(1))

    # rm / mv / cp / mkdir / touch / chmod / chown -- last arg or -o flag
    for m in re.finditer(r"\b(?:rm|mv|cp|mkdir|touch|chmod|chown|ln)\s+(.+?)(?:\s*[;&|]|$)", cmd):
        # Take all non-flag tokens as potential targets
        for token in m.group(1).split():
            if not token.startswith("-"):
                targets.append(token)

    # wget -O / curl -o
    for m in re.finditer(r"\b(?:wget\s+.*-O|curl\s+.*-o)\s*(\S+)", cmd):
        targets.append(m.group(1))

    # -o / --output flags (generic)
    for m in re.finditer(r"(?:-o|--output)\s+(\S+)", cmd):
        targets.append(m.group(1))

    return targets


def _is_inside_workspace(target: str, workspace: Path) -> bool:
    """Check if a path target resolves inside the workspace."""
    # /dev/null is always OK
    if target.strip() in ("/dev/null", "NUL", "nul"):
        return True
    try:
        resolved = (workspace / target).resolve()
        ws_resolved = workspace.resolve()
        return str(resolved).startswith(str(ws_resolved))
    except (ValueError, OSError):
        return False


def sandbox_check(cmd: str, workspace: Path) -> str | None:
    """
    Check if a command is safe to execute. Returns None if OK,
    or a human-readable block reason if the command should not run.
    """
    stripped = cmd.strip()

    # 1. Foreground / interactive process detection
    for pattern in FOREGROUND_PATTERNS:
        if re.search(pattern, stripped):
            return (f"BLOCKED: Foreground/interactive process detected ({pattern}). "
                    f"Rewrite as a one-shot command or background with timeout.")

    # 2. Sudo allowlist
    sudo_match = re.match(r"^sudo\s+(\S+)", stripped)
    if sudo_match:
        sudo_subcmd = sudo_match.group(1)
        # Strip path prefix (e.g., /usr/bin/apt -> apt)
        sudo_subcmd = sudo_subcmd.rsplit("/", 1)[-1]
        if sudo_subcmd not in SUDO_ALLOWLIST:
            return (f"BLOCKED: sudo {sudo_subcmd} is not on the allowed list. "
                    f"Allowed sudo commands: {', '.join(sorted(SUDO_ALLOWLIST))}")
        # Also check write targets for sudo commands
        # Remove the sudo prefix and check the rest
        inner_cmd = stripped[stripped.index(sudo_subcmd):]
        targets = _resolve_write_targets(inner_cmd)
        for t in targets:
            if not _is_inside_workspace(t, workspace):
                # sudo apt/systemctl etc. write to system paths -- that's expected
                # Only block sudo rm/mv/cp/chmod/chown outside workspace
                if any(op in inner_cmd for op in ("rm ", "mv ", "cp ", "chmod ", "chown ")):
                    return (f"BLOCKED: sudo command writes outside workspace: {t}")

    # 3. Write-path checking (non-sudo commands)
    if not sudo_match and _WRITE_INDICATORS.search(stripped):
        targets = _resolve_write_targets(stripped)
        for t in targets:
            if not _is_inside_workspace(t, workspace):
                return (f"BLOCKED: Command writes outside workspace: {t}. "
                        f"All file modifications must target paths within {workspace}")

    return None  # Command is OK


def execute_command(cmd: str, workspace: Path) -> dict:
    """
    Execute a shell command with sandboxing.
    Phase B: write-path enforcement, sudo allowlist, foreground blocking.
    Returns {"stdout", "stderr", "returncode", "timed_out"}.
    """
    # Sandbox check
    block_reason = sandbox_check(cmd, workspace)
    if block_reason:
        log(f"  {block_reason}")
        print("\a", end="", flush=True)  # beep on blocked command
        return {
            "stdout": "",
            "stderr": block_reason,
            "returncode": -1,
            "timed_out": False,
        }

    # Determine timeout
    install_patterns = ["apt install", "pip install", "npm install", "cargo build"]
    timeout = CONFIG["cmd_timeout_default"]
    for pat in install_patterns:
        if pat in cmd:
            timeout = CONFIG["cmd_timeout_install"]
            break

    log(f"  EXEC: {cmd} (timeout={timeout}s)")

    try:
        result = subprocess.run(
            cmd,
            shell=True,
            cwd=str(workspace),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return {
            "stdout": result.stdout[:8000],
            "stderr": result.stderr[:8000],
            "returncode": result.returncode,
            "timed_out": False,
        }
    except subprocess.TimeoutExpired:
        return {
            "stdout": "",
            "stderr": f"Command timed out after {timeout}s",
            "returncode": -1,
            "timed_out": True,
        }
    except Exception as e:
        return {
            "stdout": "",
            "stderr": str(e),
            "returncode": -1,
            "timed_out": False,
        }


# ---------------------------------------------
# TEST RUNNER
# ---------------------------------------------

def run_tests(workspace: Path, test_filename: str) -> dict:
    """
    Run pytest on the test file. Returns {"passed": bool, "output": str}.
    """
    test_path = workspace / test_filename
    if not test_path.exists():
        return {"passed": False, "output": f"Test file not found: {test_filename}"}

    result = execute_command(
        f"python -m pytest {test_filename} -v --tb=short 2>&1",
        workspace,
    )

    output = result["stdout"] + result["stderr"]
    passed = result["returncode"] == 0

    return {"passed": passed, "output": output}


# ---------------------------------------------
# CONTEXT ASSEMBLY
# ---------------------------------------------

def read_file_safe(path: Path) -> str:
    """Read a file, return empty string if missing."""
    try:
        return path.read_text(errors="ignore")
    except FileNotFoundError:
        return ""


def truncate_context(text: str, max_tokens: int) -> str:
    """Truncate text to approximate token limit."""
    max_chars = max_tokens * 3
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "\n\n[...truncated to fit context window...]"


def assemble_plan_context(
    philosophy: str, task: str, codebase_summary: str, scope_files: dict[str, str]
) -> tuple[str, str]:
    """
    Assemble system + user prompts for the PLAN phase.
    Returns (system_prompt, user_prompt).
    """
    system_prompt = f"""{philosophy}

You are the planning profile in a single-model TDD pipeline. Your job:
1. Analyse the task against the current codebase.
2. Write pytest test functions FIRST that define the expected behaviour.
   Tests must be runnable independently. Use only stdlib + pytest.
3. Then write an implementation plan: which files to create/modify,
   function signatures, and pseudocode per function.

Output format (strict -- the orchestrator parses this):

{{
  "tests": {{
    "filename": "test_<module>.py",
    "content": "<full pytest file content>"
  }},
  "plan": [
    {{
      "file": "path/to/file.py",
      "action": "create|modify",
      "functions": [
        {{
          "name": "function_name",
          "signature": "def function_name(arg1: type, arg2: type) -> return_type",
          "pseudocode": "Brief description of what this function does"
        }}
      ]
    }}
  ]
}}

Do not write implementation code. Only tests and the plan.
Do not invent libraries or APIs not mentioned in Domain Knowledge.
Output ONLY the JSON object. No markdown fences. No preamble."""

    user_parts = [task, "---", codebase_summary]
    if scope_files:
        user_parts.append("---\n## Scope File Contents")
        for path, content in scope_files.items():
            user_parts.append(f"\n### {path}\n```\n{content}\n```")

    user_prompt = "\n\n".join(user_parts)

    # Truncate if needed
    total = estimate_tokens(system_prompt + user_prompt)
    if total > CONFIG["max_context_tokens"]:
        log(f"  Context too large ({total} tokens), truncating user prompt")
        user_prompt = truncate_context(user_prompt, CONFIG["max_context_tokens"] - estimate_tokens(system_prompt) - 500)

    return system_prompt, user_prompt


def assemble_implement_context(
    philosophy: str, task: str, plan: list, scope_files: dict[str, str],
    diagnosis: str = "", test_output: str = "", stuck_hint: str = "",
    review_code: str = ""
) -> tuple[str, str]:
    """
    Assemble system + user prompts for the IMPLEMENT phase.
    When diagnosis/test_output are provided (from a review cycle),
    they give Devstral the context it needs to fix specific issues.
    review_code contains exact corrected functions from the review model.
    """
    system_prompt = f"""{philosophy}

You are the implementation model. You receive a plan with function
signatures and pseudocode. Your job:
1. Implement each function according to the plan.
2. Output the complete modified file contents.
3. Do not modify function signatures from the plan.
4. Do not add functions not in the plan.

For each file, output a command block:

===FILE: path/to/file.py===
<complete file content>
===END FILE===

If you need to run a shell command (e.g., install a dependency), output:

===CMD===
<command>
===END CMD===

Output ONLY file blocks and command blocks. No explanations."""

    plan_text = json.dumps(plan, indent=2)
    user_parts = [task, "---\n## Implementation Plan\n" + plan_text]

    # Inject review feedback so Devstral knows what to fix
    if diagnosis:
        user_parts.append("---\n## Previous Attempt Failed\n"
                          "The previous implementation had these issues:\n"
                          + diagnosis)
    if test_output:
        # Include only the failure lines to save context
        fail_lines = [l for l in test_output.split("\n")
                      if "FAILED" in l or "Error" in l or "assert" in l.lower()
                      or l.strip().startswith("E ") or l.strip().startswith(">")]
        if fail_lines:
            user_parts.append("---\n## Test Failures\n```\n"
                              + "\n".join(fail_lines[:30]) + "\n```")
    if review_code:
        user_parts.append("---\n## Corrected Functions From Review\n"
                          "Use these EXACT implementations in your output:\n```\n"
                          + review_code + "\n```")
    if stuck_hint:
        user_parts.append("---\n## IMPORTANT\n" + stuck_hint)

    if scope_files:
        user_parts.append("---\n## Current File Contents")
        for path, content in scope_files.items():
            user_parts.append(f"\n### {path}\n```\n{content}\n```")

    user_prompt = "\n\n".join(user_parts)

    total = estimate_tokens(system_prompt + user_prompt)
    if total > CONFIG["max_context_tokens"]:
        log(f"  Context too large ({total} tokens), truncating user prompt")
        user_prompt = truncate_context(user_prompt, CONFIG["max_context_tokens"] - estimate_tokens(system_prompt) - 500)

    return system_prompt, user_prompt


def assemble_test_audit_context(
    philosophy: str, task: str, test_content: str, test_output: str,
) -> tuple[str, str]:
    """
    Assemble prompts for Phase 5A: Test Audit.
    Asks the planner to verify tests against Domain Knowledge BEFORE
    blaming the implementation. Attacks the review asymmetry problem.
    """
    system_prompt = f"""{philosophy}

You are auditing tests for correctness. Some tests may be failing because
the TESTS are wrong, not the implementation. Your job:
1. Re-read the Domain Knowledge section carefully.
2. For each failing test, check: does the assertion match what DK specifies?
3. Look for: reversed inequalities, wrong expected values, misunderstood
   formulas, tests that assume behaviour not specified in DK.
4. If ALL tests look correct, say so.

Output format:
{{
  "tests_correct": true/false,
  "issues": [
    {{
      "test_name": "test_xxx",
      "problem": "brief description of what the test got wrong",
      "fix": "what the assertion should be"
    }}
  ],
  "tests": {{ "filename": "...", "content": "..." }}
}}

Include "tests" ONLY if you found issues and are providing corrected tests.
If tests_correct is true, "issues" should be an empty list and omit "tests".
Output ONLY the JSON object. No markdown fences. No preamble."""

    user_parts = [
        task,
        "---\n## Test File\n```\n" + test_content + "\n```",
        "---\n## Test Output (failures)\n```\n" + test_output + "\n```",
    ]

    user_prompt = "\n\n".join(user_parts)

    total = estimate_tokens(system_prompt + user_prompt)
    if total > CONFIG["max_context_tokens"]:
        log(f"  Context too large ({total} tokens), truncating")
        user_prompt = truncate_context(user_prompt, CONFIG["max_context_tokens"] - estimate_tokens(system_prompt) - 500)

    return system_prompt, user_prompt


def assemble_review_context(
    philosophy: str, task: str, test_content: str, test_output: str,
    impl_files: dict[str, str]
) -> tuple[str, str]:
    """
    Assemble system + user prompts for Phase 5B: Implementation Review.
    Only called after Phase 5A confirms tests are correct.
    """
    system_prompt = f"""{philosophy}

The tests have been verified as correct. The failures are in the implementation.
Your job:
1. Analyse the test failures against the implementation.
2. Identify the root cause of EACH failure.
3. Write a corrected implementation plan addressing ONLY the failures.
   Do not rewrite parts that are working.

Output format:
{{
  "plan": [ ... ],
  "diagnosis": "Brief explanation of what went wrong"
}}

Output ONLY the JSON object. No markdown fences. No preamble."""

    user_parts = [
        task,
        "---\n## Test File\n```\n" + test_content + "\n```",
        "---\n## Test Output (failures)\n```\n" + test_output + "\n```",
    ]
    if impl_files:
        user_parts.append("---\n## Current Implementation")
        for path, content in impl_files.items():
            user_parts.append(f"\n### {path}\n```\n{content}\n```")

    user_prompt = "\n\n".join(user_parts)

    total = estimate_tokens(system_prompt + user_prompt)
    if total > CONFIG["max_context_tokens"]:
        log(f"  Context too large ({total} tokens), truncating")
        user_prompt = truncate_context(user_prompt, CONFIG["max_context_tokens"] - estimate_tokens(system_prompt) - 500)

    return system_prompt, user_prompt


# ---------------------------------------------
# SCOPE FILE LOADER
# ---------------------------------------------

def load_scope_files(task_text: str, workspace: Path) -> dict[str, str]:
    """
    Extract file paths from the Scope section of task.md and load them.
    Looks for paths relative to workspace.
    """
    files = {}
    # Try to find ## Scope section
    scope_match = re.search(r"##\s*Scope\s*\n([\s\S]*?)(?=\n##|\Z)", task_text)
    if not scope_match:
        return files

    scope_text = scope_match.group(1)
    # Find file-like patterns (anything ending in a common extension)
    for match in re.finditer(r"[\w/\-\.]+\.(?:py|js|ts|json|yaml|yml|toml|cfg|txt|md)", scope_text):
        filepath = workspace / match.group()
        if filepath.exists():
            try:
                files[match.group()] = filepath.read_text(errors="ignore")
            except Exception:
                pass
    return files


# ---------------------------------------------
# MAIN ORCHESTRATOR LOOP
# ---------------------------------------------

def init_project(project_name: str):
    """
    Scaffold a new project directory with boilerplate steering files.
    Creates: project_name/philosophy.md, task.md, workspace/, .gitignore
    """
    project_dir = Path(project_name).resolve()
    if project_dir.exists():
        print(f"[ERROR] Directory already exists: {project_dir}")
        sys.exit(1)

    project_dir.mkdir(parents=True)
    (project_dir / "workspace").mkdir()

    (project_dir / "philosophy.md").write_text(
        "This project prioritises correctness over cleverness. Every function does one thing.\n"
        "Error handling is explicit -- no silent swallowing of exceptions. Dependencies are\n"
        "minimal: stdlib + requests + pytest. Code should be readable by one person six months\n"
        "from now without any comments explaining \"why\" -- the structure itself should make\n"
        "intent obvious. If a choice is between simplicity and performance, choose simplicity\n"
        "until profiling proves otherwise.\n",
        encoding="utf-8"
    )

    (project_dir / "task.md").write_text(
        "# Task\n\n"
        "[Plain language description of what needs to happen]\n\n"
        "## Scope\n\n"
        "[Which files/directories are in play, relative to workspace]\n\n"
        "workspace/my_module.py\n\n"
        "## Domain Knowledge\n\n"
        "[CRITICAL: Front-load every fact the models need that they might fabricate.\n"
        "API parameter ranges, library function signatures, algorithm specifics.\n"
        "If a quantized model might guess wrong, state it here.]\n\n"
        "## Constraints\n\n"
        "[What the models must NOT do. Packages to avoid. Patterns to follow.]\n\n"
        "- Dependencies: stdlib only.\n"
        "- All public methods must have type hints.\n",
        encoding="utf-8"
    )

    (project_dir / ".gitignore").write_text(
        "__pycache__/\n"
        "*.pyc\n"
        ".pytest_cache/\n"
        "workspace/\n"
        "isomira.log\n",
        encoding="utf-8"
    )

    print(f"[OK] Project initialised: {project_dir}")
    print(f"     Edit philosophy.md and task.md, then run:")
    print(f"     python {Path(__file__).name} --project {project_name}")


def run(task_path: str = "task.md", philosophy_path: str = "philosophy.md", project_dir: str = None):
    """
    Main entry point. Runs the SUMMARISE -> PLAN -> IMPLEMENT -> TEST -> REVIEW loop.
    If project_dir is given, steering files and workspace are read from that directory.
    Otherwise falls back to the orchestrator's own directory (legacy mode).
    """
    global LOG_FILE

    if project_dir:
        base_dir = Path(project_dir).resolve()
        if not base_dir.exists():
            print(f"[ERROR] Project directory not found: {base_dir}")
            print(f"        Run: python {Path(__file__).name} init {base_dir.name}")
            sys.exit(1)
    else:
        base_dir = Path(__file__).parent.resolve()

    workspace = (base_dir / CONFIG["workspace"]).resolve()
    workspace.mkdir(parents=True, exist_ok=True)

    # Open log in the project directory
    LOG_FILE = open(base_dir / "isomira.log", "a", encoding="utf-8")

    log("=" * 60)
    log("ISOMIRA -- Starting")
    log(f"Project:   {base_dir}")
    log(f"Workspace: {workspace}")
    log(f"Planner:   {CONFIG['planner_model']}")
    log(f"Implementer: {CONFIG['implementer_model']}")
    log(f"Consultant:  {CONFIG['consultant_model']}")
    log(f"Consultant ctx: {CONFIG['consultant_max_context_tokens']}")
    log("=" * 60)

    # Load steering files
    philosophy = read_file_safe(base_dir / philosophy_path)
    if not philosophy:
        fatal(f"Missing {philosophy_path} -- the orchestrator needs a steering directive.")

    task = read_file_safe(base_dir / task_path)
    if not task:
        fatal(f"Missing {task_path} -- no task to execute.")

    log(f"Philosophy: {estimate_tokens(philosophy)} tokens")
    log(f"Task: {estimate_tokens(task)} tokens")

    # -- PHASE 1: SUMMARISE --
    log("\n--- PHASE 1: SUMMARISE ---")
    codebase_summary = summarise_codebase(workspace)
    log(f"Codebase summary: {estimate_tokens(codebase_summary)} tokens")

    # Load scope files
    scope_files = load_scope_files(task, workspace)
    if scope_files:
        log(f"Loaded {len(scope_files)} scope files")

    # -- PHASE 2: PLAN (Consultant) --
    log("\n--- PHASE 2: PLAN (Consultant) ---")
    _saved_ctx = CONFIG["max_context_tokens"]
    CONFIG["max_context_tokens"] = CONFIG["consultant_max_context_tokens"]
    try:
        sys_prompt, usr_prompt = assemble_plan_context(philosophy, task, codebase_summary, scope_files)
    finally:
        CONFIG["max_context_tokens"] = _saved_ctx
    plan_output = call_model(CONFIG["consultant_model"], sys_prompt, usr_prompt, profile="consultant")
    plan_output = strip_think_blocks(plan_output)

    try:
        plan_data = parse_json_output(plan_output)
    except ValueError as e:
        fatal(f"Plan phase produced unparseable output: {e}")

    # Validate plan structure
    if "tests" not in plan_data or "plan" not in plan_data:
        fatal(f"Plan JSON missing required keys. Got: {list(plan_data.keys())}")

    test_filename = plan_data["tests"].get("filename", "test_module.py")
    test_content = plan_data["tests"].get("content", "")
    plan = normalize_plan(plan_data["plan"])

    if not test_content:
        fatal("Plan phase produced empty test content.")
    if not plan:
        fatal("Plan phase produced no valid plan entries.")

    log(f"Test file: {test_filename}")
    log(f"Plan entries: {len(plan)}")

    # Write test file
    (workspace / test_filename).write_text(test_content)
    original_test_count = count_test_functions(test_content)
    log(f"Wrote {test_filename} to workspace ({original_test_count} test functions)")

    # -- LOOP: IMPLEMENT -> TEST -> REVIEW --
    iteration = 0
    last_test_hash = None
    stuck_count = 0
    last_diagnosis = ""
    last_review_code = ""
    last_impl_hash = None
    impl_stable_count = 0
    last_failing_set = None
    failing_set_count = 0
    test_result = {"passed": False, "output": ""}
    STUCK_THRESHOLD = 3
    DK_PING_THRESHOLD = 5  # cumulative stuck iterations before DK ping + halt
    while True:
        iteration += 1
        log(f"\n{'=' * 40}")
        log(f"ITERATION {iteration}")
        log(f"{'=' * 40}")

        # -- PHASE 3: IMPLEMENT --
        log("\n--- PHASE 3: IMPLEMENT ---")

        # Reload scope files (they may have been modified)
        current_files = {}
        for entry in plan:
            fpath = workspace / entry["file"]
            if fpath.exists():
                current_files[entry["file"]] = fpath.read_text(errors="ignore")

        # Build stuck hint if we're in a stuck loop
        stuck_hint = ""
        if stuck_count >= STUCK_THRESHOLD:
            stuck_hint = (
                f"You have produced the SAME failing implementation {stuck_count} "
                f"times. The previous approach is fundamentally wrong. "
                f"Try a COMPLETELY different implementation strategy. "
                f"Re-read the task requirements carefully, especially "
                f"the Domain Knowledge section."
            )

        sys_prompt, usr_prompt = assemble_implement_context(
            philosophy, task, plan, current_files,
            diagnosis=last_diagnosis,
            test_output=test_result["output"] if iteration > 1 else "",
            stuck_hint=stuck_hint,
            review_code=last_review_code,
        )
        impl_output = call_model(CONFIG["implementer_model"], sys_prompt, usr_prompt, profile="implementer")

        # Parse and apply file blocks
        file_blocks = parse_file_blocks(impl_output)
        if not file_blocks:
            log("WARNING: No file blocks in implementation output")
            log(f"Raw output preview: {impl_output[:500]}")
        else:
            for block in file_blocks:
                fpath = workspace / block["path"]
                fpath.parent.mkdir(parents=True, exist_ok=True)
                fpath.write_text(block["content"])
                log(f"  Wrote: {block['path']}")

        # Track implementation stability (is Devstral producing the same code?)
        impl_content = "".join(b["content"] for b in file_blocks) if file_blocks else ""
        impl_hash = hashlib.md5(impl_content.encode()).hexdigest()
        if impl_hash == last_impl_hash:
            impl_stable_count += 1
        else:
            impl_stable_count = 0
            last_impl_hash = impl_hash

        # Parse and execute command blocks
        cmd_blocks = parse_cmd_blocks(impl_output)
        for cmd in cmd_blocks:
            result = execute_command(cmd, workspace)
            if result["returncode"] != 0:
                log(f"  CMD FAILED (rc={result['returncode']}): {result['stderr'][:200]}")
            else:
                log(f"  CMD OK: {cmd[:80]}")

        # -- PHASE 4: TEST --
        log("\n--- PHASE 4: TEST ---")
        test_result = run_tests(workspace, test_filename)

        # Partial pass tracking: count PASSED/FAILED for convergence signal
        n_passed = test_result["output"].count(" PASSED")
        n_failed = test_result["output"].count(" FAILED")
        n_total = n_passed + n_failed
        if n_total > 0:
            log(f"Tests: {n_passed}/{n_total} passed ({100*n_passed//n_total}%)")
        else:
            log(f"Tests passed: {test_result['passed']}")

        if test_result["passed"]:
            log("\n" + "=" * 60)
            log("ALL TESTS PASS -- TASK COMPLETE")
            log("=" * 60)
            print("\a", end="", flush=True)  # beep
            break

        # Show full test output in log (truncated for console readability)
        log(f"Test output:\n{test_result['output'][:4000]}")

        # -- STUCK LOOP DETECTION --
        # Two signals tracked independently:
        # 1. P/F pattern hash: exact same PASS/FAIL sequence across all tests
        # 2. Failing-set hash: same test NAMES fail, even if P/F order shuffles
        # Both use cumulative counting — minor shuffles don't fully reset.
        pf_pattern = []
        current_failing = []
        for line in test_result["output"].split("\n"):
            if "PASSED" in line:
                pf_pattern.append("P")
            elif "FAILED" in line and "::" in line:
                pf_pattern.append("F")
                # Extract just the test name (e.g. "test_get_gradient_boundary")
                parts = line.strip().split("::")
                if len(parts) >= 2:
                    current_failing.append(parts[-1].split()[0])
        test_hash = hashlib.md5("".join(pf_pattern).encode()).hexdigest()
        if test_hash == last_test_hash:
            stuck_count += 1
        else:
            stuck_count = 1
            last_test_hash = test_hash

        # Track failing test set separately — survives P/F pattern shuffles
        failing_set = frozenset(current_failing)
        if failing_set == last_failing_set:
            failing_set_count += 1
        else:
            failing_set_count = 1
            last_failing_set = failing_set

        # Effective stuck score: max of both signals
        effective_stuck = max(stuck_count, failing_set_count)

        if effective_stuck >= STUCK_THRESHOLD:
            log(f"STUCK LOOP DETECTED: effective stuck score {effective_stuck} "
                f"(pf_repeat={stuck_count}, failing_set_repeat={failing_set_count})")

        # -- DK PING: Consultant autonomous DK amendment --
        # Signal: same tests keep failing for DK_PING_THRESHOLD iterations.
        # Instead of halting, give the Consultant one chance to diagnose the
        # Domain Knowledge gap and propose an append-only amendment to task.md.
        if effective_stuck >= DK_PING_THRESHOLD:
            log(f"\n{'!' * 60}")
            log(f"DK PING: Consultant attempting autonomous DK amendment")
            log(f"  Effective stuck score: {effective_stuck} "
                f"(pf={stuck_count}, failing_set={failing_set_count})")
            log(f"{'!' * 60}")

            # Read current task.md and record original size for cap
            task_path_full = base_dir / task_path
            current_task = read_file_safe(task_path_full)
            original_task_size = len(current_task)
            TASK_SIZE_CAP = original_task_size + 2000

            # Build failing test context
            failing_tests = [l.strip() for l in test_result["output"].split("\n")
                             if "FAILED" in l and "::" in l]
            assert_lines = [l.strip() for l in test_result["output"].split("\n")
                            if l.strip().startswith("E ") and ("assert" in l.lower()
                            or "!=" in l or "==" in l or "<" in l or ">" in l)]

            # Reload implementation for full diagnostic context
            dk_impl_files = {}
            for entry in plan:
                fpath = workspace / entry["file"]
                if fpath.exists():
                    dk_impl_files[entry["file"]] = fpath.read_text(errors="ignore")

            dk_system = f"""{philosophy}

You are a diagnostic consultant. The TDD loop has been stuck for {effective_stuck}
iterations on the same failing tests. The implementation model cannot fix this.

Your job: analyse the failing tests, the implementation, and the Domain Knowledge
section of task.md. Identify what FACT is missing or ambiguous in Domain Knowledge
that causes the implementation to fail.

Output format:
{{
  "diagnosis": "What specific DK gap causes the failure",
  "dk_addition": "Exact text to APPEND to the Domain Knowledge section. Be precise and factual. Include formulas, ranges, or API details as needed.",
  "confidence": "high|medium|low"
}}

RULES:
- You may ONLY propose ADDITIONS to Domain Knowledge. Never delete or modify existing text.
- Keep dk_addition under 500 characters. Be surgical.
- If you cannot identify the gap with high/medium confidence, set dk_addition to empty string.
- Output ONLY the JSON object. No markdown fences. No preamble."""

            dk_user_parts = [
                current_task,
                "---\n## Failing Tests\n" + "\n".join(failing_tests[:10]),
                "---\n## Assertion Clues\n" + "\n".join(assert_lines[:10]),
                "---\n## Test Output\n```\n" + test_result["output"][:6000] + "\n```",
            ]
            if dk_impl_files:
                dk_user_parts.append("---\n## Current Implementation")
                for path, content in dk_impl_files.items():
                    dk_user_parts.append(f"\n### {path}\n```\n{content}\n```")

            dk_user = "\n\n".join(dk_user_parts)
            dk_user = truncate_context(dk_user,
                CONFIG["consultant_max_context_tokens"] - estimate_tokens(dk_system) - 500)

            dk_output = call_model(CONFIG["consultant_model"], dk_system, dk_user, profile="consultant")
            dk_output = strip_think_blocks(dk_output)

            try:
                dk_data = parse_json_output(dk_output)
            except ValueError as e:
                log(f"Consultant DK analysis unparseable: {e}")
                log("HALTING -- manual DK review required.")
                print("\a\a\a", end="", flush=True)
                break

            dk_diagnosis = dk_data.get("diagnosis", "")
            dk_addition = dk_data.get("dk_addition", "")
            dk_confidence = dk_data.get("confidence", "low")

            log(f"Consultant diagnosis: {dk_diagnosis}")
            log(f"Confidence: {dk_confidence}")

            if dk_addition and dk_confidence in ("high", "medium"):
                dk_addition = dk_addition.strip()
                if len(dk_addition) > 500:
                    dk_addition = dk_addition[:500]
                    log("WARNING: Truncated DK addition to 500 chars")

                # Append-only insertion into Domain Knowledge section
                proposed_task = current_task
                dk_section_match = re.search(r"(## Domain Knowledge\s*\n)", proposed_task)
                if dk_section_match:
                    insert_pos = dk_section_match.end()
                    next_section = re.search(r"\n## ", proposed_task[insert_pos:])
                    dk_end = insert_pos + next_section.start() if next_section else len(proposed_task)
                    proposed_task = (
                        proposed_task[:dk_end].rstrip()
                        + "\n\n"
                        + f"[Auto-DK iteration {iteration}] {dk_addition}\n"
                        + proposed_task[dk_end:]
                    )
                else:
                    proposed_task += f"\n\n## Domain Knowledge\n\n[Auto-DK iteration {iteration}] {dk_addition}\n"

                # Size cap: prevent unbounded task.md growth
                if len(proposed_task) > TASK_SIZE_CAP:
                    log(f"REJECTED DK amendment: would exceed size cap "
                        f"({len(proposed_task)} > {TASK_SIZE_CAP})")
                    log("HALTING -- manual DK review required.")
                    print("\a\a\a", end="", flush=True)
                    break

                # Write amended task.md
                task_path_full.write_text(proposed_task, encoding="utf-8")
                task = proposed_task
                log(f"DK AMENDED: +{len(dk_addition)} chars to Domain Knowledge")
                log(f"  Addition: {dk_addition[:200]}")

                # Reset stuck counters for fair trial with new DK
                stuck_count = 0
                failing_set_count = 0
                last_test_hash = None
                last_failing_set = None

                # Re-plan with amended DK (Consultant re-plans)
                log("\n--- RE-PLANNING with amended DK ---")
                codebase_summary = summarise_codebase(workspace)
                scope_files = load_scope_files(task, workspace)

                _saved_ctx = CONFIG["max_context_tokens"]
                CONFIG["max_context_tokens"] = CONFIG["consultant_max_context_tokens"]
                try:
                    sys_prompt, usr_prompt = assemble_plan_context(
                        philosophy, task, codebase_summary, scope_files)
                finally:
                    CONFIG["max_context_tokens"] = _saved_ctx

                plan_output = call_model(CONFIG["consultant_model"], sys_prompt, usr_prompt, profile="consultant")
                plan_output = strip_think_blocks(plan_output)

                try:
                    plan_data = parse_json_output(plan_output)
                except ValueError:
                    log("Re-plan failed to parse. Continuing with old plan.")
                    continue

                if "plan" in plan_data:
                    new_plan = normalize_plan(plan_data["plan"])
                    if new_plan:
                        plan = new_plan
                        log(f"Re-planned: {len(plan)} entries")

                if "tests" in plan_data and plan_data["tests"].get("content"):
                    proposed_content = plan_data["tests"]["content"]
                    proposed_count = count_test_functions(proposed_content)
                    if proposed_count >= original_test_count:
                        test_filename = plan_data["tests"].get("filename", test_filename)
                        test_content = proposed_content
                        (workspace / test_filename).write_text(test_content)
                        original_test_count = proposed_count
                        log(f"Re-plan updated tests: {proposed_count} functions")

                continue  # Loop continues with amended DK + new plan

            else:
                log(f"Consultant could not identify DK gap (confidence={dk_confidence})")
                log("HALTING -- manual DK review required.")
                print("\a\a\a", end="", flush=True)
                break

        # -- PHASE 5A: TEST AUDIT --
        # Consultant steps in when stuck for deeper reasoning
        if effective_stuck >= STUCK_THRESHOLD:
            audit_model = CONFIG["consultant_model"]
            audit_profile = "consultant"
            log("\n--- PHASE 5A: TEST AUDIT (Consultant -- stuck) ---")
        else:
            audit_model = CONFIG["planner_model"]
            audit_profile = "planner"
            log("\n--- PHASE 5A: TEST AUDIT ---")

        # Re-read test content (may have been updated in a previous review)
        test_content = (workspace / test_filename).read_text(errors="ignore")

        _saved_ctx = CONFIG["max_context_tokens"]
        if audit_model == CONFIG["consultant_model"]:
            CONFIG["max_context_tokens"] = CONFIG["consultant_max_context_tokens"]
        try:
            sys_prompt, usr_prompt = assemble_test_audit_context(
                philosophy, task, test_content, test_result["output"]
            )
        finally:
            CONFIG["max_context_tokens"] = _saved_ctx
        audit_output = call_model(audit_model, sys_prompt, usr_prompt, profile=audit_profile)
        if audit_model == CONFIG["consultant_model"]:
            audit_output = strip_think_blocks(audit_output)

        try:
            audit_data = parse_json_output(audit_output)
        except ValueError as e:
            log(f"WARNING: Test audit output unparseable: {e}")
            audit_data = {"tests_correct": True, "issues": []}

        tests_correct = audit_data.get("tests_correct", True)
        audit_issues = audit_data.get("issues", [])
        if audit_issues:
            log(f"Test audit found {len(audit_issues)} issue(s):")
            for issue in audit_issues[:5]:
                if isinstance(issue, dict):
                    log(f"  - {issue.get('test_name', '?')}: {issue.get('problem', '?')}")

        # Apply test corrections from audit if provided
        if not tests_correct and "tests" in audit_data and audit_data["tests"]:
            proposed_content = audit_data["tests"].get("content", "")
            proposed_count = count_test_functions(proposed_content)
            if proposed_count >= original_test_count:
                test_filename = audit_data["tests"].get("filename", test_filename)
                test_content = proposed_content
                (workspace / test_filename).write_text(test_content)
                original_test_count = proposed_count
                log(f"Audit corrected tests: {test_filename} ({proposed_count} test functions)")
                # Skip 5B — re-run with corrected tests first
                log("Skipping implementation review — re-running with corrected tests")
                continue
            else:
                log(f"REJECTED audit test update: {proposed_count} tests vs "
                    f"original {original_test_count}. Keeping original.")

        # -- PHASE 5B: IMPLEMENTATION REVIEW --
        # Consultant steps in when stuck for deeper reasoning
        if effective_stuck >= STUCK_THRESHOLD:
            review_model = CONFIG["consultant_model"]
            review_profile = "consultant"
            log("\n--- PHASE 5B: IMPLEMENTATION REVIEW (Consultant -- stuck) ---")
        else:
            review_model = CONFIG["planner_model"]
            review_profile = "planner"
            log("\n--- PHASE 5B: IMPLEMENTATION REVIEW ---")

        # Reload implementation files
        impl_files = {}
        for block in file_blocks:
            fpath = workspace / block["path"]
            if fpath.exists():
                impl_files[block["path"]] = fpath.read_text(errors="ignore")

        _saved_ctx = CONFIG["max_context_tokens"]
        if review_model == CONFIG["consultant_model"]:
            CONFIG["max_context_tokens"] = CONFIG["consultant_max_context_tokens"]
        try:
            sys_prompt, usr_prompt = assemble_review_context(
                philosophy, task, test_content, test_result["output"], impl_files
            )
        finally:
            CONFIG["max_context_tokens"] = _saved_ctx
        review_output = call_model(review_model, sys_prompt, usr_prompt, profile=review_profile)
        if review_model == CONFIG["consultant_model"]:
            review_output = strip_think_blocks(review_output)

        try:
            review_data = parse_json_output(review_output)
        except ValueError as e:
            log(f"WARNING: Review output unparseable: {e}")
            log("Retrying implementation with same plan...")
            continue

        # Apply review corrections
        if "diagnosis" in review_data:
            last_diagnosis = review_data["diagnosis"]
            log(f"Diagnosis: {last_diagnosis}")

        # Extract corrected code from review plan entries
        last_review_code = extract_review_code(review_data)
        if last_review_code:
            log(f"Extracted {last_review_code.count('# Fix:') + last_review_code.count('# Correction')} code corrections from review")

        if "plan" in review_data and review_data["plan"]:
            fallback = plan[0]["file"] if plan else ""
            new_plan = normalize_plan(review_data["plan"], fallback_file=fallback)
            if new_plan:
                plan = new_plan
                log(f"Updated plan: {len(plan)} entries")
            else:
                log("WARNING: Review plan had no valid entries after normalization, keeping previous plan")
                log(f"  Raw review plan structure: {json.dumps(review_data['plan'])[:500]}")

    LOG_FILE.close()


# ---------------------------------------------
# CLI
# ---------------------------------------------

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Isomira -- Single-model TDD orchestrator (dual-profile)")
    subparsers = parser.add_subparsers(dest="command")

    # --- init subcommand ---
    init_parser = subparsers.add_parser("init", help="Scaffold a new project directory")
    init_parser.add_argument("project_name", help="Name/path for the new project directory")

    # --- run flags (default when no subcommand) ---
    parser.add_argument("--project", default=None, help="Project directory (contains task.md, philosophy.md, workspace/)")
    parser.add_argument("--task", default="task.md", help="Path to task file (default: task.md)")
    parser.add_argument("--philosophy", default="philosophy.md", help="Path to philosophy file (default: philosophy.md)")
    parser.add_argument("--workspace", default=None, help="Workspace directory (overrides CONFIG)")
    parser.add_argument("--url", default=None, help="LMStudio API URL (overrides CONFIG)")

    args = parser.parse_args()

    if args.command == "init":
        init_project(args.project_name)
    else:
        if args.workspace:
            CONFIG["workspace"] = args.workspace
        if args.url:
            CONFIG["lmstudio_url"] = args.url

        run(task_path=args.task, philosophy_path=args.philosophy, project_dir=args.project)
