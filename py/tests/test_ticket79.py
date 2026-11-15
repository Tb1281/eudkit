"""Ticket #79: guard the leaf-act sibling ``when`` merge against RAW hazards.

``crates/eudkit-opt/src/temporal.rs`` merges adjacent sibling ``when`` blocks
with identical condition lists when both bodies are leaf acts. The transfer
merge has always been dependency-guarded, but the leaf-act merge was not: the
merged group evaluates the shared gate once, so a first body that writes a
variable the gate reads lets the second body run against a stale gate.

These pins are red before the fix and green after:

* RAW probe (``when(v==1){v.set(2)}`` then ``when(v==1){w.set(9)}``, ``v=1``):
  the second body must not run, so ``w`` stays ``0`` (a naive merge gives
  ``w=9``). Static cost 2T/2C/2A/4,824 B because the siblings stay apart;
* merge-positive control (same gate, bodies writing other variables):
  1T/1C/2A/2,420 B static and both writes land.

The same probes live in ``ts/tests/test_ticket79.ts``.
"""

from __future__ import annotations

import unittest

from py import Var, build_module, when
from py.tests.helpers import run_sim, run_static

STATIC_KEYS = ("triggers", "conditions", "actions", "total_bytes")


def _hazard() -> str:
    """First body falsifies the shared gate; the second must not ride along."""

    def build() -> None:
        v = Var(1)
        w = Var(0)
        when([v.eq(1)], lambda: v.set(2))
        when([v.eq(1)], lambda: w.set(9))

    return build_module(build).to_json()


def _positive() -> str:
    """Identical gate, bodies write variables the gate does not read."""

    def build() -> None:
        g = Var(1)
        w = Var(0)
        u = Var(0)
        when([g.eq(1)], lambda: w.set(9))
        when([g.eq(1)], lambda: u.set(7))

    return build_module(build).to_json()


def _static(hir_json: str) -> tuple[int, ...]:
    cost = run_static(hir_json)["static_cost"]
    return tuple(cost[key] for key in STATIC_KEYS)


class Ticket79LeafActMergeGuard(unittest.TestCase):
    def test_raw_probe_stays_unmerged_and_keeps_w_zero(self) -> None:
        # Vars: [v, w], v init 1. Loop 1: v==1, so v.set(2) runs; with the
        # guard the second gate re-reads v and w.set(9) never runs.
        self.assertEqual(run_sim(_hazard(), 2)["var_values"], [2, 0])
        self.assertEqual(_static(_hazard()), (2, 2, 2, 4_824))

    def test_merge_positive_case_still_merges(self) -> None:
        # Neither body touches the gate variable, so the siblings share one
        # gate evaluation: one trigger / one condition / both acts.
        self.assertEqual(run_sim(_positive(), 1)["var_values"], [1, 9, 7])
        self.assertEqual(_static(_positive()), (1, 1, 2, 2_420))


if __name__ == "__main__":
    unittest.main()
