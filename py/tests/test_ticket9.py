"""Ticket #9: STR-backed ``FixedString`` and ``memcpyEPD``/``memsetEPD`` tests."""

from __future__ import annotations

import json
import unittest

from py import (
    Always,
    Func,
    SetMemory,
    SetTo,
    FixedString,
    Var,
    build_module,
    memcpyEPD,
    memsetEPD,
    once,
    when,
)

from py.tests.helpers import (
    EPD_BASE,
    flatten_acts,
    read_dword,
    read_string_bytes,
    read_text,
    run_sim,
    run_static,
    string_epd,
    touched_dword_offsets,
)

DISPLAY_LINE_ADDR = 0x640B58
DISPLAY_LINE_EPD = (DISPLAY_LINE_ADDR - EPD_BASE) >> 2


class Ticket9Tests(unittest.TestCase):
    def test_static_write_packs_constant_dwords_and_reserves_str_marker(self):
        mod = build_module(lambda: FixedString(16).write("Hi", 0x41))
        hir_json = mod.to_json()
        parsed = json.loads(hir_json)
        strings = parsed["module"].get("strings", [])
        self.assertEqual(len(strings), 1)
        self.assertIn("EUDString#0", strings[0])
        self.assertFalse(
            any(stmt["kind"] == "when" for stmt in parsed["module"]["stmts"]),
            "a static write must not need lane-selection whens",
        )

        report = run_sim(hir_json)
        self.assertEqual(read_string_bytes(report, 0), list(b"Hi00000041"))
        self.assertEqual(report["static_cost"]["triggers"], 1)

    def test_runtime_position_write_allocates_page_pinned_straddr_variable(self):
        def prog():
            s = FixedString(8)
            pos = Var(0)
            s.writeFrom(pos, "{}", Var(1))

        parsed = json.loads(build_module(prog).to_json())
        addrs = [
            var for var in parsed["module"].get("vars", []) if "init_string" in var
        ]
        self.assertEqual(len(addrs), 1)
        self.assertEqual(addrs[0]["storage"], "page")
        self.assertEqual(addrs[0]["init_string"], {"index": 0, "offset": 0})

    def test_write_is_statically_anchored_inside_when_and_func(self):
        def prog():
            flag = Var(1)
            s = FixedString(8)
            when(flag.eq(1), lambda: s.write("AB"))
            f = Func(lambda: s.append("C"))
            f()

        hir_json = build_module(prog).to_json()
        parsed = json.loads(hir_json)
        when_stmt = next(
            stmt for stmt in parsed["module"]["stmts"] if stmt["kind"] == "when"
        )
        body = when_stmt["body"]
        self.assertTrue(body)
        self.assertTrue(
            all(stmt["kind"] in ("act", "trigger") for stmt in body),
            "a static write inside when() must lower to plain acts",
        )
        self.assertFalse(any(stmt["kind"] == "when" for stmt in body))

        report = run_sim(hir_json)
        self.assertEqual(read_text(report, 0), "ABC")

    def test_append_continues_from_lastpos_and_writefrom_overwrites(self):
        def prog():
            s = FixedString(16)
            s.write("abcdefgh")
            s.append("IJ")
            s.writeFrom(2, "XY")

        report = run_sim(build_module(prog).to_json())
        base = string_epd(0)
        self.assertEqual(read_dword(report, base), 0x59586261)
        self.assertEqual(read_dword(report, base + 1), 0x68676600)
        self.assertEqual(read_dword(report, base + 2), 0x00004A49)

    def test_static_writes_defer_their_terminator_and_pack_across_appends(self):
        def prog():
            s = FixedString(16)
            s.write("ab")
            s.append("cd")

        report = run_sim(build_module(prog).to_json())
        self.assertEqual(read_string_bytes(report, 0), list(b"abcd"))
        self.assertEqual(
            report["static_cost"]["actions"],
            2,
            "the full content dword and one terminator dword should be the only writes",
        )

    def test_skipped_branches_keep_the_deferred_tail_flushed_at_scope_entry(self):
        def prog():
            flag = Var(0)
            s = FixedString(8)
            s.write("v=")
            when(flag.eq(1), lambda: s.write("AB"))
            s.append("!")
            s.display()

        report = run_sim(build_module(prog).to_json())
        self.assertEqual(
            read_text(report, 0),
            "v=!",
            "a skipped branch must not discard the outer deferred tail",
        )

    def test_writefrom_flushes_a_deferred_tail_before_repositioning(self):
        def prog():
            s = FixedString(8)
            s.write("abc")
            s.writeFrom(1, "Z")
            s.display()

        report = run_sim(build_module(prog).to_json())
        self.assertEqual(read_string_bytes(report, 0), list(b"aZ"))

    def test_module_end_flushes_a_static_write_that_is_never_displayed(self):
        def prog():
            FixedString(8).write("tail")

        report = run_sim(build_module(prog).to_json())
        self.assertEqual(read_string_bytes(report, 0), list(b"tail"))

    def test_deferred_terminator_never_adds_alignment_padding_past_content(self):
        def prog():
            s = FixedString(8)
            s.write("12345678")
            s.display()

        report = run_sim(build_module(prog).to_json())
        self.assertEqual(read_string_bytes(report, 0), list(b"12345678"))
        self.assertEqual(
            touched_dword_offsets(report, 0),
            [0, 4, 8],
            "a full buffer may touch only its content dwords and the single NUL dword",
        )

    def test_writefrom_accepts_game_time_position(self):
        def prog():
            s = FixedString(16)
            s.write("abcdefgh")
            pos = Var(6)
            s.writeFrom(pos, "Z")

        report = run_sim(build_module(prog).to_json())
        self.assertEqual(read_dword(report, string_epd(0) + 1), 0x005A6665)

    def test_format_specs_render_hex_decimal_and_escaped_braces(self):
        def prog():
            s = FixedString(64)
            s.write("{:x}|{:x}|{:d}", 0xAB, 0xAB, 1234)
            s.append("|{{x}}|{:d}", 7)

        report = run_sim(build_module(prog).to_json())
        self.assertEqual(
            read_string_bytes(report, 0), list(b"000000ab|000000ab|1234|{x}|7")
        )

    def test_runtime_format_placeholders_render_game_values(self):
        def prog():
            s = FixedString(64)
            hex_value = Var(0x12345678)
            dec_value = Var(4321)
            s.write("{:x}", hex_value)
            s.append(":{:d}", dec_value)

        report = run_sim(build_module(prog).to_json())
        self.assertEqual(read_string_bytes(report, 0), list(b"12345678:4321"))

    def test_decimal_digit_suppression_restarts_every_loop(self):
        def prog():
            FixedString(16).print("{:d}", Var(42))

        report = run_sim(build_module(prog).to_json(), 3)
        self.assertEqual(
            read_text(report, 0),
            "42",
            "the leading-zero suppression state must not leak into the next "
            "loop",
        )

    def test_displayat_restores_saved_every_call_and_loop(self):
        def prog():
            s = FixedString(8)
            s.write("hi")
            SetMemory(DISPLAY_LINE_ADDR, SetTo, 2)
            s.displayAt(3)
            SetMemory(DISPLAY_LINE_ADDR, SetTo, 2)
            s.displayAt(3)

        report = run_sim(build_module(prog).to_json(), 3)
        self.assertEqual(
            read_dword(report, DISPLAY_LINE_EPD),
            2,
            "sim-visible restore is saved verbatim; each "
            "displayAt must still save only the current line counter, "
            "not accumulate",
        )

    def test_malformed_format_strings_fail_loudly(self):
        def build_lone_brace():
            build_module(lambda: FixedString(8).write("{"))

        def build_unknown_spec():
            build_module(lambda: FixedString(8).write("{:q}", 1))

        def build_arg_mismatch():
            build_module(lambda: FixedString(8).write("{} {}", 1))

        def build_lone_close():
            build_module(lambda: FixedString(8).append("}"))

        for build in (
            build_lone_brace,
            build_unknown_spec,
            build_arg_mismatch,
            build_lone_close,
        ):
            with self.assertRaisesRegex(Exception, "EUDSL-FORMAT"):
                build()

    def test_clear_resets_buffer_and_writes_restart_at_zero(self):
        def prog():
            s = FixedString(8)
            s.write("AAAA")
            s.clear()
            s.write("B")

        report = run_sim(build_module(prog).to_json())
        self.assertEqual(read_dword(report, string_epd(0)), 0x42)

    def test_display_printat_and_displayat_emit_display_acts(self):
        def prog():
            s = FixedString(8)
            s.write("hi")
            s.display()
            s.printAt(3, "yo")
            s.displayAt(Var(2))
            s.clear()

        parsed = json.loads(build_module(prog).to_json())
        acts = flatten_acts(parsed["module"]["stmts"])
        displays = [act for act in acts if act.get("acttype") == 9]
        self.assertEqual(len(displays), 3)
        self.assertEqual([act["str_index"] for act in displays], [0, 0, 0])
        self.assertTrue(
            any(
                act.get("dest") == DISPLAY_LINE_EPD and act.get("value") == 3
                for act in acts
            ),
            "printAt(3) must write display line 3",
        )

    def test_memcpy_and_memset_constant_and_variable_counts(self):
        def prog():
            memsetEPD(1000, 0xAABBCCDD, 3)
            memcpyEPD(2000, 1000, 3)
            n = Var(2)
            memsetEPD(3000, 0x11223344, n)

        report = run_sim(build_module(prog).to_json(), 2)
        for i in range(3):
            self.assertEqual(read_dword(report, 1000 + i), 0xAABBCCDD)
            self.assertEqual(read_dword(report, 2000 + i), 0xAABBCCDD)
        self.assertEqual(read_dword(report, 3000), 0x11223344)
        self.assertEqual(read_dword(report, 3001), 0x11223344)
        self.assertEqual(read_dword(report, 3002), 0)

    def test_zero_count_folds_to_zero_triggers(self):
        def prog():
            memcpyEPD(9000, 1000, 0)
            memsetEPD(9001, 1, 0)

        report = run_sim(build_module(prog).to_json())
        self.assertEqual(report["static_cost"]["triggers"], 0)
        self.assertEqual(report["static_cost"]["total_bytes"], 0)

    def test_block_primitives_validate_arguments(self):
        with self.assertRaisesRegex(Exception, "EUDSL-RANGE"):
            build_module(lambda: memcpyEPD(1, 2, -1))
        with self.assertRaisesRegex(Exception, "EUDSL-TYPE"):
            build_module(lambda: memsetEPD(-1, 0, 1))

    def test_string_rejects_invalid_sizes(self):
        with self.assertRaisesRegex(Exception, "EUDSL-BOUNDS"):
            build_module(lambda: FixedString(0))
        with self.assertRaisesRegex(Exception, "EUDSL-BOUNDS"):
            build_module(lambda: FixedString(65536))

    def test_capacity_exact_fit_reserves_one_terminator_byte(self):
        mod = build_module(lambda: FixedString(4).write("abcd"))
        parsed = json.loads(mod.to_json())
        self.assertEqual(
            parsed["module"]["strings"][0],
            " " * 5 + "\x7fEUDString#0\x7f",
            "the marker must start after the content region and its terminator slot",
        )

        report = run_sim(mod.to_json())
        self.assertEqual(read_string_bytes(report, 0), list(b"abcd"))
        self.assertEqual(
            touched_dword_offsets(report, 0),
            [0, 4],
            "an exact-fit write may touch only its content and terminator dwords",
        )

    def test_capacity_rejects_static_writes_and_appends_that_exceed_size(self):
        with self.assertRaisesRegex(Exception, "EUDSL-BOUNDS"):
            build_module(lambda: FixedString(4).write("abcde"))

        def prog():
            s = FixedString(4)
            s.write("abcd")
            s.append("e")

        with self.assertRaisesRegex(Exception, "EUDSL-BOUNDS"):
            build_module(prog)

    def test_writefrom_enforces_capacity_for_constant_positions(self):
        def ok():
            s = FixedString(4)
            s.write("abc")
            s.writeFrom(3, "X")

        report = run_sim(build_module(ok).to_json())
        self.assertEqual(read_string_bytes(report, 0), list(b"abcX"))

        with self.assertRaisesRegex(Exception, "EUDSL-BOUNDS"):
            build_module(lambda: FixedString(4).writeFrom(3, "XY"))

    def test_standalone_empty_when_and_once_gates_cost_nothing(self):
        cases = {
            "when(Always())": lambda: when(Always(), lambda: None),
            "once(Always())": lambda: once(Always(), lambda: None),
        }
        for label, prog in cases.items():
            with self.subTest(label=label):
                mod = build_module(prog)
                static = run_static(mod.to_json())["static_cost"]
                self.assertEqual(
                    (
                        static["triggers"],
                        static["conditions"],
                        static["actions"],
                        static["total_bytes"],
                    ),
                    (0, 0, 0, 0),
                )
                executed = run_sim(mod.to_json())["executed_cost"]
                self.assertEqual(
                    (
                        executed["triggers"],
                        executed["conditions"],
                        executed["actions"],
                    ),
                    (0, 0, 0),
                )

    def test_standalone_empty_var_gate_keeps_only_the_unused_var_cell(self):
        mod = build_module(lambda: when(Var(0).eq(1), lambda: None))
        static = run_static(mod.to_json())["static_cost"]
        self.assertEqual(
            (
                static["triggers"],
                static["conditions"],
                static["actions"],
                static["total_bytes"],
            ),
            (0, 0, 0, 4),
            "the empty gate is dead; only the unused 4-byte variable remains",
        )
        executed = run_sim(mod.to_json())["executed_cost"]
        self.assertEqual(
            (executed["triggers"], executed["conditions"], executed["actions"]),
            (0, 0, 0),
        )

    def test_append_after_scope_boundary_reinstalls_position_every_loop(self):
        def prog():
            s = FixedString(8)
            s.write("AB")
            when(Always(), lambda: None)
            s.append("CD")

        mod = build_module(prog)
        static = run_static(mod.to_json())["static_cost"]
        self.assertEqual(
            (
                static["triggers"],
                static["conditions"],
                static["actions"],
                static["total_bytes"],
            ),
            (27, 2, 332, 65_016),
            "the empty scope-boundary marker loses its dead trigger and "
            "condition; removing that statement also lets the two adjacent "
            "act runs pack into one trigger downstream, without moving the "
            "string layout",
        )

        report = run_sim(mod.to_json(), 3)
        self.assertEqual(
            read_text(report, 0),
            "ABCD",
            "the runtime append position must not drift past the static "
            "terminator",
        )

    def test_aligned_boundaries_pad_partial_dwords_with_0x1a(self):
        def prog():
            s = FixedString(16)
            s.write("AB")
            when(Always(), lambda: None)
            s.append("CD")

        report = run_sim(build_module(prog).to_json(), 2)
        self.assertEqual(
            read_string_bytes(report, 0),
            list(b"AB") + [0x1A, 0x1A] + list(b"CD"),
            "a lane-2 boundary must pad to the next dword before the dynamic "
            "append",
        )
        self.assertEqual(read_text(report, 0), "ABCD")
        self.assertTrue(
            all(offset <= 4 for offset in touched_dword_offsets(report, 0)),
            "padding must stay inside the string's own terminator slot",
        )

    def test_aligned_boundaries_do_not_pad_content_that_ends_on_a_dword(self):
        def prog():
            s = FixedString(8)
            s.write("ABCD")
            when(Always(), lambda: None)
            s.append("EF")

        report = run_sim(build_module(prog).to_json(), 2)
        self.assertEqual(read_string_bytes(report, 0), list(b"ABCDEF"))

    def test_aligned_boundaries_fall_back_when_padding_does_not_fit(self):
        def prog():
            s = FixedString(7)
            s.write("ABCDEFG")
            when(Always(), lambda: None)

        report = run_sim(build_module(prog).to_json(), 2)
        self.assertEqual(
            read_string_bytes(report, 0),
            list(b"ABCDEFG"),
            "a full buffer must not gain padding past its terminator slot",
        )

    def test_aligned_padding_still_leaves_exact_fit_content_inside_capacity(self):
        def exact():
            s = FixedString(8)
            s.write("AB")
            when(Always(), lambda: None)
            s.append("CDEF")

        report = run_sim(build_module(exact).to_json())
        self.assertEqual(
            read_string_bytes(report, 0), list(b"AB") + [0x1A, 0x1A] + list(b"CDEF")
        )
        self.assertEqual(read_text(report, 0), "ABCDEF")

        def one_byte_short():
            s = FixedString(7)
            s.write("AB")
            when(Always(), lambda: None)
            s.append("CDEF")

        with self.assertRaisesRegex(Exception, "EUDSL-BOUNDS"):
            build_module(one_byte_short)

    def test_repeated_aligned_boundaries_keep_a_stable_layout(self):
        def prog():
            s = FixedString(16)
            s.write("AB")
            when(Always(), lambda: None)
            s.append("CD")
            when(Always(), lambda: None)
            s.append("EF")

        report = run_sim(build_module(prog).to_json(), 2)
        self.assertEqual(
            read_string_bytes(report, 0),
            list(b"AB")
            + [0x1A, 0x1A]
            + list(b"CD")
            + [0x1A, 0x1A]
            + list(b"EF"),
        )
        self.assertEqual(read_text(report, 0), "ABCDEF")
        self.assertTrue(
            all(offset <= 8 for offset in touched_dword_offsets(report, 0)),
            "repeated boundaries must not walk past the terminator slot",
        )

    def test_dynamic_hex_after_an_aligned_boundary_uses_constant_byte_masks(self):
        def prog():
            s = FixedString(16)
            s.write("AB")
            when(Always(), lambda: None)
            s.append(Var(1))

        mod = build_module(prog)
        report = run_sim(mod.to_json(), 2)
        self.assertEqual(
            read_string_bytes(report, 0),
            list(b"AB") + [0x1A, 0x1A] + list(b"00000001"),
        )
        self.assertEqual(read_text(report, 0), "AB00000001")

        acts = flatten_acts(json.loads(mod.to_json())["module"]["stmts"])
        byte_masks = {0xFF, 0xFF00, 0xFF0000, 0xFF000000}
        lane_acts = [
            act
            for act in acts
            if act.get("acttype") == 45
            and act.get("eudx") == 0x4353
            and act.get("mask") in byte_masks
        ]
        self.assertGreaterEqual(
            len(lane_acts), 9, "eight hex digits plus the terminator"
        )
        self.assertEqual(
            {act.get("mask") for act in lane_acts},
            byte_masks,
            "a known lane must bake every byte mask instead of selecting it "
            "at runtime",
        )

    def test_aligned_dword_run_after_a_taken_boundary_stays_inside_capacity(self):
        def prog():
            s = FixedString(8)
            s.write("abcd")
            when(Always(), lambda: s.append("efgh"))

        report = run_sim(build_module(prog).to_json(), 2)
        self.assertEqual(read_string_bytes(report, 0), list(b"abcdefgh"))
        self.assertEqual(read_text(report, 0), "abcdefgh")
        self.assertTrue(
            all(offset <= 8 for offset in touched_dword_offsets(report, 0)),
            "the fast dword run must stop at the terminator slot",
        )

    def test_dynamic_tails_pad_at_boundaries_only_when_the_pad_fits(self):
        def fits():
            s = FixedString(12)
            s.write("AB")
            s.append(Var(1))
            when(Always(), lambda: None)

        padded = run_sim(build_module(fits).to_json(), 2)
        self.assertEqual(
            read_string_bytes(padded, 0), list(b"AB00000001") + [0x1A, 0x1A]
        )
        self.assertEqual(read_text(padded, 0), "AB00000001")

        def falls_back():
            s = FixedString(11)
            s.write("AB")
            s.append(Var(1))
            when(Always(), lambda: None)
            s.append("X")

        unpadded = run_sim(build_module(falls_back).to_json(), 2)
        self.assertEqual(read_string_bytes(unpadded, 0), list(b"AB00000001X"))
        self.assertEqual(read_text(unpadded, 0), "AB00000001X")

    def test_padding_keeps_adjacent_strings_isolated(self):
        def prog():
            a = FixedString(8)
            a.write("AB")
            b = FixedString(4)
            b.write("wxyz")
            when(Always(), lambda: None)
            a.append("C")
            a.display()
            b.display()

        report = run_sim(build_module(prog).to_json(), 2)
        self.assertEqual(read_text(report, 0), "ABC")
        self.assertEqual(read_string_bytes(report, 1), list(b"wxyz"))
        self.assertTrue(
            all(offset <= 8 for offset in touched_dword_offsets(report, 0)),
            "padding must not reach the next string's marker",
        )

    def test_dynamic_hex_content_is_bounded_by_capacity(self):
        mod = build_module(lambda: FixedString(8).write(Var(1)))
        self.assertEqual(read_string_bytes(run_sim(mod.to_json()), 0), list(b"00000001"))

        with self.assertRaisesRegex(Exception, "EUDSL-BOUNDS"):
            build_module(lambda: FixedString(7).write(Var(1)))

    def test_dynamic_decimal_and_format_part_content_use_conservative_bounds(self):
        dec = build_module(lambda: FixedString(10).write("{:d}", Var(1)))
        self.assertEqual(read_string_bytes(run_sim(dec.to_json()), 0), list(b"1"))

        mixed = build_module(lambda: FixedString(10).write("ab{:x}", Var(1)))
        self.assertEqual(
            read_string_bytes(run_sim(mixed.to_json()), 0), list(b"ab00000001")
        )

        with self.assertRaisesRegex(Exception, "EUDSL-BOUNDS"):
            build_module(lambda: FixedString(9).write("{:d}", Var(1)))
        with self.assertRaisesRegex(Exception, "EUDSL-BOUNDS"):
            build_module(lambda: FixedString(9).write("ab{:x}", Var(1)))

    def test_conditional_branches_are_capacity_checked_as_if_they_execute(self):
        def ok():
            s = FixedString(8)
            s.write("abcd")
            when(Var(0).eq(1), lambda: s.append("efgh"))

        report = run_sim(build_module(ok).to_json())
        self.assertEqual(read_string_bytes(report, 0), list(b"abcd"))

        def overflow():
            s = FixedString(8)
            s.write("abcd")
            when(Var(0).eq(1), lambda: s.append("efghi"))

        with self.assertRaisesRegex(Exception, "EUDSL-BOUNDS"):
            build_module(overflow)

    def test_runtime_writefrom_clamps_and_guards_positions_past_capacity(self):
        def out_of_range():
            s = FixedString(4)
            s.write("AAAA")
            s.writeFrom(Var(999), "XY")

        report = run_sim(build_module(out_of_range).to_json())
        self.assertEqual(
            read_string_bytes(report, 0),
            list(b"AAAA"),
            "an out-of-range runtime position must not overwrite content",
        )
        self.assertTrue(
            all(offset <= 4 for offset in touched_dword_offsets(report, 0)),
            "guarded writes must not touch bytes past the reserved terminator slot",
        )

        def overrun():
            s = FixedString(4)
            s.write("AAAA")
            s.writeFrom(Var(2), "XYZ")

        report = run_sim(build_module(overrun).to_json())
        self.assertEqual(read_string_bytes(report, 0), list(b"AAXY"))
        self.assertTrue(
            all(offset <= 4 for offset in touched_dword_offsets(report, 0)),
            "a partially fitting runtime write must stop at capacity",
        )

    def test_adjacent_strings_stay_isolated_at_capacity(self):
        def prog():
            a = FixedString(4)
            a.write("abcd")
            b = FixedString(4)
            b.write("wxyz")
            a.display()
            b.display()

        mod = build_module(prog)
        parsed = json.loads(mod.to_json())
        self.assertEqual(
            parsed["module"]["strings"][0], " " * 5 + "\x7fEUDString#0\x7f"
        )
        self.assertEqual(
            parsed["module"]["strings"][1], " " * 5 + "\x7fEUDString#1\x7f"
        )

        report = run_sim(mod.to_json())
        self.assertEqual(read_string_bytes(report, 0), list(b"abcd"))
        self.assertEqual(read_string_bytes(report, 1), list(b"wxyz"))
        self.assertEqual(
            touched_dword_offsets(report, 0),
            [0, 4],
            "the first string's writes must stop at its terminator slot",
        )

    def test_clear_on_a_small_string_masks_to_size_plus_one_bytes(self):
        def prog():
            s = FixedString(1)
            s.write("A")
            s.clear()

        parsed = json.loads(build_module(prog).to_json())
        acts = flatten_acts(parsed["module"]["stmts"])
        clears = [
            act
            for act in acts
            if act.get("mask") == 0xFFFF
            and any(
                reloc.get("target_string") == 0
                and reloc.get("field_offset") == 16
                and reloc.get("mode") == 1
                for reloc in act.get("relocs", [])
            )
        ]
        self.assertEqual(
            len(clears), 1, "FixedString(1).clear() must use a 2-byte mask"
        )

        report = run_sim(build_module(prog).to_json())
        self.assertEqual(touched_dword_offsets(report, 0), [0])
        self.assertEqual(read_dword(report, string_epd(0)) & 0xFFFF, 0)


if __name__ == "__main__":
    unittest.main()
