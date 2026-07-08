# LLMPlanner

Placeholder for v0.7 P0 work to introduce an LLM-assisted mission planner that proposes assignment graphs while preserving I-10 by routing final ledger changes through deterministic compiler validation.

## Delivered scope (partial)

The `SpecCheckSynthesizer` component (`src/hungerloop/services/spec_check_synthesizer.py`)
delivers the LLM-assisted spec-to-check synthesis portion of this placeholder.
It uses `ModelClient` outside validators, cost-guards every LLM call, and
routes accepted proposals through compiler-owned `RefinementCompiler.compile_spec_coverage`.

See `specs/v0.7_implementation/2026-07-07-loop-objective-evolution-design.md`
Section 3 for the full delivered synthesis design.

## Remaining future work

Full LLM-assisted mission planner that proposes assignment graphs (not just
acceptance checks) remains future work.
