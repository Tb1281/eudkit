/**
 * Shared deferred-binding state machine for lazy declarations (ADR 0005 §7).
 *
 * Every lazy handle resolves to a fully eager per-builder binding on first
 * resolving use inside `buildModule`. Only the `instantiate` thunk varies
 * between the eight lazy types; everything else is this state machine:
 *
 * - the eager fast path (the owning type keeps a `null` state and returns
 *   itself before calling here),
 * - the active-builder lookup,
 * - the cached binding for that builder,
 * - binding storage, resolution count, and first-binding capture,
 * - the post-build zero/one/many `EUDSL-SCOPE` policy.
 *
 * Callers keep their `resolve` method name and delegate here, so the public
 * surface is unchanged.
 */

import { _getActiveModuleBuilder } from "./trigger.ts";
import { EudslError } from "./view.ts";

export class LazyBinding<T> {
  private readonly typeName: string;
  private readonly entry: string;
  // Bindings are weak-keyed by builder object so a finished builder (and its
  // binding) can be collected once the module is built; `resolvedCount` is
  // monotonic, so post-build ambiguity stays loud even after a builder was
  // collected.
  private readonly bindings = new WeakMap<object, T>();
  private resolvedCount = 0;
  private firstBinding: T | null = null;

  /**
   * `typeName` names the handle in `EUDSL-SCOPE` diagnostics (`"Var"`,
   * `"struct"`, `"FixedArray"`, ...). `entry` is the builder entry point
   * spelled in that diagnostic; both frontends' TypeScript messages say
   * `buildModule`, so it is a parameter for symmetry with Python.
   */
  constructor(typeName: string, entry = "buildModule") {
    this.typeName = typeName;
    this.entry = entry;
  }

  /**
   * Resolve to the active builder's binding.
   *
   * With an active builder, a missing binding is created by `instantiate`
   * under that builder (separate bindings per builder, D3). With no active
   * builder, no allocation ever happens: a handle resolved under exactly one
   * builder returns its cached record, one resolved under several throws as
   * ambiguous, and a never-resolved handle throws `EUDSL-SCOPE`.
   */
  resolve(instantiate: () => T): T {
    const builder = _getActiveModuleBuilder();
    if (builder) {
      const cached = this.bindings.get(builder);
      if (cached !== undefined) {
        return cached;
      }
      const binding = instantiate();
      this.bindings.set(builder, binding);
      this.resolvedCount += 1;
      if (this.resolvedCount === 1) {
        this.firstBinding = binding;
      }
      return binding;
    }
    if (this.resolvedCount === 0) {
      throw new EudslError(
        "EUDSL-SCOPE",
        `deferred ${this.typeName} must be used inside ${this.entry}()`,
      );
    }
    if (this.resolvedCount === 1) {
      return this.firstBinding!;
    }
    throw new EudslError(
      "EUDSL-SCOPE",
      `deferred ${this.typeName} was resolved under ${this.resolvedCount} builders; post-build read is ambiguous`,
    );
  }
}
