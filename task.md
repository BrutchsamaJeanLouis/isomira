# Task

Build a **Last-Writer-Wins Element Set (LWW-Element-Set) CRDT** backed by **vector clocks** for causal ordering. This is a conflict-free replicated data type suitable for distributed systems where multiple replicas can independently add/remove elements and later merge their states.

The implementation goes in a single file `lww_set.py`.

## Scope

workspace/lww_set.py

## Domain Knowledge

### Vector Clocks
- A vector clock is a `dict[str, int]` mapping replica IDs to logical counters.
- **Incrementing:** When replica `r` performs an operation, it increments `vc[r]` by 1.
- **Comparison rules (these are precise -- do not deviate):**
  - `vc_a == vc_b`: every key in the union of both clocks has the same value (missing keys count as 0).
  - `vc_a <= vc_b` (a is dominated by or equal to b): for every key `k` in the union, `vc_a.get(k, 0) <= vc_b.get(k, 0)`.
  - `vc_a < vc_b` (a strictly happens-before b): `vc_a <= vc_b` AND `vc_a != vc_b`.
  - `vc_a` and `vc_b` are **concurrent** if neither `vc_a <= vc_b` nor `vc_b <= vc_a`.
- **Merge of two vector clocks:** For each key `k` in the union, `merged[k] = max(vc_a.get(k, 0), vc_b.get(k, 0))`.

### LWW-Element-Set
- Maintains two internal sets: an **add_set** and a **remove_set**.
- Each entry in add_set and remove_set is a tuple of `(element, vector_clock)`.
- **`add(element, replica_id)`:** Increment the vector clock for this replica, then store `(element, copy_of_current_vc)` in the add_set.
- **`remove(element, replica_id)`:** Increment the vector clock for this replica, then store `(element, copy_of_current_vc)` in the remove_set. Removing an element that was never added is a no-op (do not raise).
- **`lookup(element) -> bool`:** An element is in the set if and only if there exists an add entry for it whose vector clock is NOT strictly dominated by any remove entry for that same element. Formally: element is present iff `∃ (e, vc_add) in add_set` such that `¬∃ (e, vc_rem) in remove_set` where `vc_add < vc_rem`.
- **`elements() -> set`:** Returns the set of all elements for which `lookup` returns True.
- **`merge(other)`:** Combines two LWW-Element-Sets:
  - The merged add_set is the union of both add_sets.
  - The merged remove_set is the union of both remove_sets.
  - The merged vector clock is the pointwise max of both clocks.
  - Merge must be **commutative** (`a.merge(b)` gives same state as `b.merge(a)`), **associative**, and **idempotent** (`a.merge(a)` is a no-op).
  - Merge mutates `self` in-place and also mutates `other` to the same final state (both replicas converge).

### Critical Edge Cases (models frequently get these wrong)
- **Concurrent add and remove of the same element:** If replica A adds "x" and replica B removes "x" concurrently (neither VC dominates), the element SHOULD be present after merge (add-wins on concurrency -- this is the "bias" of this CRDT variant).
- **Re-adding after remove:** If "x" is removed, then added again with a later VC, it must reappear.
- **Multiple VCs per element:** The add_set and remove_set can contain MULTIPLE entries for the same element (with different vector clocks). All of them matter for lookup.
- **Empty/missing replica IDs in VCs:** A missing key in a VC means counter 0 for that replica. Comparisons must handle asymmetric key sets.
- **CRITICAL -- Sequential single-replica operations:** When ONE replica does add, add, add, remove in sequence, EVERY prior add's VC is strictly dominated by the remove's VC (because all operations are on the same replica counter: r1:1, r1:2, r1:3 are ALL < r1:4). The element is NOT present after this sequence. Multiple adds only survive a remove when they come from DIFFERENT replicas with concurrent VCs. Do NOT confuse "multiple adds exist" with "at least one add survives" -- survival depends entirely on VC comparison, not on count of adds.

## Constraints

- No external dependencies. stdlib only.
- All public methods must have type hints.
- The vector clock must be a plain `dict[str, int]`, not a custom class. Helper functions for VC operations (compare, merge, increment) should be module-level functions, not methods on the set.
- Do not use `datetime` or wall-clock timestamps anywhere. Ordering is purely via vector clocks.
- The `merge()` method must converge both replicas to identical state (mutate both `self` and `other`).
- Store add_set and remove_set as `list[tuple[Any, dict[str, int]]]` -- not dicts, not sets. Elements can have multiple entries with different VCs.
