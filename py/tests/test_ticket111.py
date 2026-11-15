"""Issue #111: reject unaligned absolute dword addresses at the DSL/lower seam.

Mirror of ``ts/tests/test_ticket111.ts``.

`Memory(addr)` family conditions/actions fold the byte address into a signed
EPD with `py/view.py` `EPD()` (floor), while an `emitVarStore(..., {"addr":
...})` keeps the raw byte address in HIR and lowers it through
`eudkit_lir::epd_u32` (truncating).  For an unaligned address the two disagree
silently — `0x57F0B2` gated dword `0x57F0B0` but wrote `0x57F0B4` — so the
frontend rejects `addr & 3 != 0` at the call instead.

The guard deliberately does **not** live in `EPD()`: `DatFieldHandle` folds an
unaligned DAT *field* address into the containing dword plus a byte lane, which
is a byte-lane access, not a dword read/write.  The controls below pin that
distinction.
"""

from __future__ import annotations

import unittest

from py import (
    AtLeast,
    Deaths,
    EPD,
    Memory,
    SetDeaths,
    SetMemory,
    SetTo,
    Trigger,
    UnitType,
    Var,
    build_module,
    units_dat,
    when,
)
from py.hir import emitVarStore
from py.tests.helpers import run_static
from py.view import EudslError

STATIC_KEYS = ("triggers", "conditions", "actions", "total_bytes")

FLAG = 0x57F0B4
UNALIGNED = 0x57F0B2

# Byte-identical HIR pinned on `0a5f851` (pre-#111); both frontends emit these.
MEM_ALIGNED_HIR = (
    '{"version":"3.2","module":{"stmts":[{"kind":"trigger","id":0,"conds":[],'
    '"acts":[{"mask":0,"strid":0,"wavid":0,"time":0,"dest":4294955860,'
    '"value":1,"unit":0,"acttype":45,"modifier":7,"flags":20,"padding":0,'
    '"eudx":0}],"preserved":true,"flags":4},{"kind":"when","conds":'
    '[{"locid":0,"player":4294955860,"amount":1,"unit":0,"comparison":0,'
    '"condtype":15,"restype":0,"flags":0,"eudx":0}],"body":[]}]}}'
)

STORE_ALIGNED_HIR = (
    '{"version":"3.2","module":{"vars":[{"id":0,"init":3}],"stmts":'
    '[{"kind":"varstore","src_var":0,"modifier":7,"dst_addr":5763252}]}}'
)

BASE_HIR = (
    '{"version":"3.2","module":{"stmts":[{"kind":"trigger","id":0,"conds":[],'
    '"acts":[{"mask":0,"strid":0,"wavid":0,"time":0,"dest":0,"value":1,'
    '"unit":0,"acttype":45,"modifier":7,"flags":20,"padding":0,"eudx":0}],'
    '"preserved":true,"flags":4},{"kind":"when","conds":[{"locid":0,'
    '"player":0,"amount":1,"unit":0,"comparison":0,"condtype":15,'
    '"restype":0,"flags":0,"eudx":0}],"body":[]}]}}'
)

DEATHS_HIR = (
    '{"version":"3.2","module":{"stmts":[{"kind":"trigger","id":0,"conds":[],'
    '"acts":[{"mask":0,"strid":0,"wavid":0,"time":0,"dest":4294955860,'
    '"value":0,"unit":0,"acttype":45,"modifier":7,"flags":20,"padding":0,'
    '"eudx":0}],"preserved":true,"flags":4},{"kind":"when","conds":'
    '[{"locid":0,"player":4294955860,"amount":1,"unit":0,"comparison":0,'
    '"condtype":15,"restype":0,"flags":0,"eudx":0}],"body":[]}]}}'
)

# `units_dat.at(1).armor` sits at 6_684_361 — unaligned by one byte — and the
# marine's `maxHp` at 6_693_712 is aligned.  Both still build; the armor write
# floors to dword EPD 218_841 with the byte-lane mask 0xFF00.
DAT_HIR = (
    '{"version":"3.2","module":{"stmts":[{"kind":"trigger","id":0,"conds":[],'
    '"acts":[{"mask":65280,"strid":0,"wavid":0,"time":0,"dest":218841,'
    '"value":256,"unit":0,"acttype":45,"modifier":7,"flags":20,"padding":0,'
    '"eudx":17235},{"mask":4294967295,"strid":0,"wavid":0,"time":0,'
    '"dest":221179,"value":40,"unit":0,"acttype":45,"modifier":7,"flags":20,'
    '"padding":0,"eudx":17235}],"preserved":true,"flags":4}]}}'
)


def _static(hir_json: str) -> tuple[int, ...]:
    cost = run_static(hir_json)["static_cost"]
    return tuple(cost[key] for key in STATIC_KEYS)


class UnalignedDwordAddressTests(unittest.TestCase):
    def test_memory_and_set_memory_reject_at_the_call(self) -> None:
        caught: list[EudslError] = []

        def build() -> None:
            with self.assertRaises(EudslError) as ctx:
                when([Memory(UNALIGNED, AtLeast, 1)], lambda: None)
            caught.append(ctx.exception)
            with self.assertRaises(EudslError) as ctx:
                Trigger(conds=[], acts=[SetMemory(UNALIGNED, SetTo, 1)])
            caught.append(ctx.exception)

        build_module(build)

        self.assertEqual(len(caught), 2)
        for exc in caught:
            self.assertEqual(exc.code, "EUDSL-BOUNDS")
            self.assertEqual(
                str(exc),
                "EUDSL-BOUNDS: memory address 0x57f0b2 is not 4-byte aligned",
            )

    def test_emit_var_store_rejects_at_the_call(self) -> None:
        caught: list[EudslError] = []

        def build() -> None:
            v = Var(3)
            with self.assertRaises(EudslError) as ctx:
                emitVarStore(v.id, {"addr": UNALIGNED})
            caught.append(ctx.exception)

        build_module(build)

        self.assertEqual(len(caught), 1)
        self.assertEqual(caught[0].code, "EUDSL-BOUNDS")
        self.assertEqual(
            str(caught[0]),
            "EUDSL-BOUNDS: VarStore address 0x57f0b2 is not 4-byte aligned",
        )

    def test_aligned_memory_hir_and_static_cost_unchanged(self) -> None:
        def build() -> None:
            Trigger(conds=[], acts=[SetMemory(FLAG, SetTo, 1)])
            when([Memory(FLAG, AtLeast, 1)], lambda: None)

        hir_json = build_module(build).to_json()
        self.assertEqual(hir_json, MEM_ALIGNED_HIR)
        # The empty `when` is a dead gate; the post-schedule dead-gate pass
        # drops it (Cycle 28 (Cycle 3) #03), leaving only the SetMemory trigger.
        self.assertEqual(_static(hir_json), (1, 0, 1, 2408))

    def test_aligned_var_store_hir_and_static_cost_unchanged(self) -> None:
        def build() -> None:
            v = Var(3)
            emitVarStore(v.id, {"addr": FLAG})

        hir_json = build_module(build).to_json()
        self.assertEqual(hir_json, STORE_ALIGNED_HIR)
        self.assertEqual(_static(hir_json), (3, 0, 67, 7224))

    def test_epd_base_address_unchanged(self) -> None:
        def build() -> None:
            Trigger(conds=[], acts=[SetMemory(0x58A364, SetTo, 1)])
            when([Memory(0x58A364, AtLeast, 1)], lambda: None)

        hir_json = build_module(build).to_json()
        self.assertEqual(hir_json, BASE_HIR)
        self.assertIn('"player":0', hir_json)

    def test_epd_wire_form_is_not_guarded(self) -> None:
        def build() -> None:
            Trigger(conds=[], acts=[SetDeaths(EPD(FLAG), SetTo, 0)])
            when([Deaths(EPD(FLAG), AtLeast, 1)], lambda: None)

        hir_json = build_module(build).to_json()
        self.assertEqual(hir_json, DEATHS_HIR)

    def test_unaligned_dat_field_still_reads_and_writes(self) -> None:
        # The regression pin for not guarding `EPD()`: a DAT field address is
        # a byte lane on the containing dword, not a whole-dword access.
        armor = units_dat.at(1).armor
        self.assertNotEqual(armor.address % 4, 0)

        def build() -> None:
            units_dat.at(1).armor.set(1)
            units_dat[UnitType.TerranMarine].maxHp.set(40)

        hir_json = build_module(build).to_json()
        self.assertEqual(hir_json, DAT_HIR)
        self.assertEqual(_static(hir_json), (1, 0, 2, 2408))


if __name__ == "__main__":
    unittest.main()
