"""Ticket #11: per-primitive memory costs, pinned as a golden table.

The comprehensive cost gate is one module, so its totals cannot attribute a
drift to a single primitive. Each case here builds one fresh module and pins
that primitive's own static and executed cost. `ne` expands to the deferred
flag form (reset + `when(lt)` + `when(gt)` + `flag == 1`) at the five one-shot
gate sites (#16), so its row is 4T static / 4T executed; a future change there
must show up as an explicit golden update rather than a silent drift.

Provenance for the read pin: the scan's 34T figure was the
`SetMemory(addr, SetTo, read())` sink form. Reading into a variable is the
sound form; it used to cost 36T (one 33T inline read plus 3T for the
var-to-var transfer). Since #97 a full-dword `read()` is a per-address shared
subroutine (one 32-bit-test body per distinct EPD, no per-call field patch),
and a body with a single call site is spliced back in by the optimizer. This
probe has one site, so it lowers to the 33T body itself and the fused result
copy: 33T static / 33T executed, below the old inline 36T. Multiple sites on
the same address share the body, paying the call sequence instead of another
33T copy (see `test_shared_reader_cost_is_per_site` in `test_func.py`).
"""

from __future__ import annotations

import unittest

from py import (
    CUnit,
    Db,
    FieldSpec,
    MemCond,
    SetDeaths,
    SetTo,
    Var,
    build_module,
    defineMemView,
    memcpyEPD,
    memsetEPD,
    when,
    while_loop,
)

from py.tests.helpers import read_dword, run_sim
from py.view import EPD_CURRENT_PLAYER, make_memview

# Ticket #20: a dword field with a narrow mask; the read must emit one bit test
# per mask bit (8), not per width bit (32).
_MASKED_DWORD_SCHEMA = defineMemView(
    "MaskedDw", 4, {"v": FieldSpec(0, 4, mask=0xFF00)}
)

# (name, program builder, static triggers, trigger bytes, total bytes,
# executed triggers)
CASES = [
    (
        "memview eq",
        lambda: when(CUnit(1000).hp.eq(500), lambda: SetDeaths(9000, SetTo, 1, 0)),
        1,
        2408,
        2408,
        1,
    ),
    (
        "memview ne",
        lambda: when(CUnit(1000).hp.ne(500), lambda: SetDeaths(9000, SetTo, 1, 0)),
        4,
        9_632,
        9_636,
        4,
    ),
    (
        "memview read into a variable",
        lambda: _read_into_var(),
        33,
        79_464,
        79_472,
        33,
    ),
    (
        "memview masked read into a variable",
        lambda: _masked_read_into_var(),
        12,
        28_896,
        28_900,
        12,
    ),
    (
        "MemCond",
        lambda: when(MemCond(3000, "atleast", 5), lambda: SetDeaths(9000, SetTo, 1, 0)),
        1,
        2408,
        2408,
        1,
    ),
    (
        "memsetEPD constant value and count",
        lambda: memsetEPD(3000, 0xAABBCCDD, 2),
        1,
        2408,
        2408,
        1,
    ),
    (
        "memcpyEPD constant count 2",
        lambda: memcpyEPD(4000, 3000, 2),
        41,
        98_728,
        98_736,
        81,
    ),
    (
        "memsetEPD variable count",
        lambda: _memset_var_count(),
        8,
        19_264,
        19_268,
        13,
    ),
    (
        "memcpyEPD variable count",
        lambda: _memcpy_var_count(),
        42,
        101_136,
        101_144,
        83,
    ),
]


def _read_into_var() -> None:
    read_out = Var(0)
    read_out.set(CUnit(1000).hp.read())


def _masked_read_into_var() -> None:
    SetDeaths(3000, SetTo, 0x1201, 0)
    read_out = Var(0)
    read_out.set(make_memview(_MASKED_DWORD_SCHEMA, 3000).v.read())


def _memset_var_count() -> None:
    n = Var(2)
    memsetEPD(3000, 0x11223344, n)


def _memcpy_var_count() -> None:
    n = Var(2)
    memcpyEPD(6000, 3000, n)


class MemoryCostTests(unittest.TestCase):
    def test_each_probe_pins_its_own_static_and_executed_cost(self):
        for name, prog, triggers, trigger_bytes, total_bytes, executed in CASES:
            with self.subTest(probe=name):
                report = run_sim(build_module(prog).to_json(), loops=1)
                static = report["static_cost"]
                self.assertEqual(static["triggers"], triggers)
                self.assertEqual(static["trigger_bytes"], trigger_bytes)
                self.assertEqual(static["total_bytes"], total_bytes)
                self.assertEqual(report["executed_cost"]["triggers"], executed)

    def test_read_returns_the_memory_value(self):
        def prog():
            memsetEPD(3000, 0xAABBCCDD, 1)
            _read_into_var()

        report = run_sim(build_module(prog).to_json(), loops=1)
        self.assertEqual(read_dword(report, 3000), 0xAABBCCDD)

    def test_block_probes_move_the_expected_dwords(self):
        def prog():
            memsetEPD(3100, 0xDEADBEEF, 4)
            memcpyEPD(4100, 3100, 2)
            _memset_var_count()

        report = run_sim(build_module(prog).to_json(), loops=1)
        self.assertEqual(read_dword(report, 4100), 0xDEADBEEF)
        self.assertEqual(read_dword(report, 4101), 0xDEADBEEF)
        self.assertEqual(read_dword(report, 4102), 0)
        self.assertEqual(read_dword(report, 3000), 0x11223344)
        self.assertEqual(read_dword(report, 3001), 0x11223344)
        self.assertEqual(read_dword(report, 3002), 0)


class BlockPointerCursorTests(unittest.TestCase):
    """Ticket 01: the real CP and scratch-dest fields carry the cursors."""

    @staticmethod
    def _cp(report: dict) -> int | None:
        return report["external_epds"].get(str(EPD_CURRENT_PLAYER))

    def test_constant_and_runtime_copies_end_cp_on_the_last_source_dword(self):
        for n in (1, 2, 16):
            with self.subTest(n=n):
                def prog(n=n):
                    memsetEPD(3000, 0xAABBCCDD, 16)
                    SetDeaths(EPD_CURRENT_PLAYER, SetTo, 1234, 0)
                    memcpyEPD(5000, 3000, n)

                report = run_sim(build_module(prog).to_json(), loops=1)
                self.assertEqual(
                    [read_dword(report, 5000 + i) for i in range(n)],
                    [0xAABBCCDD] * n,
                )
                self.assertEqual(read_dword(report, 5000 + n), 0)
                self.assertEqual(self._cp(report), 3000 + n - 1)

    def test_runtime_count_copies_and_advances_cp(self):
        for n in (1, 2, 16):
            with self.subTest(n=n):
                def prog(n=n):
                    memsetEPD(3000, 0xAABBCCDD, 16)
                    SetDeaths(EPD_CURRENT_PLAYER, SetTo, 1234, 0)
                    memcpyEPD(5000, 3000, Var(n))

                report = run_sim(build_module(prog).to_json(), loops=1)
                self.assertEqual(
                    [read_dword(report, 5000 + i) for i in range(n)],
                    [0xAABBCCDD] * n,
                )
                self.assertEqual(self._cp(report), 3000 + n - 1)

    def test_runtime_zero_count_leaves_cp_and_destination_untouched(self):
        def prog():
            memsetEPD(3000, 0xAABBCCDD, 16)
            SetDeaths(EPD_CURRENT_PLAYER, SetTo, 1234, 0)
            memcpyEPD(5000, 3000, Var(0))

        report = run_sim(build_module(prog).to_json(), loops=1)
        self.assertEqual(self._cp(report), 1234)
        self.assertEqual(read_dword(report, 5000), 0)

    def test_blob_source_copies_the_payload(self):
        payload = bytes(range(64))
        for n in (1, 2, 16):
            with self.subTest(n=n):
                def prog(n=n):
                    memcpyEPD(5000, Db(payload), n)

                report = run_sim(build_module(prog).to_json(), loops=1)
                expected = [
                    int.from_bytes(payload[i * 4 : (i + 1) * 4], "little")
                    for i in range(n)
                ]
                self.assertEqual(
                    [read_dword(report, 5000 + i) for i in range(n)], expected
                )
                self.assertEqual(read_dword(report, 5000 + n), 0)

    def test_two_copies_in_sequence_do_not_drift(self):
        def prog():
            memsetEPD(3000, 0x11223344, 16)
            memsetEPD(3100, 0x55667788, 16)
            SetDeaths(EPD_CURRENT_PLAYER, SetTo, 1234, 0)
            memcpyEPD(5000, 3000, 2)
            memcpyEPD(5100, 3100, 3)

        report = run_sim(build_module(prog).to_json(), loops=1)
        self.assertEqual(
            [read_dword(report, 5000 + i) for i in range(2)],
            [0x11223344] * 2,
        )
        self.assertEqual(
            [read_dword(report, 5100 + i) for i in range(3)],
            [0x55667788] * 3,
        )
        self.assertEqual(read_dword(report, 5103), 0)
        # The second copy re-seeds CP, so the end value is its last source.
        self.assertEqual(self._cp(report), 3102)

    def test_copy_inside_when_and_while_keeps_cp_correct(self):
        def in_when():
            memsetEPD(3000, 0xAABBCCDD, 16)
            SetDeaths(EPD_CURRENT_PLAYER, SetTo, 1234, 0)
            when(Var(1).eq(1), lambda: memcpyEPD(5000, 3000, 2))

        def in_while():
            memsetEPD(3000, 0xAABBCCDD, 16)
            tick = Var(0)
            SetDeaths(EPD_CURRENT_PLAYER, SetTo, 1234, 0)

            def body():
                memcpyEPD(5000, 3000, 2)
                tick.iadd(1)

            while_loop(tick.lt(2), body)

        for name, prog in (("when", in_when), ("while", in_while)):
            with self.subTest(scope=name):
                report = run_sim(build_module(prog).to_json(), loops=1)
                self.assertEqual(
                    [read_dword(report, 5000 + i) for i in range(2)],
                    [0xAABBCCDD] * 2,
                )
                self.assertEqual(self._cp(report), 3001)

    def test_constant_count_runtime_fill_re_executes_every_loop(self):
        # A constant count with a runtime fill value keeps the runtime loop, so
        # the counter must be re-stored every trigger loop (not just at module
        # init). The value it writes advances 1, 2, 3, ...
        def prog():
            v = Var(0)
            v.iadd(1)
            memsetEPD(3000, v, 2)

        for loops in (1, 2, 3):
            with self.subTest(loops=loops):
                report = run_sim(build_module(prog).to_json(), loops=loops)
                self.assertEqual(
                    [read_dword(report, 3000 + i) for i in range(2)],
                    [loops, loops],
                )
                self.assertEqual(read_dword(report, 3002), 0)

    def test_constant_count_runtime_fill_reenters_while_loop(self):
        # `while_loop` re-entry is safe: pass 1 writes the seed value, pass 2
        # writes its increment. An init-only counter would leave pass 1's bytes.
        def prog():
            v = Var(0x11111111)
            i = Var(0)

            def body():
                memsetEPD(3000, v, 2)
                v.iadd(0x11111111)
                i.iadd(1)

            while_loop(i.lt(2), body)

        report = run_sim(build_module(prog).to_json(), loops=1)
        self.assertEqual(
            [read_dword(report, 3000 + i) for i in range(2)],
            [0x22222222, 0x22222222],
        )

    def test_constant_count_fill_and_copy_repeat_in_one_loop_body(self):
        # A non-folded constant-count fill and a constant-count copy in the
        # same repeated body must both re-seed on every pass: no stale counter,
        # no synthetic-id collision between the fill and copy triggers.
        def prog():
            v = Var(0x11111111)
            i = Var(0)

            def body():
                memsetEPD(3000, v, 2)
                memcpyEPD(4000, 3000, 2)
                v.iadd(0x11111111)
                i.iadd(1)

            while_loop(i.lt(2), body)

        report = run_sim(build_module(prog).to_json(), loops=1)
        self.assertEqual(
            [read_dword(report, 3000 + i) for i in range(2)],
            [0x22222222, 0x22222222],
        )
        self.assertEqual(
            [read_dword(report, 4000 + i) for i in range(2)],
            [0x22222222, 0x22222222],
        )

    def test_fills_write_exactly_the_requested_dwords(self):
        for n in (1, 2, 16):
            with self.subTest(n=n):
                def const_fill(n=n):
                    memsetEPD(3000, 0x11223344, n)

                report = run_sim(build_module(const_fill).to_json(), loops=1)
                self.assertEqual(
                    [read_dword(report, 3000 + i) for i in range(n)],
                    [0x11223344] * n,
                )
                self.assertEqual(read_dword(report, 3000 + n), 0)

                def var_fill(n=n):
                    memsetEPD(3000, Var(0x11223344), n)

                report = run_sim(build_module(var_fill).to_json(), loops=1)
                self.assertEqual(
                    [read_dword(report, 3000 + i) for i in range(n)],
                    [0x11223344] * n,
                )
                self.assertEqual(read_dword(report, 3000 + n), 0)


if __name__ == "__main__":
    unittest.main()
