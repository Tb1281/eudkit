"""T-ABS-RAND: private per-builder LCG random state.

The sequence is pinned against a plain Python LCG rather than against another
eudkit helper, so a regression in the emitted gated adds cannot hide behind a
shared implementation. Simulated values come from the native HIR simulator;
build-time shape assertions inspect the emitted HIR directly.
"""

from __future__ import annotations

import random as stdlib_random
import unittest
from pathlib import Path

import py.random as rnd
from py import Var, buildModule
from py.camera import GAME_TICK_ADDR
from py.tests.helpers import run_sim
from py.view import EPD, EudslError

MULT = 1103515245
INC = 12345


def next_state(state: int) -> int:
    return (state * MULT + INC) & 0xFFFFFFFF


def reference_rand(seed: int, count: int) -> list[int]:
    state = seed & 0xFFFFFFFF
    draws: list[int] = []
    for _ in range(count):
        state = next_state(state)
        draws.append(state >> 16)
    return draws


def reference_dwrand(seed: int) -> tuple[int, int]:
    first = next_state(seed & 0xFFFFFFFF)
    second = next_state(first)
    return ((first & 0xFFFF0000) | (second >> 16)) & 0xFFFFFFFF, second


def run_values(build, names: list[str], loops: int = 1) -> dict[str, int]:
    """Build ``build(holder)``, simulate once, and resolve captured Vars."""
    holder: dict[str, Var] = {}

    def body() -> None:
        build(holder)

    module = buildModule(body)
    report = run_sim(module.to_json(), loops)
    return {name: report["var_values"][holder[name].id] for name in names}


def rand_sequence(seed: int, count: int) -> list[int]:
    holder: dict[str, Var] = {}

    def body() -> None:
        rnd.srand(seed)
        for index in range(count):
            holder[f"r{index}"] = rnd.rand()

    module = buildModule(body)
    report = run_sim(module.to_json(), 1)
    return [report["var_values"][holder[f"r{index}"].id] for index in range(count)]


def unseeded_sequence(count: int) -> list[int]:
    holder: dict[str, Var] = {}

    def body() -> None:
        for index in range(count):
            holder[f"r{index}"] = rnd.rand()

    module = buildModule(body)
    report = run_sim(module.to_json(), 1)
    return [report["var_values"][holder[f"r{index}"].id] for index in range(count)]


class TestModuleIdentity(unittest.TestCase):
    def test_py_random_does_not_shadow_the_stdlib_module(self) -> None:
        self.assertEqual(rnd.__name__, "py.random")
        self.assertEqual(stdlib_random.__name__, "random")
        self.assertIsNot(rnd, stdlib_random)
        self.assertTrue(Path(rnd.__file__).as_posix().endswith("py/random.py"))

    def test_constants_and_public_surface_are_exact(self) -> None:
        self.assertEqual(rnd.LCG_MULT, MULT)
        self.assertEqual(rnd.LCG_INC, INC)
        self.assertEqual(
            rnd.__all__,
            [
                "RandSeed",
                "LCG_MULT",
                "LCG_INC",
                "srand",
                "get_seed",
                "getSeed",
                "randomize",
                "rand",
                "dwrand",
                "dwRand",
                "rand_range",
                "randRange",
            ],
        )
        self.assertIs(rnd.getSeed, rnd.get_seed)
        self.assertIs(rnd.dwRand, rnd.dwrand)
        self.assertIs(rnd.randRange, rnd.rand_range)

    def test_source_has_no_trigger_rewiring(self) -> None:
        source = Path(rnd.__file__).read_text(encoding="utf-8")
        for token in (
            "SetNextPtr",
            "Switch",
            "Forward",
            "EUDLightBool",
            "EUDByteReader",
        ):
            self.assertNotIn(token, source)
        self.assertIn("GAME_TICK_ADDR", source)


class TestSequence(unittest.TestCase):
    def test_rand_matches_the_reference_sequence(self) -> None:
        for seed in (0, 1, 12345, 0xFFFFFFFF):
            with self.subTest(seed=seed):
                self.assertEqual(
                    rand_sequence(seed, 6), reference_rand(seed, 6)
                )

    def test_dwrand_joins_two_draw_hiwords_and_advances_twice(self) -> None:
        for seed in (0, 1, 12345, 0xFFFFFFFF):
            with self.subTest(seed=seed):
                values = run_values(
                    lambda holder, seed=seed: (
                        rnd.srand(seed),
                        holder.__setitem__("draw", rnd.dwrand()),
                        holder.__setitem__("state", rnd.get_seed()),
                    ),
                    ["draw", "state"],
                )
                expected, expected_state = reference_dwrand(seed)
                self.assertEqual(values["draw"], expected)
                self.assertEqual(values["state"], expected_state)

    def test_get_seed_returns_the_current_state_without_advancing(self) -> None:
        holder: dict[str, Var] = {}

        def snapshot() -> Var:
            # A copy is needed: get_seed() returns the live state Var, so two
            # captures of the same object would both read its final value.
            copy = Var(0)
            copy.set(rnd.get_seed())
            return copy

        def body() -> None:
            rnd.srand(0xBEEF)
            holder["first_view"] = rnd.get_seed()
            holder["second_view"] = rnd.get_seed()
            holder["before"] = snapshot()
            holder["draw"] = rnd.rand()
            holder["after"] = snapshot()

        module = buildModule(body)
        report = run_sim(module.to_json(), 1)
        self.assertEqual(report["var_values"][holder["before"].id], 0xBEEF)
        self.assertEqual(
            report["var_values"][holder["draw"].id],
            reference_rand(0xBEEF, 1)[0],
        )
        self.assertEqual(
            report["var_values"][holder["after"].id],
            next_state(0xBEEF),
        )
        self.assertIs(holder["first_view"], holder["second_view"])

    def test_each_module_starts_from_a_private_state(self) -> None:
        # Two modules seeded identically must replay the same sequence...
        self.assertEqual(rand_sequence(12345, 3), rand_sequence(12345, 3))
        # ...and an unseeded module after a seeded one must not inherit it.
        self.assertEqual(rand_sequence(12345, 1), reference_rand(12345, 1)[:1])
        self.assertEqual(unseeded_sequence(2), reference_rand(0, 2))

    def test_two_call_sites_in_one_module_share_one_state(self) -> None:
        holder: dict[str, Var] = {}

        def body() -> None:
            rnd.srand(7)
            holder["first"] = rnd.rand()
            holder["second"] = rnd.rand()

        module = buildModule(body)
        report = run_sim(module.to_json(), 1)
        self.assertEqual(
            [
                report["var_values"][holder["first"].id],
                report["var_values"][holder["second"].id],
            ],
            reference_rand(7, 2),
        )


class TestRandomize(unittest.TestCase):
    def test_randomize_folds_the_tick_and_updates_twice(self) -> None:
        holder: dict[str, Var] = {}

        def snapshot() -> Var:
            copy = Var(0)
            copy.set(rnd.get_seed())
            return copy

        def body() -> None:
            rnd.srand(0)
            rnd.randomize()
            holder["first"] = snapshot()
            rnd.randomize()
            holder["second"] = snapshot()

        module = buildModule(body)
        text = module.to_json()
        report = run_sim(text, 1)
        # The simulator leaves external memory zeroed, so the tick is 0 here.
        first = report["var_values"][holder["first"].id]
        second = report["var_values"][holder["second"].id]
        self.assertEqual(first, INC)
        self.assertEqual(second, next_state(INC))
        # The game-tick dword is read; two calls share one reader.
        tick_epd = str(EPD(GAME_TICK_ADDR) & 0xFFFFFFFF)
        self.assertIn(tick_epd, text)

        one_holder: dict[str, Var] = {}

        def one() -> None:
            rnd.srand(0)
            rnd.randomize()
            one_holder["state"] = rnd.get_seed()

        one_text = buildModule(one).to_json()
        self.assertGreater(text.count(tick_epd), 0)
        self.assertEqual(text.count(tick_epd), one_text.count(tick_epd))


class TestRandRange(unittest.TestCase):
    def _draws(self, lo: int, hi: int, count: int) -> list[int]:
        holder: dict[str, Var] = {}

        def body() -> None:
            rnd.srand(123)
            for index in range(count):
                holder[f"r{index}"] = rnd.rand_range(lo, hi)

        module = buildModule(body)
        report = run_sim(module.to_json(), 1)
        return [report["var_values"][holder[f"r{index}"].id] for index in range(count)]

    def test_draws_stay_inside_the_inclusive_bounds(self) -> None:
        self.assertTrue(all(0 <= value <= 100 for value in self._draws(0, 100, 8)))
        self.assertTrue(
            all(
                0 <= value <= 0xFFFFFFFE
                for value in self._draws(0, 0xFFFFFFFE, 8)
            )
        )

    def test_single_value_span_folds_without_consuming_a_draw(self) -> None:
        holder: dict[str, Var] = {}

        def body() -> None:
            rnd.srand(123)
            holder["value"] = rnd.rand_range(5, 5)
            holder["state"] = rnd.get_seed()

        module = buildModule(body)
        report = run_sim(module.to_json(), 1)
        self.assertEqual(report["var_values"][holder["value"].id], 5)
        self.assertEqual(report["var_values"][holder["state"].id], 123)


class TestValidation(unittest.TestCase):
    def assert_code(self, code: str, call) -> None:
        with self.assertRaises(EudslError) as caught:
            call()
        self.assertEqual(caught.exception.code, code)

    def test_seed_operand_domain_is_type_checked(self) -> None:
        self.assert_code("EUDSL-TYPE", lambda: buildModule(lambda: rnd.srand(True)))
        self.assert_code("EUDSL-TYPE", lambda: buildModule(lambda: rnd.srand("x")))

    def test_rand_range_bounds_are_type_and_range_checked(self) -> None:
        self.assert_code("EUDSL-TYPE", lambda: rnd.rand_range(True, 2))
        self.assert_code("EUDSL-RANGE", lambda: rnd.rand_range(10, 5))
        self.assert_code("EUDSL-RANGE", lambda: rnd.rand_range(-1, 5))
        self.assert_code("EUDSL-RANGE", lambda: rnd.rand_range(0, 0xFFFFFFFF))

    def test_calls_before_build_module_are_scope_errors(self) -> None:
        for call in (
            lambda: rnd.srand(1),
            rnd.get_seed,
            rnd.randomize,
            rnd.rand,
            rnd.dwrand,
            lambda: rnd.rand_range(0, 10),
        ):
            with self.subTest(call=call):
                self.assert_code("EUDSL-SCOPE", call)


if __name__ == "__main__":
    unittest.main()
