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
- **Edge: JSON parse fails** → `fatal()`, halt with beep
- **Edge: missing `tests` or `plan` keys** → `fatal()`
- **Edge: empty test content or empty plan** → `fatal()`
- Writes test file to workspace, records `original_test_count`

---

## Iteration Loop (starts at iteration 1, runs forever)

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
- **If all pass** → logs "TASK COMPLETE", beep, `break` — done
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
- Re-summarises codebase, re-plans with Consultant
  - **Edge: re-plan JSON parse fails** → `continue` with old plan
  - If re-plan includes updated tests with `count >= original_test_count` → writes them
- `continue` — loops back to Phase 3 with new DK + new plan

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
Iteration 1-2:  Devstral reviews (planner profile)
                Normal path: 5A test audit + 5B implementation review

Iteration 3-4:  Consultant takes over review (effective_stuck >= 3)
                Stuck hint injected into implementer prompt
                Consultant runs 5A + 5B with 61440 ctx + CoT reasoning

Iteration 5:    DK PING fires (effective_stuck >= 5)
                Consultant diagnoses Domain Knowledge gap
                Append-only amendment to task.md (500-char cap)
                Stuck counters reset, Consultant re-plans
                Loop continues with amended DK

Iteration 5+:   If DK amendment worked → stuck counters were reset,
                new plan gets a fresh trial from iteration 1 logic
                If same tests still fail → re-enters escalation ladder
                Size cap (original + 2000 chars) prevents unbounded growth
                Eventually halts if Consultant cannot resolve the gap
```
