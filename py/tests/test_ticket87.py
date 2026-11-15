"""Tests for Ticket #87 cleanups: ExpandedArg NamedTuple and geometry deduplication."""

import unittest
from py import build_module
from py.string import ExpandedArg, FixedString, FormatSpec


class Ticket87ExpandedArgTest(unittest.TestCase):
    def test_expanded_arg_namedtuple_fields(self):
        spec = FormatSpec(radix="hex")
        arg = ExpandedArg(
            kind="lit",
            bytes=[65, 66],
            spec=spec,
            value=None,
        )
        self.assertIsInstance(arg, tuple)
        self.assertEqual(arg.kind, "lit")
        self.assertEqual(arg.bytes, [65, 66])
        self.assertEqual(arg.spec, spec)
        self.assertIsNone(arg.value)
        self.assertEqual(arg.spec.radix, "hex")

        # Unpacking as 4-tuple
        kind, raw_bytes, s, val = arg
        self.assertEqual(kind, "lit")
        self.assertEqual(raw_bytes, [65, 66])
        self.assertEqual(s, spec)
        self.assertIsNone(val)

    def test_classify_arg_returns_expanded_arg(self):
        def body():
            s = FixedString(16)
            item = s._classify_arg("hello", "hex", "test")
            self.assertIsInstance(item, ExpandedArg)
            self.assertEqual(item.kind, "lit")
            self.assertEqual(item.bytes, [104, 101, 108, 108, 111])
            self.assertEqual(item.spec.radix, "hex")
            self.assertIsNone(item.value)

        build_module(body)

    def test_hex_byte_geometry_calculations(self):
        from py.string import _hex_byte_geometry

        # For lane 0, char 0: dword_delta 0, byte_in_dword 0, mask 0x000000FF
        g0 = _hex_byte_geometry(0, 0)
        self.assertEqual(g0.dword_delta, 0)
        self.assertEqual(g0.byte_in_dword, 0)
        self.assertEqual(g0.byte_mask, 0xFF)

        # For lane 1, char 3: total offset 4 -> dword_delta 1, byte_in_dword 0, mask 0xFF
        g1 = _hex_byte_geometry(1, 3)
        self.assertEqual(g1.dword_delta, 1)
        self.assertEqual(g1.byte_in_dword, 0)
        self.assertEqual(g1.byte_mask, 0xFF)

        # For lane 2, char 5: total offset 7 -> dword_delta 1, byte_in_dword 3, mask 0xFF000000
        g2 = _hex_byte_geometry(2, 5)
        self.assertEqual(g2.dword_delta, 1)
        self.assertEqual(g2.byte_in_dword, 3)
        self.assertEqual(g2.byte_mask, 0xFF000000)
