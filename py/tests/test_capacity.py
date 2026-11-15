"""T-WP10-T1: ``free_list_empty`` is the one-condition CCMU capacity truth.

``0x628438`` is the head of the CUnit free list. ``head == 0`` is the exact
answer to "the engine has no free CUnit slot right now", and it lowers to one
condition 15 on ``EPD(0x628438)`` with comparison ``Exactly`` and amount ``0``.
The helper is a predicate, not a trigger: the caller's ``when``/``once``/``until``
owns the gate and keeps its 16-condition budget.

The same probes live in ``ts/tests/test_capacity.ts``.
"""

from __future__ import annotations

import json
import unittest
from typing import Any

from py import Exactly, Memory, SetDeaths, SetMemory, SetTo, build_module, when
from py.capacity import (
    CUNIT_FREE_HEAD,
    CUNIT_FREE_TAIL,
    freeListEmpty,
    free_list_empty,
)
from py.tests.helpers import run_sim, run_static
from py.view import EPD

BODY_EPD = 9000
BODY_VALUE = 1


def _gate_hir() -> str:
    """Only the gated statement: one trigger, one condition, one body act."""

    def build() -> None:
        when(
            [free_list_empty()],
            lambda: SetDeaths(BODY_EPD, SetTo, BODY_VALUE, 0),
        )

    return build_module(build).to_json()


def _head_value_hir(head_value: int) -> str:
    """Write ``head_value`` to the free-list head, then run the gate."""

    def build() -> None:
        SetMemory(CUNIT_FREE_HEAD, SetTo, head_value)
        when(
            [free_list_empty()],
            lambda: SetDeaths(BODY_EPD, SetTo, BODY_VALUE, 0),
        )

    return build_module(build).to_json()


def _composed_hir() -> str:
    """The helper composes with another external condition in one gate."""

    def build() -> None:
        when(
            [
                free_list_empty(),
                Memory(CUNIT_FREE_TAIL, Exactly, 0),
            ],
            lambda: SetDeaths(BODY_EPD, SetTo, BODY_VALUE, 0),
        )

    return build_module(build).to_json()


def _stmts(hir_json: str) -> list[dict[str, Any]]:
    return json.loads(hir_json)["module"]["stmts"]


def _only_when(hir_json: str) -> dict[str, Any]:
    gates = [stmt for stmt in _stmts(hir_json) if stmt["kind"] == "when"]
    assert len(gates) == 1, gates
    return gates[0]


class FreeListEmptyConditionTest(unittest.TestCase):
    def test_condition_is_one_exactly_zero_memory_read(self) -> None:
        cond = free_list_empty()
        self.assertIsInstance(cond, dict)
        self.assertEqual(cond["condtype"], 15)
        self.assertEqual(cond["player"], 161845)
        self.assertEqual(cond["comparison"], 10)
        self.assertEqual(cond["amount"], 0)

    def test_outside_build_module_the_condition_still_builds(self) -> None:
        # A pure predicate constructs a condition dict outside a builder; the
        # eventual gate is what enforces EUDSL-SCOPE.
        self.assertEqual(free_list_empty()["condtype"], 15)

    def test_addresses_pin_head_and_tail_arithmetic(self) -> None:
        self.assertEqual(CUNIT_FREE_HEAD, 0x628438)
        self.assertEqual(CUNIT_FREE_TAIL, 0x62843C)
        self.assertEqual(EPD(CUNIT_FREE_HEAD), 161845)
        self.assertEqual(EPD(CUNIT_FREE_TAIL), 161846)

    def test_camel_alias_is_the_same_function(self) -> None:
        self.assertIs(freeListEmpty, free_list_empty)


class FreeListEmptyGateTest(unittest.TestCase):
    def test_when_emits_one_gate_trigger_with_one_condition(self) -> None:
        gate = _only_when(_gate_hir())
        self.assertEqual(len(gate["conds"]), 1)
        self.assertEqual(
            gate["conds"][0],
            {
                "locid": 0,
                "player": 161845,
                "amount": 0,
                "unit": 0,
                "comparison": 10,
                "condtype": 15,
                "restype": 0,
                "flags": 0,
                "eudx": 0,
            },
        )
        self.assertEqual(gate["body"][0]["dest"], BODY_EPD)
        self.assertEqual(gate["body"][0]["value"], BODY_VALUE)

    def test_static_and_executed_cost_are_one_trigger(self) -> None:
        hir_json = _gate_hir()
        static = run_static(hir_json)["static_cost"]
        executed = run_sim(hir_json, 1)["executed_cost"]
        self.assertEqual(
            (static["triggers"], static["conditions"], static["actions"]),
            (1, 1, 1),
        )
        self.assertEqual(
            (executed["triggers"], executed["conditions"], executed["actions"]),
            (1, 1, 1),
        )

    def test_two_condition_gate_stays_one_trigger(self) -> None:
        gate = _only_when(_composed_hir())
        self.assertEqual([cond["player"] for cond in gate["conds"]], [161845, 161846])
        static = run_static(_composed_hir())["static_cost"]
        self.assertEqual(
            (static["triggers"], static["conditions"]),
            (1, 2),
        )

    def test_empty_head_runs_the_body(self) -> None:
        report = run_sim(_head_value_hir(0), 1)
        self.assertEqual(report["external_epds"].get(str(BODY_EPD), 0), BODY_VALUE)

    def test_nonempty_head_skips_the_body(self) -> None:
        report = run_sim(_head_value_hir(5), 1)
        self.assertEqual(report["external_epds"].get(str(BODY_EPD), 0), 0)
        # The write itself proves the test observed the same dword the gate
        # reads, not an alias.
        self.assertEqual(
            report["external_epds"].get(str(EPD(CUNIT_FREE_HEAD)), 0), 5
        )


if __name__ == "__main__":
    unittest.main()
