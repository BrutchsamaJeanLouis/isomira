#!/usr/bin/env python3
"""
Isomoira -- Dual-model TDD orchestrator.
Phases A-D complete. One file. One loop. Tests decide when it's done.
Phase B: Command sandboxing (write-path, sudo allowlist, foreground blocking).
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

# ---------------------------------------------
# CONFIG
# ---------------------------------------------

CONFIG = {
    "planner_model": "mistralai_ministral-3-14b-reasoning-2512",
    "implementer_model": "mistralai_devstral-small-2-24b-instruct-2512",
    "lmstudio_url": "http://localhost:1234/v1",
    "workspace": "./workspace",
    "max_context_tokens": 16000,
    "cmd_timeout_default": 30,
    "cmd_timeout_install": 300,
    "temperature": 0.15,
    "top_k": 25,
    "min_p": 0.05,
    "max_tokens": 4096,
}

# ---------------------------------------------
# TOKEN ESTIMATION
# ---------------------------------------------

def estimate_tokens(text: str) -> int:
    """Rough token count. ~4 chars per token for English/code."""
    return len(text) // 4


# ---------------------------------------------
# MODEL CALLING
# ---------------------------------------------

def call_model(model_name: str, system_prompt: str, user_prompt: str) -> str:
    """
    Call LMStudio API. Blocking. Model autoswap handled by LMStudio.
    Uses requests if available, falls back to urllib.
    """
    import requests

    url = f"{CONFIG['lmstudio_url']}/chat/completions"
    payload = {
        "model": model_name,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": CONFIG["temperature"],
        "top_k": CONFIG["top_k"],
        "min_p": CONFIG["min_p"],
        "top_p": 1.0,
        "max_tokens": CONFIG["max_tokens"],
        "stream": False,
    }

    log(f"  -> Calling {model_name} ({estimate_tokens(system_prompt + user_prompt)} est. tokens in)")

    try:
        response = requests.post(url, json=payload, timeout=300)
        response.raise_for_status()
        content = response.json()["choices"][0]["message"]["content"]
        log(f"  <- Got {estimate_tokens(content)} est. tokens back")
        return content
    except requests.exceptions.ConnectionError:
        fatal("Cannot connect to LMStudio at " + CONFIG["lmstudio_url"])
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
    Ministral often returns plan entries with a "code" key containing
    the exact corrected function. Collect these so Devstral can use them.
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
    max_chars = max_tokens * 4
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

You are the planning model in a two-model TDD pipeline. Your job:
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


def assemble_review_context(
    philosophy: str, task: str, test_content: str, test_output: str,
    impl_files: dict[str, str]
) -> tuple[str, str]:
    """
    Assemble system + user prompts for the REVIEW phase.
    """
    system_prompt = f"""{philosophy}

Tests are failing. Your job:
1. Analyse the test failures against the implementation.
2. Identify the root cause of EACH failure.
3. Write a corrected implementation plan addressing ONLY the failures.
   Do not rewrite parts that are working.
4. If the tests themselves are wrong (testing for incorrect behaviour),
   you may revise the tests. Explain why.

Output format:
{{
  "tests": {{ "filename": "...", "content": "..." }},
  "plan": [ ... ],
  "diagnosis": "Brief explanation of what went wrong"
}}

Include "tests" ONLY if tests need to change.
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

def run(task_path: str = "task.md", philosophy_path: str = "philosophy.md"):
    """
    Main entry point. Runs the SUMMARISE -> PLAN -> IMPLEMENT -> TEST -> REVIEW loop.
    """
    global LOG_FILE

    base_dir = Path(__file__).parent.resolve()
    workspace = (base_dir / CONFIG["workspace"]).resolve()
    workspace.mkdir(parents=True, exist_ok=True)

    # Open log
    LOG_FILE = open(base_dir / "isomoira.log", "a", encoding="utf-8")

    log("=" * 60)
    log("ISOMOIRA -- Starting")
    log(f"Workspace: {workspace}")
    log(f"Planner:   {CONFIG['planner_model']}")
    log(f"Implementer: {CONFIG['implementer_model']}")
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

    # -- PHASE 2: PLAN --
    log("\n--- PHASE 2: PLAN ---")
    sys_prompt, usr_prompt = assemble_plan_context(philosophy, task, codebase_summary, scope_files)
    plan_output = call_model(CONFIG["planner_model"], sys_prompt, usr_prompt)

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
    test_result = {"passed": False, "output": ""}
    STUCK_THRESHOLD = 3
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
        impl_output = call_model(CONFIG["implementer_model"], sys_prompt, usr_prompt)

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
        test_hash = hashlib.md5(test_result["output"].encode()).hexdigest()
        if test_hash == last_test_hash:
            stuck_count += 1
        else:
            stuck_count = 1
            last_test_hash = test_hash

        if stuck_count >= STUCK_THRESHOLD:
            log(f"STUCK LOOP DETECTED: same test output {stuck_count} times in a row")

        # -- PHASE 5: REVIEW --
        log("\n--- PHASE 5: REVIEW ---")

        # Reload implementation files
        impl_files = {}
        for block in file_blocks:
            fpath = workspace / block["path"]
            if fpath.exists():
                impl_files[block["path"]] = fpath.read_text(errors="ignore")

        # Re-read test content (may have been updated in a previous review)
        test_content = (workspace / test_filename).read_text(errors="ignore")

        sys_prompt, usr_prompt = assemble_review_context(
            philosophy, task, test_content, test_result["output"], impl_files
        )
        review_output = call_model(CONFIG["planner_model"], sys_prompt, usr_prompt)

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

        # Extract corrected code from review plan entries (Pathway 3)
        last_review_code = extract_review_code(review_data)
        if last_review_code:
            log(f"Extracted {last_review_code.count('# Fix:') + last_review_code.count('# Correction')} code corrections from review")

        if "tests" in review_data and review_data["tests"]:
            proposed_content = review_data["tests"].get("content", "")
            proposed_count = count_test_functions(proposed_content)
            if proposed_count >= original_test_count:
                test_filename = review_data["tests"].get("filename", test_filename)
                test_content = proposed_content
                (workspace / test_filename).write_text(test_content)
                original_test_count = proposed_count
                log(f"Updated tests: {test_filename} ({proposed_count} test functions)")
            else:
                log(f"REJECTED test update: review has {proposed_count} tests, "
                    f"original has {original_test_count}. Keeping original to prevent regression.")

        if "plan" in review_data and review_data["plan"]:
            # Infer fallback file from current plan so review entries
            # that describe function-level fixes (no file path) still work
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

    parser = argparse.ArgumentParser(description="Isomoira -- Dual-model TDD orchestrator")
    parser.add_argument("--task", default="task.md", help="Path to task file (default: task.md)")
    parser.add_argument("--philosophy", default="philosophy.md", help="Path to philosophy file (default: philosophy.md)")
    parser.add_argument("--workspace", default=None, help="Workspace directory (overrides CONFIG)")
    parser.add_argument("--url", default=None, help="LMStudio API URL (overrides CONFIG)")
    args = parser.parse_args()

    if args.workspace:
        CONFIG["workspace"] = args.workspace
    if args.url:
        CONFIG["lmstudio_url"] = args.url

    run(task_path=args.task, philosophy_path=args.philosophy)
