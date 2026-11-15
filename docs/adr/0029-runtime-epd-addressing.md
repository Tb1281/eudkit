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

## Consequences

- Callers get a first-class EPD-bearing `Var` from a payload blob without
  duplicating the private `_seed_epd_cursor` trigger shape.
- `blockio.py`/`blockio.ts` keep their private shape in this ticket; ticket
  `T-WP5` refactors them onto the public primitive next wave.
- No HIR schema change, no new LIR item, and no new diagnostic code.
- The relocation writes the full 32-bit EPD into the destination value field,
  so an EPD-bearing variable has to remain full-width. The simulator test pins
  its variable to a `TriggerPage` to observe that value; storage placement
  itself stays the optimizer's decision.
- A runtime **base** still has no public surface; adding one would need an
  allocation and is out of scope (ADR 0030).
