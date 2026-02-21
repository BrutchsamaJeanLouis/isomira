# Isomoira Self-Grading Rubric: QIWM Project

Persistent evaluation framework for scoring Isomoira orchestrator runs across the QIWM (Quantum-Inspired World Model) project phases. Any future Claude session can read this file to assess run quality without full conversation history.

---

## Grading Dimensions

### 1. Convergence Speed (0-10)

How many iterations to reach full test pass?

| Score | Criteria |
|-------|----------|
| 10 | 1-2 iterations (first-shot or trivial fix) |
| 8 | 3-4 iterations (predicted baseline for well-scoped tasks) |
| 6 | 5-7 iterations (acceptable, some stuck loops resolved) |
| 4 | 8-12 iterations (significant stuck periods, but eventually converges) |
| 2 | 13+ iterations but eventually passes |
| 0 | Never converges (killed manually) |

### 2. Test Quality (0-10)

How good are the tests the planner wrote?

| Score | Criteria |
|-------|----------|
| 10 | All tests correct, good edge coverage, no float precision traps, no hallucinated expectations |
| 8 | Tests correct but thin coverage (misses obvious edge cases) |
| 6 | 1 test has wrong expectation but doesn't block convergence |
| 4 | 1-2 tests have wrong expectations that cause stuck loops (like CRDT run #5) |
| 2 | Multiple wrong tests, test protection guardrail fires repeatedly |
| 0 | Tests are fundamentally broken (wrong imports, test file doesn't parse) |

**Key indicators:**
- Float precision: Does the planner use `assert abs(x - y) < tol` or exact `assert x == y`?
- Boundary gradient: Does the test expect `/1.0` or `/2.0` divisor at boundaries?
- Obstacle semantics: Does the test check `<= radius` (correct) or `< radius` (off-by-one)?
- Empty state: Does it test empty sources/obstacles returning all-zeros?

### 3. Implementation Quality (0-10)

How good is the implementer's final code?

| Score | Criteria |
|-------|----------|
| 10 | Idiomatic numpy, vectorized computation, correct boundary handling, clean types |
| 8 | Correct but uses Python loops instead of numpy vectorization (slow but right) |
| 6 | Correct for test cases but fragile (would break on untested inputs) |
| 4 | Passes tests but has latent bugs visible on inspection |
| 2 | Passes tests via hack (e.g., hardcoding expected values) |
| 0 | Does not pass tests |

**Key indicators:**
- Does `compute_potential_field()` use `np.indices` or meshgrid (good) vs nested for-loops (acceptable) vs wrong broadcasting (bad)?
- Does `get_gradient()` handle all 4 corners + 4 edges + interior separately?
- Are type hints present and correct?
- Is the obstacle barrier exactly `+1000.0` (not `999`, not `float('inf')`)?

### 4. Orchestrator Behavior (0-10)

How well did the Isomoira infrastructure perform?

| Score | Criteria |
|-------|----------|
| 10 | All guardrails worked, no normalize_plan warnings, no rejected test updates, clean log |
| 8 | Minor warnings (normalize_plan fallback used, but plan entries still routed correctly) |
| 6 | Test protection fired correctly (prevented regression), some normalize_plan failures |
| 4 | Stuck loop detected and hint injected, but took many iterations to break out |
| 2 | Review feedback not reaching Devstral (pathway 3 failing), stuck with no escape |
| 0 | Orchestrator crashed (KeyError, encoding error, API timeout) |

### 5. Agent Disambiguation (0-10)

Did the models correctly treat this as a WORLD (environment) with no agent/AI logic?

| Score | Criteria |
|-------|----------|
| 10 | No agent-related code in toy_world.py. Pure environment. Clean separation. |
| 8 | Minor naming leakage (e.g., variable named `agent_position`) but no behavioral code |
| 6 | Devstral added a navigation/decision method that wasn't in the task |
| 4 | Planner wrote tests for agent behavior that doesn't belong in Phase 1 |
| 2 | Significant scope creep -- agent classes or decision logic in the environment file |
| 0 | Complete confusion -- models built an agent system instead of a world |

---

## Per-Phase Targets

### Phase 1: ToyWorld (Classical Baseline)

**Task:** `toy_world.py` -- grid, energy sources, obstacles, potential field, gradient extraction.

**Predicted baseline:** 8-12 tests, 3-5 iterations to completion.

**Known risk factors:**
- Float precision in tests (planner may use exact equality on computed floats)
- Boundary gradient divisor ambiguity (Domain Knowledge specifies interior formula but boundary formula is implicit -- forward/backward difference divides by 1.0 vs central difference by 2.0)
- numpy import style mismatch between test and implementation
- Obstacle barrier at exactly radius boundary (`<= radius` vs `< radius`)

**Success threshold:** Score >= 7 on each dimension. Total >= 38/50.

**Comparison baseline (CRDT):**
- CRDT best run: Convergence 4/10, Test Quality 4/10, Implementation 6/10, Orchestrator 8/10, Disambiguation N/A
- CRDT total: ~22/40 (no disambiguation dimension for CRDT)
- ToyWorld should significantly outperform CRDT on all dimensions

### Phase 2: QuantumInspiredWorld (Pilot Wave + Collapse) -- FUTURE

**Task:** Extend ToyWorld with pilot_wave_field, coherence_field, collapse_mechanism, guidance_field.

**Predicted difficulty:** Medium-High. Diffusion (Laplacian) is numpy-friendly but the coupling between fields creates state interaction that Devstral may struggle with. The collapse mechanism (agent observation modifying coherence) introduces the agent concept for the first time.

**Key risk:** The word "collapse" has quantum physics connotations that may pull Devstral toward literal quantum mechanics rather than the metaphorical scaffolding specified in the project.

### Phase 3: IIT Coherence (Phi Metric) -- FUTURE

**Task:** `compute_phi()` function measuring integrated information.

**Predicted difficulty:** High. Temporal mutual information requires sampling field states across timesteps and computing correlation matrices. This is the most mathematically dense phase and the one most likely to produce hallucinated formulas.

### Phase 4: NanoAgents (Bio-Inspired Navigation) -- FUTURE

**Task:** `NanoAgent` class with sense/decide/act/survive cycle.

**Predicted difficulty:** Medium. But this is where the "agent" disambiguation tension peaks. The Isomoira agent builds agents. The Domain Knowledge must be extremely precise about what kind of agent this is (bio-inspired navigator, not AI/LLM agent).

### Phase 5: Pygame Visualization -- FUTURE

**Task:** Interactive demo with heatmaps, sprites, controls.

**Predicted difficulty:** High for TDD. Pygame is inherently visual and stateful -- hard to test with pytest. Tests will likely be limited to "does the window open" and "do controls modify state." The visual quality can only be assessed by human inspection.

---

## How to Grade a Run

1. Read `isomoira.log` from start to end
2. Count iterations to completion (or note if killed)
3. Read the test file -- check for float precision issues, boundary edge cases, scope creep
4. Read the implementation file -- check numpy idiom, correctness, type hints
5. Search log for: `REJECTED test update`, `no valid entries after normalization`, `STUCK LOOP DETECTED`, `BLOCKED`
6. Check implementation file for any agent/decision/navigation code (Phase 1 only)
7. Score each dimension 0-10
8. Compare to predicted baseline and CRDT historical performance
9. Document findings and any Domain Knowledge fixes needed for the next run

---

## Historical Results

### CRDT Task (LWW-Element-Set with Vector Clocks)

| Run | Tests | Best Pass Rate | Iterations | Outcome | Key Issue |
|-----|-------|---------------|------------|---------|-----------|
| 1 | 17 | 12/17 | 2 (crashed) | FAIL | KeyError: 'file' in normalize_plan |
| 2 | 17 | 17/17 stuck | 15 | FAIL | Groundhog day loop (identical outputs) |
| 3 | 17 | 17/17 | ~3 | PASS (but review destroyed tests) | Review replaced 17 tests with 1 |
| 4 | 20 | 20/20 | 3 | PASS | First genuine success. Test protection + pathway 3 worked. |
| 5 | 11 | 10/11 | 12 (killed) | FAIL | Hallucinated test: sequential single-replica adds survive remove |

**Lessons learned (carry forward to QIWM):**
- Domain Knowledge precision is the #1 factor. Ambiguity in DK = hallucinated test expectations = permanent stuck loops.
- Test protection guardrail is essential. Without it, review destroys the test suite.
- Review-to-implementation feedback (pathway 3) dramatically improves convergence.
- Stuck loop detection works but the threshold (3) may be too low for tasks that need creative leaps.
- Models at temp 0.15 are near-deterministic. Identical prompts = identical outputs. Stuck means STUCK.

**New lessons from QIWM run 1:**
- Formulas without conceptual shape = reversed expectations. DK must provide the topology (what the field LOOKS LIKE), not just the equations.
- Stuck detection must hash structural patterns, not raw output. Memory addresses in pytest output change per run.
- When Devstral is correct from iteration 1 but tests are wrong, the review model blames the implementation instead of the tests. The review prompt frames failures as implementation bugs, creating a blind spot for test bugs.
- Superposition rules need explicit numeric examples showing the combined value, not just "contributions sum."

### QIWM Phase 1: ToyWorld

| Run | Tests | Best Pass Rate | Iterations | Outcome | Key Issue |
|-----|-------|---------------|------------|---------|-----------|
| 1 | 10 | 6/10 | 17 (killed) | FAIL | 4 wrong tests: reversed inequality (x2), obstacle ignores superposition, test bypasses public API via _potential |
| 2 | 11 | 6/11 | 9+ (killed) | FAIL | DK PING never fired (impl hash cosmetic changes, no break after PING) |
| 3 | 8 | 6/8 | 6 (DK PING halt) | FAIL | DK PING worked correctly, halted to save tokens. 2 test bugs remaining. |
| 4a | 8 | 6/8 | 6 (DK PING halt) | FAIL | Pre-fix orchestrator (Mistral Small planner). Review blamed impl 4x, never considered test bugs. |
| 4b | 8 | **8/8 (100%)** | **4** | **PASS** | Post-fix orchestrator (bifurcated review + partial pass tracking + len//3). First QIWM success. |

**Run 1 scores:**
| Dimension | Score | Notes |
|-----------|-------|-------|
| Convergence Speed | 0/10 | Never converged. 17 iterations, same 4 failures throughout. |
| Test Quality | 2/10 | 4/10 tests wrong. Reversed potential comparisons (adjacent < source), obstacle test ignores energy contribution, gradient test sets _potential bypassing compute. |
| Implementation Quality | 9/10 | Devstral correct from iteration 1. Clean numpy, proper boundary handling, correct formula. |
| Orchestrator Behavior | 3/10 | Stuck detection never fired (memory addresses in pytest output defeat MD5 hash). Review diagnosed wrong (blamed implementation, not tests). Test protection fired correctly 4 times. |
| Agent Disambiguation | 10/10 | No agent code leaked. Pure environment class. |
| **Total** | **24/50** | Below 38/50 threshold. |

**Run 4b scores (post-fix orchestrator):**
| Dimension | Score | Notes |
|-----------|-------|-------|
| Convergence Speed | 8/10 | 4 iterations to 8/8 pass. Within 3-4 predicted baseline. |
| Test Quality | 6/10 | 3/8 tests initially wrong (superposition accounting, gradient values, np.isclose API). All 3 self-corrected by Phase 5A without human intervention. |
| Implementation Quality | 9/10 | Devstral correct or near-correct from iteration 1. Clean numpy. |
| Orchestrator Behavior | 9/10 | Bifurcated review worked perfectly (5A caught test bugs 3x, skipped 5B each time). Partial pass tracking showed monotonic convergence (62%->62%->87%->100%). Only ding: 5A took 2 passes to fully fix tests (introduced rel_tol bug in its own correction). |
| Agent Disambiguation | 10/10 | No agent code leaked. Pure environment class. |
| **Total** | **42/50** | **Above 38/50 threshold.** First QIWM pass. |

**Run 4b trajectory:**
```
Iter 1: 5/8 (62%) — 3 test bugs (superposition, gradient interior, gradient boundary)
Iter 2: 5/8 (62%) — 5A corrected 3 test bugs, but introduced rel_tol API error
Iter 3: 7/8 (87%) — 5A caught rel_tol -> rtol fix
Iter 4: 8/8 (100%) — ALL TESTS PASS
```

**Run 4b vs Run 1 delta (same task, fixed orchestrator):**
| Dimension | Run 1 | Run 4b | Delta |
|-----------|-------|--------|-------|
| Convergence | 0/10 | 8/10 | +8 |
| Test Quality | 2/10 | 6/10 | +4 |
| Implementation | 9/10 | 9/10 | 0 |
| Orchestrator | 3/10 | 9/10 | +6 |
| Disambiguation | 10/10 | 10/10 | 0 |
| **Total** | **24/50** | **42/50** | **+18** |

**Key insight:** Implementation quality was never the bottleneck (9/10 both runs). The +18 point improvement came entirely from orchestrator fixes: bifurcated review eliminated the review asymmetry that kept blaming correct code, and partial pass tracking provided convergence visibility. The model was right from day one; the orchestrator just couldn't see it.

**Orchestrator changes between Run 1 and Run 4b:**
- Bifurcated review: Phase 5 split into 5A (test audit against DK) + 5B (implementation diagnosis)
- Token estimation: len//4 -> len//3 (honest constraint, compress early not late)
- Retry resilience: 3 retries with exponential backoff on ConnectionError/Timeout
- Partial pass tracking: Tests: N/M passed (P%) logged every iteration
- Stuck detection: P/F pattern hash + failing-set hash (dual signal)
- DK PING: break after fire, failing-set tracking survives cosmetic code changes
- Dual-profile tuning: single model (Devstral 24B) with planner T=0.6, implementer T=0.4

**Root cause analysis (Run 1):**
- DK had formulas but no conceptual shape (field landscape / well topology)
- The planner couldn't decompress formulas into correct inequality directions without the shape anchor
- Obstacle superposition stated but not reinforced with explicit "1000 + negative != exactly 1000"
- get_gradient spec said "computed from scratch each call" but test tried to bypass via _potential
- Boundary gradient formula ambiguous (divisor unspecified for forward/backward difference)
- Stuck detection completely blind due to changing memory addresses in pytest output

**Fixes applied between runs:**
- task.md: Added "CRITICAL -- Field Shape" section with explicit numeric examples and inequality direction
- task.md: Clarified obstacle stacking ("1000.0 + negative energy contribution, which is LESS than 1000.0")
- task.md: Changed interface to store field as self.potential (public), get_gradient reads from it
- task.md: Specified boundary formulas explicitly with /1.0 divisor
- task.md: Added constraint "Tests must only interact through public methods and public attributes"
- isomoira.py: Stuck detection now hashes PASS/FAIL pattern (P/F per test line) not raw output
- isomoira.py: Bifurcated review (Phase 5A test audit + Phase 5B implementation review)
- isomoira.py: Token estimation len//3, retry with backoff, partial pass tracking

---

## Domain Knowledge Iteration Protocol

When a run fails or gets stuck, check these in order:

1. **Is the test expectation correct?** Trace through the Domain Knowledge formulas by hand for the exact test inputs. If the test expects a wrong value, the DK needs a clarifying statement.
2. **Is the DK ambiguous?** If the planner and implementer profiles disagree on a behavior, the DK didn't specify it precisely enough. Add an explicit statement resolving the ambiguity.
3. **Is the DK missing a critical fact?** If the model hallucinated a behavior not mentioned in DK, add a "CRITICAL" bullet that explicitly states the correct behavior and warns against the hallucination.
4. **Is the test scope correct?** If the planner wrote tests for behavior outside the current phase (e.g., agent tests in Phase 1), the task.md disambiguation section needs strengthening.

After any DK fix: clear workspace, clear log, run fresh. Never patch implementation -- always let the models converge from scratch with the corrected DK.
