# Concurrent Fan-out and Join

Placeholder for v0.7 P0 work to replace v0.6 sequential scheduling with concurrent assignment execution, explicit join semantics, and conflict detection/merge rules for shared candidate workspaces.

## Delivered scope (commit-selection interface only)

The deterministic `select_commit_candidate` function
(`src/hungerloop/services/commit_selection.py`) is delivered as a fan-out-ready
commit-selection interface. It orders candidates by most newly passed checks,
fewest failing checks, and lexicographic candidate id, without using score.

See `specs/v0.7_implementation/2026-07-07-loop-objective-evolution-design.md`
Section 4.3 for the delivered commit-selection design.

## Remaining future work

Concurrent assignment execution, explicit join semantics, and conflict
detection/merge rules for shared candidate workspaces remain future work.
