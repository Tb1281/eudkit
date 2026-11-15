"""UTF-8 string bytes for ``FixedString`` (T-UTF8, issue #1).

``FixedString(size)`` keeps a **byte** capacity (ADR 0003). Literal content is
lowered to canonical RFC 3629 UTF-8 bytes, so Korean, Japanese, Chinese, emoji,
and Latin text all work without changing the capacity model. An embedded NUL
(which would terminate the ``STRx`` entry) and a lone UTF-16 surrogate (which is
never valid UTF-8) are rejected with ``EUDSL-TYPE`` before any bytes are
emitted. Format-spec ``fill`` remains one ASCII byte (``EUDSL-ASCII``).
"""

from __future__ import annotations

import unittest
from typing import Callable

from py import Always, EudslError, FixedString, build_module, when
from py.string import encode_utf8
from py.tests.helpers import raw_string_bytes, run_sim


def assert_code(
    test: unittest.TestCase, fn: Callable[[], object], code: str
) -> EudslError:
    with test.assertRaises(EudslError) as ctx:
        fn()
    test.assertEqual(ctx.exception.code, code, str(ctx.exception))
    return ctx.exception


def build_and_sim(prog: Callable[[], object]) -> dict:
    return run_sim(build_module(prog).to_json())


class EncodeUtf8Test(unittest.TestCase):
    """The Py/TS parity table from the T-UTF8 spec."""

    def test_parity_byte_tables(self) -> None:
        cases = [
            ("ABC", [0x41, 0x42, 0x43]),
            ("é", [0xC3, 0xA9]),
            ("한", [0xED, 0x95, 0x9C]),
            ("日本語", [0xE6, 0x97, 0xA5, 0xE6, 0x9C, 0xAC, 0xE8, 0xAA, 0x9E]),
            ("🙂", [0xF0, 0x9F, 0x99, 0x82]),
            (
                "中文한글🙂",
                [
                    0xE4, 0xB8, 0xAD,
                    0xE6, 0x96, 0x87,
                    0xED, 0x95, 0x9C,
                    0xEA, 0xB8, 0x80,
                    0xF0, 0x9F, 0x99, 0x82,
                ],
            ),
            ("a한🙂", [0x61, 0xED, 0x95, 0x9C, 0xF0, 0x9F, 0x99, 0x82]),
        ]
        for text, want in cases:
            with self.subTest(text=text):
                self.assertEqual(encode_utf8(text, "test"), want)

    def test_two_and_three_byte_boundaries(self) -> None:
        # U+007F / U+0080 and U+07FF / U+0800 are the encoding-length edges.
        self.assertEqual(encode_utf8("\x7f", "test"), [0x7F])
        self.assertEqual(encode_utf8("\x80", "test"), [0xC2, 0x80])
        self.assertEqual(encode_utf8("\u07ff", "test"), [0xDF, 0xBF])
        self.assertEqual(encode_utf8("\u0800", "test"), [0xE0, 0xA0, 0x80])
        self.assertEqual(encode_utf8("\uffff", "test"), [0xEF, 0xBF, 0xBF])
        self.assertEqual(
            encode_utf8("\U0010ffff", "test"), [0xF4, 0x8F, 0xBF, 0xBF]
        )

    def test_valid_astral_scalar_is_four_bytes(self) -> None:
        # `\U0001F642` is the scalar U+1F642; the UTF-16 surrogate pair
        # `\ud83d\ude42` is two lone surrogates in Python, not one code point.
        self.assertEqual(encode_utf8("\U0001F642", "test"), [0xF0, 0x9F, 0x99, 0x82])

    def test_rejects_embedded_nul(self) -> None:
        err = assert_code(
            self, lambda: encode_utf8("a\x00b", "FixedString.write()"), "EUDSL-TYPE"
        )
        self.assertIn("FixedString.write()", str(err))
        self.assertIn("U+0000", str(err))

    def test_rejects_lone_surrogates(self) -> None:
        for text in ("\ud800", "\udfff", "\udc00", "a\ud83db"):
            with self.subTest(text=repr(text)):
                err = assert_code(
                    self, lambda t=text: encode_utf8(t, "test"), "EUDSL-TYPE"
                )
                self.assertIn("surrogate", str(err))

    def test_fixed_string_path_rejects_nul_and_lone_surrogate(self) -> None:
        for text in ("\x00", "\ud800"):
            with self.subTest(text=repr(text)):

                def prog(t: str = text) -> None:
                    s = FixedString(8)
                    s.write(t)

                assert_code(self, lambda p=prog: build_module(p), "EUDSL-TYPE")


class FixedStringUtf8Test(unittest.TestCase):
    """Multi-byte content is charged and written by bytes."""

    def test_two_byte_content_needs_two_byte_capacity(self) -> None:
        def too_small() -> None:
            s = FixedString(1)
            s.write("é")

        assert_code(self, lambda: build_module(too_small), "EUDSL-BOUNDS")

        def fits() -> None:
            s = FixedString(2)
            s.write("é")

        report = build_and_sim(fits)
        self.assertEqual(
            raw_string_bytes(report, 3),
            [0xC3, 0xA9, 0x00],
            "C3 A9 occupy offsets 0..1 and the NUL sits at offset 2",
        )

    def test_two_multibyte_literals_fit_exactly(self) -> None:
        def fits() -> None:
            s = FixedString(4)
            s.write("é", "é")

        report = build_and_sim(fits)
        self.assertEqual(raw_string_bytes(report, 5), [0xC3, 0xA9, 0xC3, 0xA9, 0x00])

        def too_small() -> None:
            s = FixedString(3)
            s.write("é", "é")

        assert_code(self, lambda: build_module(too_small), "EUDSL-BOUNDS")

    def test_three_byte_hangul_needs_three_byte_capacity(self) -> None:
        def fits() -> None:
            s = FixedString(3)
            s.write("한")

        report = build_and_sim(fits)
        self.assertEqual(raw_string_bytes(report, 4), [0xED, 0x95, 0x9C, 0x00])

        def too_small() -> None:
            s = FixedString(2)
            s.write("한")

        assert_code(self, lambda: build_module(too_small), "EUDSL-BOUNDS")

    def test_japanese_literal_round_trips_through_fixed_string(self) -> None:
        def fits() -> None:
            s = FixedString(9)
            s.write("日本語")

        report = build_and_sim(fits)
        self.assertEqual(
            raw_string_bytes(report, 10),
            [
                0xE6, 0x97, 0xA5,
                0xE6, 0x9C, 0xAC,
                0xE8, 0xAA, 0x9E,
                0x00,
            ],
        )

        def too_small() -> None:
            s = FixedString(8)
            s.write("日本語")

        assert_code(self, lambda: build_module(too_small), "EUDSL-BOUNDS")

    def test_mixed_han_hangul_emoji_round_trips_end_to_end(self) -> None:
        want = [
            0xE4, 0xB8, 0xAD,
            0xE6, 0x96, 0x87,
            0xED, 0x95, 0x9C,
            0xEA, 0xB8, 0x80,
            0xF0, 0x9F, 0x99, 0x82,
            0x00,
        ]

        def fits() -> None:
            s = FixedString(16)
            s.write("中文한글🙂")

        report = build_and_sim(fits)
        self.assertEqual(raw_string_bytes(report, 17), want)

        def too_small() -> None:
            s = FixedString(15)
            s.write("中文한글🙂")

        assert_code(self, lambda: build_module(too_small), "EUDSL-BOUNDS")

    def test_pad_byte_never_splits_a_code_point(self) -> None:
        # "é" is 2 content bytes; a scope boundary pads to lane 0 with
        # (-2) % 4 = 2 copies of STRING_PAD_BYTE, never between C3 and A9.
        # (A module-end tail stays unaligned, so force a scope boundary.)
        def prog() -> None:
            s = FixedString(8)
            s.write("é")
            when(Always(), lambda: None)

        report = build_and_sim(prog)
        self.assertEqual(
            raw_string_bytes(report, 5),
            [0xC3, 0xA9, 0x1A, 0x1A, 0x00],
            "the pad bytes follow the complete UTF-8 sequence",
        )

    def test_non_ascii_literal_is_not_an_ascii_error(self) -> None:
        # EUDSL-ASCII is reachable only from the format-spec fill path.
        def prog() -> None:
            s = FixedString(4)
            s.write("한")
            when(Always(), lambda: None)

        report = build_and_sim(prog)
        self.assertEqual(raw_string_bytes(report, 5), [0xED, 0x95, 0x9C, 0x1A, 0x00])

    def test_multibyte_format_fill_is_rejected(self) -> None:
        def prog() -> None:
            s = FixedString(16)
            s.write("{:한>4d}", 1)

        assert_code(self, lambda: build_module(prog), "EUDSL-ASCII")

    def test_ascii_literals_are_byte_identical(self) -> None:
        def prog() -> None:
            s = FixedString(8)
            s.write("ABC")
            when(Always(), lambda: None)

        report = build_and_sim(prog)
        self.assertEqual(raw_string_bytes(report, 5), [0x41, 0x42, 0x43, 0x1A, 0x00])


if __name__ == "__main__":
    unittest.main()
