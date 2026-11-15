"""Ticket #69: merge sibling ``when`` blocks with transfer bodies under a guard.

``crates/eudkit-opt/src/temporal.rs`` already merges adjacent sibling ``when``
blocks with identical conditions when both bodies are leaf acts (the 64-act
merge). Pure transfer bodies fell through unmerged, so the same gate cost
``3T + 4T`` per extra sibling instead of sharing one conditional group. The
merge is only legal when no transfer writes a variable the shared gate reads
(the guard); otherwise the second gate must observe the first body's writes.

These pins are red before the fix and green after:

* N=2 siblings: 4T/1C/71A/9,644 B static, 4T/1C/71A executed
  (was 7T/2C/74A/16,868 B and 8T/2C/138A);
* N=8 siblings: 4T/1C/83A/9,668 B static, 4T/1C/83A executed
  (was 25T/8C/104A/60,236 B and 32T/8C/552A) — O(N) -> O(1);
* different-gate pair: 7T/2C/74A/16,868 B static, 8T/2C/138A executed,
  values unchanged. This is the mutation pin for the identical-gates
  precondition: deleting ``w1.conds == w2.conds`` in the transfer branch
  merges the pair into 5T/1C/72A/12,052 B and 6T/1C/136A. The values are
  identical either way, so the cost tuple is the pin;
* transfer-order pair: 5T/1C/74A/12,044 B static, 6T/1C/138A executed
  (was 7T/2C/76A/16,860 B and 8T/2C/140A), values unchanged — the shared
  gate passes the guard here, so this pins post-merge order preservation,
  not the guard;
* two siblings inside ``while(c.lt(2))``: 6T/2C/74A/14,460 B static,
  13T/5C/148A executed (was 9T/3C/77A/21,684 B and 21T/7C/282A);
* multi-condition gate (``Memory`` + ``Switch``): 7T/2C/75A/16,868 B static,
  4T/1C/67A executed (was 9T/4C/77A/21,684 B and 5T/2C/67A).

The guard is load-bearing. A naive merge corrupts the runtime ``writeFrom``
capacity guards in ``test_ticket9`` (sibling ``when(pos < cap)`` blocks must
re-check ``pos`` after each byte): ``AAXYZ`` instead of ``AAXY`` and 438T ->
434T. Here the scan's guard shape stays unmerged at 7T/2C/74A/16,864 B and
keeps ``h=0`` (a naive merge gives 5T static and ``h=8``).

The same probes live in ``ts/tests/test_ticket69.ts``.
"""

from __future__ import annotations

import unittest

from py import AtLeast, Memory, Switch, SwitchSet, Var, build_module, when, while_loop
from py.tests.helpers import run_sim, run_static

STATIC_KEYS = ("triggers", "conditions", "actions", "total_bytes")
EXEC_KEYS = ("triggers", "conditions", "actions")


def _siblings(n: int) -> str:
    """N sibling ``when``s with one identical-gate transfer each."""

    def build() -> None:
        gate = Var(0)
        srcs = [Var(1 + i) for i in range(n)]
        dsts = [Var(0) for _ in range(n)]
        for i in range(n):
            when([gate.eq(0)], lambda dst=dsts[i], src=srcs[i]: dst.set(src))

    return build_module(build).to_json()


def _one_when(n: int) -> str:
    """The same N transfers inside one ``when`` (the hand-written control)."""

    def build() -> None:
        gate = Var(0)
        srcs = [Var(1 + i) for i in range(n)]
        dsts = [Var(0) for _ in range(n)]

        def body() -> None:
            for src, dst in zip(srcs, dsts, strict=True):
                dst.set(src)

        when([gate.eq(0)], body)

    return build_module(build).to_json()


def _transfer_order_pair() -> str:
    """Two same-gate siblings: pins the post-merge transfer order, not the guard.

    The shared gate reads var 0; both bodies write vars 1 and 2, so no
    destination is in the read set and the guard *passes*. The siblings
    merge and the coalescer preserves the merged order (``x.set(y)`` then
    ``y.set(x)``), keeping both at 7 — a guard-less merge produces the same
    values. The guard pin is the different-gates case below.
    """

    def build() -> None:
        gate = Var(0)
        x = Var(5)
        y = Var(7)
        when([gate.eq(0)], lambda: x.set(y))
        when([gate.eq(0)], lambda: y.set(x))

    return build_module(build).to_json()


def _different_gates_pair() -> str:
    """Two adjacent transfer siblings under *different* gates: must not merge.

    Mutation pin: with ``w1.conds == w2.conds`` deleted in the transfer
    branch of the merge step, the pair folds into one gate and the cost
    drops to 5T/1C/72A/12,052 B static, 6T/1C/136A executed. The runtime
    values are identical either way, so the cost tuple is the pin.
    """

    def build() -> None:
        g1 = Var(0)
        g2 = Var(0)
        s = Var(5)
        d = Var(0)
        e = Var(0)
        when([g1.eq(0)], lambda: d.set(s))
        when([g2.eq(0)], lambda: e.set(s))

    return build_module(build).to_json()


def _while_pair() -> str:
    """Two identical-gate transfer siblings inside ``while(c.lt(2))``."""

    def build() -> None:
        c = Var(0)
        x = Var(5)
        y = Var(7)
        z = Var(0)
        w = Var(9)

        def body() -> None:
            when([c.lt(2)], lambda: x.set(y))
            when([c.lt(2)], lambda: z.set(w))
            c.iadd(1)

        while_loop([c.lt(2)], body)

    return build_module(build).to_json()


def _multi_cond() -> str:
    """A two-condition gate (``Memory`` + ``Switch``) shared by both siblings."""

    def build() -> None:
        s = Var(8)
        h = Var(0)
        src = Var(9)
        r = Var(0)
        conds = [Memory(0x640B58, AtLeast, 1), Switch(1, SwitchSet)]
        when(conds, lambda: s.set(src))
        when(conds, lambda: h.set(src))
        r.set(src)

    return build_module(build).to_json()


def _guard() -> str:
    """First sibling writes the variable its own gate reads: must not merge."""

    def build() -> None:
        g = Var(1)
        s = Var(8)
        h = Var(0)
        when([g.eq(1)], lambda: g.set(s))
        when([g.eq(1)], lambda: h.set(s))

    return build_module(build).to_json()


def _cost(hir_json: str) -> tuple[tuple[int, ...], tuple[int, ...]]:
    static = run_static(hir_json)["static_cost"]
    executed = run_sim(hir_json, 1)["executed_cost"]
    return (
        tuple(static[key] for key in STATIC_KEYS),
        tuple(executed[key] for key in EXEC_KEYS),
    )


class Ticket69SiblingWhenCostPins(unittest.TestCase):
    def test_n1_control_is_unchanged(self) -> None:
        static, executed = _cost(_siblings(1))
        self.assertEqual(static, (4, 1, 69, 9_640))
        self.assertEqual(executed, (4, 1, 69))

    def test_n2(self) -> None:
        static, executed = _cost(_siblings(2))
        self.assertEqual(static, (4, 1, 71, 9_644))
        self.assertEqual(executed, (4, 1, 71))

    def test_n8(self) -> None:
        static, executed = _cost(_siblings(8))
        self.assertEqual(static, (4, 1, 83, 9_668))
        self.assertEqual(executed, (4, 1, 83))

    def test_merged_pair_preserves_transfer_order(self) -> None:
        static, executed = _cost(_transfer_order_pair())
        self.assertEqual(static, (5, 1, 74, 12_044))
        self.assertEqual(executed, (6, 1, 138))
        values = run_sim(_transfer_order_pair(), 1)["var_values"]
        self.assertEqual((values[1], values[2]), (7, 7))

    def test_different_gates_stay_unmerged(self) -> None:
        # The identical-gates precondition, pinned by cost: a merge here
        # would evaluate the second sibling's gate once, at the first
        # sibling's evaluation, instead of re-evaluating it after the first
        # body's write. Values are unchanged, so the cost tuple is the pin.
        static, executed = _cost(_different_gates_pair())
        self.assertEqual(static, (7, 2, 74, 16_868))
        self.assertEqual(executed, (8, 2, 138))
        values = run_sim(_different_gates_pair(), 1)["var_values"]
        self.assertEqual(values, [0, 0, 5, 5, 5])

    def test_two_siblings_inside_while(self) -> None:
        static, executed = _cost(_while_pair())
        self.assertEqual(static, (6, 2, 74, 14_460))
        self.assertEqual(executed, (13, 5, 148))
        values = run_sim(_while_pair(), 1)["var_values"]
        self.assertEqual(values, [2, 7, 7, 9, 9])

    def test_multi_condition_gate(self) -> None:
        static, executed = _cost(_multi_cond())
        self.assertEqual(static, (7, 2, 75, 16_868))
        self.assertEqual(executed, (4, 1, 67))
        values = run_sim(_multi_cond(), 1)["var_values"]
        self.assertEqual(values, [8, 0, 9, 9])

    def test_static_triggers_are_flat_in_sibling_count(self) -> None:
        # O(1) in the sibling count: the merged group lands exactly on the
        # hand-written one-``when`` cost for the same N transfers.
        self.assertEqual(run_static(_siblings(1))["static_cost"]["triggers"], 4)
        self.assertEqual(run_static(_siblings(8))["static_cost"]["triggers"], 4)
        self.assertEqual(_cost(_siblings(8)), _cost(_one_when(8)))
        self.assertEqual(_cost(_siblings(2)), _cost(_one_when(2)))


class Ticket69SiblingWhenSemanticPins(unittest.TestCase):
    def test_merged_transfer_group_writes_both_destinations_through_one_gate(self) -> None:
        # Transfer-body equivalent of the leaf-act merged-gate pin in
        # py/tests/test_ticket6.py: two identical-gate sibling transfer bodies
        # become one static conditional group: one condition count, both
        # destinations written, and the same cost as writing them in one when.
        static, executed = _cost(_siblings(2))
        self.assertEqual(static[1], 1)  # one shared gate, evaluated once
        self.assertEqual(executed[1], 1)
        values = run_sim(_siblings(2), 1)["var_values"]
        # var ids: gate 0, srcs 1..2 (init 1, 2), dsts 3..4 (init 0).
        self.assertEqual(values[3], 1)
        self.assertEqual(values[4], 2)

    def test_guard_keeps_gate_writing_siblings_unmerged(self) -> None:
        # The first body writes the gate variable. Merging would fold the
        # second gate into the first evaluation: 5T static and ``h=8``,
        # silently changing the program. The guard must reject it.
        static, _ = _cost(_guard())
        self.assertEqual(static, (7, 2, 74, 16_864))
        values = run_sim(_guard(), 1)["var_values"]
        self.assertEqual(values, [8, 8, 0])  # g, s, h — h stays 0
        executed = run_sim(_guard(), 2)["executed_cost"]
        self.assertEqual(
            tuple(executed[key] for key in EXEC_KEYS), (2, 2, 0)
        )


if __name__ == "__main__":
    unittest.main()
