"""Ticket #91: merge adjacent identically-gated ``once`` blocks too.

``crates/eudkit-opt/src/temporal.rs`` step 3 merged only sibling ``when``
blocks, so two adjacent ``once`` blocks with identical condition lists each
packed on their own in step 4 into a ``preserved: false`` ``Trigger`` — two
rows that self-disable independently, at full per-row cost. #91 extends the
same step to ``Once``+``Once`` (never ``Once``+``When``) with the *same*
dependency guard #90 defined, so an external-state gate cannot ride over an
earlier body's write (defect A in ``once`` form).

These pins are red before the fix and green after (static cost, the
hand-built one-``once`` control cost):

* gated leaf-act N=8: 8T/8C/8A/19,296 B → 1T/1C/8A/2,440 B;
* gated leaf-act N=2: 2T/2C/2A/4,824 B → 1T/1C/2A/2,416 B;
* gated transfer N=8: 25T/8C/104A/60,236 B → 4T/1C/83A/9,668 B;
* gated transfer N=2: 7T/2C/74A/16,868 B → 4T/1C/71A/9,644 B;
* 0-cond transfer N=8: 25T/0C/104A/60,232 B → 4T/0C/83A/9,664 B;
* paged-var gate with an init trigger: 3T/2C/3A/7,236 B →
  2T/1C/3A/4,828 B, values ``[1, 9, 7]`` both ways.

Transfer twins use a distinct source var per sibling (the #69 shape); a shared
source var does not coalesce and is not the pin. ``executed_cost`` reports the
*last* loop and is ``0T`` for both merged and unmerged ``once``s after loop 1,
so the pins are static cost plus ``var_values``.

The negative pins must stay unmerged, different-gate first (the F1 lesson
from #73: it is the mutation pin for the identical-conds precondition):
different gates 7T/2C/74A/16,868 B; an earlier body that writes the gate var
7T/2C/74A/16,864 B; a non-leaf pair 11T/4C/78A/26,500 B; the external-state
hazard 3T/2C/3A/7,228 B with ``d == 0`` at loops=1 (a naive merge gives
2T/1C/3A/4,820 B with ``d == 7``); the var-gate hazard 3T/2C/3A/7,232 B with
values ``[2, 0]`` (a naive merge gives 2T/1C/3A/4,824 B, values ``[2, 9]``);
and a mixed ``once``+``when`` pair 2T/2C/2A/4,824 B, values ``[1, 1, 4]``
after 4 loops.

The same probes live in ``ts/tests/test_ticket91.ts``.
"""

from __future__ import annotations

import unittest

from py import (
    AtLeast,
    Memory,
    SetDeaths,
    SetMemory,
    SetTo,
    Trigger,
    Var,
    build_module,
    once,
    when,
)
from py.tests.helpers import run_sim, run_static

STATIC_KEYS = ("triggers", "conditions", "actions", "total_bytes")

FLAG = 0x57F0B4


def _static(hir_json: str) -> tuple[int, ...]:
    cost = run_static(hir_json)["static_cost"]
    return tuple(cost[key] for key in STATIC_KEYS)


def _leaf_siblings(n: int) -> str:
    """N sibling ``once``s with one identical paged-var gate and one act each."""

    def build() -> None:
        gate = Var(0)
        vars_ = [Var(0) for _ in range(n)]
        for i, v in enumerate(vars_):
            once([gate.eq(0)], lambda v=v, i=i: v.set(1 + i))

    return build_module(build).to_json()


def _transfer_siblings(n: int) -> str:
    """N sibling ``once``s with one identical-gate transfer each (#69 shape)."""

    def build() -> None:
        gate = Var(0)
        srcs = [Var(1 + i) for i in range(n)]
        dsts = [Var(0) for _ in range(n)]
        for i in range(n):
            once([gate.eq(0)], lambda dst=dsts[i], src=srcs[i]: dst.set(src))

    return build_module(build).to_json()


def _zero_cond_transfer_siblings(n: int) -> str:
    """The same N transfers under 0-cond ``once``s; no unused gate var."""

    def build() -> None:
        srcs = [Var(1 + i) for i in range(n)]
        dsts = [Var(0) for _ in range(n)]
        for i in range(n):
            once([], lambda dst=dsts[i], src=srcs[i]: dst.set(src))

    return build_module(build).to_json()


def _paged_pair_with_init() -> str:
    """Identical paged-var gates plus an unrelated static init trigger."""

    def build() -> None:
        g = Var(1)
        w = Var(0)
        u = Var(0)
        Trigger(conds=[], acts=[SetDeaths(0, SetTo, 1)])
        once([g.eq(1)], lambda: w.set(9))
        once([g.eq(1)], lambda: u.set(7))

    return build_module(build).to_json()


def _different_gates() -> str:
    """Two adjacent transfer ``once``s under *different* gates: must not merge.

    Mutation pin: deleting the identical-conds check merges the pair and the
    static cost drops. The values are identical either way, so the cost tuple
    is the pin.
    """

    def build() -> None:
        g1 = Var(0)
        g2 = Var(0)
        s = Var(5)
        d = Var(0)
        e = Var(0)
        once([g1.eq(0)], lambda: d.set(s))
        once([g2.eq(0)], lambda: e.set(s))

    return build_module(build).to_json()


def _guard_writes_gate() -> str:
    """Same gate, but the earlier body writes the gate variable: must not merge."""

    def build() -> None:
        c = Var(1)
        s = Var(5)
        d = Var(0)
        once([c.eq(1)], lambda: c.set(s))
        once([c.eq(1)], lambda: d.set(s))

    return build_module(build).to_json()


def _nonleaf_pair() -> str:
    """Two identical-gate ``once``s whose bodies are nested ``when``s."""

    def build() -> None:
        g = Var(1)
        h = Var(0)
        s = Var(5)
        d = Var(0)
        e = Var(0)
        once([g.eq(1)], lambda: when([h.eq(0)], lambda: d.set(s)))
        once([g.eq(1)], lambda: when([h.eq(0)], lambda: e.set(s)))

    return build_module(build).to_json()


def _external_state_hazard() -> str:
    """First body clears the memory cell the shared gate reads (defect A)."""

    def build() -> None:
        Trigger(conds=[], acts=[SetMemory(FLAG, SetTo, 1)])
        d = Var(0)
        once([Memory(FLAG, AtLeast, 1)], lambda: SetMemory(FLAG, SetTo, 0))
        once([Memory(FLAG, AtLeast, 1)], lambda: d.set(7))

    return build_module(build).to_json()


def _var_gate_hazard() -> str:
    """First body rewrites the paged-var gate: the second must re-observe it."""

    def build() -> None:
        g = Var(1)
        w = Var(0)
        Trigger(conds=[], acts=[SetDeaths(0, SetTo, 1)])
        once([g.eq(1)], lambda: g.set(2))
        once([g.eq(1)], lambda: w.set(9))

    return build_module(build).to_json()


def _mixed_pair() -> str:
    """``once``+``when`` under the same gate: never merged, different cadences."""

    def build() -> None:
        g = Var(1)
        x = Var(0)
        y = Var(0)
        once([g.eq(1)], lambda: x.set(1))
        when([g.eq(1)], lambda: y.iadd(1))

    return build_module(build).to_json()


class Ticket91OnceSiblingMerge(unittest.TestCase):
    def test_gated_leaf_once_siblings_merge_n8(self) -> None:
        self.assertEqual(run_sim(_leaf_siblings(8), 1)["var_values"], [0, 1, 2, 3, 4, 5, 6, 7, 8])
        self.assertEqual(_static(_leaf_siblings(8)), (1, 1, 8, 2_440))

    def test_gated_leaf_once_siblings_merge_n2(self) -> None:
        self.assertEqual(run_sim(_leaf_siblings(2), 1)["var_values"], [0, 1, 2])
        self.assertEqual(_static(_leaf_siblings(2)), (1, 1, 2, 2_416))

    def test_gated_transfer_once_siblings_merge_n8(self) -> None:
        self.assertEqual(
            run_sim(_transfer_siblings(8), 1)["var_values"],
            [0, 1, 2, 3, 4, 5, 6, 7, 8, 1, 2, 3, 4, 5, 6, 7, 8],
        )
        self.assertEqual(_static(_transfer_siblings(8)), (4, 1, 83, 9_668))

    def test_gated_transfer_once_siblings_merge_n2(self) -> None:
        self.assertEqual(
            run_sim(_transfer_siblings(2), 1)["var_values"],
            [0, 1, 2, 1, 2],
        )
        self.assertEqual(_static(_transfer_siblings(2)), (4, 1, 71, 9_644))

    def test_zero_cond_transfer_once_siblings_merge_n8(self) -> None:
        self.assertEqual(
            run_sim(_zero_cond_transfer_siblings(8), 1)["var_values"],
            [1, 2, 3, 4, 5, 6, 7, 8, 1, 2, 3, 4, 5, 6, 7, 8],
        )
        self.assertEqual(
            _static(_zero_cond_transfer_siblings(8)), (4, 0, 83, 9_664)
        )

    def test_paged_var_gate_once_siblings_merge(self) -> None:
        self.assertEqual(
            run_sim(_paged_pair_with_init(), 1)["var_values"], [1, 9, 7]
        )
        self.assertEqual(_static(_paged_pair_with_init()), (2, 1, 3, 4_828))

    def test_different_gates_stay_unmerged(self) -> None:
        self.assertEqual(_static(_different_gates()), (7, 2, 74, 16_868))

    def test_earlier_body_writing_the_gate_var_stays_unmerged(self) -> None:
        self.assertEqual(_static(_guard_writes_gate()), (7, 2, 74, 16_864))

    def test_nonleaf_once_pair_stays_unmerged(self) -> None:
        self.assertEqual(_static(_nonleaf_pair()), (11, 4, 78, 26_500))

    def test_external_state_gate_hazard_stays_unmerged(self) -> None:
        self.assertEqual(run_sim(_external_state_hazard(), 1)["var_values"], [0])
        self.assertEqual(_static(_external_state_hazard()), (3, 2, 3, 7_228))

    def test_var_gate_hazard_stays_unmerged(self) -> None:
        self.assertEqual(run_sim(_var_gate_hazard(), 1)["var_values"], [2, 0])
        self.assertEqual(_static(_var_gate_hazard()), (3, 2, 3, 7_232))

    def test_mixed_once_and_when_stays_unmerged(self) -> None:
        self.assertEqual(run_sim(_mixed_pair(), 4)["var_values"], [1, 1, 4])
        self.assertEqual(_static(_mixed_pair()), (2, 2, 2, 4_824))


if __name__ == "__main__":
    unittest.main()
