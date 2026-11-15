"""Ticket #85: Performance warning (EUDSL-WARN-PERF) for multi-variable bare decimal format strings."""

from __future__ import annotations

import json
import unittest
import warnings

from py import (
    EudslWarning,
    FixedString,
    Var,
    build_module,
    on_warning,
    parse_format,
)
from py.tests.helpers import run_sim, string_epd


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


def capture_warnings(action):
    captured = []
    unsubscribe = on_warning(lambda code, msg: captured.append((code, msg)))
    try:
        action()
    finally:
        unsubscribe()
    return captured


class Ticket85PerformanceWarningTest(unittest.TestCase):
    def test_multi_bare_decimals_emit_warning(self):
        cases = [
            "{} {}",
            "{:d} {:d}",
            "{} {:d}",
            "{:d} {}",
            "wave={} hp={}",
            "a={} b={} c={}",
            "val1={:d} val2={:d} val3={:d}",
            "prefix {} middle {:d} suffix",
        ]
        for fmt in cases:
            captured = capture_warnings(lambda fmt=fmt: parse_format(fmt, "test"))
            self.assertEqual(
                len(captured),
                1,
                f"Expected exactly 1 warning for {fmt!r}, got {len(captured)}",
            )
            code, msg = captured[0]
            self.assertEqual(code, "EUDSL-WARN-PERF")
            self.assertIn("EUDSL-WARN-PERF", msg)
            self.assertIn("~688T to ~51T", msg)
            self.assertIn("{:\\x1a>4d}", msg)

    def test_single_or_zero_bare_decimal_no_warning(self):
        cases = [
            "hello world",
            "{}",
            "{:d}",
            "wave={}",
            "hp={:d}",
            "score: {}",
        ]
        for fmt in cases:
            captured = capture_warnings(lambda fmt=fmt: parse_format(fmt, "test"))
            self.assertEqual(
                len(captured),
                0,
                f"Expected 0 warnings for {fmt!r}, got {len(captured)}",
            )

    def test_hex_placeholders_no_warning(self):
        cases = [
            "{:x}",
            "{:x} {:x}",
            "{:08x} {:08x}",
            "addr1={:x} addr2={:x}",
            "{:x} {}",
            "{} {:x}",
        ]
        for fmt in cases:
            captured = capture_warnings(lambda fmt=fmt: parse_format(fmt, "test"))
            self.assertEqual(
                len(captured),
                0,
                f"Expected 0 warnings for hex {fmt!r}, got {len(captured)}",
            )

    def test_fixed_width_decimals_no_warning(self):
        cases = [
            "{:4d} {:4d}",
            "{:04d} {:04d}",
            "{:\x1a>4d} {:\x1a>4d}",
            "wave={:\x1a>4d} hp={:\x1a>4d}",
            "{:04d} {}",
            "{} {:4d}",
            "{:>4d} {:>4d}",
            "{:10d} {:10d}",
        ]
        for fmt in cases:
            captured = capture_warnings(lambda fmt=fmt: parse_format(fmt, "test"))
            self.assertEqual(
                len(captured),
                0,
                f"Expected 0 warnings for fixed-width {fmt!r}, got {len(captured)}",
            )

    def test_warnings_warn_integration(self):
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            parse_format("wave={} hp={}", "test")
            self.assertEqual(len(w), 1)
            self.assertTrue(issubclass(w[0].category, UserWarning))
            self.assertIn("EUDSL-WARN-PERF", str(w[0].message))
            self.assertIn("~688T to ~51T", str(w[0].message))

    def test_compilation_and_simulation_succeeds(self):
        captured = []
        unsubscribe = on_warning(lambda code, msg: captured.append((code, msg)))
        try:
            def prog():
                hud = FixedString(32)
                wave = Var(42)
                hp = Var(100)
                hud.write("wave={} hp={}", wave, hp)

            mod = build_module(prog)
            report = run_sim(mod.to_json())
            rendered = read_text(report)
            self.assertEqual(rendered, "wave=42 hp=100")
        finally:
            unsubscribe()

        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0][0], "EUDSL-WARN-PERF")

    def test_ts_and_py_identical_warnings_and_hir(self):
        import subprocess
        from pathlib import Path

        repo_root = Path(__file__).resolve().parent.parent.parent
        ts_code = """
import { FixedString, Var, buildModule, onWarning } from "./ts/index.ts";
const captured = [];
onWarning((w) => captured.push([w.code, w.message]));
const mod = buildModule(() => {
  const hud = new FixedString(32);
  const wave = Var(42);
  const hp = Var(100);
  hud.write("wave={} hp={}", wave, hp);
});
process.stdout.write(JSON.stringify({ warnings: captured, hir: JSON.parse(mod.toJson()) }));
"""
        out = subprocess.check_output(
            ["node", "--experimental-strip-types", "--no-warnings", "--input-type=module", "-e", ts_code],
            cwd=repo_root,
            text=True,
        )
        parsed = json.loads(out)
        captured = []
        unsubscribe = on_warning(lambda code, msg: captured.append([code, msg]))
        try:
            def prog():
                hud = FixedString(32)
                wave = Var(42)
                hp = Var(100)
                hud.write("wave={} hp={}", wave, hp)

            mod = build_module(prog)
            py_hir = json.loads(mod.to_json())
        finally:
            unsubscribe()

        self.assertEqual(len(captured), 1)
        self.assertEqual(parsed["warnings"], captured)
        self.assertEqual(parsed["hir"], py_hir)
