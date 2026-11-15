"""Ticket #80: Direct 4-test digit extraction engine with compile-time lane pre-shifting."""

from __future__ import annotations

import json
import unittest

from py import (
    Always,
    FixedString,
    Var,
    build_module,
    when,
)
from py.tests.helpers import run_sim, string_epd
from py.view import EudslError


def _varstore_count(stmts: list[dict]) -> int:
    return sum(1 for stmt in stmts if stmt.get("kind") == "varstore")


def read_text(report: dict, str_index: int = 0) -> str:
    raw = read_bytes(report, str_index)
    if 0 in raw:
        raw = raw[: raw.index(0)]
    return bytes(raw).decode("latin-1")


def read_bytes(report: dict, str_index: int = 0) -> list[int]:
    epds = report.get("external_epds", {})
    epd_base = string_epd(str_index)
    out: list[int] = []
    for dword_idx in range(64):
        val = epds.get(str(epd_base + dword_idx), 0)
        out.extend(list(val.to_bytes(4, "little")))
    return out


class Ticket80DirectDigitExtractionTest(unittest.TestCase):
    def test_variable_width_decimal_edge_cases(self):
        cases = [
            (0, "0"),
            (9, "9"),
            (99, "99"),
            (999, "999"),
            (1000, "1000"),
            (4294967295, "4294967295"),
        ]
        for val, expected in cases:
            # Test bare {}
            mod = build_module(lambda v=val: FixedString(16).write("{}", Var(v)))
            rep = run_sim(mod.to_json())
            self.assertEqual(read_text(rep, 0), expected, f"bare {{}} failed for {val}")

            # Test {:d}
            mod = build_module(lambda v=val: FixedString(16).write("{:d}", Var(v)))
            rep = run_sim(mod.to_json())
            self.assertEqual(read_text(rep, 0), expected, f"{{:d}} failed for {val}")

    def test_fixed_width_zero_pad_edge_cases(self):
        cases = [
            (0, "0000"),
            (9, "0009"),
            (99, "0099"),
            (999, "0999"),
            (1000, "1000"),
            (4294967295, "7295"),  # modulo 10^4 wrap
        ]
        for val, expected in cases:
            mod = build_module(lambda v=val: FixedString(16).write("{:04d}", Var(v)))
            rep = run_sim(mod.to_json())
            self.assertEqual(read_text(rep, 0), expected, f"{{:04d}} failed for {val}")

    def test_fixed_width_space_pad_edge_cases(self):
        cases = [
            (0, "   0"),  # units place NEVER suppresses
            (9, "   9"),
            (99, "  99"),
            (999, " 999"),
            (1000, "1000"),
            (4294967295, "7295"),  # modulo 10^4 wrap
        ]
        for val, expected in cases:
            mod = build_module(lambda v=val: FixedString(16).write("{:4d}", Var(v)))
            rep = run_sim(mod.to_json())
            self.assertEqual(read_text(rep, 0), expected, f"{{:4d}} failed for {val}")

    def test_fixed_width_pad_byte_edge_cases(self):
        cases = [
            (0, "\x1a\x1a\x1a0"),  # units place NEVER suppresses
            (9, "\x1a\x1a\x1a9"),
            (99, "\x1a\x1a99"),
            (999, "\x1a999"),
            (1000, "1000"),
            (4294967295, "7295"),  # modulo 10^4 wrap
        ]
        for val, expected in cases:
            mod = build_module(lambda v=val: FixedString(16).write("{:\x1a>4d}", Var(v)))
            rep = run_sim(mod.to_json())
            self.assertEqual(read_text(rep, 0), expected, f"{{:\\x1a>4d}} failed for {val}")

    def test_dynamic_fixed_width_decimal_edge_cases(self):
        cases = [
            ("{:04d}", 42, "0042"),
            ("{:4d}", 42, "  42"),
            ("{:\x1a>4d}", 42, "\x1a\x1a42"),
            ("{:04d}", 12345, "2345"),  # modulo 10^4 wrap
            ("{:4d}", 0, "   0"),  # units place NEVER suppresses
        ]
        for fmt, val, expected in cases:
            with self.subTest(fmt=fmt, val=val):
                def prog(fmt=fmt, val=val):
                    s = FixedString(16)
                    start = Var(2)
                    s.writeFrom(start, fmt, Var(val))

                rep = run_sim(build_module(prog).to_json())
                raw = bytes(read_bytes(rep)[2 : 2 + len(expected)])
                self.assertEqual(raw.decode("latin-1"), expected)

    def test_fixed_width_width_boundaries(self):
        # Width 1 (always units place, never suppresses)
        cases_w1 = [(0, "0"), (7, "7"), (42, "2")]
        for val, expected in cases_w1:
            mod = build_module(lambda v=val: FixedString(4).write("{:1d}", Var(v)))
            rep = run_sim(mod.to_json())
            self.assertEqual(read_text(rep, 0), expected, f"{{:1d}} failed for {val}")

        # Width 10 with space pad
        cases_w10_space = [
            (0, "         0"),
            (4294967295, "4294967295"),
        ]
        for val, expected in cases_w10_space:
            mod = build_module(lambda v=val: FixedString(16).write("{:10d}", Var(v)))
            rep = run_sim(mod.to_json())
            self.assertEqual(read_text(rep, 0), expected, f"{{:10d}} failed for {val}")

        # Width 10 with zero pad
        cases_w10_zero = [
            (0, "0000000000"),
            (4294967295, "4294967295"),
        ]
        for val, expected in cases_w10_zero:
            mod = build_module(lambda v=val: FixedString(16).write("{:010d}", Var(v)))
            rep = run_sim(mod.to_json())
            self.assertEqual(read_text(rep, 0), expected, f"{{:010d}} failed for {val}")

    def test_static_pos_preservation_and_following_tokens(self):
        def prog():
            s = FixedString(32)
            s.write("hp={:04d}/{}", Var(50), Var(100))

        mod = build_module(prog)
        rep = run_sim(mod.to_json())
        self.assertEqual(read_text(rep, 0), "hp=0050/100")

    def test_exact_capacity_fit(self):
        def prog():
            s = FixedString(4)
            s.write("{:04d}", Var(42))

        mod = build_module(prog)
        rep = run_sim(mod.to_json())
        self.assertEqual(read_text(rep, 0), "0042")

    def test_fixed_width_constant_wraps_like_var(self):
        cases = [
            ("{:4d}", 0, "   0"),
            ("{:04d}", 0, "0000"),
            ("{:4d}", 12345, "2345"),
            ("{:04d}", 12345, "2345"),
            ("{:*>4d}", 42, "**42"),
            ("{:*>4d}", 0, "***0"),
            ("{:A>4d}", 42, "AA42"),
            ("{:A>4d}", 0, "AAA0"),
            ("{:~>4d}", 7, "~~~7"),
            ("{:10d}", 4294967295, "4294967295"),
        ]
        for fmt, val, expected in cases:
            with self.subTest(fmt=fmt, val=val):
                const_mod = build_module(
                    lambda fmt=fmt, val=val: FixedString(16).write(fmt, val)
                )
                var_mod = build_module(
                    lambda fmt=fmt, val=val: FixedString(16).write(fmt, Var(val))
                )
                const_rep = run_sim(const_mod.to_json())
                var_rep = run_sim(var_mod.to_json())
                self.assertEqual(read_text(const_rep, 0), expected)
                self.assertEqual(read_text(var_rep, 0), expected)

    def test_fixed_width_constant_capacity_fit_and_bounds(self):
        mod = build_module(lambda: FixedString(8).write("{:4d}", 12345))
        self.assertEqual(read_text(run_sim(mod.to_json()), 0), "2345")

        mod = build_module(lambda: FixedString(4).write("{:4d}", 12345))
        self.assertEqual(read_text(run_sim(mod.to_json()), 0), "2345")

        with self.assertRaisesRegex(EudslError, "EUDSL-BOUNDS"):
            build_module(lambda: FixedString(3).write("{:4d}", 12345))

    def test_static_fixed_width_decimal_has_no_pointer_patching(self):
        captured: list[FixedString] = []

        def prog():
            s = FixedString(16)
            captured.append(s)
            s.write("{:\x1a>4d}", Var(42))

        mod = build_module(prog)
        s = captured[0]
        self.assertIsNone(
            s._pos_var,
            "static fixed-width decimal must not instantiate a runtime pos var",
        )

        stmts = json.loads(mod.to_json())["module"]["stmts"]
        self.assertEqual(
            _varstore_count(stmts),
            0,
            "static fixed-width decimal must not patch trigger action fields",
        )
        self.assertFalse(
            any(stmt.get("kind") == "when" for stmt in stmts),
            "static fixed-width decimal must not branch on a runtime byte lane",
        )

        report = run_sim(mod.to_json())
        self.assertEqual(read_text(report, 0), "\x1a\x1a42")

    def test_static_fixed_width_decimal_folds_at_every_byte_lane(self):
        cases = [(42, "0042"), (0, "0000"), (4294967295, "7295")]
        for lane in range(4):
            for val, expected in cases:
                with self.subTest(lane=lane, val=val):
                    captured: list[FixedString] = []

                    def prog(lane=lane, val=val):
                        s = FixedString(lane + 4)
                        captured.append(s)
                        s.writeFrom(lane, "{:04d}", Var(val))

                    mod = build_module(prog)
                    s = captured[0]
                    self.assertIsNone(
                        s._pos_var,
                        f"lane {lane}: static fixed-width decimal must not "
                        "instantiate a runtime pos var",
                    )
                    stmts = json.loads(mod.to_json())["module"]["stmts"]
                    self.assertEqual(
                        _varstore_count(stmts),
                        0,
                        f"lane {lane}: static fixed-width decimal must not "
                        "patch trigger action fields",
                    )

                    raw = read_bytes(run_sim(mod.to_json()))
                    self.assertEqual(
                        bytes(raw[lane : lane + 4]).decode("latin-1"),
                        expected,
                        f"lane {lane}: digits must render at their compile-time "
                        "byte lane",
                    )
                    self.assertEqual(
                        raw[lane + 4],
                        0,
                        f"lane {lane}: the terminator must sit at capacity",
                    )

    def test_static_fixed_width_decimal_masks_neighbours(self):
        captured: list[FixedString] = []

        def prog():
            s = FixedString(16)
            captured.append(s)
            s.write("a{:04d}b{:\x1a>4d}c", Var(42), Var(7))

        mod = build_module(prog)
        s = captured[0]
        self.assertIsNone(s._pos_var)
        stmts = json.loads(mod.to_json())["module"]["stmts"]
        self.assertEqual(_varstore_count(stmts), 0)

        raw = read_bytes(run_sim(mod.to_json()))
        self.assertEqual(
            bytes(raw[:11]).decode("latin-1"), "a0042b\x1a\x1a\x1a7c"
        )

    def test_multi_loop_reset(self):
        def prog():
            s = FixedString(16)
            s.write("{:04d}", Var(42))

        mod = build_module(prog)
        rep = run_sim(mod.to_json(), loops=3)
        self.assertEqual(read_text(rep, 0), "0042")

    def test_hir_parity_with_ts(self):
        import subprocess
        from pathlib import Path

        repo_root = Path(__file__).resolve().parent.parent.parent

        def prog():
            s1 = FixedString(64)
            s1.write("hp={:04d}/{}", Var(50), Var(100))
            s1.append("|pad={:\x1a>4d}|space={:4d}", Var(7), Var(9))

            s2 = FixedString(16)
            s2.write("{:10d}", Var(12345))

            s3 = FixedString(16)
            s3.write("{:d}", Var(0))

            s4 = FixedString(16)
            start = Var(2)
            s4.writeFrom(start, "{:04d}", Var(42))

            s5 = FixedString(32)
            s5.write("wrap={:4d}|star={:*>4d}", 12345, 42)
            s5.append("{:04d}", 0)
            s5.append("{:*>4d}", Var(7))

        py_json = build_module(prog).to_json()

        ts_script = """
import { FixedString, Var, buildModule } from "./ts/index.ts";
const tsMod = buildModule(() => {
  const s1 = new FixedString(64);
  s1.write("hp={:04d}/{}", Var(50), Var(100));
  s1.append("|pad={:\\x1a>4d}|space={:4d}", Var(7), Var(9));

  const s2 = new FixedString(16);
  s2.write("{:10d}", Var(12345));

  const s3 = new FixedString(16);
  s3.write("{:d}", Var(0));

  const s4 = new FixedString(16);
  const start = Var(2);
  s4.writeFrom(start, "{:04d}", Var(42));

  const s5 = new FixedString(32);
  s5.write("wrap={:4d}|star={:*>4d}", 12345, 42);
  s5.append("{:04d}", 0);
  s5.append("{:*>4d}", Var(7));
});
process.stdout.write(tsMod.toJson());
"""
        ts_json = subprocess.check_output(
            ["node", "--input-type=module", "-e", ts_script],
            cwd=str(repo_root),
            text=True,
        )
        self.assertEqual(py_json, ts_json)


if __name__ == "__main__":
    unittest.main()
