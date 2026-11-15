# Location handles over the 5-dword table and build-time geometry

Status: accepted (2026-11-16)

## Context

`eudplib`'s `locf` manipulates 20-byte (5-dword) records at `0x58DC4C` using
action-rewriting (`_locfgen2`/`_locfgen4` with `SetDest`), and `euddraft`'s
`lib/draw.py` creates circles, stars, lines, and polygons using `math` at
build time (carrying a `dots` NameError at `draw.py:163`).

## Decision

1. **`Location` handle over 5-dword records:** Each record stores `(left, top,
   right, bottom)` in dwords `0..3` (`dword 4` untouched). `Location` accepts
   either a plain `int` id (`0..255`) or a runtime `Var`.
2. **Coordinate and ID separation:** Constant paths emit packed `SetMemory`
   triggers (1T); runtime coordinates or runtime IDs store through `memsetEPD`
   without `SetNextPtr`/`VProc` action rewriting. `getloc_tl` returns
   `(left, top)` matching `locf.py`'s implementation rather than its docstring.
3. **Raw location id boundary:** Broadcast helpers and `CUnitView.set_location`
   continue taking raw `0..255` integer location ids so `location` stays
   decoupled from `player` and `view`.
4. **Build-time vector geometry:** `draw_circle`, `draw_line`, `draw_polygon`,
   and `draw_star` precompute points in Python/TS at build time, validate all
   operands upfront, emit 5 actions per dot (`4x SetMemory` + `CreateUnit`)
   packed up to 64 actions per trigger, and document that the target location is
   clobbered to the final dot.
