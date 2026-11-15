"""FE0: one canonical operand predicate / describer per frontend.

Mirror of ``ts/tests/test_value_helpers.ts``. The operand-domain guards are
spread across ``view``, ``hir``, ``stock_rows_built``, ``string``, ``blockio``
and ``datadumper``; FE0 makes ``py/view.py`` the only definition site and
leaves the private call-site names (``_is_plain_int`` / ``_describe``) as
aliases.

Semantics pinned here (unchanged by the dedupe):

* a *plain integer* is ``int`` but not ``bool``; ``float`` (including an
  integral float, ``NaN`` and ``Infinity``), ``str``, ``None`` and runtime
  handles are rejected;
* ``_describe_value`` keeps the exact old rendering: ``True``/``False`` via
  ``repr``, ints bare, strings via ``repr``, everything else ``a <Type>``;
* ``string`` keeps its own ``json.dumps`` string rendering (the pre-dedupe
  ``FixedString`` messages), delegating every other operand to the canonical
  describer.
"""

from __future__ import annotations

import unittest
from typing import Any, Callable

from py import view
from py.var import lazyVar


class PlainIntPredicateTest(unittest.TestCase):
    def test_accepts_int_only(self) -> None:
        self.assertTrue(view._is_plain_int(0))
        self.assertTrue(view._is_plain_int(-1))

    def test_rejects_bool(self) -> None:
        self.assertFalse(view._is_plain_int(True))
        self.assertFalse(view._is_plain_int(False))

    def test_rejects_non_integral_float(self) -> None:
        self.assertFalse(view._is_plain_int(1.5))

    def test_rejects_integral_float(self) -> None:
        self.assertFalse(view._is_plain_int(1.0))

    def test_rejects_nan_and_infinity(self) -> None:
        self.assertFalse(view._is_plain_int(float("nan")))
        self.assertFalse(view._is_plain_int(float("inf")))
        self.assertFalse(view._is_plain_int(float("-inf")))

    def test_rejects_string_none_and_var_like(self) -> None:
        self.assertFalse(view._is_plain_int("1"))
        self.assertFalse(view._is_plain_int(None))
        self.assertFalse(view._is_plain_int(lazyVar(0)))

    def test_frontends_share_the_canonical_predicate(self) -> None:
        from py import blockio, hir, stock_rows_built, string

        for module in (hir, stock_rows_built, string, blockio):
            with self.subTest(module=module.__name__):
                self.assertIs(getattr(module, "_is_plain_int", None), view._is_plain_int)


class DescribeValueTest(unittest.TestCase):
    def test_renders_bool_via_repr(self) -> None:
        self.assertEqual(view._describe_value(True), "True")
        self.assertEqual(view._describe_value(False), "False")

    def test_renders_ints_bare(self) -> None:
        self.assertEqual(view._describe_value(0), "0")
        self.assertEqual(view._describe_value(-1), "-1")

    def test_renders_non_integral_float_nan_and_infinity(self) -> None:
        self.assertEqual(view._describe_value(1.5), "a float")
        self.assertEqual(view._describe_value(float("nan")), "a float")
        self.assertEqual(view._describe_value(float("inf")), "a float")
        self.assertEqual(view._describe_value(float("-inf")), "a float")

    def test_renders_string_via_repr(self) -> None:
        self.assertEqual(view._describe_value("abc"), "'abc'")

    def test_renders_none_by_type_name(self) -> None:
        self.assertEqual(view._describe_value(None), "a NoneType")

    def test_renders_var_like_by_type_name(self) -> None:
        self.assertEqual(view._describe_value(lazyVar(0)), "a Var")

    def test_frontends_share_the_canonical_describer(self) -> None:
        from py import blockio, datadumper

        for module in (blockio, datadumper):
            with self.subTest(module=module.__name__):
                self.assertIs(getattr(module, "_describe", None), view._describe_value)


class StringDescribeAdapterTest(unittest.TestCase):
    """``string`` keeps the pre-dedupe ``json.dumps`` string rendering."""

    def test_renders_strings_via_json_dumps(self) -> None:
        from py import string

        self.assertEqual(string._describe("abc"), '"abc"')
        self.assertEqual(string._describe('a "quote"'), '"a \\"quote\\""')

    def test_delegates_every_other_operand(self) -> None:
        from py import string

        for value in (True, False, 0, -1, 1.5, float("nan"), None, lazyVar(0)):
            with self.subTest(value=value):
                self.assertEqual(
                    string._describe(value), view._describe_value(value)
                )

    def test_size_error_message_is_byte_identical_to_pre_dedupe(self) -> None:
        from py import string

        with self.assertRaises(view.EudslError) as ctx:
            string._check_fixed_string_size("abc")
        self.assertEqual(
            str(ctx.exception),
            "EUDSL-BOUNDS: FixedString(): size must be a positive integer "
            '(bytes), got "abc"',
        )


class CallSiteDescribeAgreementTest(unittest.TestCase):
    """The narrowed guards render operands exactly like their pre-dedupe helper."""

    _BAD_VALUES = (
        ("bool", True),
        ("float", 1.5),
        ("nan", float("nan")),
        ("inf", float("inf")),
        ("string", "abc"),
        ("none", None),
    )

    def _assert_rendered(
        self, call: Callable[[Any], Any], value: Any, rendered: str
    ) -> None:
        with self.assertRaises(view.EudslError) as ctx:
            call(value)
        self.assertIn(f"got {rendered}", str(ctx.exception))

    def test_string_size_guard(self) -> None:
        from py import string

        for case, value in self._BAD_VALUES:
            with self.subTest(case=case):
                self._assert_rendered(
                    string._check_fixed_string_size, value, string._describe(value)
                )

    def test_blockio_epd_guard(self) -> None:
        from py import blockio

        for case, value in self._BAD_VALUES:
            with self.subTest(case=case):
                self._assert_rendered(
                    lambda v: blockio._epd_value(v, "memcpyEPD()", "src"),
                    value,
                    view._describe_value(value),
                )

    def test_datadumper_payload_guard(self) -> None:
        from py import datadumper

        for case, value in self._BAD_VALUES:
            with self.subTest(case=case):
                self._assert_rendered(
                    datadumper._require_payload, value, view._describe_value(value)
                )

    def test_datadumper_offsets_and_flags_guards(self) -> None:
        from py import datadumper

        for case, value in (("float", 1.5), ("none", None)):
            with self.subTest(guard="out_offsets", case=case):
                self._assert_rendered(
                    datadumper._require_out_offsets,
                    value,
                    view._describe_value(value),
                )
            with self.subTest(guard="flags", case=case):
                self._assert_rendered(
                    datadumper._require_flags, value, view._describe_value(value)
                )

    def test_exact_pre_dedupe_blockio_and_datadumper_messages(self) -> None:
        from py import blockio, datadumper

        with self.assertRaises(view.EudslError) as ctx:
            blockio._epd_value(True, "memcpyEPD()", "src")
        self.assertEqual(
            str(ctx.exception),
            "EUDSL-TYPE: memcpyEPD(): src must be a non-negative integer EPD "
            "or a game-time value, got True",
        )

        with self.assertRaises(view.EudslError) as ctx:
            datadumper._require_payload(None)
        self.assertEqual(
            str(ctx.exception),
            "EUDSL-TYPE: dataDumper(): input_data must be bytes, got a NoneType",
        )


if __name__ == "__main__":
    unittest.main()
