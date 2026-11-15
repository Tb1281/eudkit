# Runtime EPD addressing: an EPD is a value, not an address

Status: accepted (2026-11-15)

## Context

An **EPD** is the dword index of a 4-byte-aligned StarCraft memory address,
`EPD(addr) = (addr - 0x58A364) / 4`. It is the unit every Deaths-table
condition and action speaks: a `Var`'s slot, a `CUnit` field, and the
CurrentPlayer pointer are all addressed by an EPD.

Most EPDs are compile-time constants: a `MemView` folds an address to an EPD
plus a byte lane, and a `DbRef` names a byte offset inside a module blob.
Module blob payload addresses are different. `lir2payload` lays every payload
item out at assembly time (`align4(previous end)`), so a blob's payload
address — and therefore its EPD — does not exist until the map is assembled.
`py/blockio.py:_seed_epd_cursor` already materializes that value privately for
a `memcpyEPD` blob source: one zero-initialized `Var`, one preserved
zero-condition trigger, and one mode-1 relocation into the `Var`'s value
field. Nothing else in the tree exposed the shape.

Two near-misses have to stay distinct:

- an EPD **value** (a dword index that may be stored, compared, or used as a
  base), and
- a runtime-**indexed** container (a base plus a runtime offset that selects
  arbitrary memory).

The first is a scalar and is shippable. The second needs a backing allocation
and is deliberately rejected (ADR 0030 records the deferred shadow design).

## Decision

### 1. Vocabulary

- A **runtime EPD** is an EPD whose numeric value is not known at build time.
  It is materialized into a `Var` by an assembly-time relocation.
- An **EPD-bearing value** is a `Var` whose 32-bit content is an EPD rather
  than a byte address, a count, or a boolean. It may seed CurrentPlayer, index
  a payload dword, or be compared like any other EPD.
- A **payload blob EPD** is the runtime EPD of a `Db`/`DbRef` payload dword,
  `EPD(payload_addr + byteOffset)`.

An EPD is a dword index, never a byte address. Do not subtract `EPD_BASE / 4`
twice or carry a byte address in an EPD-bearing value.

### 2. One public constructor

`epd_of_db(ref)` in Python and `epdOfDb(ref)` in TypeScript (alias
`epd_of_db` for spelling parity) are the only public constructors for a
payload blob EPD:

```python
epd_of_db(db | db_ref) -> Var
```

`ref` must be a `Db` or `DbRef`; a `Db` means offset `0`. `DbRef` already
guarantees a non-negative, 4-byte-aligned byte offset, so the target dword is
always addressable by the mode-1 conversion. Anything else is `EUDSL-TYPE`
with `epd_of_db(): expected Db or DbRef, got ...`.

Calling it outside `buildModule` is `EUDSL-SCOPE`, inherited from `Var` and
`Trigger` construction. A malformed `DbRef` is `EUDSL-BOUNDS`, inherited from
`DbRef.__init__`. No new diagnostic code is introduced; the 16-code contract
is unchanged.

### 3. Emission shape

The helper allocates exactly one fresh `Var(0)` and emits exactly one preserved
trigger with **zero conditions** and **one action**:

```python
out = Var(0)
act = out.act_set(0)
act.setdefault("relocs", []).append({
    "field_offset": ACT_FIELD_OFFSETS["value"],  # 20
    "target_blob": ref.blobId,
    "target_offset": ref.byteOffset,             # already % 4 == 0
    "mode": 1,
})
Trigger(conds=[], acts=[act], preserved=True)
return out
```

`act_set(0)` contributes the destination relocation (the new `Var`'s value
field, mode 1); the helper appends the value-field relocation to the blob. No
`out.set(epd_of_db(...))`-style extra action is emitted. TypeScript mirrors the
same shape with `VarImpl`, `ACT_FIELD_OFFSETS.value`, and
`new Trigger({ conds: [], acts: [act], preserved: true })`.

### 4. Assembly semantics

Lowering converts the mode-1 `HirReloc` into a payload relocation targeting
the emitted blob item plus `target_offset`. At payload assembly
`lir2payload` writes:

```
field = epd_u32(blob_payload_addr + target_offset)
```

That is exactly `(payload_addr - EPD_BASE) / 4` for offset `0`, and one dword
further per `DbRef` offset. Blob payload addresses are stable across map
assembly because `lir2payload` aligns every item before it is emitted; the
relocation therefore never goes stale (ticket `T-WP4` re-plan clause (a) does
not fire).

The Rust regression test `crates/eudkit-lower/tests/epd_of_db.rs` builds the
same HIR directly and simulates one loop: the variable must equal the emitted
blob payload address converted to an EPD, and the trigger must carry `0`
conditions and `1` action. That pins the mode-1 path to the blob payload, not
to the blob's first four bytes and not to the byte address (mode 4).

### 5. A base value, not a runtime-indexed container

`epd_of_db` yields a base **value**. It does not allocate memory beyond the
`Var`, does not add an `EPD + runtime offset` addressing mode, and does not
turn a blob into an array. Runtime bases remain rejected: `with_epd` (ticket
`T-WP5`) is the sanctioned CP-relative body form, and the runtime-indexed
shadow backing is recorded as deferred in ADR 0030.

**CP-relative reader bodies.** A shared `func` body may read a runtime
address only by binding CP before the call: a condition's `player` field must
be a literal EPD, so the address cannot be passed as a runtime value. The body
therefore reads `CURRENT_PLAYER` (`13`) and every call pays one CP store and
one restore. This is the shape of the 20T CUnit pointer reader (`T-WP6`): 18
bit tests against the dword CP currently holds, seeded `0x100002` with bias
`0xFFE9D727`, and no per-address body cache because the target is always
`CURRENT_PLAYER`.

`_with_epd_saved(saved_cp, epd, body)` is the loop contract. The caller hoists
the 33T ambient read once, then each node pays only the store, the call, the
restore and its own advance. Never hoist a store: CP is clobbered by the body
and by other readers, so each call must bind its own base. Always hoist the
read: the ambient value is loop-invariant, while the binding is not.

### 6. Parity

For the same program both frontends emit byte-identical HIR:

```python
d = Db(b"\x01\x02\x03\x04\x05\x06\x07\x08")
v = epd_of_db(d + 4)
```

- one `Var` with init `0`;
- one preserved trigger with `conds: []` and exactly one action whose
  `relocs` are the destination `target_var` reloc (mode 1) followed by
  `{field_offset: 20, target_blob: 0, target_offset: 4, mode: 1}`.

The `Var` id differs only by the surrounding module's allocation order.

### 7. Runtime CP binding: `with_epd`

`with_epd(epd, body=None, offset=0)` in Python and `withEpd(epd, fn, offset =
0)` in TypeScript bind CurrentPlayer to a **runtime** EPD for the duration of a
body. The operand is an `int`, `Var`, `ReadonlyVar`, `Expr`, or `Db`/`DbRef`
(the last routed through `epd_of_db`); `bool` and every other value is
`EUDSL-TYPE` before any act is queued. `offset` must be a plain integer in the
signed 32-bit range (`EUDSL-RANGE`). The runtime base is copied into a private
scratch `Var`, so a caller's `Var` is never mutated, and a non-zero `offset`
pre-hops that copy with one relative `Add`.

Hops are **relative** (`Add k - currentOffset`), not absolute. The base is not
known at build time, so `SetTo base + target` is impossible; repeating the
current offset emits nothing. `EpdCursor.restore()` resets only the build-time
offset and emits nothing.

The ambient CurrentPlayer dword is read with the shared
`read_current_player_register` before binding and restored **unconditionally**
in a `finally`, so it survives a body that clobbers CP and survives an
exception. Nesting is LIFO because each call owns its own saved `Var` and
scratch base: an inner call restores the outer binding, and the outer call
restores the ambient value.

The four restore policies, stated once:

| surface | CP after the call |
|---|---|
| `with_cp` / `withCP` | cursor-only: re-anchored to the constant base when the cursor moved; a read at offset 0 can leave CP at 0 |
| `with_epd` / `withEpd` | the ambient pre-call value, always |
| `PageSlotEPD` read | 0 |
| `memcpyEPD` / `memsetEPD` | the seed/advance end value (still clobbers) |

Cost: one full-dword ambient read (the shared reader is a 33T body or a call),
one bind store, one restore, plus the optional one-act pre-hop — at most 36T
per non-shared site, the budget in ticket `T-WP5`. A private
`_with_epd_saved(saved, ...)` form for callers that already hold the ambient CP
is the loop contract (`T-WP6`) and stays private rather than widening the
public signature.

What remains rejected: a runtime **base** for `with_cp` (the int-only guard
is a correctness requirement), a general `store(computed_addr, value)`, and a
public runtime-indexed container (ADR 0030).

## Consequences

- Callers get a first-class EPD-bearing `Var` from a payload blob without
  duplicating the private `_seed_epd_cursor` trigger shape.
- `blockio.py`/`blockio.ts` share the CP seed and mode-1 blob-reloc shapes
  through `_set_cp_from_var` / `_emit_blob_epd_into` (TS: `emitBlobEpdInto`,
  ticket `T-WP5`) without
  changing a single HIR byte: `memcpyEPD`/`memsetEPD`/`connectDList` still
  leave CP clobbered, so wrapping them in `with_epd` would be a behavior
  change, not a refactor.
- `with_epd` adds the missing runtime CP binding without widening the
  addressing model: it is a scalar base plus constant relative hops, not a
  general computed-address store or a runtime-indexed container.
- No HIR schema change, no new LIR item, and no new diagnostic code.
- The relocation writes the full 32-bit EPD into the destination value field,
  so an EPD-bearing variable has to remain full-width. The simulator test pins
  its variable to a `TriggerPage` to observe that value; storage placement
  itself stays the optimizer's decision.
- A runtime **base** still has no public surface; adding one would need an
  allocation and is out of scope (ADR 0030).
