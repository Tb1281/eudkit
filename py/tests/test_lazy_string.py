"""``lazyFixedString`` public API tests (globals P4).

P4 uses a wrapper plus explicit method delegation (ADR 0005 D8), not a
subclass and not a deferred flag on ``FixedString``: a handle declared
anywhere (no builder needed) that resolves to a fully eager binding on
first resolving use inside ``build_module``, one binding per builder. The
eager ``FixedString`` is unchanged; the lazy factory is opt-in only.

Compatible with both ``pytest`` and ``python3 -m unittest``.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any, Callable

from py import (
    FixedString,
    Var,
    build_module,
    lazyFixedString,
    lazy_fixed_string,
    when,
)
from py.tests import lazy_string_fixture as fixture
from py.tests.helpers import (
    flatten_stmts,
    raw_string_bytes,
    run_sim,
    string_dword_bytes,
)
from py.view import EudslError

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
EUDKIT_COMPILE = REPO_ROOT / "target" / "debug" / "eudkit-compile"


def run_dump_ir(hir_json: str) -> str:
    # Full pipeline comparison: `dump-ir` runs optimize_module then
    # lowering, so it exercises the storage analysis and the rendered
    # LIR. The comparison is the ENTIRE dump-ir output (storage report
    # including the per-var Allocations listing, plus the rendered LIR).
    with tempfile.TemporaryDirectory(prefix="eudkit-py-p4-") as tmpdir:
        hir_path = Path(tmpdir) / "prog.hir.json"
        hir_path.write_text(hir_json, encoding="utf-8")
        out = subprocess.check_output(
            [str(EUDKIT_COMPILE), "dump-ir", str(hir_path)],
            text=True,
        )
        assert "LIR v3.0" in out, "dump-ir output must contain an LIR section"
        assert "Allocations:" in out, "dump-ir output must contain the Allocations listing"
        return out


def _varstore_count(stmts: list[dict[str, Any]]) -> int:
    return sum(1 for stmt in flatten_stmts(stmts) if stmt.get("kind") == "varstore")


def _string_write_acts(stmts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Every action that writes through the module string table."""
    out: list[dict[str, Any]] = []
    for stmt in flatten_stmts(stmts):
        if isinstance(stmt.get("acts"), list):
            out.extend(stmt["acts"])
        elif stmt.get("kind") == "act":
            out.append(stmt)
    return [
        act
        for act in out
        if any(r.get("target_string") is not None for r in act.get("relocs", []))
    ]


def _bit_test_triggers(
    stmts: list[dict[str, Any]], target_offset: int = 20
) -> list[dict[str, Any]]:
    """The 32 value-bit tests for the fixture's ``val`` variable (var 0)."""
    return [
        stmt
        for stmt in stmts
        if stmt.get("kind") == "trigger"
        and any(
            c.get("condtype") == 15
            and any(
                r.get("target_var") == 0 and r.get("target_offset") == target_offset
                for r in c.get("relocs", [])
            )
            for c in stmt.get("conds", [])
        )
    ]


def hir_json(mod: Any) -> dict[str, Any]:
    return json.loads(mod.to_json())


def decls(mod: Any) -> list[list[int]]:
    return [[v["id"], v["init"]] for v in hir_json(mod)["module"]["vars"]]


def assert_code(test: unittest.TestCase, fn: Callable[[], Any], code: str) -> None:
    with test.assertRaises(EudslError) as cm:
        fn()
    test.assertEqual(cm.exception.code, code)


def error_of(fn: Callable[[], Any]) -> EudslError:
    try:
        fn()
    except EudslError as exc:
        return exc
    raise AssertionError("expected the call to throw")


def _walk_stmts(stmts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Recursively flatten HIR statements, including temporal bodies."""
    out: list[dict[str, Any]] = []
    for stmt in stmts:
        out.append(stmt)
        out.extend(_walk_stmts(stmt.get("body") or []))
        out.extend(_walk_stmts(stmt.get("else_body") or []))
    return out


def _varstore_count(stmts: list[dict[str, Any]]) -> int:
    return sum(1 for stmt in _walk_stmts(stmts) if stmt.get("kind") == "varstore")


def _string_write_acts(stmts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Every action that writes through the module string table."""
    out: list[dict[str, Any]] = []
    for stmt in _walk_stmts(stmts):
        if isinstance(stmt.get("acts"), list):
            out.extend(stmt["acts"])
        elif stmt.get("kind") == "act":
            out.append(stmt)
    return [
        act
        for act in out
        if any(r.get("target_string") is not None for r in act.get("relocs", []))
    ]


def assert_dump_equal(
    test: unittest.TestCase,
    lazy_body: Callable[[], Any],
    eager_body: Callable[[], Any],
    label: str,
) -> None:
    lazy_mod = build_module(lazy_body)
    eager_mod = build_module(eager_body)
    test.assertEqual(
        run_dump_ir(lazy_mod.to_json()),
        run_dump_ir(eager_mod.to_json()),
        f"{label} must be dump-ir-identical to its eager twin",
    )


class LazyFixedStringTest(unittest.TestCase):
    def test_declaration_outside_build_module_allocates_nothing(self) -> None:
        s = lazyFixedString(16)
        self.assertEqual(s.size, 16)

        def body() -> None:
            v = Var(0)
            v.set(1)

        with_handle = build_module(body)
        without_handle = build_module(body)
        # Identical output proves the unused handle consumed no string
        # index, emitted no decls/flushers, and no statements.
        self.assertEqual(hir_json(with_handle), hir_json(without_handle))
        self.assertEqual(decls(with_handle), [[0, 0]])
        self.assertIn(
            "Total Variables: 1",
            run_dump_ir(with_handle.to_json()),
            "an unused handle must not add variables",
        )

    def test_resolving_reads_and_methods_outside_builder_throw(self) -> None:
        s = lazyFixedString(8)
        assert_code(self, lambda: s.message_index, "EUDSL-SCOPE")
        assert_code(self, lambda: s.write("x"), "EUDSL-SCOPE")
        assert_code(self, lambda: s.append("x"), "EUDSL-SCOPE")
        assert_code(self, lambda: s.writeFrom(0, "x"), "EUDSL-SCOPE")
        assert_code(self, lambda: s.write_from(0, "x"), "EUDSL-SCOPE")
        assert_code(self, lambda: s.print("x"), "EUDSL-SCOPE")
        assert_code(self, lambda: s.printAt(0, "x"), "EUDSL-SCOPE")
        assert_code(self, lambda: s.print_at(0, "x"), "EUDSL-SCOPE")
        assert_code(self, lambda: s.display(), "EUDSL-SCOPE")
        assert_code(self, lambda: s.displayAt(0), "EUDSL-SCOPE")
        assert_code(self, lambda: s.display_at(0), "EUDSL-SCOPE")
        assert_code(self, lambda: s.clear(), "EUDSL-SCOPE")

    def test_bounds_errors_match_eager_at_declaration(self) -> None:
        for size in (-1, 0, 1.5, 65536, float("nan"), "8"):
            lazy_error = error_of(lambda size=size: lazyFixedString(size))
            eager_error = error_of(lambda size=size: FixedString(size))
            self.assertEqual(lazy_error.code, "EUDSL-BOUNDS", repr(size))
            self.assertEqual(str(lazy_error), str(eager_error), repr(size))

    def test_first_use_binding_equals_eager_twin(self) -> None:
        lazy = lazyFixedString(16)

        def body() -> None:
            lazy.write("Hi")
            out = Var(0)
            out.set(lazy.message_index)

        def twin_body() -> None:
            eager = FixedString(16)
            eager.write("Hi")
            out = Var(0)
            out.set(eager.message_index)

        mod = build_module(body)
        twin = build_module(twin_body)
        self.assertEqual(hir_json(mod), hir_json(twin))
        self.assertEqual(len(hir_json(mod)["module"]["strings"]), 1)

    def test_core_operations_match_eager_twin(self) -> None:
        assert_dump_equal(
            self,
            lambda: _static_operations(),
            lambda: _static_operations(eager=True),
            "static FixedString operations",
        )

        assert_dump_equal(
            self,
            lambda: _runtime_operations(),
            lambda: _runtime_operations(eager=True),
            "runtime FixedString operations",
        )

    def test_out_of_order_first_use_follows_first_use_order(self) -> None:
        a = lazyFixedString(8)
        b = lazyFixedString(8)

        def lazy_body() -> None:
            b.write("B")
            a.write("A")

        def b_first() -> None:
            bb = FixedString(8)
            bb.write("B")
            aa = FixedString(8)
            aa.write("A")

        def a_first() -> None:
            aa = FixedString(8)
            aa.write("A")
            bb = FixedString(8)
            bb.write("B")

        lazy_ir = run_dump_ir(build_module(lazy_body).to_json())
        self.assertEqual(
            lazy_ir,
            run_dump_ir(build_module(b_first).to_json()),
            "B-first eager twin must match",
        )
        self.assertNotEqual(
            lazy_ir,
            run_dump_ir(build_module(a_first).to_json()),
            "declaration-order twin must differ",
        )

    def test_conditional_first_use_matches_same_scope_eager_twin(self) -> None:
        lazy = lazyFixedString(8)

        def lazy_body() -> None:
            flag = Var(1)
            when(flag.eq(1), lambda: lazy.write("A"))
            lazy.append("B")
            lazy.display()

        eager: dict[str, Any] = {}

        def eager_body() -> None:
            flag = Var(1)

            def body() -> None:
                eager["handle"] = FixedString(8)
                eager["handle"].write("A")

            when(flag.eq(1), body)
            eager["handle"].append("B")
            eager["handle"].display()

        self.assertEqual(
            run_dump_ir(build_module(lazy_body).to_json()),
            run_dump_ir(build_module(eager_body).to_json()),
            "conditional first use must match the same-scope eager twin",
        )

    def test_never_used_handles_emit_nothing(self) -> None:
        s = lazyFixedString(8)

        def body() -> None:
            v = Var(0)
            v.set(1)

        with_handle = build_module(body)
        without_handle = build_module(body)
        self.assertEqual(hir_json(with_handle), hir_json(without_handle))
        self.assertEqual(decls(with_handle), [[0, 0]])
        self.assertNotIn("strings", hir_json(with_handle)["module"])
        self.assertIsNotNone(s)

    def test_two_builder_use_creates_separate_bindings(self) -> None:
        lazy = lazyFixedString(8)
        indices: dict[str, int] = {}

        def body1() -> None:
            lazy.write("A")
            indices["first"] = lazy.message_index

        def body2() -> None:
            FixedString(8).write("seed")
            lazy.write("B")
            indices["second"] = lazy.message_index

        def twin1() -> None:
            eager = FixedString(8)
            eager.write("A")

        def twin2() -> None:
            FixedString(8).write("seed")
            eager = FixedString(8)
            eager.write("B")

        mod1 = build_module(body1)
        mod2 = build_module(body2)
        self.assertEqual(hir_json(mod1), hir_json(build_module(twin1)), "builder 1 twin")
        self.assertEqual(hir_json(mod2), hir_json(build_module(twin2)), "builder 2 twin")
        self.assertNotEqual(
            indices["first"],
            indices["second"],
            "bindings must have separate string indices",
        )

    def test_post_build_single_record_rule_and_no_post_build_allocation(self) -> None:
        single = lazyFixedString(8)
        captured: dict[str, int] = {}

        def single_body() -> None:
            single.write("A")
            captured["index"] = single.message_index

        build_module(single_body)
        self.assertEqual(
            single.message_index, captured["index"], "single record must be cached"
        )

        multi = lazyFixedString(8)
        build_module(lambda: multi.write("A"))
        build_module(lambda: multi.write("B"))
        assert_code(self, lambda: multi.message_index, "EUDSL-SCOPE")

        never = lazyFixedString(8)
        assert_code(self, lambda: never.message_index, "EUDSL-SCOPE")

        # A post-build throw and a post-build cached read must both
        # allocate nothing: the next build still starts at id 0 with one
        # var and no strings.
        def after_body() -> None:
            v = Var(0)
            v.set(1)

        after = build_module(after_body)
        self.assertEqual(decls(after), [[0, 0]])
        self.assertNotIn("strings", hir_json(after)["module"])

    def test_cross_file_handle_matches_eager_twin(self) -> None:
        assert_dump_equal(
            self,
            lambda: _cross_file_lazy(),
            lambda: _cross_file_eager(),
            "cross-file FixedString",
        )

    def test_byte_identity_corpus(self) -> None:
        cases: list[tuple[str, Callable[[], Any], Callable[[], Any]]] = []

        s1 = lazyFixedString(32)

        def lazy1() -> None:
            s1.write("abc")
            s1.append("de")
            s1.writeFrom(2, "Z")
            out = Var(0)
            out.set(s1.message_index)
            s1.clear()
            s1.write("done")
            s1.display()

        def eager1() -> None:
            e = FixedString(32)
            e.write("abc")
            e.append("de")
            e.writeFrom(2, "Z")
            out = Var(0)
            out.set(e.message_index)
            e.clear()
            e.write("done")
            e.display()

        cases.append(("write/append/writeFrom/clear/messageIndex", lazy1, eager1))

        s2 = lazyFixedString(32)

        def lazy2() -> None:
            s2.print("hello")
            s2.printAt(3, "{}", 0x2A)
            s2.displayAt(0)

        def eager2() -> None:
            e = FixedString(32)
            e.print("hello")
            e.printAt(3, "{}", 0x2A)
            e.displayAt(0)

        cases.append(("print and printAt", lazy2, eager2))

        s3 = lazyFixedString(32)

        def lazy3() -> None:
            value = Var(0x41)
            pos = Var(2)
            s3.write("A{}", value)
            s3.append(value)
            s3.write_from(pos, "Q")
            s3.print_at(1, "{}", value)
            s3.display_at(pos)

        def eager3() -> None:
            value = Var(0x41)
            pos = Var(2)
            e = FixedString(32)
            e.write("A{}", value)
            e.append(value)
            e.write_from(pos, "Q")
            e.print_at(1, "{}", value)
            e.display_at(pos)

        cases.append(("runtime values, position, and aliases", lazy3, eager3))

        first = lazyFixedString(16)
        second = lazyFixedString(16)

        def lazy4() -> None:
            second.write("B")
            first.write("A")
            second.append("b")
            first.append("a")
            first.display()
            second.display()

        def eager4() -> None:
            e2 = FixedString(16)
            e2.write("B")
            e1 = FixedString(16)
            e1.write("A")
            e2.append("b")
            e1.append("a")
            e1.display()
            e2.display()

        cases.append(("two strings, first-use/flusher order", lazy4, eager4))

        s5 = lazyFixedString(16)

        def lazy5() -> None:
            value = Var(0x42)
            s5.write("abc")
            s5.write(value)
            s5.append("f")
            s5.display()

        def eager5() -> None:
            value = Var(0x42)
            e = FixedString(16)
            e.write("abc")
            e.write(value)
            e.append("f")
            e.display()

        cases.append(("partial static tail then dynamic byte", lazy5, eager5))

        s6 = lazyFixedString(8)

        def lazy6() -> None:
            s6.write("12345678")
            s6.display()

        def eager6() -> None:
            e = FixedString(8)
            e.write("12345678")
            e.display()

        cases.append(("full buffer terminator", lazy6, eager6))

        for name, lazy_body, eager_body in cases:
            self.assertEqual(
                run_dump_ir(build_module(lazy_body).to_json()),
                run_dump_ir(build_module(eager_body).to_json()),
                f"corpus case {name} must be byte-identical",
            )

    def test_snake_case_alias_is_same_factory(self) -> None:
        self.assertIs(lazy_fixed_string, lazyFixedString)

    def test_direct_hex_bit_accumulation_no_shr(self) -> None:
        def prog():
            s = FixedString(16)
            v = Var(0xDEADBEEF)
            s.write("{:x}", v)

        mod = build_module(prog)
        data = json.loads(mod.to_json())
        stmts = data["module"]["stmts"]

        bit_test_trigs = _bit_test_triggers(stmts)

        self.assertEqual(
            len(bit_test_trigs),
            32,
            "Direct bit-accumulation must emit exactly 32 bit-test triggers for val",
        )
        for trig in bit_test_trigs:
            self.assertEqual(
                len(trig.get("acts", [])),
                2,
                "Each bit-test trigger must update both target dword and nibble",
            )

        sim_report = run_sim(mod.to_json())
        self.assertEqual(string_dword_bytes(sim_report, 2), b"deadbeef")

    def test_constant_offset_folding_multi_placeholder_lane_aligned(self) -> None:
        captured_string: list[FixedString] = []

        def prog():
            s = FixedString(32)
            captured_string.append(s)
            w = Var(0x12345678)
            h = Var(0x00000064)
            s.write("W:  {:x} H: {:x}", w, h)

        mod = build_module(prog)
        s = captured_string[0]
        self.assertIsNone(
            s._pos_var,
            "Lane-aligned fixed-width format runs must not instantiate runtime pos var",
        )
        self.assertEqual(s._static_pos, 24)

        data = json.loads(mod.to_json())
        stmts = data["module"]["stmts"]

        self.assertEqual(
            _varstore_count(stmts),
            0,
            "constant-offset folding must not patch trigger action fields",
        )
        init_writes = [
            act
            for act in _string_write_acts(stmts)
            if act.get("acttype") == 45
            and act.get("modifier") == 7
            and act.get("mask") == 0
            and act.get("value") == 0x30303030
            and any(
                r.get("target_string") is not None for r in act.get("relocs", [])
            )
        ]
        self.assertEqual(
            sorted(
                next(
                    r["target_offset"]
                    for r in act["relocs"]
                    if r.get("target_string") is not None
                )
                for act in init_writes
            ),
            [4, 8, 16, 20],
            "Must emit exactly 4 direct dword initializers for two 8-byte "
            "hex values",
        )

        sim_report = run_sim(mod.to_json())
        self.assertEqual(
            string_dword_bytes(sim_report, 6).decode("ascii"),
            "W:  12345678 H: 00000064",
        )

    def test_constant_offset_folding_scope_padded(self) -> None:
        captured_string: list[FixedString] = []

        def prog():
            s = FixedString(32)
            captured_string.append(s)
            w = Var(0xDEADBEEF)
            h = Var(0x0000002A)
            s.write("wave={:x} hp={:x}", w, h)

        mod = build_module(prog)
        s = captured_string[0]
        self.assertIsNone(s._pos_var)
        self.assertEqual(s._static_pos, 25)

        sim_report = run_sim(mod.to_json())
        raw = string_dword_bytes(sim_report, 7)
        self.assertEqual(raw[:25].decode("ascii"), "wave=deadbeef hp=0000002a")
        self.assertEqual(raw[25], 0)

    def test_unaligned_multi_placeholder_fallback(self) -> None:
        captured_string: list[FixedString] = []

        def prog():
            s = FixedString(32)
            captured_string.append(s)
            start = Var(0)
            w = Var(0x12345678)
            h = Var(0x00000064)
            s.writeFrom(start, "W:{:x} H:{:x}", w, h)

        mod = build_module(prog)
        s = captured_string[0]
        self.assertIsNotNone(
            s._pos_var,
            "Unaligned multi-placeholder must fall back to dynamic position variable",
        )

        sim_report = run_sim(mod.to_json())
        self.assertEqual(
            string_dword_bytes(sim_report, 6)[:21], b"W:12345678 H:00000064"
        )

    def test_fixed_width_decimal_capacity_uses_declared_width(self) -> None:
        def fits() -> None:
            s = FixedString(4)
            s.write("{:4d}", Var(1))

        build_module(fits)

        def overflows() -> None:
            s = FixedString(3)
            s.write("{:4d}", Var(1))

        assert_code(self, lambda: build_module(overflows), "EUDSL-BOUNDS")

    def test_exact_fit_placeholder_plus_literal_terminates_at_capacity(self) -> None:
        def prog():
            s = FixedString(10)
            s.write("a{:x}b", Var(0xDEADBEEF))

        report = run_sim(build_module(prog).to_json())
        self.assertEqual(
            raw_string_bytes(report, 11),
            [ord("a")] + list(b"deadbeef") + list(b"b") + [0],
            "content must occupy 0..9 and the NUL must sit at offset 10",
        )

    def test_exact_fit_multi_placeholder_terminates_at_capacity(self) -> None:
        def prog():
            s = FixedString(25)
            s.write("wave={:x} hp={:x}", Var(0xDEADBEEF), Var(0x2A))

        report = run_sim(build_module(prog).to_json())
        self.assertEqual(
            raw_string_bytes(report, 26),
            list(b"wave=deadbeef hp=0000002a") + [0],
            "content must occupy 0..24 and the NUL must sit at offset 25",
        )

    def test_unaligned_fold_fits_exact_capacity(self) -> None:
        def prog():
            s = FixedString(10)
            s.write("a{:x}b", Var(0xDEADBEEF))

        report = run_sim(build_module(prog).to_json())
        self.assertEqual(
            raw_string_bytes(report, 11),
            list(b"adeadbeefb") + [0],
            "an unaligned fold must still fit exactly without writing past "
            "the NUL",
        )

    def test_unaligned_fixed_width_hex_folds_to_direct_writes(self) -> None:
        captured: list[FixedString] = []

        def prog():
            s = FixedString(10)
            captured.append(s)
            s.write("a{:x}b", Var(0xDEADBEEF))

        mod = build_module(prog)
        s = captured[0]
        self.assertIsNone(
            s._pos_var,
            "unaligned fixed-width hex must not instantiate a runtime position var",
        )

        stmts = json.loads(mod.to_json())["module"]["stmts"]
        self.assertEqual(
            _varstore_count(stmts),
            0,
            "fixed-width hex must not patch trigger action fields at runtime",
        )

        bit_test_trigs = _bit_test_triggers(stmts)
        self.assertEqual(
            len(bit_test_trigs),
            32,
            "direct folding must emit exactly the 32 value bit tests, "
            "with no pos>>2 triggers",
        )

        offsets = {
            next(
                r["target_offset"]
                for r in act["relocs"]
                if r.get("target_string") is not None
            )
            for act in _string_write_acts(stmts)
        }
        self.assertEqual(
            sorted(offsets),
            [0, 4, 8],
            "an 8-byte value starting at lane 1 must cover dwords 0, 1, and 2",
        )

        report = run_sim(mod.to_json())
        self.assertEqual(
            raw_string_bytes(report, 11),
            list(b"adeadbeefb") + [0],
            "unaligned direct writes must render the value at its byte lane",
        )

    def test_fixed_width_hex_folds_at_every_byte_lane(self) -> None:
        for lane in range(4):
            with self.subTest(lane=lane):
                captured: list[FixedString] = []

                def prog(lane=lane):
                    s = FixedString(lane + 8)
                    captured.append(s)
                    s.writeFrom(lane, "{:x}", Var(0xDEADBEEF))

                mod = build_module(prog)
                s = captured[0]
                self.assertIsNone(
                    s._pos_var,
                    f"lane {lane}: fixed-width hex must not instantiate "
                    "a runtime position var",
                )

                stmts = json.loads(mod.to_json())["module"]["stmts"]
                self.assertEqual(
                    _varstore_count(stmts),
                    0,
                    f"lane {lane}: fixed-width hex must not patch trigger "
                    "action fields",
                )

                bit_test_trigs = _bit_test_triggers(stmts)
                self.assertEqual(
                    len(bit_test_trigs),
                    32,
                    f"lane {lane}: exactly the 32 value bit tests, with no "
                    "pos>>2 triggers",
                )

                init_offsets = {
                    next(
                        r["target_offset"]
                        for r in act["relocs"]
                        if r.get("target_string") is not None
                    )
                    for act in _string_write_acts(stmts)
                    if act.get("value") == 0x30303030
                }
                self.assertEqual(
                    sorted(init_offsets),
                    [0, 4] if lane == 0 else [0, 4, 8],
                    f"lane {lane}: 8 bytes must cover the expected dwords",
                )

                report = run_sim(mod.to_json())
                self.assertEqual(
                    raw_string_bytes(report, lane + 9),
                    [0] * lane + list(b"deadbeef") + [0],
                    f"lane {lane}: value must render at its byte lane",
                )

    def test_write_placeholder_without_padding_room_folds(self) -> None:
        captured: list[FixedString] = []

        def prog():
            s = FixedString(9)
            captured.append(s)
            s.write("a{:x}", Var(0xDEADBEEF))

        mod = build_module(prog)
        self.assertIsNone(captured[0]._pos_var)
        stmts = json.loads(mod.to_json())["module"]["stmts"]
        self.assertEqual(_varstore_count(stmts), 0)
        report = run_sim(mod.to_json())
        self.assertEqual(raw_string_bytes(report, 10), list(b"adeadbeef") + [0])

    def test_mixed_placeholders_fold_without_mid_string_padding(self) -> None:
        captured: list[FixedString] = []

        def prog():
            s = FixedString(22)
            captured.append(s)
            s.write("a{:x}b{:x}c", Var(0xDEADBEEF), Var(0x0000002A))

        mod = build_module(prog)
        s = captured[0]
        self.assertIsNone(s._pos_var)
        self.assertEqual(s._static_pos, 19)
        stmts = json.loads(mod.to_json())["module"]["stmts"]
        self.assertEqual(_varstore_count(stmts), 0)

        report = run_sim(mod.to_json())
        self.assertEqual(
            raw_string_bytes(report, 20),
            list(b"adeadbeefb0000002ac") + [0],
            "both placeholders fold at their compile-time byte lanes",
        )

    def test_decimal_keeps_dynamic_fallback(self) -> None:
        captured: list[FixedString] = []

        def prog():
            s = FixedString(16)
            captured.append(s)
            s.write("{:d}", Var(1234))

        mod = build_module(prog)
        s = captured[0]
        self.assertIsNotNone(
            s._pos_var,
            "decimal formatting still needs the runtime position variable",
        )
        module = json.loads(mod.to_json())["module"]
        stmts = module["stmts"]
        # Variable-width decimal now goes through the one shared `func` body
        # per module, so its `pos >> 2` cursor seed (the dynamic patching
        # fallback) is emitted in `module.funcs` rather than at the call site.
        kernel_stmts = [
            stmt for fn in module.get("funcs") or [] for stmt in fn.get("body") or []
        ]
        self.assertGreater(
            _varstore_count(stmts) + _varstore_count(kernel_stmts),
            0,
            "decimal formatting keeps the documented dynamic patching fallback",
        )

        report = run_sim(mod.to_json())
        self.assertEqual(
            raw_string_bytes(report, 5),
            list(b"1234") + [0],
            "decimal formatting must still render variable-width digits",
        )

    def test_capacity_monotonic_unpadded(self) -> None:
        for size in [10, 11, 12]:
            def prog(sz=size):
                s = FixedString(sz)
                s.write("a{:x}b", Var(0xDEADBEEF))

            mod = build_module(prog)
            with tempfile.TemporaryDirectory(prefix="eudkit-py-mono-") as tmpdir:
                hir_path = Path(tmpdir) / "prog.hir.json"
                hir_path.write_text(mod.to_json(), encoding="utf-8")
                out = subprocess.check_output(
                    [str(EUDKIT_COMPILE), "sim", str(hir_path), "1"], text=True
                ).strip()
                sim_report = json.loads(out)
                epds = sim_report["external_epds"]
                epd_base = (0x20000000 - 0x58A364) // 4
                chars = []
                for d in range(3):
                    val = epds.get(str(epd_base + d), 0)
                    chars.append(val.to_bytes(4, "little"))
                raw = b"".join(chars)
                self.assertEqual(raw[:10], b"adeadbeefb")
                self.assertEqual(raw[10], 0)

        with self.assertRaises(EudslError) as cm:
            def bad():
                s = FixedString(9)
                s.write("a{:x}b", Var(0xDEADBEEF))
            build_module(bad)
        self.assertEqual(cm.exception.code, "EUDSL-BOUNDS")

    def test_capacity_monotonic_multi_placeholder(self) -> None:
        def prog():
            s = FixedString(25)
            s.write("wave={:x} hp={:x}", Var(0xDEADBEEF), Var(0x2A))

        mod = build_module(prog)
        with tempfile.TemporaryDirectory(prefix="eudkit-py-mono2-") as tmpdir:
            hir_path = Path(tmpdir) / "prog.hir.json"
            hir_path.write_text(mod.to_json(), encoding="utf-8")
            out = subprocess.check_output(
                [str(EUDKIT_COMPILE), "sim", str(hir_path), "1"], text=True
            ).strip()
            sim_report = json.loads(out)
            epds = sim_report["external_epds"]
            epd_base = (0x20000000 - 0x58A364) // 4
            chars = []
            for d in range(7):
                val = epds.get(str(epd_base + d), 0)
                chars.append(val.to_bytes(4, "little"))
            raw = b"".join(chars)
            self.assertEqual(raw[:25], b"wave=deadbeef hp=0000002a")
            self.assertEqual(raw[25], 0)

        with self.assertRaises(EudslError) as cm:
            def bad():
                s = FixedString(24)
                s.write("wave={:x} hp={:x}", Var(0xDEADBEEF), Var(0x2A))
            build_module(bad)
        self.assertEqual(cm.exception.code, "EUDSL-BOUNDS")


def _static_operations(eager: bool = False) -> Any:
    s = FixedString(32) if eager else lazyFixedString(32)
    s.write("ab")
    s.append("cd")
    s.writeFrom(1, "Z")
    s.write("")
    s.clear()
    s.write("tail")
    s.display()
    return s


def _runtime_operations(eager: bool = False) -> Any:
    s = FixedString(32) if eager else lazyFixedString(32)
    pos = Var(3)
    value = Var(0x41)
    s.write(value)
    s.append("{}", value)
    s.writeFrom(pos, "Q")
    s.printAt(2, "{}", value)
    s.displayAt(pos)
    return s


def _cross_file_lazy() -> None:
    out = Var(0)
    fixture.s.write("x")
    out.set(fixture.s.message_index)


def _cross_file_eager() -> None:
    out = Var(0)
    eager = FixedString(8)
    eager.write("x")
    out.set(eager.message_index)


if __name__ == "__main__":
    unittest.main()
