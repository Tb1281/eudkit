"""Ticket #98: sibling gate fusion admitted for any flat body (phase 1).

``crates/eudkit-opt/src/temporal.rs`` step 3 merges adjacent identically-gated
sibling ``when``/``once`` blocks. Before #98 the admission predicate only
accepted two body kinds: leaf act runs (up to the 64-act ``Trigger`` cap) and
pure ``VarTransfer``/``VarBatch`` runs. A body holding a ``VarStore`` — or a
transfer and an act side by side — never merged even under a plain paged-var
gate the #90 hazard guard would accept.

Phase 1 widens the admission to any *flat* body (``Act | VarTransfer |
VarBatch | VarStore``); nested ``when``/``once``/``While`` still rejects, and
the #90 storage clauses are reused unchanged. Phase 1 left an external-state
gate (``Memory``/``Deaths``/``Switch``) over a ``VarStore`` body unmerged;
phase 2 (#112) now adds a precise-address clause, so the ``Memory(FLAG)`` pair
over stores at ``TABLE``/``TABLE+4`` merges and moved into the merge class
below.

The merge pins below are red before the change and green after; the hazard
pins are green on both sides (they pin that the widening did not weaken the
#90 guard).  Figures are ``(triggers, conditions, actions, total_bytes)`` from
``run_static``; ``var_values`` comes from ``run_sim``.  All rows were
re-derived with this repo's own harness on the #98 branch.

The same probes live in ``ts/tests/test_ticket98.ts``.
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
    emitVarStore,
    when,
)
from py.tests.helpers import run_sim, run_static

STATIC_KEYS = ("triggers", "conditions", "actions", "total_bytes")

FLAG = 0x57F0B4
TABLE = 0x600000


def _static(hir_json: str) -> tuple[int, ...]:
    cost = run_static(hir_json)["static_cost"]
    return tuple(cost[key] for key in STATIC_KEYS)


def _eight_store_bodies() -> str:
    """Paged gate ``g == 1``, eight single-``VarStore`` bodies.

    The idiomatic unrolled ``for i: if g: table[i] = src`` shape.  A store
    needs no read-set admission (its destination is an absolute address), so
    all eight bodies share one firing.
    """

    def build() -> None:
        g = Var(1)
        src = Var(3)
        for i in range(8):
            when([g.eq(1)], lambda i=i: emitVarStore(src.id, {"addr": TABLE + 4 * i}))

    return build_module(build).to_json()


def _four_pair_store_bodies() -> str:
    """Paged gate, four bodies of two stores each: the accumulated re-check."""

    def build() -> None:
        g = Var(1)
        src = Var(3)
        for i in range(4):
            when(
                [g.eq(1)],
                lambda i=i: [
                    emitVarStore(src.id, {"addr": TABLE + 8 * i}),
                    emitVarStore(src.id, {"addr": TABLE + 4 + 8 * i}),
                ],
            )

    return build_module(build).to_json()


def _mixed_act_transfer_pair() -> str:
    """Paged gate over ``[act, act]`` and ``[transfer, act]`` bodies."""

    def build() -> None:
        g = Var(1)
        w = Var(0)
        src = Var(3)
        when([g.eq(1)], lambda: [w.set(9), SetDeaths(TABLE, SetTo, 1)])
        when([g.eq(1)], lambda: [src.set(w), SetDeaths(TABLE + 4, SetTo, 2)])

    return build_module(build).to_json()


def _store_transfer_pair() -> str:
    """Paged gate over two ``[VarStore, VarTransfer]`` bodies."""

    def build() -> None:
        g = Var(1)
        a = Var(3)
        b = Var(5)
        x = Var(0)
        y = Var(0)
        when([g.eq(1)], lambda: [emitVarStore(a.id, {"addr": TABLE}), x.set(a)])
        when([g.eq(1)], lambda: [emitVarStore(b.id, {"addr": TABLE + 4}), y.set(b)])

    return build_module(build).to_json()


def _dst_trigger_pair() -> str:
    """Paged gate over two trigger-field store bodies.

    A ``VarStore`` with ``dst_trigger`` writes a trigger field, not paged
    storage, so a paged gate (which reads only paged storage) is safe; an
    external gate would still reject the body (clause (ii)).
    """

    def build() -> None:
        g = Var(1)
        src = Var(3)
        Trigger(conds=[], acts=[])
        when([g.eq(1)], lambda: emitVarStore(src.id, {"trigger_id": 0, "offset": 20}))
        when([g.eq(1)], lambda: emitVarStore(src.id, {"trigger_id": 0, "offset": 24}))

    return build_module(build).to_json()


def _external_gate_store_pair() -> str:
    """External ``Memory`` gate over two store bodies at distinct dwords.

    Phase 2 (#112) keys both stores on ``Epd(TABLE)``/``Epd(TABLE+4)`` and the
    gate on ``Epd(FLAG)``, so the pair merges; this records that hand-off.
    """

    def build() -> None:
        src = Var(3)
        Trigger(conds=[], acts=[SetMemory(FLAG, SetTo, 1)])
        when(
            [Memory(FLAG, AtLeast, 1)],
            lambda: emitVarStore(src.id, {"addr": TABLE}),
        )
        when(
            [Memory(FLAG, AtLeast, 1)],
            lambda: emitVarStore(src.id, {"addr": TABLE + 4}),
        )

    return build_module(build).to_json()


def _gate_writing_store_body() -> str:
    """First body writes the gate variable, then a store: must stay apart."""

    def build() -> None:
        g = Var(1)
        src = Var(3)
        d = Var(0)
        when([g.eq(1)], lambda: [g.set(2), emitVarStore(src.id, {"addr": TABLE})])
        when([g.eq(1)], lambda: d.set(9))

    return build_module(build).to_json()


def _cap_boundary(first: int, second: int) -> str:
    """Two identically-gated leaf-act bodies straddling the 64-act cap."""

    def build() -> None:
        g = Var(1)
        when(
            [g.eq(1)],
            lambda: [SetDeaths(TABLE + 4 * i, SetTo, 1) for i in range(first)],
        )
        when(
            [g.eq(1)],
            lambda: [SetDeaths(TABLE + 4 * (100 + i), SetTo, 1) for i in range(second)],
        )

    return build_module(build).to_json()


def _three_siblings() -> str:
    """Three identically-gated paged siblings, one paged act each."""

    def build() -> None:
        g = Var(1)
        a = Var(0)
        b = Var(0)
        c = Var(0)
        when([g.eq(1)], lambda: a.set(1))
        when([g.eq(1)], lambda: b.set(2))
        when([g.eq(1)], lambda: c.set(3))

    return build_module(build).to_json()


def _three_siblings_middle_writes_gate() -> str:
    """The middle sibling writes the gate variable: the third must not ride."""

    def build() -> None:
        a = Var(0)
        b = Var(0)
        g = Var(1)
        c = Var(0)
        when([g.eq(1)], lambda: a.set(2))
        when([g.eq(1)], lambda: [g.set(2), b.set(1)])
        when([g.eq(1)], lambda: c.set(3))

    return build_module(build).to_json()


def _ordering_after_merge() -> str:
    """Body-2's store reads body-1's act; a later gate must see the store."""

    def build() -> None:
        g = Var(1)
        v = Var(0)
        d = Var(0)
        when([g.eq(1)], lambda: v.set(5))
        when([g.eq(1)], lambda: emitVarStore(v.id, {"addr": TABLE}))
        when([Memory(TABLE, AtLeast, 5)], lambda: d.iadd(1))

    return build_module(build).to_json()


def _reader_after_transfer() -> str:
    """Body-2's transfer reads body-1's transfer destination.

    The merge must not hoist the transfer ahead of body-1's firing: ``z = x``
    has to observe the transfer ``x = y``, so ``z`` ends at 5, not the stale 0.

    Both bodies are transfers (``Var.set(Var)`` emits a ``VarTransfer``), so
    this pin does not exercise the scheduler's act-hoist guard
    (``act_is_agnostic``) — the two guard pins below do that.
    """

    def build() -> None:
        g = Var(1)
        x = Var(0)
        y = Var(5)
        z = Var(0)
        when([g.eq(1)], lambda: x.set(y))
        when([g.eq(1)], lambda: z.set(x))

    return build_module(build).to_json()


def _act_writing_merged_store_source() -> str:
    """Body-2 act writes the source of body-1's store.

    The scheduler must keep the act behind the store firing: the store has
    to read ``v == 0`` before ``v.set(2)`` runs, so a later ``Memory(TABLE)
    >= 1`` gate must not fire. This is a scheduler-guard pin, not a merge
    pin: it is green on the pre-#98 tree too (separate triggers preserve
    order) and goes red if ``act_is_agnostic`` ever claims the act can run
    either side of the store firing, so no static cost is pinned here.
    """

    def build() -> None:
        g = Var(1)
        v = Var(0)
        d = Var(0)
        when([g.eq(1)], lambda: emitVarStore(v.id, {"addr": TABLE}))
        when([g.eq(1)], lambda: v.set(2))
        when([Memory(TABLE, AtLeast, 1)], lambda: d.set(1))

    return build_module(build).to_json()


def _opaque_act_after_merged_store() -> str:
    """Body-2 act writes the merged store's destination.

    ``SetMemory`` has an opaque (absolute-address) destination, so the
    scheduler must keep it behind the store firing: the store writes
    ``TABLE = 0`` first, then the act writes ``TABLE = 9``. Same guard-pin
    shape as above — green before #98, red under a broken
    ``act_is_agnostic`` — so no static cost is pinned here.
    """

    def build() -> None:
        g = Var(1)
        v = Var(0)
        d = Var(0)
        when([g.eq(1)], lambda: emitVarStore(v.id, {"addr": TABLE}))
        when([g.eq(1)], lambda: SetMemory(TABLE, SetTo, 9))
        when([Memory(TABLE, AtLeast, 1)], lambda: d.set(1))

    return build_module(build).to_json()


class Ticket98FlatBodyMerge(unittest.TestCase):
    def test_eight_store_bodies_share_one_firing(self) -> None:
        self.assertEqual(run_sim(_eight_store_bodies(), 1)["var_values"], [1, 3])
        self.assertEqual(_static(_eight_store_bodies()), (11, 1, 90, 26_492))

    def test_four_pair_store_bodies_share_one_firing(self) -> None:
        self.assertEqual(run_sim(_four_pair_store_bodies(), 1)["var_values"], [1, 3])
        self.assertEqual(_static(_four_pair_store_bodies()), (11, 1, 90, 26_492))

    def test_mixed_act_transfer_pair_shares_one_gate(self) -> None:
        self.assertEqual(
            run_sim(_mixed_act_transfer_pair(), 1)["var_values"], [1, 9, 9]
        )
        self.assertEqual(_static(_mixed_act_transfer_pair()), (4, 1, 72, 9_640))

    def test_store_transfer_pair_shares_one_gate(self) -> None:
        self.assertEqual(
            run_sim(_store_transfer_pair(), 1)["var_values"], [1, 3, 5, 3, 5]
        )
        self.assertEqual(_static(_store_transfer_pair()), (6, 1, 77, 14_460))

    def test_dst_trigger_store_pair_shares_one_gate(self) -> None:
        self.assertEqual(run_sim(_dst_trigger_pair(), 1)["var_values"], [1, 3])
        self.assertEqual(_static(_dst_trigger_pair()), (6, 1, 72, 14_452))

    def test_external_gate_over_store_bodies_merges(self) -> None:
        # Phase 2 (#112): the gate reads Epd(FLAG), the bodies write
        # Epd(TABLE)/Epd(TABLE+4), so the dwords are provably disjoint.
        # Simulation state is identical to the pre-#112 unmerged pin.
        self.assertEqual(run_sim(_external_gate_store_pair(), 1)["var_values"], [3])
        self.assertEqual(
            run_sim(_external_gate_store_pair(), 1)["external_epds"],
            {"120615": 3, "120616": 3, "4294955860": 1},
        )
        self.assertEqual(_static(_external_gate_store_pair()), (6, 1, 73, 14_448))


class Ticket98FlatBodyHazards(unittest.TestCase):
    def test_store_body_that_writes_the_gate_var_stays_unmerged(self) -> None:
        self.assertEqual(
            run_sim(_gate_writing_store_body(), 1)["var_values"], [2, 3, 0]
        )
        self.assertEqual(_static(_gate_writing_store_body()), (5, 2, 71, 12_048))


class Ticket98CapAndAccumulation(unittest.TestCase):
    def test_leaf_act_cap_still_bounds_a_leaf_pair(self) -> None:
        # 63+1 fits one 64A trigger; 63+2 does not, and the pair must not
        # fall through to the uncapped flat admission.
        self.assertEqual(_static(_cap_boundary(63, 1)), (1, 1, 64, 2_412))
        self.assertEqual(_static(_cap_boundary(63, 2)), (2, 2, 65, 4_820))

    def test_three_leaf_siblings_all_merge(self) -> None:
        self.assertEqual(run_sim(_three_siblings(), 1)["var_values"], [1, 1, 2, 3])
        self.assertEqual(_static(_three_siblings()), (1, 1, 3, 2_420))

    def test_third_sibling_does_not_ride_on_a_stale_gate(self) -> None:
        self.assertEqual(
            run_sim(_three_siblings_middle_writes_gate(), 1)["var_values"],
            [2, 1, 2, 0],
        )
        self.assertEqual(
            _static(_three_siblings_middle_writes_gate()), (3, 3, 4, 7_240)
        )


class Ticket98Ordering(unittest.TestCase):
    def test_store_in_a_merged_body_keeps_source_order(self) -> None:
        # Merging body 1 into body 2's store must not hoist the store ahead
        # of body-1's act: the later `Memory(X) >= 5` gate has to observe the
        # store, so `d` reaches 2 after two loops (a hoist would see v == 0).
        self.assertEqual(run_sim(_ordering_after_merge(), 2)["var_values"], [1, 5, 2])
        self.assertEqual(_static(_ordering_after_merge()), (5, 2, 71, 12_048))

    def test_transfer_reading_a_merged_transfer_destination_runs_after_it(
        self,
    ) -> None:
        # `when(g){x=y}` + `when(g){z=x}` merges into one gate; the scheduler
        # must keep `z = x` behind the transfer firing, so z is 5, not 0.
        self.assertEqual(run_sim(_reader_after_transfer(), 1)["var_values"], [1, 5, 5, 5])
        self.assertEqual(_static(_reader_after_transfer()), (5, 1, 72, 12_048))

    def test_act_writing_a_merged_store_source_stays_behind_the_store(
        self,
    ) -> None:
        # A hoist would let the store read v == 2, so the Memory gate would
        # fire and d would reach 1; correct order leaves TABLE == 0, d == 0.
        self.assertEqual(
            run_sim(_act_writing_merged_store_source(), 1)["var_values"],
            [1, 2, 0],
        )

    def test_opaque_act_after_a_merged_store_stays_behind_it(self) -> None:
        # A hoist would let the store overwrite the act, leaving TABLE == 0
        # and d == 0; correct order leaves TABLE == 9, so the gate fires.
        self.assertEqual(
            run_sim(_opaque_act_after_merged_store(), 1)["var_values"],
            [1, 0, 1],
        )


if __name__ == "__main__":
    unittest.main()
