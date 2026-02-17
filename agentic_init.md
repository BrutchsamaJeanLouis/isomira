# Isomoira -- Deployment Guide for New Projects

Step-by-step instructions for deploying the agentic TDD orchestrator against a new project or workspace.

---

## Prerequisites (one-time setup)

### 1. LMStudio running with both models loaded

Open LMStudio. Ensure these models are available (names must match CONFIG in isomoira.py):

```
Planner:      mistralai_ministral-3-14b-reasoning-2512   (Q6_K)
Implementer:  mistralai_devstral-small-2-24b-instruct-2512  (IQ4_XS)
```

LMStudio must be serving on `http://localhost:1234/v1`. Verify with:

```powershell
curl http://localhost:1234/v1/models
```

You should see both models listed. LMStudio handles autoswap between them -- you don't need to manually load/unload.

### 2. Python dependencies installed

```powershell
pip install requests pytest
```

That's it. The orchestrator is stdlib + these two.

---

## Per-Project Setup

### Step 1: Clean the workspace

The orchestrator writes all generated code into the `workspace/` directory (relative to where `isomoira.py` lives, unless overridden with `--workspace`).

For a fresh task, clean it:

```powershell
# From the isomira directory
Remove-Item -Recurse -Force .\workspace\* -ErrorAction SilentlyContinue
```

If you want to point at an EXISTING project directory instead of `./workspace`, use `--workspace` at runtime (Step 5). The orchestrator will read existing files there and write new ones into it.

### Step 2: Write philosophy.md

This file is your project's steering directive. It gets prepended to EVERY model call as part of the system prompt. Keep it to 5-6 sentences. It shapes HOW the models interpret code, not WHAT they build.

Location: `C:\Users\brutc\isomira\philosophy.md`

Template:

```markdown
This project prioritises [X] over [Y]. Every function does one thing.
Error handling is [explicit/defensive/fail-fast]. Dependencies are
[minimal/specific list]. Code should be readable by [audience] [timeframe]
from now. If a choice is between [tradeoff A] and [tradeoff B], choose
[winner] until [condition] proves otherwise.
```

Real example (current):

```markdown
This project prioritises correctness over cleverness. Every function does one thing.
Error handling is explicit -- no silent swallowing of exceptions. Dependencies are
minimal: stdlib + requests + pytest. Code should be readable by one person six months
from now without any comments explaining "why" -- the structure itself should make
intent obvious. If a choice is between simplicity and performance, choose simplicity
until profiling proves otherwise.
```

Key rules:
- No Unicode em dashes or special characters (Windows cp1252 encoding issue). Use `--` instead.
- Keep it under 150 tokens (~600 chars). It's injected on every call.
- This is NOT a task description. It's a design philosophy.

### Step 3: Write task.md

This is the actual job specification. It gets re-injected on every loop iteration to prevent model drift.

Location: `C:\Users\brutc\isomira\task.md`

Required structure:

```markdown
# Task

[Plain language description of what needs to happen. 2-4 sentences max.
Be specific about the deliverable -- file names, function names, behaviour.]

## Scope

[List the file paths that are in play, relative to workspace.
One path per line. These are the files the models will read/write.]

workspace/my_module.py
workspace/utils.py

## Domain Knowledge

[CRITICAL SECTION. This is your hallucination shield.
Front-load every fact the models need that they might fabricate.
API parameter ranges, library function signatures, algorithm specifics,
data format details. If a quantized model might guess wrong, state it here.]

- The frobnicate() function takes a float between 0.0 and 1.0, NOT an integer.
- Use subprocess.run(), not os.system().
- The output format is newline-delimited JSON, not a JSON array.

## Constraints

[What the models must NOT do. Packages to avoid. Patterns to follow.
Negative constraints are as important as positive ones.]

- No external dependencies beyond stdlib.
- Do not use asyncio.
- All functions must have type hints.
```

Key rules:
- The **Domain Knowledge** section is the most important part. Quantized models fill gaps with plausible fabrications. Every fact you state here is a fact they won't hallucinate.
- Be specific about file paths in Scope. The orchestrator uses these to load existing file contents and feed them to the models.
- Scope paths should be relative to the workspace root (e.g., `my_module.py` not `C:\full\path\my_module.py`). The `workspace/` prefix is optional -- the orchestrator strips it.

### Step 4: Verify file layout

Before running, confirm you have:

```
isomira/
  isomoira.py          # the orchestrator (don't touch)
  philosophy.md        # your project steering directive (Step 2)
  task.md              # your task specification (Step 3)
  requirements.txt     # requests + pytest
  workspace/           # clean or containing existing project files
```

### Step 5: Run

Basic (uses defaults):

```powershell
cd C:\Users\brutc\isomira
python .\isomoira.py
```

With overrides:

```powershell
# Point at a different workspace
python .\isomoira.py --workspace C:\Users\brutc\my_other_project

# Different task/philosophy files (for multiple tasks)
python .\isomoira.py --task task_v2.md --philosophy philosophy_webdev.md

# Different LMStudio URL
python .\isomoira.py --url http://192.168.1.50:1234/v1
```

All flags:

| Flag | Default | Purpose |
|------|---------|---------|
| `--task` | `task.md` | Path to task specification |
| `--philosophy` | `philosophy.md` | Path to philosophy directive |
| `--workspace` | `./workspace` | Working directory for generated code |
| `--url` | `http://localhost:1234/v1` | LMStudio API endpoint |

---

## What Happens During a Run

```
PHASE 1: SUMMARISE    Orchestrator scans workspace via AST. No model call.
PHASE 2: PLAN         Ministral writes pytest tests + implementation plan.
PHASE 3: IMPLEMENT    Devstral writes code based on the plan.
PHASE 4: TEST         Orchestrator runs pytest. If all pass -> DONE.
PHASE 5: REVIEW       If tests fail, Ministral diagnoses and revises plan.
                      Loop back to PHASE 3.
```

The loop runs indefinitely until all tests pass or you kill it with Ctrl+C.

---

## Monitoring a Run

### Console output

The orchestrator prints timestamped progress to stdout. Key things to watch:

```
-> Calling [model]     Model call starting (expect 1-3 min each)
<- Got N tokens back   Model responded
Wrote: filename        File written to workspace
Tests passed: True     SUCCESS -- loop will exit
Tests passed: False    FAILURE -- entering review cycle
Diagnosis: ...         Ministral's root-cause analysis of failures
```

### Log file

Everything also goes to `isomoira.log` (UTF-8 encoded, append mode). Survives crashes. Check it with:

```powershell
Get-Content .\isomoira.log -Tail 50
```

### Inspect generated files

During or after a run, look at what the models produced:

```powershell
ls .\workspace\
cat .\workspace\test_*.py     # the tests Ministral wrote
cat .\workspace\*.py           # the implementation Devstral wrote
```

---

## Troubleshooting

### "Cannot connect to LMStudio"

LMStudio isn't running or isn't serving on port 1234. Check:

```powershell
curl http://localhost:1234/v1/models
```

### "Plan phase produced unparseable output"

Ministral returned something that isn't valid JSON. This happens when the model wraps its output in extra text. The orchestrator tries to extract JSON from the response but sometimes fails. Options:
- Rerun. The model may produce valid JSON on the next attempt.
- Simplify the task.md -- shorter, clearer instructions help quantized models stay on format.

### "Plan phase produced no valid plan entries"

The plan JSON was valid but the `plan` array had no entries with a recognizable `file` key. The normalizer checks for `file`, `filename`, `filepath`, `path`, and `file_path`. If the model used something else entirely, the entries get dropped.

### Tests fail repeatedly with the same error

The orchestrator currently has no stuck-loop detection (Phase D TODO). If you see the same failure 5+ times, kill it (Ctrl+C) and either:
- Fix the issue manually in workspace and rerun
- Add more detail to the Domain Knowledge section of task.md
- Simplify the task scope

### UnicodeEncodeError

All orchestrator strings are ASCII-safe. If you see encoding errors, they're from model output containing Unicode. The log() function replaces unencodable chars automatically, but if a crash happens before log() (e.g., in file writes), add `encoding="utf-8"` to the relevant `write_text()` call.

---

## Tips for Writing Good Tasks

1. **One deliverable per task.** "Build module X" not "Build modules X, Y, and Z." Run separate tasks for each.

2. **Overspecify the Domain Knowledge.** If you know the answer to a question the model might get wrong, state it. The models are quantized -- they hallucinate on specifics.

3. **Name your files explicitly.** Don't say "create a module." Say "create `workspace/parser.py`." The models need concrete paths.

4. **State what NOT to do.** "Do not use asyncio" is as valuable as "use threading." Negative constraints prevent the models from wandering into patterns that break your architecture.

5. **Keep total task.md under 1000 tokens.** The task gets re-injected every iteration. A 2000-token task eats 12% of the 16k context budget on every call.

6. **Test the test.** After the first run, read the test file Ministral wrote. If the tests are wrong (testing for incorrect behaviour), the loop will never converge. Fix the tests manually and rerun, or add corrective detail to Domain Knowledge.

---

## Running Multiple Tasks Sequentially

For a multi-step project, create numbered task files:

```
task_01_data_model.md
task_02_parser.md
task_03_api_layer.md
```

Run them in order. Each run builds on the workspace left by the previous one:

```powershell
python .\isomoira.py --task task_01_data_model.md
# Wait for completion, verify output
python .\isomoira.py --task task_02_parser.md
# The workspace now has files from task 01, so task 02 can import them
python .\isomoira.py --task task_03_api_layer.md
```

You can also swap philosophy files between tasks if different modules need different design priorities:

```powershell
python .\isomoira.py --task task_api.md --philosophy philosophy_defensive.md
python .\isomoira.py --task task_perf.md --philosophy philosophy_fast.md
```

---

## Quick-Start Checklist

```
[ ] LMStudio running on localhost:1234 with both models available
[ ] pip install requests pytest
[ ] workspace/ is clean (or contains existing project files to build on)
[ ] philosophy.md written (5-6 sentences, ASCII only, under 150 tokens)
[ ] task.md written with all four sections (Task, Scope, Domain Knowledge, Constraints)
[ ] Domain Knowledge section front-loads every fact models might hallucinate
[ ] Scope section lists exact file paths relative to workspace
[ ] Run: python .\isomoira.py
[ ] Monitor console output for test pass/fail cycle
[ ] On completion, inspect workspace/ for generated code
```
