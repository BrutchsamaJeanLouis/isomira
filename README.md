# Isomira

A single-model orchestrator for agentic coding. Devstral plans, implements, and reviews via dual-profile tuning. Tests decide when it's done.

---

## Quick Start

```bash
# 1. Clone into your home directory
#    Windows: C:\Users\<you>\isomira
#    Linux:   /home/<you>/isomira
git clone https://github.com/brutchjd/isomira.git ~/isomira
cd ~/isomira
pip install requests pytest

# 2. Start LMStudio with Devstral 24B on localhost:1234

# 3. Create a project
python isomira.py init myproject

# 4. Edit steering files
#    myproject/philosophy.md  -- design philosophy (5-6 sentences)
#    myproject/task.md        -- task spec with Domain Knowledge

# 5. Run
python isomira.py --project myproject
```

---

## What This Is

A Python CLI tool that runs a TDD loop using a single local LLM (Devstral 24B) served by LMStudio with dual-profile tuning. The planner profile writes tests and architectural plans. The implementer profile writes code. The loop continues until all tests pass. No human in the loop after task submission.

The orchestrator handles context compression, command execution sandboxing, and phase handoffs. The models never touch the shell directly — the orchestrator mediates all execution.

---

## Architecture Overview

```
┌─────────────────────────────────────────────────────┐
│                    ISOMIRA                          │
│                                                      │
│  ┌──────────┐    ┌──────────┐    ┌───────────────┐  │
│  │ task.md   │    │philosophy│    │  codebase     │  │
│  │ (user)    │    │ .md      │    │  summary      │  │
│  └─────┬─────┘    └────┬─────┘    └──────┬────────┘  │
│        │               │                 │           │
│        └───────────┬───┘─────────────────┘           │
│                    │                                  │
│           ┌────────▼────────┐                        │
│           │  PHASE RUNNER   │ (state machine)        │
│           └────────┬────────┘                        │
│                    │                                  │
│     ┌──────────────┼──────────────┐                  │
│     ▼              ▼              ▼                  │
│  PLAN           IMPLEMENT      REVIEW                │
│  (planner)     (implementer)  (planner)              │
│     │              │              │                  │
│     └──────────────┼──────────────┘                  │
│                    │                                  │
│           ┌────────▼────────┐                        │
│           │ COMMAND EXECUTOR │                        │
│           │ (sandboxed)     │                        │
│           └────────┬────────┘                        │
│                    │                                  │
│           ┌────────▼────────┐                        │
│           │  TEST RUNNER    │                        │
│           │  (done signal)  │                        │
│           └─────────────────┘                        │
└─────────────────────────────────────────────────────┘
```

---

## Core Design Constraints

These are non-negotiable and should guide every implementation decision:

1. **~20k usable context per model call.** The model is quantized. Every token of context must earn its place. The orchestrator compresses aggressively between phases.

2. **Sequential, not parallel.** One model active at a time. One phase completes before the next begins. No concurrent model calls. LMStudio handles model swapping via its autoswap/TTL mechanism.

3. **TDD governs the loop.** The planning model writes tests FIRST. Implementation is done when tests pass. Not when the model says "done." Not after N iterations. When `pytest` returns 0.

4. **Models propose, orchestrator executes.** Models never get direct shell access. They output command strings. The orchestrator inspects, sanitises, and executes them with appropriate constraints.

5. **Context re-anchoring.** Every time context would exceed ~16k tokens, compress. Every time a new loop iteration starts, re-inject `task.md` and `philosophy.md`. These are the steering signal that prevents drift over long loops.

---

## Requirements

- **GPU:** 16GB+ VRAM (tested on RTX 4060 Ti 16GB)
- **RAM:** 32GB+ available
- **OS:** Windows (WSL) or Linux
- **Model server:** [LMStudio](https://lmstudio.ai/) (OpenAI-compatible API at `http://localhost:1234/v1`)
- **Model:** Devstral 24B (`mistralai_devstral-small-2-24b-instruct-2512`) -- single model, dual-profile (planner + implementer)
- **Python:** 3.10+
- **Dependencies:** `requests`, `pytest`

---

## File Structure

The orchestrator lives in your home directory. Projects live wherever you create them.

```
~/isomira/                 # orchestrator (clone once)
├── README.md
├── isomira.py             # single-file orchestrator
├── requirements.txt       # requests + pytest
├── philosophy.md          # boilerplate template
├── task.md                # boilerplate template
└── docs/

~/myproject/               # project directory (one per project)
├── philosophy.md          # project steering directive (5-6 sentences)
├── task.md                # task spec with Domain Knowledge
├── workspace/             # generated code lives here
├── isomira.log            # run log (gitignored)
└── .gitignore
```

Create project directories with `python isomira.py init myproject`. Run with `python isomira.py --project ~/myproject`.

Legacy mode (no `--project` flag) uses the orchestrator's own directory for steering files, for backwards compatibility.

### v1 is a single file: `isomira.py`

Do not split into multiple modules prematurely. The orchestrator is a state machine with helper functions. It fits in one file until it doesn't. Refactor only when a function exceeds ~80 lines or when a clear module boundary emerges from actual use.

---

## Key Files the Orchestrator Reads

### `task.md`

User-authored task specification. Markdown format. The orchestrator reads this at the start and re-injects it on every loop iteration. Structure:

```markdown
# Task

[Plain language description of what needs to happen]

## Scope

[Which files/directories are in play]

## Domain Knowledge

[Any API details, filter parameters, library specifics the models need.
This is where you inject facts that prevent hallucination.
e.g., "ffmpeg freezedetect noise parameter range is 0-1, not seconds"]

## Constraints

[Anything the models should NOT do. Packages to avoid. Patterns to follow.]
```

The `Domain Knowledge` section is critical. From model testing, quantized models fill knowledge gaps with plausible fabrications. Front-loading domain facts here is the primary defense against hallucinated API parameters and nonexistent library functions.

### `philosophy.md`

NOT a task file. A crystallised 5-6 sentence project directive that shapes how the models interpret the fragments of codebase they see within the constrained context window. This gets prepended to every model call as part of the system prompt. It provides interpretive coherence when the models can only see small slices of the project.

Example structure (user writes this per-project):

```markdown
This project prioritises fault tolerance over performance. Every external
process call must have a timeout and error capture. Files are processed
sequentially to avoid resource contention on constrained hardware. Code
should be readable by a single developer — no abstractions that exist
only for testability. If a library doesn't ship with Ubuntu, justify its
inclusion.
```

---

## Phase Definitions

### Phase 1: SUMMARISE (orchestrator, no model)

**Actor:** The orchestrator itself (Python code, not a model call)

**Input:** Workspace directory path from `task.md`

**Action:** Generate a compressed codebase summary:
- File tree (paths only, no content)
- For each Python file: function/class signatures extracted via `ast` module
- Import graph (which file imports what)
- Total line counts per file

**Output:** A string called `codebase_summary` — compact structured text, typically 1-3k tokens even for medium projects.

**Why this isn't a model call:** Parsing Python AST is deterministic. Using a model to summarise code wastes context tokens and introduces hallucination risk. The `ast` module gives exact function signatures for free.

**Non-Python files:** For JS/TS, use regex extraction of `export function`, `export class`, `module.exports`. For other languages, fall back to file tree + line counts only. Don't over-invest here — if the project is Python, `ast` covers it. If it's mixed, the file tree plus task.md scoping is sufficient.

---

### Phase 2: PLAN (Devstral — planner profile)

**Actor:** Devstral 24B (planner profile)

**Input context (must fit ~16k tokens total):**
```
[system] philosophy.md content
[user]   task.md content
         ---
         codebase_summary (from Phase 1)
         ---
         [contents of files listed in task.md Scope section]
```

**System prompt directive:**
```
You are the planning profile in a single-model TDD pipeline. Your job:
1. Analyse the task against the current codebase.
2. Write pytest test functions FIRST that define the expected behaviour.
   Tests must be runnable independently. Use only stdlib + pytest.
3. Then write an implementation plan: which files to create/modify,
   function signatures, and pseudocode per function.

Output format (strict — the orchestrator parses this):

```json
{
  "tests": {
    "filename": "test_<module>.py",
    "content": "<full pytest file content>"
  },
  "plan": [
    {
      "file": "path/to/file.py",
      "action": "create|modify",
      "functions": [
        {
          "name": "function_name",
          "signature": "def function_name(arg1: type, arg2: type) -> return_type",
          "pseudocode": "Brief description of what this function does"
        }
      ]
    }
  ]
}
```

Do not write implementation code. Only tests and the plan.
Do not invent libraries or APIs not mentioned in Domain Knowledge.
```

**Output:** Parsed JSON containing a test file and an implementation plan.

**Exit condition:** Valid JSON with non-empty `tests.content` and at least one plan entry. If JSON parsing fails, retry with a message: "Your previous output was not valid JSON. Output only the JSON object, no markdown fences, no preamble."

**Max retries:** 3. If still invalid, write error to log and halt with beep.

---

### Phase 3: IMPLEMENT (Devstral — implementer profile)

**Actor:** Devstral 24B (implementer profile)

**Input context (must fit ~16k tokens total):**
```
[system] philosophy.md content
[user]   task.md content (Domain Knowledge section is critical here)
         ---
         Implementation plan from Phase 2 (just the plan, not tests)
         ---
         [contents of files to be modified — only the ones in the plan]
```

**System prompt directive:**
```
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

Output ONLY file blocks and command blocks. No explanations.
```

**Output:** Parsed file blocks and command blocks.

**File blocks** get written to disk by the orchestrator.

**Command blocks** get routed through the Command Executor (see below).

**Exit condition:** At least one file block produced that matches a file in the plan.

---

### Phase 4: TEST (orchestrator, no model)

**Actor:** The orchestrator runs pytest directly.

**Action:**
1. Write the test file from Phase 2 to the workspace (if not already written).
2. Run: `pytest test_<module>.py -v --tb=short 2>&1`
3. Capture full output.

**If all tests pass:** DONE. Exit the loop. Print summary. Beep.

**If tests fail:** Continue to Phase 5 (Review).

---

### Phase 5: REVIEW (Devstral — planner profile)

**Actor:** Devstral 24B (planner profile)

**Input context:**
```
[system] philosophy.md content
[user]   task.md content
         ---
         Test file content
         ---
         pytest output (failures only, truncated if needed)
         ---
         Current implementation (files that were modified)
```

**System prompt directive:**
```
Tests are failing. Your job:
1. Analyse the test failures against the implementation.
2. Identify the root cause of EACH failure.
3. Write a corrected implementation plan addressing ONLY the failures.
   Do not rewrite parts that are working.
4. If the tests themselves are wrong (testing for incorrect behaviour),
   you may revise the tests. Explain why.

Output format (same as Phase 2):
{
  "tests": { "filename": "...", "content": "..." },  // include ONLY if tests changed
  "plan": [ ... ],  // corrected plan entries ONLY for failing parts
  "diagnosis": "Brief explanation of what went wrong"
}
```

**Output:** Corrected plan (and optionally corrected tests).

**After this:** Loop back to Phase 3 (Implement) with the corrected plan. Then Phase 4 (Test). Then Phase 5 (Review) again if still failing.

**The loop runs indefinitely until tests pass.** There is no max iteration count. The models figure it out or the user kills it manually. The context compression between iterations prevents context overflow from killing the loop.

---

## Context Compression

**When to compress:** Before every model call, check if the assembled context exceeds ~14k tokens (leave headroom below 20k). If it exceeds, compress.

**How to compress (v1 — no model involved):**

1. **Test output:** Keep only failing test names + assertion error lines. Strip full tracebacks to first relevant line.
2. **Implementation files:** If a file hasn't changed since last iteration, replace its content with `[unchanged since iteration N]`.
3. **Plan:** Keep only the entries relevant to failing tests. Drop completed/passing entries.
4. **Diagnosis history:** Keep only the most recent diagnosis. Previous ones are superseded.

**Re-anchoring on every iteration:**
- `philosophy.md` → always re-injected in full (5-6 sentences ≈ 100-150 tokens)
- `task.md` → always re-injected in full (Domain Knowledge is the hallucination shield)
- `codebase_summary` → always re-injected in full (structural awareness)

These three files are the "power steering." They cost ~2-4k tokens combined and prevent the models from drifting as context compresses over many iterations.

---

## Command Executor

All shell commands proposed by models pass through this layer. The executor is a Python class with these rules:

### Read Access
- **Everywhere.** The models can `cat`, `grep`, `find`, `ls` any path on the system.

### Write/Modify Access
- **Workspace directory only.** Any command that creates, modifies, or deletes files must target paths within the configured workspace.
- **Detection method:** Parse the command for output-producing flags (`>`, `>>`, `-o`, `--output`, `tee`) and check target paths. Parse `rm`, `mv`, `cp`, `mkdir`, `touch`, `chmod` targets.
- If a write target is outside workspace: **DO NOT EXECUTE.** Sound a terminal beep (`\a`). Print: `[BLOCKED] Command writes outside workspace: {cmd}`. Log it. Feed the block message back to the model as the command result so it can adjust.

### System Commands (sudo)
- **Allowed:** `sudo apt install/remove`, `sudo systemctl start/stop/restart`, `sudo kill`, `sudo lsof`, `sudo fuser`, `sudo service`, `sudo ufw`, `sudo netstat/ss`
- **Blocked:** `sudo rm`, `sudo mv`, `sudo cp`, `sudo chmod`, `sudo chown` targeting paths outside workspace.
- **Detection:** Allowlist of sudo subcommands. If the command after `sudo` is not on the allowlist, block with beep.
- **No password prompt:** Configure WSL sudoers for passwordless sudo, OR pre-authenticate with `sudo -v` at orchestrator startup and refresh periodically. This prevents the stdin-blocking-on-password problem entirely.

### Timeouts
- **Default:** 30 seconds
- **Long-running (detected by command name):** `apt install`, `pip install`, `npm install`, `cargo build` → 300 seconds
- **Infinite/foreground (detected by pattern):** `tail -f`, `watch`, `python -m http.server`, `npm run dev` → **BLOCKED.** Feed back: "This is a foreground process. Rewrite as a one-shot command or background with timeout."

### Output Capture
- All commands return `{"stdout": "...", "stderr": "...", "returncode": N, "timed_out": bool}`
- Truncate stdout/stderr to 2000 chars before feeding back to model (context budget).

---

## LMStudio API Integration

LMStudio exposes an OpenAI-compatible API at `http://localhost:1234/v1`.

### Model Calling

```python
import requests

def call_model(model_name: str, system_prompt: str, user_prompt: str) -> str:
    """Call LMStudio API. Model autoswap handled by LMStudio."""
    response = requests.post(
        "http://localhost:1234/v1/chat/completions",
        json={
            "model": model_name,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ],
            "temperature": 0.15,
            "max_tokens": 4096,
            "stream": False
        },
        timeout=300
    )
    return response.json()["choices"][0]["message"]["content"]
```

### Model Names

These must match exactly what LMStudio loads. Configure in a `config` dict at the top of `isomira.py`:

```python
CONFIG = {
    "planner_model": "mistralai_devstral-small-2-24b-instruct-2512",
    "implementer_model": "mistralai_devstral-small-2-24b-instruct-2512",
    "lmstudio_url": "http://localhost:1234/v1",
    "workspace": "./workspace",
    "max_context_tokens": 16000,
    "cmd_timeout_default": 30,
    "cmd_timeout_install": 300,
}
```

### Sampling Parameters

The orchestrator sends fixed sampling parameters per model role. These are not configurable by the models.

**Planner profile:** `temperature: 0.6, top_p: 0.95, min_p: 0.05, repeat_penalty: 1.05`
**Implementer profile:** `temperature: 0.4, top_p: 0.85, min_p: 0.05, repeat_penalty: 1.05`

Same model, different tuning. The planner profile runs hotter for broader test exploration. The implementer profile runs tighter for precise code generation. See `docs/singleModel.md` for the full dual-profile spec.

---

## Build Phases

### Phase A: Walking Skeleton -- COMPLETE

State machine loop (SUMMARISE -> PLAN -> IMPLEMENT -> TEST -> REVIEW -> loop), LMStudio API integration, AST-based codebase summary, output parsers (FILE/CMD blocks + JSON), pytest runner, context assembly with re-anchoring. Validated on trivial math_utils task and LWW-Element-Set CRDT task.

### Phase B: Command Executor + Sandboxing -- COMPLETE

Three-layer sandbox: (1) write-path enforcement with target extraction from redirects/tee/rm/mv/cp/mkdir/touch/chmod/wget/curl, path traversal resolution, /dev/null exemption; (2) sudo allowlist (apt/systemctl/service/kill/lsof/fuser/ufw/netstat/ss only); (3) foreground process blocking (tail -f/watch/http.server/npm run dev/vim/top etc). Blocked commands return reason as stderr for model self-correction. 34/34 test scenarios pass.

### Phase C: Context Compression + Re-anchoring -- COMPLETE

Token estimation (len//4), context truncation, re-anchoring (philosophy.md + task.md + codebase_summary injected every iteration), test output compression (failure lines only for Devstral context).

### Phase D: Robustness -- COMPLETE

normalize_plan() handles wildly varying model schemas (10+ key aliases, regex .py scanning, fallback_file inference). Test protection guardrail (count_test_functions rejects review updates that shrink the suite). Stuck loop detection via MD5 hash of test output. Review-to-implementation feedback pipeline: diagnosis + test failures + code corrections (extract_review_code) wired into Devstral context. UTF-8 logging, Windows cp1252 safe console output.

### Validation History

- **LWW-Element-Set CRDT with vector clocks** (5 runs): Partial ordering, concurrent operations, add-wins bias, merge convergence. Best: 10/11 in 11 iterations. Final test failure traced to hallucinated test expectation (sequential single-replica operations confused with concurrent multi-replica). Fixed via Domain Knowledge injection.
- **Trivial math_utils** (calibration): 1-2 iterations to completion.

---

## Future Pathway (post-v1)

These are NOT part of the initial build. They are documented here so the architecture doesn't accidentally prevent them.

### Semantic Retrieval Layer
Replace compressed-summary-only context with a vector store (ChromaDB or similar). Embed function-level chunks. Retrieve only relevant chunks per phase. This becomes valuable when the workspace exceeds ~50 files and the summary alone eats too much context.

**Prerequisite:** A local embedding model that runs on CPU without competing for GPU VRAM. Candidate: `nomic-embed-text` via Ollama (140MB, CPU-only).

### Multi-Language AST Support
v1 only parses Python via `ast`. Extend to JS/TS via `tree-sitter` bindings. This expands the codebase summary quality for mixed-language projects.

### Session Persistence + Resume
Write loop state (current phase, iteration count, last passing tests, compressed context) to disk after each phase. Allow `isomira.py --resume` to pick up where it left off after a crash or intentional stop.

### Model Evaluation Harness
Run the same task against different model/quant combinations and score outputs automatically using test pass rate. This turns Isomira into a model benchmarking tool for your specific workflow, not just generic benchmarks.

### Third Model Role: Debugger
When the implement→review loop is stuck (same failure 5+ times), hand off to a third model (or Claude via API) specifically for debugging. This model gets the failing test, the implementation, and all previous diagnoses. It only activates on stuck loops.

### Workspace Git Integration
Auto-commit after each successful test pass. Auto-branch before each task. This gives you rollback and diff visibility without the models needing to manage git.

---

## Philosophy on Overengineering

All four build phases (A-D) are complete and validated. The orchestrator is a single file containing a state machine with helper functions. Future pathway items (semantic retrieval, session persistence, third model role) remain deferred until real usage on multi-file projects demands them.

The orchestrator is a for-loop with a match statement. Keep it that way as long as possible.
