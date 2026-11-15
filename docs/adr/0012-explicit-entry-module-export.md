# Explicit root-source entry module export

Status: accepted (2026-11-06)

## Context

The CLI (`eudkit synth`, `eudkit dump-ir`, `eudkit compile`, and frozen
`--plan` builds) executes the frontend source and reads the compiled HIR
module it produced. An earlier revision exposed `lastModule` (TypeScript) and
`last_module` (Python): a process-global that remembered the most recent
`buildModule` result so the frontend could read it after loading the source.
That made the entry an implicit side channel. It was ambiguous when a helper
or test built more than one module, and it could not be checked against the
source namespace before the process had already run arbitrary top-level code.

The frontends now read the compiled module directly from the root source's
namespace (with precise diagnostics for a missing or invalid entry), so the capture hook is
redundant. This change removes it and records the contract.

[ADR 0005](0005-lazy-global-declarations.md) decision 8 states that
`buildModule` is a single entry point, one per map, like Rust `main`; a nested
call throws and sequential calls are separate compilations. That decision
governs how many modules one compilation may construct. It does not say which
value the CLI reads from the source. This ADR defines that second boundary
and leaves ADR 0005 unchanged.

## Decision

1. **The root source exports exactly one compiled module named `module`.**
   The root source is the file path given to `synth`, `dump-ir`, or `compile`.
   TypeScript must export it:

   ```ts
   export const module = buildModule(() => {
     // module body
   });
   ```

   Python must assign it at module scope:

   ```python
   def prog() -> None:
       # module body

   module = build_module(prog)
   ```

   The CLI reads only the root namespace. A missing `module`, or a `module`
   that is not a compiled module, is a frontend error naming the source and
   the expected shape.

2. **`buildModule` / `build_module` remains the HIR construction entry.** It
   runs the body once at compile time and returns the `CompiledModule`; it is
   not the capture hook. The root-source requirement is about handing that
   result to the CLI, not about how the result is built.

3. **Helpers may declare globals, build, or export modules, but cannot
   provide the entry.** Only the root source's `module` export or assignment
   is read. A helper's own `module` export is ignored, even if the root
   imports the helper. Helpers may still declare module-scope globals and
   build modules for their own use; the restriction is about which namespace
   the CLI reads, not what helpers may construct.

4. **Explicit re-export/import is intent; implicit capture is not.** The
   language mechanics differ, but both frontends accept only a root binding
   that the source explicitly establishes:

   - In TypeScript, `import { module } from "./helper"` creates a local
     binding and is NOT a root export; the helper's module stays isolated.
     `export * from "./helper"` and `export { module } from "./helper"` are
     explicit root re-exports, and the CLI accepts the re-exported `module`.
   - In Python, module scope has no import/export distinction. `from helper
     import module` and `module = helper.module` are explicit module-scope
     assignments, and the CLI accepts the resulting `module`. Plain
     `import helper` leaves the root namespace without `module` and stays
     isolated.

   This is deliberate parity of *explicit intent*, not implicit capture: in
   both languages the root source must name `module` in its own namespace.
   A helper's module never becomes the entry merely because the root loaded
   the helper.

5. **Entry validation is structural, not nominal.** The CLI cannot share
   class identity between a source imported from `ts/index.ts` and the
   packaged `dist/` build, so it accepts the entry by the `toJson`
   (TypeScript) / `to_json` (Python) method rather than by `instanceof` /
   `isinstance`. A non-conforming plain object receives the entry diagnostic
   naming its actual shape; a conforming module-like object flows on to
   normal HIR validation.

6. **The implicit capture API is removed.** `lastModule` and `last_module`
   are not public API and must not return. The frontends read the namespace
   directly, so no process-global "most recent module" state exists.

## Considered options

- **Keep `lastModule` as a fallback.** Rejected. It makes the entry depend on
  the last call in the process, not on the source the CLI was given, and it
  hides the missing-export error until after arbitrary top-level code runs.
- **Use a default export instead of `module`.** Rejected. Python has no
  equivalent default-export binding, so the two frontends would diverge.
- **Read any exported compiled module.** Rejected. It would let a helper
  provide the entry accidentally, which is exactly the ambiguity this
  contract removes.

## Consequences

- The entry is explicit, statically inspectable from the source namespace,
  and identical in shape across TypeScript and Python.
- The explicit re-export/import boundary and structural validation are
  locked by CLI end-to-end tests in both frontends.
- The DSL can still call `buildModule` without exporting it (tests and
  helper-only modules); only CLI-driven root sources need the `module`
  entry.
- `buildModule` is documented as the HIR construction entry, not as a capture
  hook. The glossary, both language READMEs, the LSP hover text, and the
  smoke-map README follow this ADR.
