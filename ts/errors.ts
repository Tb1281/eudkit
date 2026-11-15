/**
 * Leaf error type shared by every DSL module.
 *
 * `EudslError` lives here (rather than in `view`) so the condition combinators
 * in `conditions` can raise it without importing `view`, which itself imports
 * `conditions`. `view` re-exports the class, so the public import surface is
 * unchanged.
 */
export class EudslError extends RangeError {
  readonly code: string;

  constructor(code: string, message: string) {
    super(`${code}: ${message}`);
    this.name = "EudslError";
    this.code = code;
  }
}
