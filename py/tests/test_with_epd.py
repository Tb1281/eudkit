"""T-WP5: ``with_epd`` runtime CP binding, ambient save/restore, blockio parity.

``with_epd`` is the runtime-EPD sibling of ``with_cp``: the CP register is
seeded from a ``Var`` (or a ``Db``/``DbRef`` routed through ``epd_of_db``)
instead of a build-time constant, so cursor hops are relative ``Add`` deltas.
The ambient CurrentPlayer value is saved with the shared 32-bit reader and
restored unconditionally, including on the exception path and when the body
clobbers CP. ``with_cp`` remains int-only.

The blockio snapshot pins the pre-refactor ``memcpyEPD``/``memsetEPD``/
``connectDList`` HIR byte-for-byte (SHA-256 captured before ``py/blockio.py``
was refactored onto the shared seed helpers).
"""

from __future__ import annotations

import hashlib
import json
import unittest

from py import (
    Add,
    AddrReloc,
    Db,
    EPD_CURRENT_PLAYER,
    EudslError,
    Expr,
    SetMemory,
    SetTo,
    Var,
    as_readonly,
    build_module,
    connectDList,
    emitVarStore,
    memcpyEPD,
    memsetEPD,
    with_cp,
    with_epd,
)
from py.cp import CP_ADDR, read_current_player_register
from py.tests.helpers import EPD_BASE, flatten_stmts, read_dword, run_sim

SET_DEATHS = 45
SET_TO = 7
ACT_ADD = 8

#: External observation dwords (outside the synthetic payload).
OBSERVE = 0x6D0000


def _epd(addr: int) -> int:
    return (addr - EPD_BASE) // 4


def _module(prog) -> dict:
    return json.loads(build_module(prog).to_json())["module"]


def _acts(module: dict) -> list[dict]:
    out: list[dict] = []
    for stmt in flatten_stmts(list(module["stmts"])):
        out.extend(stmt.get("acts") or [])
        if stmt.get("kind") == "act":
            out.append(stmt)
    return out


def _cp_hops(module: dict) -> list[int]:
    return [
        act["value"]
        for act in _acts(module)
        if act.get("acttype") == SET_DEATHS
        and act.get("dest") == EPD_CURRENT_PLAYER
        and act.get("modifier") == ACT_ADD
    ]


def _observe(addr: int) -> None:
    """Store the live CurrentPlayer value into an external observation dword."""
    saved = read_current_player_register()
    emitVarStore(saved.id, AddrReloc(addr), SetTo)


class WithEpdAmbientTests(unittest.TestCase):
    def test_ambient_cp_is_restored_after_the_body(self) -> None:
        def prog() -> None:
            SetMemory(CP_ADDR, SetTo, 77)
            with_epd(1000, lambda cp: cp.at(2).set(7))
            _observe(OBSERVE)

        report = run_sim(build_module(prog).to_json())
        self.assertEqual(read_dword(report, _epd(OBSERVE)), 77)

    def test_restore_runs_even_when_the_body_clobbers_cp(self) -> None:
        def prog() -> None:
            SetMemory(CP_ADDR, SetTo, 77)
            with_epd(1000, lambda cp: SetMemory(CP_ADDR, SetTo, 999))
            _observe(OBSERVE)

        report = run_sim(build_module(prog).to_json())
        self.assertEqual(read_dword(report, _epd(OBSERVE)), 77)

    def test_restore_runs_on_the_exception_path(self) -> None:
        def prog() -> None:
            SetMemory(CP_ADDR, SetTo, 77)

            def boom(cp) -> None:
                cp.at(1).set(9)
                raise RuntimeError("boom")

            try:
                with_epd(1000, boom)
            except RuntimeError:
                pass
            _observe(OBSERVE)

        report = run_sim(build_module(prog).to_json())
        self.assertEqual(read_dword(report, _epd(OBSERVE)), 77)

    def test_context_manager_form_also_restores(self) -> None:
        def prog() -> None:
            SetMemory(CP_ADDR, SetTo, 77)
            with with_epd(1000, offset=4) as cp:
                cp.at(2).set(5)
            _observe(OBSERVE)

        report = run_sim(build_module(prog).to_json())
        self.assertEqual(read_dword(report, _epd(OBSERVE)), 77)

    def test_nested_calls_restore_in_lifo_order(self) -> None:
        def prog() -> None:
            SetMemory(CP_ADDR, SetTo, 77)

            def inner(cp_inner) -> None:
                _observe(OBSERVE + 8)
                cp_inner.at(0).set(2)

            def outer(cp_outer) -> None:
                _observe(OBSERVE)
                with_epd(2000, inner)
                _observe(OBSERVE + 4)

            with_epd(1000, outer)
            _observe(OBSERVE + 12)

        report = run_sim(build_module(prog).to_json())
        self.assertEqual(read_dword(report, _epd(OBSERVE)), 1000)
        self.assertEqual(read_dword(report, _epd(OBSERVE + 4)), 1000)
        self.assertEqual(read_dword(report, _epd(OBSERVE + 8)), 2000)
        self.assertEqual(read_dword(report, _epd(OBSERVE + 12)), 77)


class WithEpdEmissionTests(unittest.TestCase):
    def test_zero_offset_emits_no_pre_hop(self) -> None:
        module = _module(lambda: with_epd(1000, lambda cp: cp.at(0).set(1)))
        self.assertEqual(_cp_hops(module), [])

    def test_offset_pre_hops_the_private_scratch_copy(self) -> None:
        module = _module(lambda: with_epd(1000, lambda cp: None, offset=3))
        adds = [
            act["value"]
            for act in _acts(module)
            if act.get("acttype") == SET_DEATHS and act.get("modifier") == ACT_ADD
        ]
        self.assertEqual(adds, [3], "offset=3 must be one scratch Add")

    def test_at_uses_a_relative_hop_not_an_absolute_base(self) -> None:
        module = _module(lambda: with_epd(1000, lambda cp: cp.at(2).set(7)))
        self.assertEqual(_cp_hops(module), [2])
        self.assertFalse(
            any(
                act.get("acttype") == SET_DEATHS
                and act.get("dest") == EPD_CURRENT_PLAYER
                and act.get("modifier") == SET_TO
                for act in _acts(module)
            ),
            "a runtime base must never emit SetTo base+target",
        )

    def test_repeating_the_offset_never_hops_twice(self) -> None:
        module = _module(
            lambda: with_epd(
                1000,
                lambda cp: (
                    cp.at(2).set(7),
                    cp.at(2).set(8),
                    cp.moveTo(3),
                    cp.at(3).set(9),
                ),
            )
        )
        self.assertEqual(_cp_hops(module), [2, 1])

    def test_offset_zero_emits_no_hop_even_with_a_body_hop(self) -> None:
        module = _module(lambda: with_epd(1000, lambda cp: cp.at(2).set(7)))
        self.assertEqual(
            [value for value in _cp_hops(module) if value == 3],
            [],
            "the default offset must not add a pre-hop act",
        )

    def test_dbref_base_routes_through_epd_of_db(self) -> None:
        module = _module(
            lambda: with_epd(Db(b"\x01\x02\x03\x04\x05\x06\x07\x08"), lambda cp: None)
        )
        triggers = [stmt for stmt in module["stmts"] if stmt["kind"] == "trigger"]
        relocs = triggers[0]["acts"][0]["relocs"]
        blob_reloc = next(reloc for reloc in relocs if "target_blob" in reloc)
        self.assertEqual(blob_reloc["mode"], 1)
        base_id = next(
            reloc["target_var"] for reloc in relocs if reloc["field_offset"] == 16
        )
        transfers = [
            stmt
            for stmt in module["stmts"]
            if stmt["kind"] == "vartransfer" and stmt["src_var"] == base_id
        ]
        self.assertEqual(len(transfers), 1, "the blob EPD must be copied once")
        self.assertNotEqual(transfers[0]["dst_var"], base_id)

    def test_dbref_offset_routes_through_epd_of_db(self) -> None:
        module = _module(
            lambda: with_epd(Db(b"\x01\x02\x03\x04\x05\x06\x07\x08") + 4, lambda cp: None)
        )
        triggers = [stmt for stmt in module["stmts"] if stmt["kind"] == "trigger"]
        relocs = triggers[0]["acts"][0]["relocs"]
        blob_reloc = next(reloc for reloc in relocs if "target_blob" in reloc)
        self.assertEqual(blob_reloc["mode"], 1)
        self.assertEqual(blob_reloc["target_offset"], 4)

    def test_readonly_var_materializes_without_an_extra_temp(self) -> None:
        holder: dict[str, Var] = {}

        def prog() -> None:
            value = Var(1000)
            holder["value"] = value
            with_epd(as_readonly(value), lambda cp: None)

        module = _module(prog)
        transfers = [
            stmt
            for stmt in module["stmts"]
            if stmt["kind"] == "vartransfer" and stmt["src_var"] == holder["value"].id
        ]
        self.assertEqual(len(transfers), 1)

    def test_expr_base_materializes_exactly_once(self) -> None:
        holder: dict[str, Var] = {}

        def prog() -> None:
            value = Var(1000)
            holder["value"] = value
            with_epd(Expr("add", value, 5), lambda cp: None)

        module = _module(prog)
        transfers = [
            stmt
            for stmt in module["stmts"]
            if stmt["kind"] == "vartransfer" and stmt["src_var"] == holder["value"].id
        ]
        self.assertEqual(
            len(transfers), 1, "the Expr operand must be evaluated exactly once"
        )


class WithEpdDiagnosticsTests(unittest.TestCase):
    def test_non_epd_operands_are_eudsl_type(self) -> None:
        for bad in ("x", 1.5, None, True, object()):
            with self.subTest(bad=bad):
                with self.assertRaisesRegex(EudslError, "EUDSL-TYPE"):
                    build_module(lambda bad=bad: with_epd(bad, lambda cp: None))

    def test_offset_outside_the_signed_32_bit_range_is_eudsl_range(self) -> None:
        for bad in (2**31, -(2**31) - 1, True, 1.5, "3", None):
            with self.subTest(bad=bad):
                with self.assertRaisesRegex(EudslError, "EUDSL-RANGE"):
                    build_module(
                        lambda bad=bad: with_epd(1000, lambda cp: None, offset=bad)
                    )

    def test_invalid_offset_queues_no_act(self) -> None:
        def prog() -> None:
            try:
                with_epd(1000, lambda cp: None, offset=2**31)
            except EudslError:
                pass

        self.assertEqual(_module(prog)["stmts"], [])

    def test_invalid_epd_queues_no_act(self) -> None:
        def prog() -> None:
            try:
                with_epd("not an epd", lambda cp: None)
            except EudslError:
                pass

        self.assertEqual(_module(prog)["stmts"], [])

    def test_with_cp_still_rejects_a_dynamic_base_before_any_act(self) -> None:
        def prog() -> None:
            try:
                with_cp(Var(0), lambda cp: None)
            except EudslError:
                pass

        module = _module(prog)
        self.assertEqual(module["stmts"], [])
        self.assertTrue(module.get("vars"), "the argument Var itself still allocates")

    def test_camel_case_alias_is_the_same_helper(self) -> None:
        from py import withEpd

        self.assertIs(withEpd, with_epd)

    def test_body_must_be_callable_through_both_spellings(self) -> None:
        from py import withEpd

        for label, fn in (("with_epd", with_epd), ("withEpd", withEpd)):
            with self.subTest(label=label):
                with self.assertRaises(EudslError) as ctx:
                    build_module(lambda fn=fn: fn(1000, 5))
                self.assertEqual(ctx.exception.code, "EUDSL-TYPE")
                self.assertIn("callable", str(ctx.exception))


#: SHA-256 of the pre-refactor HIR for `_blockio_program` (captured before
#: `py/blockio.py` was refactored onto `_emit_epd_reloc_into` /
#: `_set_cp_from_var`). The TypeScript mirror pins the same digest.
BLOCKIO_HIR_SHA256 = "04160598ab5c3038e6743eafe1ee44f4405f9b1caf89af0a96b53edec209aa60"


def _blockio_program() -> None:
    d = Db(b"\x01\x02\x03\x04\x05\x06\x07\x08")
    memsetEPD(1000, 0xAABBCCDD, 3)
    memcpyEPD(2000, 1000, 3)
    n = Var(2)
    memsetEPD(3000, 0x11223344, n)
    fill = Var(9)
    memsetEPD(3100, fill, 2)
    memcpyEPD(4000, d, 2)
    memcpyEPD(5000, d, 1)
    memcpyEPD(6000, 1000, 1)
    src = Var(1000)
    memcpyEPD(7000, src, 3)
    memcpyEPD(8000, 1000, n)
    dst = Var(9000)
    memcpyEPD(dst, src, n)
    connectDList(0x64EED8, 0x64EEDC, 112, 3, 0)


class BlockioRefactorIdentityTests(unittest.TestCase):
    def test_blockio_hir_is_byte_identical_after_refactor(self) -> None:
        hir = build_module(_blockio_program).to_json()
        self.assertEqual(
            hashlib.sha256(hir.encode()).hexdigest(),
            BLOCKIO_HIR_SHA256,
            "the blockio seed refactor must not change a single HIR byte",
        )


if __name__ == "__main__":
    unittest.main()
