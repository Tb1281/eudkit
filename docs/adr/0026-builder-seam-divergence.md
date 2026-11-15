# Builder-seam divergences between the TypeScript and Python frontends are intentional

Status: accepted (2026-11-14)

## Context

Both frontends expose a builder seam between the trigger layer and the module
builder. TypeScript declares `ModuleBuilderHook` (`ts/trigger.ts`) as a
**3-required + 10-optional** interface: `allocTriggerId`, `registerTrigger`,
and `consumeQueuedActs` are required; allocation, string, scope-serial,
flusher, transfer, and blob hooks are optional. Python's `_ModuleBuilderState`
(`py/hir.py`) is a concrete, duck-typed class instead; callers feature-detect
methods rather than depending on a declared structural type.

The mirrors also differ in two observable behaviours. They look like drift and
invite a "deterministic Set-backed flushers" cleanup, so the divergence needs a
decision record rather than a comment alone.

## Decision

The divergences are intentional and pinned:

1. **Flusher storage.** TypeScript flushers live in a `Set`, so registering the
   same function twice runs it once per flush. Python flushers live in a
   `list`, so the same registration runs twice.
2. **Root pop.** TypeScript `popScope` pops the root scope unconditionally.
   Python `pop_scope` guards the root, returning `[]` and keeping the root.

The contract comments at `ts/trigger.ts` (the `ModuleBuilderHook` block) and
`py/hir.py` (`_ModuleBuilderState`) say not to unify these without a
defect-level reason. `test_builder_seam.py` and `test_builder_seam.ts` pin both
asymmetries per language, so the pins fail if either behaviour changes.

## Consequences

- Reject the "deterministic Set-backed flushers" cleanup. No unification
  without a defect that the current behaviour actually causes.
- A narrower required interface plus capability objects is a separate future
  design. It is not a bug fix and should not be smuggled in under this ADR.
- The seam stays duck-typed in Python and structural in TypeScript; both are
  tested directly rather than only through whole-program output.
