# Player-name records and the layout-A nickname matcher

Status: accepted (2026-11-16)

## Context

A player's StarCraft nickname is matched by comparing the name field against
candidate strings. eudkit could already read and write arbitrary EPDs, but it
had no name-table helpers and no way to express "the first candidate that
matches this player's name" without hand-writing a chain. A matcher that
compares only the leading bytes also has a prefix bug: a record containing
`"Armoha"` matched the candidate `"Armo"`.

This ADR records the table layout, the byte contract, the mask that makes the
match exact, the build-time unroll (layout A), and why the runtime byte reader
(layout C) is deferred.

## Decision

### 1. The name table and its below-base EPD

Player names live in the 36-byte records at `0x57EEE8`:

```text
PLAYER_NAME_TABLE     = 0x57EEE8   first byte of player 0's record
PLAYER_NAME_EPDS      = 9          dwords per record
PLAYER_NAME_BYTES     = 36         == 4 * 9
PLAYER_NAME_MAX_BYTES = 35         longest name that still fits its NUL
```

There are twelve slots, `0..11`; the helpers validate that range. The table is
below `EPD_BASE = 0x58A364`, so `EPD(0x57EEE8)` is negative:
`(0x57EEE8 - 0x58A364) / 4 = -11551`, wrapped to the unsigned `4294955745`
that the engine reads. `EPD()` already wraps that value, and the engine's
`EPD_BASE + epd * 4` arithmetic round-trips it to `0x57EEE8`, so no special
below-base addressing mode is needed. `player_name_epd(p)` is
`(EPD(0x57EEE8) + 9 * p) & 0xFFFFFFFF`; `player_name_addr(p)` is the byte
form `0x57EEE8 + 36 * p`. Both are pure integer helpers: they need no builder
and raise only `EUDSL-TYPE` (non-plain-int) or `EUDSL-RANGE` (outside `0..11`).

### 2. The UTF-8 byte contract

A nickname is matched as the exact byte sequence that `encode_utf8(name,
"nickname")` produces — the same encoding authority `FixedString` uses (ADR
0003). There is no second encoder in the matcher, and no locale or
code-page conversion. A consequence is that non-ASCII names compare byte for
byte: `"마린"` is `EB A7 88 EB A6 B0`, so its first dword is `0xEB88A7EB` and
the word holding the NUL is `0xB0A6` under a `0xFFFFFF` mask.

`encode_utf8` also owns the rejection rules: an embedded `\x00` or a lone
surrogate is `EUDSL-TYPE`. A name may encode to at most 35 bytes; 36 bytes is
`EUDSL-BOUNDS` because the record must still hold the trailing NUL.

### 3. The NUL-inclusive final mask

For name bytes `b`, let `w = len(b) // 4` and `o = len(b) % 4`. The first `w`
dwords are compared whole against `word(b, i)` with mask `0xFFFFFFFF`. The
final dword is compared with

```text
mask   = (1 << (8 * (o + 1))) - 1
amount = word(b, w) & mask
```

The `+ 1` includes the NUL byte at lane `o`. The engine computes `raw & mask`
for condition 15 and compares it against `amount` verbatim, so `amount` must
be passed pre-masked. This is the correctness property, not an optimization:

- `"Armo"` (4 bytes) produces a whole-word condition plus a final NUL-only
  arm `(amount = 0, mask = 0xFF)`. A record holding `"Armoha"` has `'h'` in
  that lane, so it fails the second condition and cannot match `"Armo"`.
- `"Armoha"` (6 bytes) compares `0x6F6D7241` whole, then `0x6168` under
  `0xFFFFFF`: bytes `"ha"` plus the NUL. A record holding only `"Armoh"` fails.
- A name whose length is a multiple of four still gets the final arm
  (`o == 0`, `mask == 0xFF`, `amount == 0`), so the NUL is always checked.
- A 35-byte name reaches all nine words; `o == 3`, the last mask is
  `0xFFFFFFFF`, and the NUL sits in byte 35, inside the record.

Because the final mask includes the NUL, matching is exact and
case-sensitive: no prefix, suffix, or substring match exists by construction.
Bytes after the NUL are never inspected, so a record with non-zero padding
after the terminator still matches.

### 4. Layout A: the build-time unroll and its cost

`match_names(names, on_match, players=range(8))` emits one ordered `when`
chain per player (layout A). Each `(player, name)` arm is one trigger over the
record's dwords; the else arm recurses to the next candidate, and the first
matching arm runs `on_match(player, name)` and stops. The shape mirrors
`dispatch`'s small-N else-chain (ADR 0025), including its "first match wins"
rule and its rejection of ambiguous duplicate keys.

`len(conds) == len(bytes) // 4 + 1 <= 9 <= 16`, so every arm is exactly one
trigger and never needs ADR 0027 chunking. The static cost is
`len(players) * len(names)` triggers (8 players × 3 names = 24T). Execution
per pass is at most `len(names)` arms per player, each evaluating at most nine
conditions, plus one body run per player that matches. The chain is linear;
the number of names is caller-controlled and is not gated or warned about.

Validation is eager and complete before the first emission, so a rejected
call leaves no partial chain: scope first (`EUDSL-SCOPE` outside
`buildModule`), then names (type, non-empty, unique, encodable, at most 35
bytes), then players (ordered iterable, plain ints in `0..11`, non-empty,
unique; a `set` is `EUDSL-TYPE` because its order is not deterministic), then
`on_match` (callable).

### 5. Layout C is deferred, not omitted

Layout C is a runtime byte reader: one loop walking a
NUL-separated name database, comparing bytes, with `O(bytes)` executed cost
and `O(1)` static cost per name. Expressing it needs `SetNextPtr`-style
trigger rewiring or a runtime pointer patch so the walk can resume at the next
name. Both are rejected items in eudkit's trigger model, and the write would
also couple the cost model to the length of the name database. Layout C is
therefore out of scope; layout A is the supported shape. If a future trigger
model permits pointer continuation, it can be revisited under a new ADR.

The flag-Var alternative to the else-chain (`matched = Var(0)`, then
`when(matched.eq(0) and conds, ...)`) was also rejected for layout A: it costs
one extra read and write per arm without improving the trigger count, and the
else-chain already matches the established `dispatch` precedent.

### 6. Per-client caveat

A player name is client-local data. In a multiplayer game the matcher's
conditions can therefore evaluate differently on different clients, and a
body that writes simulated game memory will desync. This is a property of the
technique, not of the encoding: the matcher only reads the local name field.
eudkit does not detect or enforce this — there is no diagnostic and no
compile-time guard — so authors must keep such bodies local (UI, local
messages, local state) or accept the desync.

## Consequences

- `player_name_epd`/`playerNameEpd` and `player_name_addr`/`playerNameAddr`
  expose the table with the same guards in both frontends.
- The matcher's bytes are the `FixedString` bytes: `encode_utf8`/`encodeUtf8`
  remains the single encoding authority.
- No prefix collision: `"Armo"` cannot match `"Armoha"`, and trailing garbage
  after the NUL is ignored.
- Every arm is one trigger with at most nine conditions; no chunking, no new
  HIR node, and no new diagnostic code.
- Py/TS parity is preserved by construction; the TypeScript module is pinned
  against the Python HIR fixture.
- The simulator exercises the feature without a map by seeding the below-base
  EPD in `external_epds`.
- Layout C remains unavailable until pointer-continuation triggers exist;
  that is a recorded decision, not an oversight.

## Rejected alternatives

- **Matching a prefix or substring "for convenience."** The NUL-inclusive
  mask is the correctness property; the Armo/Armoha prefix bug is the
  counter-example.
- **Re-encoding nicknames locally.** `encode_utf8` remains the encoding
  authority (ADR 0003); a local encoder would drift from `FixedString`.
- **Comparing the full 36-byte record.** Bytes after the NUL are unspecified
  padding, so a match would fail on trailing garbage.
- **Shipping layout C behind a flag.** It still needs rejected
  `SetNextPtr` rewiring and would make static cost depend on database length.
- **The flag-Var shape.** One extra read and write per arm, no trigger-count
  benefit, and it diverges from the `dispatch` else-chain precedent.
