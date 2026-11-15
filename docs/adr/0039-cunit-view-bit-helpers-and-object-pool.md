# `CUnitView`, raw bit/split helpers, flag vocabularies, and `ObjectPool`

Status: accepted (2026-11-16)

## Context

`eudkit` already provides `MemView`/`CUnit` field readers (`20T` CUnit pointer
goldens) and `FixedArray`/`Stack`, but lacked a typed CUnit facade (`owner`,
`order`, `userActionFlags`, `statusFlags.Burrowed/Invincible`, `pos`, `sprite`,
`CSprite.player`), raw bit/dword-split helpers, unit/DAT flag namespaces, and a
deterministic index-recycling `ObjectPool`.

## Decision

1. **`CUnitView` facade (not subclass):** `CUnitView` wraps a `CUnit`
   `MemViewInstance` (`self.mem`) rather than subclassing `MemViewInstance`
   (whose `__setattr__` intercepts attribute assignments).
2. **Rule V1 (condition-EPD boundary):** Bit/mask *conditions* (`read_bit`) only
   accept compile-time-constant aligned addresses. Runtime pointer queries such
   as `is_burrowed()` return a `Var` via `with_epd`, and constant-value runtime
   masked writes patch one pointer trigger destination via `emitVarStore`.
3. **Owner-only `give_to` and `set_location` contract:** `give_to` writes only
   the CUnit `owner` byte (no per-player list relinking). `set_location` takes
   a raw `0..255` integer location id and stores packed `(top << 16) | left` to
   CUnit `pos`. `set_location` mirrors the one-dword pointer store locally in TS
   to avoid cyclic `view -> blockio -> cp -> trigger -> view` imports.
4. **Distinct flag vocabularies:** `StatusFlags` and `PathingFlags` are CUnit
   bitmasks, whereas `GroupFlags` and `AvailabilityFlags` target unit-type DAT
   tables (`0x6637A0` / `0x661518`), documented explicitly.
5. **Deterministic LIFO `ObjectPool`:** Uses a `Stack(capacity)` pre-populated
   with `capacity - 1 .. 0` over a `FixedArray`, returning `POOL_NULL = 0xFFFFFFFF`
   when exhausted (`capacity <= 0xFFFFFFFE`).
