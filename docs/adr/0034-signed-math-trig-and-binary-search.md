# Signed division/modulo, table-driven trig, and binary search

Status: accepted (2026-11-16)

## Context

`eudkit` previously shipped only `isqrt` in `mathf`. Users porting `eudplib`
scripts need signed integer division/modulo (`towards_zero`, `floor`, `euclid`),
compile-time exponent integer powers (`pow`), trig helpers (`atan2`,
`lengthdir`, plus 1/256-turn variants), and monotone range search
(`binary_min`, `binary_max`) without introducing `Forward`, `SetNextPtr`, or
per-call payload table duplication.

## Decision

1. **Signed div/mod fixup over the shared restoring ladder:** All six helpers
   share `_signed_divmod` using the single unsigned restoring ladder (`divmod` in
   `var`) or constant-divisor ladder (`const`), converting operands to
   sign-magnitude and fixing up quotient/remainder signs per rounding mode
   (`towards_zero` = C/JS truncation, `floor` = Python `//`/`%`, `euclid` =
   non-negative remainder). Build-time zero divisor raises `EUDSL-DIV-ZERO`;
   game-time divisor zero preserves `(0, a)`.
2. **Compile-time `pow`:** `pow(base, exp, bits)` requires a plain `int`
   exponent `0..31` and lowers via MSB-first exponentiation-by-squaring over
   `Var.imul`.
3. **One module-level Q16 trig payload cache per builder:** `ATAN256_Q16`,
   `COS_DEG_Q16`, `SIN_DEG_Q16`, `COS_256_Q16`, and `SIN_256_Q16` are cached per
   builder (`WeakKeyDictionary` / `WeakMap`), materialized once as `Db` blobs,
   and read at runtime via `with_epd` + `read_epd`. `atan2` derives degrees from
   the 1/256-turn table without allocating a second degree table.
4. **Condition-only binary search:** `binary_min`/`binary_max` run
   `(hi - lo + 1).bit_length()` steps over `[lo, hi]` (`hi < 0xFFFFFFFF`),
   including `1` step when `lo == hi`, and return `hi + 1` / `lo - 1` when no
   element satisfies the predicate (avoiding `EUDBinaryMin`'s `maxv` ambiguity).
