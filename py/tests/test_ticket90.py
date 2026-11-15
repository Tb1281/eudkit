"""Ticket #90: an external-state gate must not merge over an act that writes it.

``crates/eudkit-opt/src/temporal.rs`` step 3 merges adjacent sibling ``when``
blocks with identical condition lists when both bodies are leaf acts. The
merge guard only checks whether a body act carries a ``relocs[].target_var``
in the gate's read set, and ``cond_read_vars`` only collects reloc targets.
A ``Memory`` / ``Deaths`` / ``Switch`` gate has no relocs — it names its
location with an absolute number — so the guard passes vacuously and the
first body's write to that cell is treated as harmless. The merged group then
evaluates the shared gate once, before the first body runs, and the second
body rides along on a stale gate.

These hazard pins are red before the fix and green after; each builds one
init trigger (so the gate holds on loop 1) plus the pair, and asserts the
correct (unmerged) static cost and that the second body never runs:

* ``Memory(FLAG, AtLeast, 1)`` + ``SetMemory(FLAG, SetTo, 0)`` / ``d.set(7)``;
* ``Deaths(EPD(FLAG), AtLeast, 1)`` + ``SetDeaths(...)`` / ``d.set(7)``;
* ``Switch(1, SwitchSet)`` + ``SetSwitch(1, SwitchActionClear)`` / ``d.set(7)``.

The positive pins must stay merged: paged-var gates (plain and with an init
trigger), an external gate whose leaf bodies write only paged vars (the
``body_writes_only_paged_vars`` path), an external gate over a transfer pair,
and a masked ``DeathsX`` read — the last catches a ``locid``/``eudx``
over-tightening of ``cond_keyed_by_paged_var``.

The same probes live in ``ts/tests/test_ticket90.ts``.
"""

from __future__ import annotations

import unittest

from py import (
    AtLeast,
    Deaths,
    DeathsX,
    EPD,
    Memory,
    SetDeaths,
    SetMemory,
    SetSwitch,
    SetTo,
    Switch,
    SwitchActionClear,
    SwitchActionSet,
    SwitchSet,
    Trigger,
    Var,
    build_module,
    when,
)
from py.tests.helpers import run_sim, run_static

STATIC_KEYS = ("triggers", "conditions", "actions", "total_bytes")

FLAG = 0x57F0B4


def _static(hir_json: str) -> tuple[int, ...]:
    cost = run_static(hir_json)["static_cost"]
    return tuple(cost[key] for key in STATIC_KEYS)


def _memory_hazard() -> str:
    """First body clears the memory cell the shared gate reads."""

    def build() -> None:
        Trigger(conds=[], acts=[SetMemory(FLAG, SetTo, 1)])
        d = Var(0)
        when([Memory(FLAG, AtLeast, 1)], lambda: SetMemory(FLAG, SetTo, 0))
        when([Memory(FLAG, AtLeast, 1)], lambda: d.set(7))

    return build_module(build).to_json()


def _deaths_hazard() -> str:
    """Same shape, but the gate and the write go through the Deaths wire form."""

    def build() -> None:
        Trigger(conds=[], acts=[SetMemory(FLAG, SetTo, 1)])
        d = Var(0)
        when(
            [Deaths(EPD(FLAG), AtLeast, 1)],
            lambda: SetDeaths(EPD(FLAG), SetTo, 0),
        )
        when([Deaths(EPD(FLAG), AtLeast, 1)], lambda: d.set(7))

    return build_module(build).to_json()


def _switch_hazard() -> str:
    """Same shape for the Switch condition, which names an absolute switch id."""

    def build() -> None:
        Trigger(conds=[], acts=[SetSwitch(1, SwitchActionSet)])
        d = Var(0)
        when([Switch(1, SwitchSet)], lambda: SetSwitch(1, SwitchActionClear))
        when([Switch(1, SwitchSet)], lambda: d.set(7))

    return build_module(build).to_json()


def _paged_pair() -> str:
    """Identical paged-var gates; both leaf bodies write other variables."""

    def build() -> None:
        g = Var(1)
        w = Var(0)
        u = Var(0)
        when([g.eq(1)], lambda: w.set(9))
        when([g.eq(1)], lambda: u.set(7))

    return build_module(build).to_json()


def _paged_pair_with_init() -> str:
    """The paged-var pair plus an unrelated static init trigger."""

    def build() -> None:
        g = Var(1)
        w = Var(0)
        u = Var(0)
        Trigger(conds=[], acts=[SetDeaths(0, SetTo, 1)])
        when([g.eq(1)], lambda: w.set(9))
        when([g.eq(1)], lambda: u.set(7))

    return build_module(build).to_json()


def _external_gate_paged_bodies() -> str:
    """External gate, but both leaf bodies write only paged variables."""

    def build() -> None:
        Trigger(conds=[], acts=[SetMemory(FLAG, SetTo, 1)])
        w = Var(0)
        u = Var(0)
        when([Memory(FLAG, AtLeast, 1)], lambda: w.set(9))
        when([Memory(FLAG, AtLeast, 1)], lambda: u.set(7))

    return build_module(build).to_json()


def _external_gate_transfer_pair() -> str:
    """External ``Memory`` + ``Switch`` gate over two distinct transfer runs."""

    def build() -> None:
        src0 = Var(3)
        src1 = Var(5)
        dst0 = Var(0)
        dst1 = Var(0)
        Trigger(conds=[], acts=[SetMemory(FLAG, SetTo, 1)])
        Trigger(conds=[], acts=[SetSwitch(1, SwitchActionSet)])
        when(
            [Memory(FLAG, AtLeast, 1), Switch(1, SwitchSet)],
            lambda: dst0.set(src0),
        )
        when(
            [Memory(FLAG, AtLeast, 1), Switch(1, SwitchSet)],
            lambda: dst1.set(src1),
        )

    return build_module(build).to_json()


def _masked_pair() -> str:
    """Masked ``DeathsX`` read carries eudx/locid but is still a paged-var gate."""

    def build() -> None:
        b = Var(3)
        w = Var(0)
        u = Var(0)
        when([DeathsX(b.value, AtLeast, 1, 0, 0xFF)], lambda: w.set(9))
        when([DeathsX(b.value, AtLeast, 1, 0, 0xFF)], lambda: u.set(7))

    return build_module(build).to_json()


class Ticket90ExternalGateMergeGuard(unittest.TestCase):
    def test_memory_gate_hazard_stays_unmerged(self) -> None:
        # Loop 1: FLAG holds, first body clears it; the guarded second gate
        # re-reads it, so d.set(7) never runs.
        self.assertEqual(run_sim(_memory_hazard(), 1)["var_values"], [0])
        self.assertEqual(_static(_memory_hazard()), (3, 2, 3, 7_228))

    def test_deaths_gate_hazard_stays_unmerged(self) -> None:
        self.assertEqual(run_sim(_deaths_hazard(), 1)["var_values"], [0])
        self.assertEqual(_static(_deaths_hazard()), (3, 2, 3, 7_228))

    def test_switch_gate_hazard_stays_unmerged(self) -> None:
        self.assertEqual(run_sim(_switch_hazard(), 1)["var_values"], [0])
        self.assertEqual(_static(_switch_hazard()), (3, 2, 3, 7_228))

    def test_paged_var_gate_still_merges(self) -> None:
        self.assertEqual(run_sim(_paged_pair(), 1)["var_values"], [1, 9, 7])
        self.assertEqual(_static(_paged_pair()), (1, 1, 2, 2_420))

    def test_paged_var_gate_with_init_still_merges(self) -> None:
        self.assertEqual(run_sim(_paged_pair_with_init(), 1)["var_values"], [1, 9, 7])
        self.assertEqual(_static(_paged_pair_with_init()), (2, 1, 3, 4_828))

    def test_external_gate_with_paged_only_bodies_still_merges(self) -> None:
        self.assertEqual(
            run_sim(_external_gate_paged_bodies(), 1)["var_values"], [9, 7]
        )
        self.assertEqual(
            _static(_external_gate_paged_bodies()), (2, 1, 3, 4_824)
        )

    def test_external_gate_transfer_pair_still_merges(self) -> None:
        self.assertEqual(
            run_sim(_external_gate_transfer_pair(), 1)["var_values"], [3, 5, 3, 5]
        )
        self.assertEqual(
            _static(_external_gate_transfer_pair()), (6, 2, 73, 14_456)
        )

    def test_masked_deathsx_gate_still_merges(self) -> None:
        self.assertEqual(run_sim(_masked_pair(), 1)["var_values"], [3, 9, 7])
        self.assertEqual(_static(_masked_pair()), (1, 1, 2, 2_420))


if __name__ == "__main__":
    unittest.main()
