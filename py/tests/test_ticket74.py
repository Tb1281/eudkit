"""Ticket #74: Format mini-language parser and decimal default switch."""

from __future__ import annotations

import unittest

from py import (
    EudslError,
    FixedString,
    Var,
    build_module,
)
from py.string import FormatSpec, _parse_format
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


class Ticket74FormatParserTest(unittest.TestCase):
    def test_default_placeholder_defaults_to_decimal(self):
        parts, count = _parse_format("hello {}", "test")
        self.assertEqual(count, 1)
        self.assertEqual(len(parts), 2)
        self.assertEqual(parts[0], ("lit", "hello "))
        self.assertEqual(
            parts[1],
            ("arg", FormatSpec(fill=None, align=None, width=None, radix="dec")),
        )

    def test_explicit_dec_and_hex(self):
        parts, count = _parse_format("{:d} {:x}", "test")
        self.assertEqual(count, 2)
        self.assertEqual(
            parts[0],
            ("arg", FormatSpec(fill=None, align=None, width=None, radix="dec")),
        )
        self.assertEqual(parts[1], ("lit", " "))
        self.assertEqual(
            parts[2],
            ("arg", FormatSpec(fill=None, align=None, width=None, radix="hex")),
        )

    def test_hex_with_explicit_width_8(self):
        cases = [
            ("{:08x}", FormatSpec(fill="0", align=">", width=8, radix="hex")),
            ("{:8x}", FormatSpec(fill=" ", align=">", width=8, radix="hex")),
            ("{:>8x}", FormatSpec(fill=" ", align=">", width=8, radix="hex")),
            ("{:\x1a>8x}", FormatSpec(fill="\x1a", align=">", width=8, radix="hex")),
        ]
        for fmt, expected in cases:
            parts, count = _parse_format(fmt, "test")
            self.assertEqual(count, 1)
            self.assertEqual(parts[0], ("arg", expected), f"Failed for {fmt!r}")

    def test_decimal_shorthands(self):
        cases = [
            ("{:04d}", FormatSpec(fill="0", align=">", width=4, radix="dec")),
            ("{:4d}", FormatSpec(fill=" ", align=">", width=4, radix="dec")),
            ("{:\x1a>4d}", FormatSpec(fill="\x1a", align=">", width=4, radix="dec")),
            ("{:>4d}", FormatSpec(fill=" ", align=">", width=4, radix="dec")),
            # Omitted type defaults to 'd' when width/fill is specified
            ("{:04}", FormatSpec(fill="0", align=">", width=4, radix="dec")),
            ("{:4}", FormatSpec(fill=" ", align=">", width=4, radix="dec")),
            ("{:>4}", FormatSpec(fill=" ", align=">", width=4, radix="dec")),
            ("{:\x1a>4}", FormatSpec(fill="\x1a", align=">", width=4, radix="dec")),
        ]
        for fmt, expected in cases:
            parts, count = _parse_format(fmt, "test")
            self.assertEqual(count, 1)
            self.assertEqual(parts[0], ("arg", expected), f"Failed for {fmt!r}")

    def test_width_boundaries(self):
        # Positive integer 1-10
        p1, _ = _parse_format("{:1d}", "test")
        self.assertEqual(p1[0][1].width, 1)

        p10, _ = _parse_format("{:10d}", "test")
        self.assertEqual(p10[0][1].width, 10)

        p010, _ = _parse_format("{:010d}", "test")
        self.assertEqual(p010[0][1].width, 10)
        self.assertEqual(p010[0][1].fill, "0")

    def test_rejections_with_eudsl_format(self):
        bad_specs = [
            # Left alignment (<) is rejected
            "{:<4d}",
            "{:0<4d}",
            "{:<}",
            "{:<d}",
            "{:<8x}",
            # Unsupported alignment
            "{:^4d}",
            "{:=4d}",
            "{:>}",
            "{:>d}",
            # Hex width != 8
            "{:4x}",
            "{:04x}",
            "{:1x}",
            "{:7x}",
            "{:9x}",
            "{:10x}",
            "{:010x}",
            # Width out of range (must be positive 1-10)
            "{:0d}",
            "{:0x}",
            "{:0}",
            "{:11d}",
            "{:99d}",
            "{:00d}",
            "{:004d}",
            # Unknown types / syntax
            "{:q}",
            "{:s}",
            "{:04s}",
            "{:4dd}",
            "{:d4}",
            "{:x8}",
            "{:foo}",
            # Malformed braces
            "{",
            "}",
            "{:4d",
        ]
        for fmt in bad_specs:
            with self.assertRaises(
                EudslError, msg=f"Expected EUDSL-FORMAT for {fmt!r}"
            ) as ctx:
                _parse_format(fmt, "test")
            self.assertEqual(
                ctx.exception.code,
                "EUDSL-FORMAT",
                f"Wrong code for {fmt!r}: {ctx.exception}",
            )

    def test_fixed_string_write_default_decimal(self):
        def prog():
            s = FixedString(16)
            s.write("val={}", 42)

        mod = build_module(prog)
        rep = run_sim(mod.to_json())
        self.assertEqual(read_text(rep, 0), "val=42")

    def test_fixed_string_write_explicit_hex(self):
        def prog():
            s = FixedString(32)
            s.write("{:x}|{:08x}", 0x2A, 0x2A)

        mod = build_module(prog)
        rep = run_sim(mod.to_json())
        self.assertEqual(read_text(rep, 0), "0000002a|0000002a")

    def test_fixed_string_write_static_padded_decimal(self):
        def prog():
            s = FixedString(32)
            s.write("{:04d}|{:4d}|{:\x1a>4d}|{:04}", 7, 7, 7, 7)

        mod = build_module(prog)
        rep = run_sim(mod.to_json())
        self.assertEqual(read_text(rep, 0), "0007|   7|\x1a\x1a\x1a7|0007")

    def test_fixed_string_capacity_respects_placeholder_width(self):
        # Width 4 needs only 4 content bytes
        def ok_prog():
            s = FixedString(4)
            v = Var(1)
            s.write("{:04d}", v)

        mod = build_module(ok_prog)
        self.assertIsNotNone(mod)

        # Capacity 3 cannot fit width 4
        def overflow_prog():
            s = FixedString(3)
            v = Var(1)
            s.write("{:04d}", v)

        with self.assertRaises(EudslError) as ctx:
            build_module(overflow_prog)
        self.assertEqual(ctx.exception.code, "EUDSL-BOUNDS")

        # Default {} needs 10 content bytes for decimal
        def default_overflow():
            s = FixedString(9)
            v = Var(1)
            s.write("{}", v)

        with self.assertRaises(EudslError) as ctx:
            build_module(default_overflow)
        self.assertEqual(ctx.exception.code, "EUDSL-BOUNDS")


if __name__ == "__main__":
    unittest.main()
