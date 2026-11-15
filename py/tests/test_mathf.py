"""`py.mathf.isqrt`: fixed-point integer square root (issue #22).

`isqrt` is the only kernel in the camera-follow port that is shared with
future callers, so it gets its own contract tests:

* a build-time constant folds to `math.isqrt(value << 2 * fixed_bits)`;
* a runtime value is read from memory and must agree with the same oracle at
  the full range boundary (`2**value_bits - 1`) and at the caller's Q8 scale;
* the kernel is emitted inline, so running it twice in one module must give
  the same answer twice — a stale remainder or root accumulator would only
  show up from the second pass on.

The comparison oracle is `math.isqrt` (floor semantics), matching the
restoring digit-by-digit algorithm the helper emits.
"""

from __future__ import annotations

import math
import unittest
from typing import Any

from py import FieldSpec, SetDeaths, SetTo, Var, build_module, defineMemView, when
from py.mathf import isqrt
from py.tests.helpers import run_sim
from py.view import EudslError, make_memview

WORD_ADDR = 3000
_WORD = defineMemView("MathfWord", 4, {"v": FieldSpec(0, 4)})


def _seed(value: int) -> None:
    SetDeaths(WORD_ADDR, SetTo, value, 0)


def _constant(value: int, value_bits: int, fixed_bits: int) -> int:
    """Fold a build-time constant through the simulator."""
    ids: dict[str, int] = {}

    def prog() -> None:
        out = Var(0)
        ids["out"] = out.id
        out.set(isqrt(value, value_bits, fixed_bits))

    report = run_sim(build_module(prog).to_json(), 1)
    return report["var_values"][ids["out"]]


def _runtime(values: list[int], value_bits: int, fixed_bits: int) -> list[int]:
    """Run `isqrt` once per element, feeding each pass from memory."""
    ids: dict[int, int] = {}

    def prog() -> None:
        loop = Var(0)
        _seed(values[0])
        view = make_memview(_WORD, WORD_ADDR)
        for index, value in enumerate(values):
            out = Var(0)
            ids[index] = out.id

            def body(value: int = value, out: Var = out) -> None:
                _seed(value)
                out.set(isqrt(view.v.read(), value_bits, fixed_bits))

            when(loop.eq(index), body)
            loop.iadd(1)

    report = run_sim(build_module(prog).to_json(), len(values))
    return [report["var_values"][ids[i]] for i in range(len(values))]


class IsqrtConstantTest(unittest.TestCase):
    def test_constants_fold_to_the_python_oracle(self) -> None:
        cases: list[tuple[int, int, int]] = [
            (0, 32, 0),
            (1, 32, 0),
            (2, 32, 0),
            (3, 32, 0),
            (4, 32, 0),
            (99, 32, 0),
            (100, 32, 0),
            (1600, 32, 0),
            ((1 << 16) - 1, 16, 0),
            (1600, 16, 8),
            ((1 << 32) - 1, 32, 0),
        ]
        for value, value_bits, fixed_bits in cases:
            with self.subTest(value=value, value_bits=value_bits, fixed_bits=fixed_bits):
                expected = math.isqrt(value << (2 * fixed_bits))
                self.assertEqual(_constant(value, value_bits, fixed_bits), expected)

    def test_constant_still_returns_a_var(self) -> None:
        # The folded value is a `Var`, so it composes with runtime arithmetic
        # instead of leaking a plain Python int into the DSL.
        seen: list[Any] = []

        def prog() -> None:
            result = isqrt(1600, 32, 0)
            seen.append(result)
            out = Var(0)
            out.set(result)

        module = build_module(prog)
        self.assertIsInstance(seen[0], Var)
        # Ticket 04 acceptance: a folded constant assigned to a var is still
        # three static triggers (the packed constant updates plus setup/restore).
        report = run_sim(module.to_json(), 1)
        self.assertEqual(report["static_cost"]["triggers"], 3)


class IsqrtRuntimeTest(unittest.TestCase):
    def test_floor_semantics_across_the_range(self) -> None:
        values = [0, 1, 2, 3, 4, 5, 8, 9, 15, 16, 99, 100, 1600, 65535]
        expected = [math.isqrt(v) for v in values]
        self.assertEqual(_runtime(values, 16, 0), expected)

    def test_wider_input_uses_the_declared_bit_width(self) -> None:
        values = [0, 1, 255, 65536, 1 << 20, (1 << 32) - 1]
        expected = [math.isqrt(v) for v in values]
        self.assertEqual(_runtime(values, 32, 0), expected)

    def test_q8_fixed_point_matches_the_scaled_oracle(self) -> None:
        # The camera port asks for `floor(256 * sqrt(sumsq))` with sumsq < 2**16.
        values = [0, 1, 2, 1600, 3200, 9999, (1 << 16) - 1]
        expected = [math.isqrt(v << 16) for v in values]
        self.assertEqual(_runtime(values, 16, 8), expected)

    def test_repeated_passes_do_not_accumulate(self) -> None:
        # Same value twice: a leaked remainder/root would change pass 2.
        self.assertEqual(_runtime([1600, 1600], 16, 0), [40, 40])
        # Different values twice: pass 2 must not inherit pass 1's state.
        self.assertEqual(_runtime([1600, 81], 16, 0), [40, 9])
        self.assertEqual(_runtime([(1 << 16) - 1, 0], 16, 8), [math.isqrt(((1 << 16) - 1) << 16), 0])

    def test_prototype_sweep_matches_math_isqrt(self) -> None:
        # Exact squares and the uint32 boundary catch both recurrence
        # mistakes found while prototyping: a missing root doubling, and
        # comparing against the pre-doubling `u`.
        values = [
            0, 1, 2, 3, 4, 8, 9, 15, 16, 99, 100, 1000, 65535, 65536,
            0xFFFFFFFF,
        ]
        self.assertEqual(_runtime(values, 32, 0), [math.isqrt(v) for v in values])


# One runtime `isqrt` site at the default widths: `o.set(isqrt(x, 32, 0))`.
# The ticket-04 `u = 2*root` recurrence replaces the per-step `trial`
# rebuild, trading 16 static triggers for 17 static actions and shrinking the
# page by 38,528 bytes. Executed cost falls for every pinned input because the
# three var-to-var transfers per step are gone.
ISQRT_SITE_STATIC = {
    "triggers": 211,
    "conditions": 48,
    "actions": 726,
    "trigger_bytes": 508088,
    "blob_bytes": 12,
    "total_bytes": 508100,
}
ISQRT_EXECUTED_PINS = {
    1000: (268, 48, 6251),
    0: (243, 48, 5510),
    0x40000000: (248, 48, 5658),
    0xFFFFFFFF: (323, 48, 7894),
}


class IsqrtCostPin(unittest.TestCase):
    """Static and executed cost of one runtime `isqrt` site."""

    def _report(self, value: int) -> dict[str, Any]:
        def build(value: int = value) -> None:
            src = Var(value)
            out = Var(0)
            out.set(isqrt(src, 32, 0))

        return run_sim(build_module(build).to_json(), 1)

    def test_static_cost(self) -> None:
        self.assertEqual(self._report(1000)["static_cost"], ISQRT_SITE_STATIC)

    def test_executed_cost_pins(self) -> None:
        for value, want in ISQRT_EXECUTED_PINS.items():
            with self.subTest(value=value):
                cost = self._report(value)["executed_cost"]
                self.assertEqual(
                    (cost["triggers"], cost["conditions"], cost["actions"]),
                    want,
                )


class IsqrtValidationTest(unittest.TestCase):
    def _error(self, factory, **kwargs: Any) -> EudslError:
        """Build `isqrt(*factory())` and return the error it raises.

        Args are made inside the module scope because a `Var` cannot be
        constructed outside `build_module`.
        """
        with self.assertRaises(EudslError) as caught:

            def prog() -> None:
                isqrt(*factory(), **kwargs)

            build_module(prog)
        return caught.exception

    def test_bit_widths_are_range_checked(self) -> None:
        self.assertEqual(self._error(lambda: (Var(1), 0)).code, "EUDSL-RANGE")
        self.assertEqual(self._error(lambda: (Var(1), 33)).code, "EUDSL-RANGE")
        self.assertEqual(self._error(lambda: (Var(1), 32, 17)).code, "EUDSL-RANGE")

    def test_constants_must_fit_the_declared_width(self) -> None:
        self.assertEqual(self._error(lambda: (-1,)).code, "EUDSL-RANGE")
        self.assertEqual(self._error(lambda: (1 << 16, 16)).code, "EUDSL-RANGE")

    def test_widths_must_be_plain_integers(self) -> None:
        self.assertEqual(self._error(lambda: (Var(1), 16.0)).code, "EUDSL-TYPE")
        self.assertEqual(self._error(lambda: (Var(1), True)).code, "EUDSL-TYPE")
        self.assertEqual(self._error(lambda: (Var(1), 16, "0")).code, "EUDSL-TYPE")

    def test_value_must_be_a_var_or_a_plain_integer(self) -> None:
        self.assertEqual(self._error(lambda: (1.5,)).code, "EUDSL-TYPE")
        self.assertEqual(self._error(lambda: (True,)).code, "EUDSL-TYPE")
        self.assertEqual(self._error(lambda: ("1600",)).code, "EUDSL-TYPE")

    def test_expr_operand_is_rejected(self) -> None:
        """FE4: `isqrt` takes a `Var` or a constant, never a bare `Expr`.

        The TS signature now advertises `VarImpl | number`; this pins the py
        side of the same narrowed runtime domain, so a later "convenience"
        widening cannot land on one frontend only.
        """
        self.assertEqual(self._error(lambda: (Var(1).add(1),)).code, "EUDSL-TYPE")


if __name__ == "__main__":
    unittest.main()
