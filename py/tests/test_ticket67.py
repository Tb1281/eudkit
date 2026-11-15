"""Ticket #67: coalesce adjacent variable transfers inside temporal bodies.

The opt pipeline used to batch adjacent ``VarTransfer`` statements only in
``module.stmts``; every ``when``/``once``/``while`` body (and every
``until``/``edge``/``every``/``after`` desugar expansion) fired one page per
transfer. ADR 0001 §3 records the unconditional rule, so this is a defect
against the design, not a deferred decision.

These pins are red before the fix (body transfers stay split) and green after
(the same ``can_coalesce_transfer`` rule runs over every statement list):

* ``when`` body N=2: 4T/1C/71A/9,644 B static, 4T/1C/71A executed
  (was 5T/1C/72A/12,052 B and 6T/1C/136A);
* ``when`` body N=8: 4T/1C/83A/9,668 B static, 4T/1C/83A executed
  (was 11T/1C/90A/26,524 B and 18T/1C/538A);
* ``while`` body N=2: 4T/1C/72A/9,644 B static, 9T/3C/144A executed
  (was 5T/1C/73A/12,052 B and 13T/3C/274A);
* ``while`` body N=8: 4T/1C/84A/9,668 B static, 9T/3C/168A executed
  (was 11T/1C/91A/26,524 B and 37T/3C/1,078A);
* ``once`` body N=2/N=8: 4T/1C/71A/9,644 B and 4T/1C/83A/9,668 B static,
  4T/1C/71A and 4T/1C/83A executed — the shared ``optimize_stmts`` recursion
  makes ``once`` cost the same as ``when``;
* ``else_body`` (JSON surgery, no public surface yet) N=2/N=8: 5T/1C/71A/
  12,052 B and 5T/1C/83A/12,076 B static; 2T/1C/2A executed while the gate is
  true and 4T/1C/69A / 4T/1C/81A executed when the else arm runs (the extra
  trigger over the body case is the else-branch machinery, 2,408 B).

The guard rules do not relax inside a body: RAW, WAR, non-additive WAW,
duplicate source, and a ``VarStore`` in the middle all still split the run
(each pins 5 static triggers versus 4 for a legal run); additive WAW still
coalesces; 65 adjacent transfers still form two batches.
The same probes live in ``ts/tests/test_ticket67.ts``.
"""

from __future__ import annotations

import json
import unittest

from py import SetMemory, SetTo, Var, build_module, once, when, while_loop
from py.tests.helpers import run_sim, run_static

STATIC_KEYS = ("triggers", "conditions", "actions", "total_bytes")
EXEC_KEYS = ("triggers", "conditions", "actions")

STORE_ADDR = 0x640B58


def _when_body(n: int) -> str:
    """True-gated ``when`` body of N adjacent ``dst[i].set(src[i])`` copies."""

    def build() -> None:
        gate = Var(1)
        srcs = [Var(1 + i) for i in range(n)]
        dsts = [Var(0) for i in range(n)]

        def body() -> None:
            for src, dst in zip(srcs, dsts, strict=True):
                dst.set(src)

        when([gate.eq(1)], body)

    return build_module(build).to_json()


def _while_body(n: int) -> str:
    """N adjacent copies plus ``g.iadd(1)`` inside ``whileLoop(g.lt(2))``."""

    def build() -> None:
        srcs = [Var(1 + i) for i in range(n)]
        dsts = [Var(0) for i in range(n)]
        g = Var(0)

        def body() -> None:
            for src, dst in zip(srcs, dsts, strict=True):
                dst.set(src)
            g.iadd(1)

        while_loop([g.lt(2)], body)

    return build_module(build).to_json()


def _once_body(n: int) -> str:
    """True-at-start ``once`` body of N adjacent ``dst[i].set(src[i])`` copies."""

    def build() -> None:
        gate = Var(0)
        srcs = [Var(1 + i) for i in range(n)]
        dsts = [Var(0) for i in range(n)]

        def body() -> None:
            for src, dst in zip(srcs, dsts, strict=True):
                dst.set(src)

        once([gate.eq(0)], body)

    return build_module(build).to_json()


def _else_body(n: int, *, amount: int = 1) -> tuple[str, list[int]]:
    """``when`` whose N-transfer body is moved into ``else_body`` by JSON surgery.

    The DSL has no ``else`` surface yet (#51 is a decision), so the else arm is
    exercised at the HIR-JSON seam: build a conventional true-gated ``when``,
    then move its body into ``else_body``. ``amount=2`` closes the gate so the
    else arm runs. Returns the HIR JSON plus the N destination var ids.
    """
    ids: list[int] = []

    def build() -> None:
        gate = Var(1)
        srcs = [Var(1 + i) for i in range(n)]
        dsts = [Var(0) for i in range(n)]
        with when([gate.eq(amount)]):
            for src, dst in zip(srcs, dsts, strict=True):
                dst.set(src)
        ids.extend(dst.id for dst in dsts)

    doc = json.loads(build_module(build).to_json())
    node = next(
        stmt
        for stmt in doc["module"]["stmts"]
        if stmt["kind"] == "when" and stmt.get("body")
    )
    node["else_body"] = node["body"]
    node["body"] = []
    return json.dumps(doc), ids


def _cost(hir_json: str) -> tuple[tuple[int, ...], tuple[int, ...]]:
    static = run_static(hir_json)["static_cost"]
    executed = run_sim(hir_json, 1)["executed_cost"]
    return (
        tuple(static[key] for key in STATIC_KEYS),
        tuple(executed[key] for key in EXEC_KEYS),
    )


class Ticket67BodyCostPins(unittest.TestCase):
    def test_when_body_n2(self) -> None:
        static, executed = _cost(_when_body(2))
        self.assertEqual(static, (4, 1, 71, 9_644))
        self.assertEqual(executed, (4, 1, 71))

    def test_when_body_n8(self) -> None:
        static, executed = _cost(_when_body(8))
        self.assertEqual(static, (4, 1, 83, 9_668))
        self.assertEqual(executed, (4, 1, 83))

    def test_while_body_n2(self) -> None:
        static, executed = _cost(_while_body(2))
        self.assertEqual(static, (4, 1, 72, 9_644))
        self.assertEqual(executed, (9, 3, 144))

    def test_while_body_n8(self) -> None:
        static, executed = _cost(_while_body(8))
        self.assertEqual(static, (4, 1, 84, 9_668))
        self.assertEqual(executed, (9, 3, 168))

    def test_static_triggers_are_flat_in_run_length(self) -> None:
        # O(1) in the run length: one batch setup/restore pair per body,
        # independent of how many transfers the run holds.
        self.assertEqual(run_static(_when_body(1))["static_cost"]["triggers"], 4)
        self.assertEqual(run_static(_when_body(8))["static_cost"]["triggers"], 4)
        self.assertEqual(run_static(_while_body(1))["static_cost"]["triggers"], 4)
        self.assertEqual(run_static(_while_body(8))["static_cost"]["triggers"], 4)


class Ticket67OnceBodyPins(unittest.TestCase):
    """``once`` bodies go through the same ``optimize_stmts`` recursion."""

    def test_once_body_n2(self) -> None:
        static, executed = _cost(_once_body(2))
        self.assertEqual(static, (4, 1, 71, 9_644))
        self.assertEqual(executed, (4, 1, 71))

    def test_once_body_n8(self) -> None:
        static, executed = _cost(_once_body(8))
        self.assertEqual(static, (4, 1, 83, 9_668))
        self.assertEqual(executed, (4, 1, 83))

    def test_once_static_triggers_are_flat_in_run_length(self) -> None:
        self.assertEqual(run_static(_once_body(1))["static_cost"]["triggers"], 4)
        self.assertEqual(run_static(_once_body(8))["static_cost"]["triggers"], 4)


class Ticket67ElseBodyPins(unittest.TestCase):
    """``else_body`` lists are covered by the same rule (HIR-JSON seam, #51)."""

    def test_else_body_n2_runs_when_the_gate_is_true(self) -> None:
        hir, ids = _else_body(2)
        static, executed = _cost(hir)
        self.assertEqual(static, (5, 1, 71, 12_052))
        self.assertEqual(executed, (2, 1, 2))
        values = run_sim(hir, 1)["var_values"]
        self.assertEqual([values[var_id] for var_id in ids], [0, 0])

    def test_else_body_n8_runs_when_the_gate_is_true(self) -> None:
        hir, _ids = _else_body(8)
        static, executed = _cost(hir)
        self.assertEqual(static, (5, 1, 83, 12_076))
        self.assertEqual(executed, (2, 1, 2))

    def test_closed_gate_runs_the_else_arm_and_coalesces_it(self) -> None:
        hir2, ids2 = _else_body(2, amount=2)
        self.assertEqual(_cost(hir2), ((5, 1, 71, 12_052), (4, 1, 69)))
        values = run_sim(hir2, 1)["var_values"]
        self.assertEqual([values[var_id] for var_id in ids2], [1, 2])
        hir8, ids8 = _else_body(8, amount=2)
        self.assertEqual(_cost(hir8), ((5, 1, 83, 12_076), (4, 1, 81)))
        values = run_sim(hir8, 1)["var_values"]
        self.assertEqual([values[var_id] for var_id in ids8], list(range(1, 9)))


class Ticket67BodyGuardPins(unittest.TestCase):
    """The module-root guard cases, mirrored into a ``when`` body."""

    def _run(self, body, *, n_vars: int = 4) -> tuple[dict[str, int], int]:
        ids: dict[str, int] = {}

        def build() -> None:
            gate = Var(1)
            src = Var(10)
            src2 = Var(20)
            dst = Var(0)
            dst2 = Var(0)
            acc = Var(1)
            body(gate, src, src2, dst, dst2, acc)
            ids.update(
                src=src.id,
                src2=src2.id,
                dst=dst.id,
                dst2=dst2.id,
                acc=acc.id,
            )

        hir = build_module(build).to_json()
        static_triggers = run_static(hir)["static_cost"]["triggers"]
        values = run_sim(hir, 1)["var_values"]
        return {name: values[var_id] for name, var_id in ids.items()}, static_triggers

    def test_raw_hazard_splits_and_values_are_correct(self) -> None:
        def body(gate, src, src2, dst, dst2, acc) -> None:
            with when([gate.eq(1)]):
                dst.set(src)
                src2.set(dst)

        values, static_triggers = self._run(body)
        # A split run keeps per-transfer firing: no batch saves the trigger.
        self.assertEqual(static_triggers, 5)
        self.assertEqual(values["dst"], 10)
        self.assertEqual(values["src2"], 10)

    def test_war_hazard_splits_and_values_are_correct(self) -> None:
        def body(gate, src, src2, dst, dst2, acc) -> None:
            with when([gate.eq(1)]):
                dst.set(src)
                src.set(src2)

        values, static_triggers = self._run(body)
        self.assertEqual(static_triggers, 5)
        self.assertEqual(values["dst"], 10)
        self.assertEqual(values["src"], 20)

    def test_non_additive_waw_splits_and_last_write_wins(self) -> None:
        def body(gate, src, src2, dst, dst2, acc) -> None:
            with when([gate.eq(1)]):
                dst.set(src)
                dst.set(src2)

        values, static_triggers = self._run(body)
        self.assertEqual(static_triggers, 5)
        self.assertEqual(values["dst"], 20)

    def test_duplicate_source_splits_and_both_destinations_are_written(self) -> None:
        def body(gate, src, src2, dst, dst2, acc) -> None:
            with when([gate.eq(1)]):
                dst.set(src)
                dst2.set(src)

        values, static_triggers = self._run(body)
        self.assertEqual(static_triggers, 5)
        self.assertEqual(values["dst"], 10)
        self.assertEqual(values["dst2"], 10)

    def test_var_store_joins_the_run(self) -> None:
        # #96: a store no longer breaks the transfer run. It is a batch member
        # like any transfer: the independent transfers coalesce around it, both
        # sources and the store share one page firing, and the values are
        # unchanged. (Before #96 the store forced a second firing: 5 triggers.)
        def body(gate, src, src2, dst, dst2, acc) -> None:
            with when([gate.eq(1)]):
                dst.set(src)
                SetMemory(STORE_ADDR, SetTo, 7)
                dst2.set(src2)

        values, static_triggers = self._run(body)
        self.assertEqual(static_triggers, 4)
        self.assertEqual(values["dst"], 10)
        self.assertEqual(values["dst2"], 20)

    def test_additive_waw_coalesces_and_sums(self) -> None:
        def body(gate, src, src2, dst, dst2, acc) -> None:
            with when([gate.eq(1)]):
                acc.iadd(src)
                acc.iadd(src2)

        values, static_triggers = self._run(body)
        # Additive WAW is the one hazard the rule lets share a batch.
        self.assertEqual(static_triggers, 4)
        self.assertEqual(values["acc"], 31)

    def test_legal_run_shares_one_batch_and_hazard_does_not(self) -> None:
        # Shape through the public cost seam: a legal two-transfer body needs
        # one batch setup/restore pair, a RAW-split body needs two.
        def legal() -> None:
            gate = Var(1)
            src = Var(10)
            src2 = Var(20)
            dst = Var(0)
            dst2 = Var(0)
            with when([gate.eq(1)]):
                dst.set(src)
                dst2.set(src2)

        def hazard() -> None:
            gate = Var(1)
            src = Var(10)
            src2 = Var(20)
            dst = Var(0)
            dst2 = Var(0)
            with when([gate.eq(1)]):
                dst.set(src)
                src2.set(dst)

        self.assertEqual(run_static(build_module(legal).to_json())["static_cost"]["triggers"], 4)
        self.assertEqual(run_static(build_module(hazard).to_json())["static_cost"]["triggers"], 5)

    def test_65_transfer_body_forms_two_batches_and_writes_every_destination(self) -> None:
        n = 65
        ids: dict[str, list[int]] = {}

        def build() -> None:
            srcs = [Var(1000 + i) for i in range(n)]
            dsts = [Var(0) for i in range(n)]
            with when(True):
                for src, dst in zip(srcs, dsts, strict=True):
                    dst.set(src)
            ids["dst"] = [dst.id for dst in dsts]

        hir = build_module(build).to_json()
        report = run_sim(hir, 1)
        self.assertEqual(
            [report["var_values"][var_id] for var_id in ids["dst"]],
            [1000 + i for i in range(n)],
        )
        # 65 transfers exceed one page capacity: two batches, still O(1) pages.
        self.assertEqual(run_static(hir)["static_cost"]["triggers"], 8)


if __name__ == "__main__":
    unittest.main()
