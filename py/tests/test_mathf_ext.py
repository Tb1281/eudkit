"""Extended contracts for signed div/mod, integer powers, trig, and search.

The `isqrt` suite in :mod:`py.tests.test_mathf` stays unchanged; this module
covers the helpers absorbed by `T-ABS-MATH`. Every runtime fixture seeds its
operands from memory through `SetDeaths`, so the emitted ladders and tables
are exercised rather than build-time folds.
"""

from __future__ import annotations

import math
import unittest
from typing import Any, Callable

from py import (
    ReadonlyVar,
    SetDeaths,
    SetTo,
    Trigger,
    Var,
    build_module,
    when,
)
from py.mathf import (
    TRIG_TABLE_Q,
    TRIG_TABLE_SCALE,
    atan2,
    atan2_256,
    atan2Fixed8,
    binary_max,
    binary_min,
    binaryMax,
    binaryMin,
    div_euclid,
    div_floor,
    div_towards_zero,
    divEuclid,
    divFloor,
    divTowardsZero,
    lengthdir,
    lengthdir_256,
    lengthdirFixed8,
    mod_euclid,
    mod_floor,
    mod_towards_zero,
    modEuclid,
    modFloor,
    modTowardsZero,
    pow,
    pow_int,
    powInt,
)
from py.tests.helpers import flatten_stmts, run_sim
from py.view import EudslError

DWORD = 0xFFFFFFFF

#: The §4.1 sign matrix: (dividend, divisor).
SIGN_MATRIX: list[tuple[int, int]] = [
    (7, 2),
    (-7, 2),
    (7, -2),
    (-7, -2),
    (-1, 2),
    (5, 7),
    (0, 5),
]

#: Expected (quotient, remainder) per mode, in the same order as `SIGN_MATRIX`.
SIGN_EXPECTED: dict[str, list[tuple[int, int]]] = {
    "towards_zero": [
        (3, 1),
        (-3, -1),
        (-3, 1),
        (3, -1),
        (0, -1),
        (0, 5),
        (0, 0),
    ],
    "floor": [
        (3, 1),
        (-4, 1),
        (-4, -1),
        (3, -1),
        (-1, 1),
        (0, 5),
        (0, 0),
    ],
    "euclid": [
        (3, 1),
        (-4, 1),
        (-3, 1),
        (4, 1),
        (-1, 1),
        (0, 5),
        (0, 0),
    ],
}

DIV_FUNCS: dict[str, tuple[Callable[[Any, Any], Var], Callable[[Any, Any], Var]]] = {
    "towards_zero": (div_towards_zero, mod_towards_zero),
    "floor": (div_floor, mod_floor),
    "euclid": (div_euclid, mod_euclid),
}


def _u(value: int) -> int:
    """Interpret a Python integer as an unsigned dword."""
    return value & DWORD


def _s(value: int) -> int:
    """Interpret an unsigned dword as a signed 32-bit integer."""
    value &= DWORD
    return value - 0x100000000 if value & 0x80000000 else value


def _constant_call(fn: Callable[[int, int], Var], a: int, b: int) -> tuple[Any, int]:
    """Build one constant-folded call and return ``(module, result_id)``."""
    box: dict[str, int] = {}

    def prog() -> None:
        out = Var(0)
        box["id"] = out.id
        out.set(fn(a, b))

    module = build_module(prog)
    return module, box["id"]


def _assert_constant_shape(test: unittest.TestCase, module: Any, value: int) -> None:
    """A folded result is one constant act plus a copy, with no runtime ladder."""
    stmts = module.to_hir()["module"]["stmts"]
    test.assertEqual([s["kind"] for s in stmts], ["trigger", "vartransfer"])
    trigger = stmts[0]
    test.assertEqual(trigger["conds"], [])
    test.assertEqual(len(trigger["acts"]), 1)
    test.assertEqual(trigger["acts"][0]["value"], _u(value))
    test.assertFalse(any(s["kind"] == "when" for s in flatten_stmts(stmts)))


def _runtime_call_cases(
    fn: Callable[[Var, Var], Var], cases: list[tuple[int, int]]
) -> tuple[Any, list[int]]:
    """Run one runtime call per case with both operands seeded from memory."""
    box: dict[str, Any] = {}

    def prog() -> None:
        loop = Var(0)
        a = Var(0)
        b = Var(0)
        rows: list[Var] = []

        for index, (a_value, b_value) in enumerate(cases):
            out = Var(0)
            rows.append(out)

            def body(
                a_value: int = a_value,
                b_value: int = b_value,
                out: Var = out,
            ) -> None:
                SetDeaths(a.value, SetTo, _u(a_value), 0)
                SetDeaths(b.value, SetTo, _u(b_value), 0)
                out.set(fn(a, b))

            when(loop.eq(index), body)

        loop.iadd(1)
        box["ids"] = [out.id for out in rows]

    module = build_module(prog)
    report = run_sim(module.to_json(), len(cases))
    return module, [report["var_values"][i] for i in box["ids"]]


def _run_cases(
    cases: list[tuple[Any, ...]],
    emit: Callable[..., Any],
    arity: int,
) -> list[list[int]]:
    """Run an arbitrary helper once per case with seeded inputs.

    The whole call sits inside the per-case ``when`` body, matching
    :mod:`py.tests.test_mathf`: a seed emitted as a sibling trigger would be
    visible to the call's own conditions only on the next loop.
    """
    box: dict[str, list[list[int]]] = {}

    def prog() -> None:
        loop = Var(0)
        inputs = [Var(0) for _ in range(len(cases[0]))]
        rows: list[list[Var]] = []

        for index, case in enumerate(cases):
            outs = [Var(0) for _ in range(arity)]
            rows.append(outs)

            def body(
                case: tuple[Any, ...] = case,
                outs: list[Var] = outs,
            ) -> None:
                for var, value in zip(inputs, case):
                    SetDeaths(var.value, SetTo, _u(int(value)), 0)
                result = emit(*inputs)
                if not isinstance(result, tuple):
                    result = (result,)
                for dst, src in zip(outs, result):
                    dst.set(src)

            when(loop.eq(index), body)

        loop.iadd(1)
        box["ids"] = [[out.id for out in row] for row in rows]

    report = run_sim(build_module(prog).to_json(), len(cases))
    return [[report["var_values"][i] for i in row] for row in box["ids"]]


def _binary_result(
    fn: Callable[[int, int, Callable[[Var], Any]], Var],
    lo: int,
    hi: int,
    predicate: Callable[[Var], Any],
) -> tuple[Any, int]:
    """Build one binary search and return ``(module, result_id)``."""
    box: dict[str, int] = {}

    def prog() -> None:
        out = Var(0)
        box["id"] = out.id
        out.set(fn(lo, hi, predicate))

    return build_module(prog), box["id"]


def _error(factory: Callable[[], Any]) -> EudslError:
    """Run ``factory`` inside a module and return the error it raises."""
    with unittest.TestCase().assertRaises(EudslError) as caught:

        def prog() -> None:
            factory()

        build_module(prog)
    return caught.exception


class SignedDivisionFoldTest(unittest.TestCase):
    """Build-time folding for all three rounding modes (§4.1)."""

    def test_sign_matrix_folds_to_the_documented_values(self) -> None:
        for mode, (div_fn, mod_fn) in DIV_FUNCS.items():
            for (a, b), (q, r) in zip(SIGN_MATRIX, SIGN_EXPECTED[mode]):
                with self.subTest(mode=mode, a=a, b=b):
                    module, q_id = _constant_call(div_fn, a, b)
                    _assert_constant_shape(self, module, q)
                    self.assertEqual(
                        run_sim(module.to_json(), 1)["var_values"][q_id], _u(q)
                    )

                    module, r_id = _constant_call(mod_fn, a, b)
                    _assert_constant_shape(self, module, r)
                    self.assertEqual(
                        run_sim(module.to_json(), 1)["var_values"][r_id], _u(r)
                    )


class SignedDivisionRuntimeTest(unittest.TestCase):
    """Same matrix through the runtime ladders with memory-seeded operands."""

    def test_sign_matrix_runs_through_the_ladder(self) -> None:
        for mode, (div_fn, mod_fn) in DIV_FUNCS.items():
            with self.subTest(mode=mode, op="div"):
                module, values = _runtime_call_cases(div_fn, SIGN_MATRIX)
                self.assertEqual(
                    values, [_u(q) for q, _ in SIGN_EXPECTED[mode]]
                )
                stmts = module.to_hir()["module"]["stmts"]
                self.assertEqual(
                    sum(1 for s in stmts if s["kind"] == "when"),
                    len(SIGN_MATRIX),
                )
                self.assertTrue(
                    any(s["kind"] == "trigger" for s in flatten_stmts(stmts))
                )

            with self.subTest(mode=mode, op="mod"):
                module, values = _runtime_call_cases(mod_fn, SIGN_MATRIX)
                self.assertEqual(
                    values, [_u(r) for _, r in SIGN_EXPECTED[mode]]
                )

    def test_game_time_zero_divisor_keeps_the_unsigned_contract(self) -> None:
        cases = [(-7, 0), (7, 0)]
        for mode, (div_fn, mod_fn) in DIV_FUNCS.items():
            with self.subTest(mode=mode, op="div"):
                _, values = _runtime_call_cases(div_fn, cases)
                self.assertEqual(values, [0, 0])
            with self.subTest(mode=mode, op="mod"):
                _, values = _runtime_call_cases(mod_fn, cases)
                # The remainder is the original signed dividend.
                self.assertEqual(values, [_u(-7), 7])


class SignedDivisionValidationTest(unittest.TestCase):
    def test_build_time_zero_divisor_is_rejected(self) -> None:
        for mode, (div_fn, mod_fn) in DIV_FUNCS.items():
            with self.subTest(mode=mode, op="div"):
                self.assertEqual(_error(lambda fn=div_fn: fn(Var(1), 0)).code,
                                 "EUDSL-DIV-ZERO")
            with self.subTest(mode=mode, op="mod"):
                self.assertEqual(_error(lambda fn=mod_fn: fn(0, 0)).code,
                                 "EUDSL-DIV-ZERO")

    def test_rejection_emits_no_partial_statements(self) -> None:
        def baseline() -> None:
            a = Var(0)
            out = Var(0)
            out.set(1)

        def failing_runtime() -> None:
            a = Var(0)
            out = Var(0)
            out.set(1)
            try:
                div_floor(a, 0)
            except EudslError:
                pass

        def failing_constant() -> None:
            a = Var(0)
            out = Var(0)
            out.set(1)
            try:
                mod_floor(0, 0)
            except EudslError:
                pass

        want = build_module(baseline).to_json()
        self.assertEqual(build_module(failing_runtime).to_json(), want)
        self.assertEqual(build_module(failing_constant).to_json(), want)

    def test_operands_must_be_in_the_documented_domain(self) -> None:
        for bad in (True, "7", None):
            with self.subTest(bad=bad):
                self.assertEqual(
                    _error(lambda bad=bad: div_floor(bad, 1)).code,
                    "EUDSL-TYPE",
                )

        def trigger_operand() -> None:
            struct = Trigger(conds=[], acts=[])
            div_floor(struct, 1)

        self.assertEqual(_error(trigger_operand).code, "EUDSL-TYPE")

    def test_plain_int_only_parameters(self) -> None:
        self.assertEqual(_error(lambda: pow(Var(2), True)).code, "EUDSL-TYPE")
        self.assertEqual(_error(lambda: pow(Var(2), 2.0)).code, "EUDSL-TYPE")
        self.assertEqual(_error(lambda: pow(Var(2), 32)).code, "EUDSL-RANGE")
        self.assertEqual(
            _error(lambda: pow(Var(2), 2, bits=0)).code, "EUDSL-RANGE"
        )
        self.assertEqual(
            _error(lambda: pow(Var(2), 2, bits=33)).code, "EUDSL-RANGE"
        )
        self.assertEqual(_error(lambda: binary_min(5, 4, lambda x: x.ge(0))).code,
                         "EUDSL-RANGE")
        self.assertEqual(
            _error(lambda: binary_min(0, DWORD, lambda x: x.ge(0))).code,
            "EUDSL-RANGE",
        )
        self.assertEqual(
            _error(lambda: binary_max(0, DWORD, lambda x: x.lt(1))).code,
            "EUDSL-RANGE",
        )
        self.assertEqual(
            _error(lambda: binary_min(0, 1, 5)).code,  # type: ignore[arg-type]
            "EUDSL-TYPE",
        )


class PowTest(unittest.TestCase):
    def test_constant_and_runtime_results(self) -> None:
        cases = [(2, 0, 32), (3, 1, 32), (3, 2, 32), (3, 5, 32), (2, 31, 32)]
        for base, exp, bits in cases:
            with self.subTest(base=base, exp=exp, bits=bits):
                expected = (base**exp) & ((1 << bits) - 1)
                module, out_id = _constant_call(
                    lambda b, e: pow(b, e, bits=bits), base, exp
                )
                _assert_constant_shape(self, module, expected)
                self.assertEqual(
                    run_sim(module.to_json(), 1)["var_values"][out_id], expected
                )

        for base, exp in [(3, 5), (7, 31), (0xFFFFFFFF, 2)]:
            with self.subTest(runtime_base=base, exp=exp):
                box: dict[str, int] = {}

                def prog(base: int = base, exp: int = exp) -> None:
                    src = Var(0)
                    out = Var(0)
                    box["src"] = src.id
                    box["out"] = out.id
                    SetDeaths(src.value, SetTo, _u(base), 0)
                    out.set(pow(src, exp))

                report = run_sim(build_module(prog).to_json(), 1)
                self.assertEqual(
                    report["var_values"][box["out"]], (base**exp) & DWORD
                )
                self.assertEqual(
                    report["var_values"][box["src"]], _u(base),
                    "the operand must not be mutated",
                )

    def test_bits_truncate_the_result(self) -> None:
        for bits, expected in [(1, 1), (8, 243), (16, 243), (32, 243)]:
            with self.subTest(bits=bits):
                module, out_id = _constant_call(
                    lambda b, e: pow(b, e, bits=bits), 3, 5
                )
                self.assertEqual(
                    run_sim(module.to_json(), 1)["var_values"][out_id],
                    expected & ((1 << bits) - 1),
                )

        expected = (3**5) & 0xFF
        box: dict[str, int] = {}

        def prog() -> None:
            src = Var(0)
            out = Var(0)
            box["out"] = out.id
            SetDeaths(src.value, SetTo, 3, 0)
            out.set(pow(src, 5, bits=8))

        report = run_sim(build_module(prog).to_json(), 1)
        self.assertEqual(report["var_values"][box["out"]], expected)

    def test_mul_sites_match_the_exponent_shape(self) -> None:
        import py.var as var_module

        def count_muls(exp: int) -> int:
            original = var_module.mul_shift_add
            calls: list[int] = []

            def counting(a: Var, b: Var, bits: int = 32) -> Var:
                calls.append(bits)
                return original(a, b, bits)

            var_module.mul_shift_add = counting  # type: ignore[assignment]
            try:
                def prog(exp: int = exp) -> None:
                    src = Var(3)
                    out = Var(0)
                    out.set(pow(src, exp))

                build_module(prog)
            finally:
                var_module.mul_shift_add = original  # type: ignore[assignment]
            return len(calls)

        for exp in [0, 1, 2, 4, 5, 31]:
            sites = 0 if exp == 0 else (exp.bit_length() - 1) + (exp.bit_count() - 1)
            with self.subTest(exp=exp):
                self.assertEqual(count_muls(exp), sites)


class TrigTest(unittest.TestCase):
    """Table-driven `atan2` / `atan2_256` against the stdlib oracle."""

    def test_atan2_matches_the_degrees_oracle(self) -> None:
        grid = [
            (y, x)
            for y in (-1000, -1, 0, 1, 1000)
            for x in (-1000, -1, 0, 1, 1000)
        ]
        values = _run_cases(grid, lambda y, x: atan2(y, x), 1)
        for (y, x), (actual,) in zip(grid, values):
            expected = math.degrees(math.atan2(y, x)) % 360
            difference = abs(actual - expected)
            difference = min(difference, 360 - difference)
            self.assertLessEqual(
                difference, 1, f"atan2({y}, {x}) = {actual}, want {expected}"
            )
        self.assertEqual(values[grid.index((0, 0))], [0])

    def test_atan2_256_matches_the_turn_oracle(self) -> None:
        grid = [
            (y, x)
            for y in (-1000, -1, 0, 1, 1000)
            for x in (-1000, -1, 0, 1, 1000)
        ]
        values = _run_cases(grid, lambda y, x: atan2_256(y, x), 1)
        for (y, x), (actual,) in zip(grid, values):
            expected = (math.degrees(math.atan2(y, x)) % 360) * 256 / 360
            difference = abs(actual - expected)
            difference = min(difference, 256 - difference)
            self.assertLessEqual(
                difference, 1, f"atan2_256({y}, {x}) = {actual}, want {expected}"
            )
        self.assertEqual(values[grid.index((0, 0))], [0])

    def test_cardinal_points_are_exact(self) -> None:
        cases = [(0, 1, 0), (1, 0, 90), (0, -1, 180), (-1, 0, 270)]
        values = _run_cases(
            [(y, x) for y, x, _ in cases], lambda y, x: atan2(y, x), 1
        )
        self.assertEqual([value[0] for value in values], [want for _, _, want in cases])


class LengthdirTest(unittest.TestCase):
    def test_degree_variant_matches_the_trig_oracle(self) -> None:
        angles = [0, 45, 90, 135, 180, 225, 270, 315, 359]
        lengths = [1, 100, -100]
        cases = [(length, angle) for angle in angles for length in lengths]
        values = _run_cases(cases, lambda length, angle: lengthdir(length, angle), 2)
        for (length, angle), (dx, dy) in zip(cases, values):
            expected_dx = int(length * math.cos(math.radians(angle)))
            expected_dy = int(length * math.sin(math.radians(angle)))
            self.assertLessEqual(
                abs(_s(dx) - expected_dx), 1,
                f"lengthdir({length}, {angle}) dx = {_s(dx)}, want {expected_dx}",
            )
            self.assertLessEqual(
                abs(_s(dy) - expected_dy), 1,
                f"lengthdir({length}, {angle}) dy = {_s(dy)}, want {expected_dy}",
            )

    def test_256_turn_variant_matches_the_trig_oracle(self) -> None:
        cases = [(100, angle) for angle in [0, 64, 128, 192, 255]]
        values = _run_cases(
            cases, lambda length, angle: lengthdir_256(length, angle), 2
        )
        for (length, angle), (dx, dy) in zip(cases, values):
            radians = 2 * math.pi * angle / 256
            expected_dx = int(length * math.cos(radians))
            expected_dy = int(length * math.sin(radians))
            self.assertLessEqual(abs(_s(dx) - expected_dx), 1)
            self.assertLessEqual(abs(_s(dy) - expected_dy), 1)


class TrigTableTest(unittest.TestCase):
    def test_tables_are_shared_per_builder(self) -> None:
        def prog() -> None:
            y = Var(0)
            x = Var(0)
            a = Var(0)
            b = Var(0)
            a.set(atan2(y, x))
            b.set(lengthdir(y, x)[0])

        first = build_module(prog)
        second = build_module(prog)
        for module in (first, second):
            self.assertEqual(len(module.blobs), 5)
            self.assertEqual(
                sorted(blob["size"] for blob in module.blobs),
                [260, 260, 364, 364, 1028],
            )
        # Per-builder keying: a second buildModule rebuilds the payloads
        # instead of reusing the first module's blob declarations.
        self.assertIsNot(first.blobs, second.blobs)

    def test_table_scale_is_q16(self) -> None:
        self.assertEqual(TRIG_TABLE_Q, 16)
        self.assertEqual(TRIG_TABLE_SCALE, 1 << 16)


class BinarySearchTest(unittest.TestCase):
    def test_min_and_max_over_spans(self) -> None:
        for span in [1, 2, 3, 8, 256, 1000]:
            lo = 0
            hi = span - 1
            threshold = max(lo, hi // 2)
            with self.subTest(span=span, op="min"):
                module, out_id = _binary_result(
                    binary_min, lo, hi, lambda x, t=threshold: x.ge(t)
                )
                report = run_sim(module.to_json(), 1)
                self.assertEqual(report["var_values"][out_id], threshold)

                module, out_id = _binary_result(
                    binary_min, lo, hi, lambda x: x.ge(hi + 1)
                )
                self.assertEqual(
                    run_sim(module.to_json(), 1)["var_values"][out_id],
                    hi + 1,
                )

            with self.subTest(span=span, op="max"):
                last = max(lo, hi // 2)
                module, out_id = _binary_result(
                    binary_max, lo, hi, lambda x, t=last + 1: x.lt(t)
                )
                self.assertEqual(
                    run_sim(module.to_json(), 1)["var_values"][out_id], last
                )

                module, out_id = _binary_result(
                    binary_max, lo, hi, lambda x: x.lt(0)
                )
                self.assertEqual(
                    run_sim(module.to_json(), 1)["var_values"][out_id], _u(lo - 1)
                )

    def test_step_count_and_predicate_actions(self) -> None:
        for span in [1, 2, 3, 8, 256, 1000]:
            lo = 0
            hi = span - 1
            steps = (hi - lo + 1).bit_length()
            with self.subTest(span=span):
                module, _ = _binary_result(
                    binary_min, lo, hi, lambda x: x.ge(hi // 2)
                )
                stmts = module.to_hir()["module"]["stmts"]
                self.assertEqual(
                    sum(1 for s in stmts if s["kind"] == "when"), steps
                )
                # One act per step comes from the search's reject arm; a
                # predicate that emitted actions would add more.
                self.assertEqual(
                    sum(
                        1
                        for s in flatten_stmts(stmts)
                        if s["kind"] == "act"
                    ),
                    steps,
                )

    def test_lo_equals_hi_still_tests_once(self) -> None:
        module, out_id = _binary_result(binary_min, 5, 5, lambda x: x.ge(5))
        self.assertEqual(
            run_sim(module.to_json(), 1)["var_values"][out_id], 5
        )
        module, out_id = _binary_result(binary_min, 5, 5, lambda x: x.ge(6))
        self.assertEqual(
            run_sim(module.to_json(), 1)["var_values"][out_id], 6
        )
        module, out_id = _binary_result(binary_max, 5, 5, lambda x: x.lt(5))
        self.assertEqual(
            run_sim(module.to_json(), 1)["var_values"][out_id], 4
        )


class ExportSurfaceTest(unittest.TestCase):
    def test_all_and_aliases(self) -> None:
        import py.mathf as mathf

        expected = [
            "MathOperand", "TRIG_TABLE_Q", "TRIG_TABLE_SCALE",
            "div_towards_zero", "divTowardsZero", "div_floor", "divFloor",
            "div_euclid", "divEuclid", "mod_towards_zero", "modTowardsZero",
            "mod_floor", "modFloor", "mod_euclid", "modEuclid",
            "pow", "pow_int", "powInt",
            "atan2", "atan2_256", "atan2Fixed8",
            "lengthdir", "lengthdir_256", "lengthdirFixed8",
            "binary_min", "binaryMin", "binary_max", "binaryMax",
        ]
        self.assertEqual(mathf.__all__, expected)
        aliases = {
            "divTowardsZero": div_towards_zero,
            "divFloor": div_floor,
            "divEuclid": div_euclid,
            "modTowardsZero": mod_towards_zero,
            "modFloor": mod_floor,
            "modEuclid": mod_euclid,
            "pow_int": pow,
            "powInt": pow,
            "atan2Fixed8": atan2_256,
            "lengthdirFixed8": lengthdir_256,
            "binaryMin": binary_min,
            "binaryMax": binary_max,
        }
        for name, target in aliases.items():
            with self.subTest(alias=name):
                self.assertIs(getattr(mathf, name), target)


class OperandDomainTest(unittest.TestCase):
    def test_readonly_var_and_expr_materialize_like_explicit_temps(self) -> None:
        def plain() -> None:
            a = Var(7)
            b = Var(2)
            out = Var(0)
            out.set(div_floor(a, b))

        def wrapped() -> None:
            a = Var(7)
            b = Var(2)
            out = Var(0)
            out.set(div_floor(ReadonlyVar(a), ReadonlyVar(b)))

        def explicit() -> None:
            a = Var(7)
            out = Var(0)
            tmp = Var(0)
            tmp.set(a.add(1))
            out.set(div_floor(tmp, 2))

        def inline() -> None:
            a = Var(7)
            out = Var(0)
            out.set(div_floor(a.add(1), 2))

        self.assertEqual(build_module(plain).to_json(), build_module(wrapped).to_json())
        self.assertEqual(build_module(explicit).to_json(), build_module(inline).to_json())


if __name__ == "__main__":
    unittest.main()
