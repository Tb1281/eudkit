"""Ticket #10: constant-count block fills fold at build time (Python frontend)."""

from __future__ import annotations

import json
import unittest

from py import MAX_FOLD_CHUNKS, Var, build_module, memcpyEPD, memsetEPD

from py.tests.helpers import read_dword, run_sim, run_static

FILL = 0xAABBCCDD
TRIGGER_BYTES = 2408


def _report(prog) -> dict:
    return run_sim(build_module(prog).to_json(), loops=1)


def _static(report: dict) -> dict:
    return report["static_cost"]


class MemsetConstantFoldTests(unittest.TestCase):
    def test_constant_fills_emit_one_packed_trigger_per_64_dwords(self):
        # ceil(count/64) triggers, each a full 2,408-byte trigger page.
        for count, triggers in [(2, 1), (64, 1), (65, 2), (448, 7), (1024, 16)]:
            with self.subTest(count=count):
                report = _report(lambda count=count: memsetEPD(3000, FILL, count))
                self.assertEqual(_static(report)["triggers"], triggers)
                self.assertEqual(
                    _static(report)["trigger_bytes"], triggers * TRIGGER_BYTES
                )
                self.assertEqual(_static(report)["total_bytes"], triggers * TRIGGER_BYTES)
                self.assertEqual(report["executed_cost"]["triggers"], triggers)

    def test_constant_fill_writes_every_dword_and_stops_after_the_last(self):
        for count in (2, 63, 64, 65):
            with self.subTest(count=count):
                report = _report(lambda count=count: memsetEPD(3000, FILL, count))
                for i in range(count):
                    self.assertEqual(read_dword(report, 3000 + i), FILL, f"dword {i}")
                self.assertEqual(
                    read_dword(report, 3000 + count),
                    0,
                    "the fill must not write past its last dword",
                )
                if count == 64:
                    # The chunking boundary: dword 63 is in the packed trigger,
                    # dword 64 is not written at all.
                    self.assertEqual(read_dword(report, 3000 + 63), FILL)
                    self.assertEqual(read_dword(report, 3000 + 64), 0)

    def test_count_449_now_folds_to_eight_triggers(self):
        report = _report(lambda: memsetEPD(3000, FILL, 449))
        self.assertEqual(
            _static(report)["triggers"], 8, "449 must fold to 8T, not keep the loop"
        )
        self.assertEqual(_static(report)["trigger_bytes"], 8 * TRIGGER_BYTES)
        self.assertEqual(_static(report)["total_bytes"], 8 * TRIGGER_BYTES)
        self.assertEqual(report["executed_cost"]["triggers"], 8)
        parsed = json.loads(build_module(lambda: memsetEPD(3000, FILL, 449)).to_json())
        self.assertNotIn(
            "while",
            [stmt["kind"] for stmt in parsed["module"]["stmts"]],
            "a folded fill must not lower to the runtime loop",
        )
        self.assertNotIn("vars", parsed["module"], "a folded fill allocates no vars")
        self.assertEqual(read_dword(report, 3000), FILL)
        self.assertEqual(read_dword(report, 3000 + 448), FILL)
        self.assertEqual(read_dword(report, 3000 + 449), 0)

    def test_count_1024_folds_to_sixteen_triggers(self):
        report = _report(lambda: memsetEPD(3000, FILL, 1024))
        self.assertEqual(_static(report)["triggers"], 16)
        self.assertEqual(_static(report)["trigger_bytes"], 16 * TRIGGER_BYTES)
        self.assertEqual(_static(report)["total_bytes"], 16 * TRIGGER_BYTES)
        self.assertEqual(report["executed_cost"]["triggers"], 16)
        self.assertEqual(read_dword(report, 3000 + 1023), FILL)
        self.assertEqual(read_dword(report, 3000 + 1024), 0)

    def test_fold_cap_is_512_chunks(self):
        count = MAX_FOLD_CHUNKS * 64
        report = _report(lambda: memsetEPD(3000, FILL, count))
        self.assertEqual(_static(report)["triggers"], MAX_FOLD_CHUNKS)
        self.assertEqual(
            _static(report)["trigger_bytes"], MAX_FOLD_CHUNKS * TRIGGER_BYTES
        )
        self.assertEqual(
            _static(report)["total_bytes"], MAX_FOLD_CHUNKS * TRIGGER_BYTES
        )
        self.assertEqual(report["executed_cost"]["triggers"], MAX_FOLD_CHUNKS)
        parsed = json.loads(
            build_module(lambda: memsetEPD(3000, FILL, count)).to_json()
        )
        self.assertNotIn("while", [stmt["kind"] for stmt in parsed["module"]["stmts"]])
        self.assertNotIn("vars", parsed["module"])
        self.assertEqual(read_dword(report, 3000), FILL)
        self.assertEqual(read_dword(report, 3000 + count - 1), FILL)
        self.assertEqual(read_dword(report, 3000 + count), 0)

    def test_count_past_the_cap_keeps_the_loop(self):
        # Do NOT simulate: the native simulator panics at 100,000 executed
        # triggers and this loop needs ~196,616. Assert the HIR shape and the
        # static cost only (`dump-ir --json --no-exec`).
        count = MAX_FOLD_CHUNKS * 64 + 1
        mod = build_module(lambda: memsetEPD(3000, FILL, count))
        parsed = json.loads(mod.to_json())
        self.assertIn(
            "while",
            [stmt["kind"] for stmt in parsed["module"]["stmts"]],
            "an over-cap fill must still lower to the runtime loop",
        )
        # Cycle 27 #01: the destination cursor is the copy trigger's dest
        # field in real memory, and the constant count is initialised straight
        # into the counter var, so only the counter is allocated.
        self.assertEqual(len(parsed["module"]["vars"]), 1, "counter only")
        static = run_static(mod.to_json())["static_cost"]
        # Cycle 27 #01: the runtime-zero guard is emitted for every
        # destination form, so the non-folded loop pays one extra trigger.
        self.assertEqual(static["triggers"], 6)
        self.assertEqual(static["trigger_bytes"], 6 * TRIGGER_BYTES)
        self.assertEqual(static["total_bytes"], 6 * TRIGGER_BYTES + 4)

    def test_variable_count_and_variable_value_keep_the_runtime_loop(self):
        def with_count():
            n = Var(2)
            memsetEPD(3000, 0x11223344, n)

        def with_value():
            v = Var(FILL)
            memsetEPD(3000, v, 2)

        count_report = _report(with_count)
        # Cycle 27 #01: the destination advances through the copy trigger's
        # dest field, so the loop no longer pays a per-pass dst-cursor store.
        # The runtime-zero guard is emitted for every destination form, which
        # adds one static and one executed trigger (8T / 13T).
        self.assertEqual(_static(count_report)["triggers"], 8)
        self.assertEqual(_static(count_report)["trigger_bytes"], 8 * TRIGGER_BYTES)
        self.assertEqual(_static(count_report)["total_bytes"], 8 * TRIGGER_BYTES + 4)
        self.assertEqual(count_report["executed_cost"]["triggers"], 13)
        self.assertEqual(read_dword(count_report, 3000), 0x11223344)
        self.assertEqual(read_dword(count_report, 3001), 0x11223344)
        self.assertEqual(read_dword(count_report, 3002), 0)

        value_report = _report(with_value)
        self.assertEqual(_static(value_report)["triggers"], 8)
        self.assertEqual(_static(value_report)["trigger_bytes"], 8 * TRIGGER_BYTES)
        self.assertEqual(_static(value_report)["total_bytes"], 8 * TRIGGER_BYTES + 4)
        self.assertEqual(value_report["executed_cost"]["triggers"], 15)
        self.assertEqual(read_dword(value_report, 3000), FILL)
        self.assertEqual(read_dword(value_report, 3001), FILL)
        self.assertEqual(read_dword(value_report, 3002), 0)


class MemcpyConstantUnrollTests(unittest.TestCase):
    def test_constant_count_one_emits_the_copy_body_once(self):
        # #97: the CP-cursor dword read inside `emit_copy` goes through the
        # shared reader. A single call site is inlined by the optimizer, so the
        # 33T reader body is the whole read cost (the old per-site inline read
        # was 40T: a 32-act accumulator reset + 31 bit tests + an EPD-base
        # subtract). Every further *distinct* address pays 5T static for the
        # call sequence plus one body (see `test_shared_reader_cost_is_per_site`).
        report = _report(lambda: memcpyEPD(4000, 3000, 1))
        self.assertEqual(_static(report)["triggers"], 38)
        self.assertEqual(_static(report)["trigger_bytes"], 91_504)
        self.assertEqual(_static(report)["total_bytes"], 91_508)
        self.assertEqual(report["executed_cost"]["triggers"], 39)

        parsed = json.loads(build_module(lambda: memcpyEPD(4000, 3000, 1)).to_json())
        self.assertNotIn(
            "while",
            [stmt["kind"] for stmt in parsed["module"]["stmts"]],
            "a count of 1 must not emit a while_loop",
        )
        # Three block-copy vars plus the reader's body result and the call's
        # destination var (#97); still no remaining-count var.
        self.assertEqual(
            len(parsed["module"]["vars"]),
            5,
            "a count of 1 must not allocate the remaining-count var",
        )

    def test_constant_count_one_copies_exactly_one_dword(self):
        def prog():
            memsetEPD(3000, 0xDEADBEEF, 4)
            memcpyEPD(4000, 3000, 1)

        report = _report(prog)
        self.assertEqual(read_dword(report, 4000), 0xDEADBEEF)
        self.assertEqual(
            read_dword(report, 4001), 0, "count 1 must leave the next dword untouched"
        )

    def test_constant_count_two_keeps_the_runtime_loop(self):
        # #97: same shared-reader re-baseline as the count-1 case above; the
        # loop body still carries exactly one read call site.
        # Cycle 27 #01: the source cursor is CurrentPlayer itself and the
        # destination is the copy trigger's dest field, so the executed loop
        # no longer stores two cursor vars (84T -> 81T).
        report = _report(lambda: memcpyEPD(4000, 3000, 2))
        self.assertEqual(_static(report)["triggers"], 41)
        self.assertEqual(_static(report)["trigger_bytes"], 98_728)
        self.assertEqual(_static(report)["total_bytes"], 98_736)
        self.assertEqual(report["executed_cost"]["triggers"], 81)

        parsed = json.loads(build_module(lambda: memcpyEPD(4000, 3000, 2)).to_json())
        self.assertIn("while", [stmt["kind"] for stmt in parsed["module"]["stmts"]])

        def prog():
            memsetEPD(3000, 0xDEADBEEF, 4)
            memcpyEPD(4000, 3000, 2)

        looped = _report(prog)
        self.assertEqual(read_dword(looped, 4000), 0xDEADBEEF)
        self.assertEqual(read_dword(looped, 4001), 0xDEADBEEF)
        self.assertEqual(read_dword(looped, 4002), 0)


class BlockPrimitiveContractTests(unittest.TestCase):
    def test_zero_count_still_no_ops_before_value_validation(self):
        def prog():
            memcpyEPD(9000, 1000, 0)
            memsetEPD(9001, "bad", 0)

        report = _report(prog)
        self.assertEqual(_static(report)["triggers"], 0)
        self.assertEqual(_static(report)["total_bytes"], 0)

    def test_validation_order_stays_dst_then_count_then_value(self):
        # dstEPD is validated first: the invalid type wins over the bad count.
        with self.assertRaisesRegex(Exception, "EUDSL-TYPE"):
            build_module(lambda: memsetEPD(-1, "bad", -1))
        # count is validated before value: the range error wins over the type.
        with self.assertRaisesRegex(Exception, "EUDSL-RANGE"):
            build_module(lambda: memsetEPD(3000, "bad", -1))
        # ... and value is validated last, after the zero-count early return.
        with self.assertRaisesRegex(Exception, "EUDSL-TYPE"):
            build_module(lambda: memsetEPD(3000, "bad", 1))
        with self.assertRaisesRegex(Exception, "EUDSL-TYPE"):
            build_module(lambda: memcpyEPD(-1, 3000, 1))
        with self.assertRaisesRegex(Exception, "EUDSL-RANGE"):
            build_module(lambda: memcpyEPD(4000, 3000, -1))
        # Constant counts above 2^32-1 are rejected in both primitives, with
        # the same message; 0xFFFFFFFF itself is still accepted.
        with self.assertRaisesRegex(
            Exception,
            r"EUDSL-RANGE.*memsetEPD\(\): count must be at most 0xFFFFFFFF "
            r"dwords, got 4294967296",
        ):
            build_module(lambda: memsetEPD(3000, 0xFF, 0x100000000))
        with self.assertRaisesRegex(
            Exception,
            r"EUDSL-RANGE.*memcpyEPD\(\): count must be at most 0xFFFFFFFF "
            r"dwords, got 4294967296",
        ):
            build_module(lambda: memcpyEPD(4000, 3000, 0x100000000))

    def test_max_uint32_count_is_accepted_and_keeps_the_loop(self):
        mod = build_module(lambda: memsetEPD(3000, 0xFF, 0xFFFFFFFF))
        parsed = json.loads(mod.to_json())
        self.assertIn(
            "while",
            [stmt["kind"] for stmt in parsed["module"]["stmts"]],
            "0xFFFFFFFF dwords is over the fold cap, so it keeps the loop",
        )


if __name__ == "__main__":
    unittest.main()
