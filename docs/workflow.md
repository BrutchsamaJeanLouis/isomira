# Isomira Workflow Loop

Step-by-step flow with every edge case documented.

---

## Phase 1: SUMMARISE (orchestrator, no model)

- AST-parse workspace for file tree, Python signatures, imports, line counts
- **Edge: workspace empty** → returns `"(empty workspace)"`

---

## Phase 2: PLAN (Consultant — Ministral 14B, 61440 ctx)

- Context limit temporarily swapped to 61440 for assembly
- Consultant generates plan with `<think>` CoT → stripped via `strip_think_blocks()` before JSON parse
- `plan_generation = 1` (first plan)
- **Edge: JSON parse fails** → `fatal()`, halt with beep
- **Edge: missing `tests` or `plan` keys** → `fatal()`
- **Edge: empty test content or empty plan** → `fatal()`
- Writes test file to workspace, records `original_test_count`

---

## Iteration Loop (starts at iteration 1, runs forever)

Each iteration logs: `ITERATION {N} (plan gen {G})`

### Phase 3: IMPLEMENT (Devstral, implementer profile, T=0.4)

- Reloads current files from plan entries on disk
- If `stuck_count >= 3`, injects `stuck_hint` telling Devstral to try a completely different strategy
- Includes `last_diagnosis`, `test_output` (failure lines only), and `last_review_code` from previous review cycle
- **Edge: no file blocks in output** → logs warning, continues (no files written)
- Writes all file blocks to workspace
- Tracks impl stability via MD5 hash (`impl_stable_count` increments if identical code produced)
- Executes CMD blocks through sandbox
- **Edge: CMD blocked by sandbox** → beep, block reason fed back as stderr
- **Edge: CMD times out** → returns timeout error

### Phase 4: TEST (orchestrator, pytest)

- Runs `python -m pytest <test_file> -v --tb=short`
- **Exit gate**: `test_result["passed"] AND iteration >= MIN_ITERATIONS`
  - `MIN_ITERATIONS` set via `--min-iterations` CLI flag (default: 2)
  - **If all pass AND iteration >= MIN_ITERATIONS** → logs "TASK COMPLETE" with iteration count and plan generation, beep, `break` — done
  - **If all pass BUT iteration < MIN_ITERATIONS** → logs "minimum not reached, continuing" — proceeds to Phase 5 as if tests failed. This prevents premature exit before the implementer has run.
- **If fail** → logs pass/fail ratio, continues

### Stuck Detection (two independent signals)

1. **P/F pattern hash**: MD5 of PASSED/FAILED sequence. Same hash → `stuck_count += 1`, different → reset to 1
2. **Failing set hash**: frozenset of failing test names. Same set → `failing_set_count += 1`, different → reset to 1
3. `effective_stuck = max(stuck_count, failing_set_count)`
4. If `effective_stuck >= 3` → logs "STUCK LOOP DETECTED"

### DK PING (effective_stuck >= 5) — Consultant autonomous DK amendment

- Records original `task.md` size, sets cap at `original + 2000` chars
- Sends full context (task, failing tests, assertion clues, implementation, test output) to Consultant with 61440 ctx
- Strips `<think>` blocks, parses JSON expecting `{diagnosis, dk_addition, confidence}`
- **Edge: unparseable output** → halt with triple beep
- **Edge: `confidence == "low"` or empty `dk_addition`** → halt with triple beep
- **Edge: `dk_addition > 500` chars** → truncated to 500
- **Edge: amended task.md exceeds size cap** → halt with triple beep
- On success: appends `[Auto-DK iteration N] <addition>` to Domain Knowledge section
  - **Edge: no `## Domain Knowledge` section** → appends one at end of file
- Resets all stuck counters (`stuck_count`, `failing_set_count`, `last_test_hash`, `last_failing_set`)
- Increments `plan_generation`
- Re-summarises codebase, re-plans with Consultant
  - **Edge: re-plan JSON parse fails** → `continue` with old plan
  - If re-plan includes updated tests with `count >= original_test_count` → writes them
- `continue` — loops back to Phase 3 with new DK + new plan
- **Note on exit gate**: `iteration` is global and monotonic (never resets). After a DK re-plan at iteration 12, `iteration >= MIN_ITERATIONS` is already satisfied, so the new plan can exit on its first successful test. The `--min-iterations` flag is a global floor, not per-plan.

### Phase 5A: TEST AUDIT (Devstral normally, Consultant when effective_stuck >= 3)

- Context limit swapped if Consultant; `<think>` stripped if Consultant
- **Edge: audit output unparseable** → defaults to `{tests_correct: True, issues: []}`
- If tests found incorrect AND corrected tests provided AND `count >= original_test_count`:
  - Writes corrected tests, updates `original_test_count`
  - `continue` — skips 5B, re-runs from Phase 3 with corrected tests
- **Edge: proposed test count < original** → rejected, keeps original tests

### Phase 5B: IMPLEMENTATION REVIEW (Devstral normally, Consultant when effective_stuck >= 3)

- Context limit swapped if Consultant; `<think>` stripped if Consultant
- **Edge: review output unparseable** → logs warning, `continue` with same plan
- Extracts `diagnosis` → stored as `last_diagnosis` for next implementer call
- Extracts corrected code from plan entries (`extract_review_code`) → stored as `last_review_code`
- Normalizes review plan with `fallback_file` from current plan
  - **Edge: review plan has no valid entries after normalization** → keeps previous plan
- `plan` updated → loops back to Phase 3

---

## Model Call Retry Logic (in `call_model`)

- 4 attempts total (1 initial + 3 retries)
- Backoff delays: 2s, 8s, 32s (handles LMStudio model swap latency)
- **ConnectionError/Timeout** → retries
- **All 4 attempts fail** → `fatal()`
- **Other exception** → `fatal()` immediately

---

## Escalation Ladder Summary

```
Iteration 1:    Tests may pass vacuously (no impl yet)
                Exit gate blocks: iteration 1 < MIN_ITERATIONS (2)
                Forced into review → implement cycle

Iteration 2+:   Exit gate open (iteration >= MIN_ITERATIONS)
                Normal path: Devstral reviews (planner profile)
                5A test audit + 5B implementation review

Stuck 3-4:      Consultant takes over review (effective_stuck >= 3)
                Stuck hint injected into implementer prompt
                Consultant runs 5A + 5B with 61440 ctx + CoT reasoning

Stuck 5:        DK PING fires (effective_stuck >= 5)
                Consultant diagnoses Domain Knowledge gap
                Append-only amendment to task.md (500-char cap)
                Stuck counters reset, plan_generation increments
                Consultant re-plans, loop continues with amended DK

Post-DK:        iteration is still high (global, never resets)
                Exit gate is already satisfied (iteration >> MIN_ITERATIONS)
                New plan gets tested immediately — can exit on first pass
                If same tests still fail → re-enters escalation ladder
                Size cap (original + 2000 chars) prevents unbounded growth
                Eventually halts if Consultant cannot resolve the gap
```

---

## `--min-iterations` Estimation Guide

LLMs routinely claim "done" before implementation is complete. Tests may pass vacuously (imports succeed, no assertions hit real code yet) or against stale workspace files. The `--min-iterations` flag is a global floor that forces the loop to keep cycling regardless of test results.

**Per-iteration cost breakdown:**
- IMPLEMENT call: ~4096 output tokens
- REVIEW call: ~2048 output tokens
- Consultant calls (PLAN, stuck review): ~8192 output tokens (less frequent)
- pytest run: ~2-5 seconds (negligible vs model calls)

**At 20 tok/s (local, e.g. RTX 4060 Ti 16GB):**
- IMPLEMENT: ~205s (~3.4 min)
- REVIEW: ~102s (~1.7 min)
- Total per iteration: ~5-6 min (with overhead, pytest, file I/O)

**At 60 tok/s (cloud API or high-end GPU):**
- IMPLEMENT: ~68s (~1.1 min)
- REVIEW: ~34s (~0.6 min)
- Total per iteration: ~2 min

### Recommended values

| Task scope | Dev equivalent | `--min-iterations` | ~Time at 20 tok/s (local) | ~Time at 60 tok/s (cloud) | Typical failure modes |
|---|---|---|---|---|---|
| Minor refactor | 1 day | `3` | ~18 min | ~6 min | Missed edge case, incomplete rename |
| Feature update | 1 week | `6` | ~36 min | ~12 min | Hallucinated APIs, partial integration, wrong signatures |
| Full project (oneshot) | 2 months | `12` | ~72 min | ~24 min | Architectural drift, DK gaps, cascading test failures |

### How it interacts with DK PING across multiple plan generations

The `--min-iterations` is a **global floor** on the monotonic `iteration` counter. The `iteration` counter never resets — not on DK re-plan, not on test correction, not on any `continue` branch.

**Example: full project with `--min-iterations 12`**

```
Iteration 1-2:   Plan gen 1. Implementer runs. Tests may pass vacuously.
                 Exit blocked: iteration < 12.

Iteration 3-5:   Tests failing on same set. Stuck detected at 3.
                 Consultant takes over review (5A + 5B).

Iteration 5:     DK PING fires. Consultant amends task.md.
                 Stuck counters reset. plan_generation = 2.
                 Consultant re-plans.

Iteration 6-8:   Plan gen 2. Implementer runs with new DK.
                 Exit blocked: iteration < 12.

Iteration 8-10:  Tests still failing on a different set.
                 Stuck detected again at 3 (stuck counters were reset).
                 Consultant takes over review again.

Iteration 10:    DK PING fires again. Second DK amendment.
                 plan_generation = 3. Re-plan.

Iteration 11:    Plan gen 3. Implementer runs. Tests pass.
                 Exit blocked: 11 < 12. Continues.

Iteration 12:    Implementer re-runs. Tests still pass.
                 Exit allowed: 12 >= 12. TASK COMPLETE.
```

This means a `--min-iterations 12` run with 2 DK pings gives:
- Plan gen 1: ~5 iterations (then DK ping)
- Plan gen 2: ~5 iterations (then DK ping)
- Plan gen 3: ~2 iterations (then exit)
- Each plan generation gets a meaningful trial

If only 1 DK ping fires (at iteration 5), the remaining 7 iterations all go to plan gen 2 — plenty of trial time for the amended DK.

---

## CLI Reference

```
python isomira.py init <project_name>

python isomira.py --project <dir> [options]
  --min-iterations N   Minimum iterations before exit allowed (default: 2)
  --task <file>        Task file path (default: task.md)
  --philosophy <file>  Philosophy file path (default: philosophy.md)
  --workspace <dir>    Workspace directory (overrides CONFIG)
  --url <url>          LMStudio API URL (overrides CONFIG)
```
