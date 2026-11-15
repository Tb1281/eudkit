# Capacity-Aware Aligned FixedString Writes

Status: accepted (2026-11-04; amended 2026-11-06)

## Context

`FixedString(size)` reserves `size` content bytes plus one NUL at offset `size` in
an STR entry. Static runs defer their trailing NUL so adjacent writes pack into
dword `SetDeaths` actions. Dynamic bytes (`append` after a scope boundary,
`writeFrom`, hex/format placeholders) patch a scratch pointer and must select
the byte lane at runtime, which costs a four-way `when` per byte.

An earlier prototype padded every dword-shaped tail to the next boundary with
the zero-width byte `0x1A` and reserved four extra backing bytes. That let the
following dynamic bytes bake a constant byte mask, but repeated scope boundaries
could consume unbounded padding and write past `FixedString(size)` into the STR
marker or the next string.

## Decision

1. **Capacity is the only slack.** `FixedString(size)` content occupies offsets
   `0..size-1`; offset `size` is the NUL. There is no fixed `+4` tail
   allowance. Static overflow fails at build time with `EUDSL-BOUNDS`.
2. **Align at scope boundaries, not per append.** `pushScope` and `popScope`
   flush a partial static tail to lane 0 with `0x1A` padding and place the NUL
   on the boundary. Module end and display flushes stay unaligned because no
   run follows them. Repeated boundaries therefore consume up to three pad
   bytes plus a terminator each, and each pad is capacity-checked before it is
   written.
3. **Fallback is unaligned, never overflow.** If the padded tail plus its NUL
   would exceed `size`, the boundary emits the plain tail and marks the lane
   unknowable. Dynamic boundary pads are written only when the tracked content
   bound plus the pad still fits. A runtime `writeFrom` position makes the lane
   unknowable until the next absolute write.
4. **Known lanes bake their mask.** When the lane is provable, a dynamic byte
   emits one `SetDeathsX` with a constant byte mask instead of a four-way
   branch. Decimal digits are emitted from conditional branches, so they always
   use the unknown-lane path.
5. **`0x1A` is the pad byte.** The committed smoke map
   (`tests/outputmap/smoke/aligned-string-padding.scx`) renders padded and plain
   lines identically in StarCraft: Remastered; the pad must not render a glyph,
   move the cursor, or change line length.

## Consequences

- The compile-time content bound advances by padding, so `FixedString(7)` with
  `write("AB")`, a scope boundary, and `append("CDEF")` fails rather than
  overwriting the terminator slot; `FixedString(8)` succeeds.
- A boundary flush can emit up to two extra dword writes for a displayed
  static string, even when no dynamic write follows. This is bounded by the
  declared capacity and is visible in the cost gates.
- Aligned text is byte-identical to the plain path after stripping `0x1A`
  bytes; tests assert both the raw padded layout and the visible text.

## Amendment (2026-11-06)

ADR 0011 decision 5 defines a single content-layout walk for one format run.
Format runs do not insert mid-string `0x1A` padding (the final #73 resolution);
fixed-width placeholders fold at their compile-time byte lane instead. The walk
is capacity-checked as content alongside the placeholders and following
literals, and an overlong run fails with `EUDSL-BOUNDS`. This is not a
scope-boundary flush: the decisions above still govern `pushScope` /
`popScope` and module/display tails.
