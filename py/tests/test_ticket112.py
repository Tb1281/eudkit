"""Ticket #112: precise external-gate address keys for sibling gate fusion.

``crates/eudkit-opt/src/temporal.rs`` step 3 merges adjacent identically-gated
sibling ``when``/``once`` blocks. #98 phase 1 admitted any *flat* body but kept
the #90 storage guard unchanged: under an external-state gate (``Memory`` /
``Deaths``, condtype 15; ``Switch``, condtype 11) the earlier body had to write
only paged variables, so any absolute write — every ``VarStore``, every
``SetMemory``/``SetDeaths`` — refused the merge even when the body wrote a
*different* dword than the gate reads.

Phase 2 adds a third admission clause: the merge is allowed when every
external cell the earlier body writes is provably a different dword from every
cell the gate reads. The key model is a private ``StorageKey`` in
``temporal.rs``: paged variables (``Var``), EPD addresses (``Epd``, the raw
HIR field value — ``Memory(FLAG).player == SetMemory(FLAG).dest ==
epd_u32(FLAG)``), and switch ids (``Switch``). Unknown writes fall back to
today's answer, so the clause only ever admits a merge; it never withdraws one.

The merge pins below are red before the change and green after; the hazard
pins are green on both sides. Figures are ``(triggers, conditions, actions,
total_bytes)`` from ``run_static``; ``var_values``/``external_epds`` come from
``run_sim``. Every row was re-derived with this repo's own harness, and the
merged reference (the same statements hand-written in one ``when``) reports
identical simulation state.

The unaligned-``dst_addr`` case cannot be built through the DSL after #111
(the frontends reject unaligned absolute dword addresses), so its unit pin
lives in ``crates/eudkit-opt/src/temporal.rs`` next to the key model.

Cycle 3 #02 adds the *backward* merge in the same step 3: an unconditional act
run between two identically-gated ``when`` siblings is moved ahead of the gate
when it provably commutes with the earlier leaf body and the gate's storage
(``when(G){A}; X; when(G){B}`` -> ``X; when(G){A;B}``). The adopted matrix
below is red before that path and green after; the rejected matrix pins the
narrow scope (gate-cell/gate-var writes, RAW/WAR, conditional barriers,
``VarStore`` barriers, and the deliberate ``once`` exclusion). Figures are
``(triggers, conditions, actions, total_bytes)`` from ``run_static``, and the
``exec`` triples are ``(triggers, conditions, actions)`` from ``run_sim``.

The same probes live in ``ts/tests/test_ticket112.ts``.
"""

from __future__ import annotations

import unittest

from py import (
    Always,
    AtLeast,
    CurrentPlayer,
    Deaths,
    EPD,
    FixedString,
    Memory,
    SetDeaths,
    SetMemory,
    SetStringDword,
    SetSwitch,
    SetTo,
    Switch,
    SwitchActionSet,
    SwitchSet,
    Trigger,
    Var,
    build_module,
    emitVarStore,
    once,
    when,
)
from py.tests.helpers import run_sim, run_static

STATIC_KEYS = ("triggers", "conditions", "actions", "total_bytes")
EXEC_KEYS = ("triggers", "conditions", "actions")

FLAG = 0x57F0B4
TABLE = 0x600000
TABLE_EPD = 120_615


def _static(hir_json: str) -> tuple[int, ...]:
    cost = run_static(hir_json)["static_cost"]
    return tuple(cost[key] for key in STATIC_KEYS)


def _executed(hir_json: str, loops: int = 1) -> tuple[int, ...]:
    cost = run_sim(hir_json, loops)["executed_cost"]
    return tuple(cost[key] for key in EXEC_KEYS)


def _memory_gate_eight_store_bodies() -> str:
    """``Memory(FLAG)`` gate, eight single-store bodies at distinct dwords."""

    def build() -> None:
        src = Var(3)
        Trigger(conds=[], acts=[SetMemory(FLAG, SetTo, 1)])
        for i in range(8):
            when(
                [Memory(FLAG, AtLeast, 1)],
                lambda i=i: emitVarStore(src.id, {"addr": TABLE + 4 * i}),
            )

    return build_module(build).to_json()


def _memory_gate_two_setmemory_acts() -> str:
    """``Memory(FLAG)`` gate, two ``SetMemory`` acts on distinct cells."""

    def build() -> None:
        Trigger(conds=[], acts=[SetMemory(FLAG, SetTo, 1)])
        when([Memory(FLAG, AtLeast, 1)], lambda: SetMemory(TABLE, SetTo, 5))
        when([Memory(FLAG, AtLeast, 1)], lambda: SetMemory(TABLE + 4, SetTo, 6))

    return build_module(build).to_json()


def _deaths_gate_two_store_bodies() -> str:
    """``Deaths(EPD(FLAG))`` gate, two store bodies at distinct dwords."""

    def build() -> None:
        src = Var(3)
        Trigger(conds=[], acts=[SetMemory(FLAG, SetTo, 1)])
        when(
            [Deaths(EPD(FLAG), AtLeast, 1)],
            lambda: emitVarStore(src.id, {"addr": TABLE}),
        )
        when(
            [Deaths(EPD(FLAG), AtLeast, 1)],
            lambda: emitVarStore(src.id, {"addr": TABLE + 4}),
        )

    return build_module(build).to_json()


def _mixed_act_store_pair() -> str:
    """``Memory(FLAG)`` gate over ``[act, store]`` and ``[store, act]``."""

    def build() -> None:
        src = Var(3)
        act = Var(1)
        Trigger(conds=[], acts=[SetMemory(FLAG, SetTo, 1)])
        when(
            [Memory(FLAG, AtLeast, 1)],
            lambda: [act.set(5), emitVarStore(src.id, {"addr": TABLE})],
        )
        when(
            [Memory(FLAG, AtLeast, 1)],
            lambda: [emitVarStore(src.id, {"addr": TABLE + 4}), act.set(6)],
        )

    return build_module(build).to_json()


def _switch_gate_two_set_switch_acts() -> str:
    """``Switch(1)`` gate, two ``SetSwitch`` acts on other switches."""

    def build() -> None:
        Trigger(conds=[], acts=[SetSwitch(1, SwitchActionSet)])
        when([Switch(1, SwitchSet)], lambda: SetSwitch(2, SwitchActionSet))
        when([Switch(1, SwitchSet)], lambda: SetSwitch(3, SwitchActionSet))

    return build_module(build).to_json()


def _same_cell_hazard_from_gate_clearing_source() -> str:
    """The earlier body clears the very cell the shared gate reads.

    Unmerged, the second gate re-reads ``FLAG == 0`` and never fires, so
    ``TABLE`` stays unwritten. A merge would evaluate the gate once and let
    both stores run (``TABLE == 7``) — the equality case the key model must
    keep rejecting.
    """

    def build() -> None:
        a = Var(0)
        b = Var(7)
        Trigger(conds=[], acts=[SetMemory(FLAG, SetTo, 1)])
        when(
            [Memory(FLAG, AtLeast, 1)],
            lambda: emitVarStore(a.id, {"addr": FLAG}),
        )
        when(
            [Memory(FLAG, AtLeast, 1)],
            lambda: emitVarStore(b.id, {"addr": TABLE}),
        )

    return build_module(build).to_json()


def _current_player_gate_store_pair() -> str:
    """``Deaths(CurrentPlayer)`` names a dynamic base, so its key is unknown."""

    def build() -> None:
        src = Var(3)
        Trigger(conds=[], acts=[SetMemory(FLAG, SetTo, 1)])
        when(
            [Deaths(CurrentPlayer, AtLeast, 1)],
            lambda: emitVarStore(src.id, {"addr": TABLE}),
        )
        when(
            [Deaths(CurrentPlayer, AtLeast, 1)],
            lambda: emitVarStore(src.id, {"addr": TABLE + 4}),
        )

    return build_module(build).to_json()


def _dst_trigger_store_pair_under_memory_gate() -> str:
    """A trigger-field store writes no address the key model can compare."""

    def build() -> None:
        src = Var(3)
        Trigger(conds=[], acts=[SetMemory(FLAG, SetTo, 1)])
        when(
            [Memory(FLAG, AtLeast, 1)],
            lambda: emitVarStore(src.id, {"trigger_id": 0, "offset": 20}),
        )
        when(
            [Memory(FLAG, AtLeast, 1)],
            lambda: emitVarStore(src.id, {"trigger_id": 0, "offset": 24}),
        )

    return build_module(build).to_json()


def _set_string_dword_pair_under_memory_gate() -> str:
    """``SetStringDword`` writes through a string reloc, not an address key."""

    def build() -> None:
        buf = FixedString(64)
        Trigger(conds=[], acts=[SetMemory(FLAG, SetTo, 1)])
        when(
            [Memory(FLAG, AtLeast, 1)],
            lambda: SetStringDword(buf.message_index, 0, 1),
        )
        when(
            [Memory(FLAG, AtLeast, 1)],
            lambda: SetStringDword(buf.message_index, 4, 2),
        )

    return build_module(build).to_json()


# ---------------------------------------------------------------------------
# Cycle 3 #02: backward merge across an unconditional act run
# ---------------------------------------------------------------------------


def _paged_gate_bare_act_run() -> str:
    """``when(g){A}; SetDeaths(9000); when(g){B}``: an absolute moved act."""

    def build() -> None:
        g = Var(1)
        a = Var(0)
        b = Var(0)
        when([g.eq(1)], lambda: a.set(1))
        SetDeaths(9000, SetTo, 5)
        when([g.eq(1)], lambda: b.set(1))

    return build_module(build).to_json()


def _paged_gate_run_writes_another_var() -> str:
    """Paged gate, the run writes an unrelated variable ``x``."""

    def build() -> None:
        g = Var(1)
        a = Var(0)
        b = Var(0)
        x = Var(0)
        when([g.eq(1)], lambda: a.set(1))
        x.set(5)
        when([g.eq(1)], lambda: b.set(1))

    return build_module(build).to_json()


def _external_gate_run_writes_a_disjoint_cell() -> str:
    """``Memory(FLAG)`` gate (FLAG starts 0), the run writes ``FLAG+4``."""

    def build() -> None:
        when([Memory(FLAG, AtLeast, 1)], lambda: SetDeaths(TABLE, SetTo, 1))
        SetMemory(FLAG + 4, SetTo, 5)
        when([Memory(FLAG, AtLeast, 1)], lambda: SetDeaths(TABLE + 4, SetTo, 2))

    return build_module(build).to_json()


def _three_separated_paged_groups() -> str:
    """Three identically-gated siblings with two unconditional runs between."""

    def build() -> None:
        g = Var(1)
        a = Var(0)
        b = Var(0)
        c = Var(0)
        when([g.eq(1)], lambda: a.set(1))
        SetDeaths(9000, SetTo, 5)
        when([g.eq(1)], lambda: b.set(1))
        SetDeaths(9004, SetTo, 6)
        when([g.eq(1)], lambda: c.set(1))

    return build_module(build).to_json()


def _external_gate_run_writes_the_gate_cell() -> str:
    """``Memory(FLAG)`` gate (FLAG starts 0), the run writes ``FLAG`` itself."""

    def build() -> None:
        when([Memory(FLAG, AtLeast, 1)], lambda: SetDeaths(TABLE, SetTo, 1))
        SetMemory(FLAG, SetTo, 0)
        when([Memory(FLAG, AtLeast, 1)], lambda: SetDeaths(TABLE + 4, SetTo, 2))

    return build_module(build).to_json()


def _paged_gate_run_writes_the_gate_var() -> str:
    """Paged gate, the run writes the gate variable itself (``g.iadd(1)``)."""

    def build() -> None:
        g = Var(1)
        when([g.eq(1)], lambda: SetDeaths(TABLE, SetTo, 1))
        g.iadd(1)
        when([g.eq(1)], lambda: SetDeaths(TABLE + 4, SetTo, 2))

    return build_module(build).to_json()


def _body_reads_the_run_written_var() -> str:
    """RAW: the earlier body reads ``x`` and the run writes ``x``."""

    def build() -> None:
        g = Var(1)
        x = Var(0)
        when([g.eq(1)], lambda: SetDeaths(TABLE, SetTo, x))
        x.set(5)
        when([g.eq(1)], lambda: SetDeaths(TABLE + 4, SetTo, 2))

    return build_module(build).to_json()


def _run_reads_the_body_written_var() -> str:
    """WAR: the earlier body writes ``x`` and the run reads ``x``."""

    def build() -> None:
        g = Var(1)
        x = Var(0)
        when([g.eq(1)], lambda: x.set(5))
        SetDeaths(TABLE, SetTo, x)
        when([g.eq(1)], lambda: SetDeaths(TABLE + 4, SetTo, 2))

    return build_module(build).to_json()


def _conditional_trigger_between_the_gates() -> str:
    """A conditional intervening trigger is not an unconditional act run."""

    def build() -> None:
        g = Var(1)
        a = Var(0)
        b = Var(0)
        when([g.eq(1)], lambda: a.set(1))
        when([Always()], lambda: SetDeaths(TABLE, SetTo, 5))
        when([g.eq(1)], lambda: b.set(1))

    return build_module(build).to_json()


def _two_separated_once_siblings() -> str:
    """Two separated ``once`` blocks: the backward path is ``when`` only."""

    def build() -> None:
        g = Var(1)
        a = Var(0)
        b = Var(0)
        once([g.eq(1)], lambda: a.set(1))
        SetDeaths(9000, SetTo, 5)
        once([g.eq(1)], lambda: b.set(1))

    return build_module(build).to_json()


def _adjacent_paged_when_control() -> str:
    """Adjacent identical gates: the existing forward merge, unchanged."""

    def build() -> None:
        g = Var(1)
        a = Var(0)
        b = Var(0)
        when([g.eq(1)], lambda: a.set(1))
        when([g.eq(1)], lambda: b.set(1))

    return build_module(build).to_json()


class Ticket112BackwardMergeAdopted(unittest.TestCase):
    def test_paged_gate_merges_across_a_bare_act_run(self) -> None:
        # Before: 3/2/3/7,228 static, 3/2/3 executed.
        sim = run_sim(_paged_gate_bare_act_run(), 1)
        self.assertEqual(sim["var_values"], [1, 1, 1])
        self.assertEqual(sim["external_epds"].get("9000", 0), 5)
        self.assertEqual(_executed(_paged_gate_bare_act_run()), (2, 1, 3))
        self.assertEqual(
            _static(_paged_gate_bare_act_run()), (2, 1, 3, 4_820)
        )

    def test_paged_gate_merges_across_a_var_write(self) -> None:
        # Before: 3/2/3/7,232 static, 3/2/3 executed.
        sim = run_sim(_paged_gate_run_writes_another_var(), 1)
        self.assertEqual(sim["var_values"], [1, 1, 1, 5])
        self.assertEqual(
            _executed(_paged_gate_run_writes_another_var()), (2, 1, 3)
        )
        self.assertEqual(
            _static(_paged_gate_run_writes_another_var()), (2, 1, 3, 4_824)
        )

    def test_external_gate_merges_across_a_disjoint_cell_write(self) -> None:
        # Before: 3/2/3/7,224 static, 3/2/1 executed. FLAG stays 0, so the
        # bodies do not run; only the moved disjoint write executes.
        sim = run_sim(_external_gate_run_writes_a_disjoint_cell(), 1)
        flag_key = str(EPD(FLAG) & 0xFFFFFFFF)
        moved_key = str(EPD(FLAG + 4) & 0xFFFFFFFF)
        self.assertEqual(sim["external_epds"].get(flag_key, 0), 0)
        self.assertEqual(sim["external_epds"].get(moved_key, 0), 5)
        self.assertEqual(
            _executed(_external_gate_run_writes_a_disjoint_cell()), (2, 1, 1)
        )
        self.assertEqual(
            _static(_external_gate_run_writes_a_disjoint_cell()),
            (2, 1, 3, 4_816),
        )

    def test_three_separated_groups_merge_into_one_gate(self) -> None:
        # Before: 5/3/5/12,044 static, 5/3/5 executed. After: 2T, not 3T —
        # the two moved unconditional acts pack into one trigger.
        sim = run_sim(_three_separated_paged_groups(), 1)
        self.assertEqual(sim["var_values"], [1, 1, 1, 1])
        self.assertEqual(sim["external_epds"].get("9000", 0), 5)
        self.assertEqual(sim["external_epds"].get("9004", 0), 6)
        self.assertEqual(
            _executed(_three_separated_paged_groups()), (2, 1, 5)
        )
        self.assertEqual(
            _static(_three_separated_paged_groups()), (2, 1, 5, 4_820)
        )


class Ticket112BackwardMergeRejected(unittest.TestCase):
    def test_external_gate_run_writing_the_gate_cell_stays_unmerged(self) -> None:
        # Before and after: 3/2/3/7,224 static, 3/2/1 executed.
        sim = run_sim(_external_gate_run_writes_the_gate_cell(), 1)
        self.assertEqual(sim["external_epds"].get(str(EPD(FLAG) & 0xFFFFFFFF), 0), 0)
        self.assertEqual(
            _executed(_external_gate_run_writes_the_gate_cell()), (3, 2, 1)
        )
        self.assertEqual(
            _static(_external_gate_run_writes_the_gate_cell()),
            (3, 2, 3, 7_224),
        )

    def test_paged_gate_run_writing_the_gate_var_stays_unmerged(self) -> None:
        # Before and after: 3/2/3/7,228 static, 3/2/2 executed.
        sim = run_sim(_paged_gate_run_writes_the_gate_var(), 1)
        self.assertEqual(sim["var_values"], [2])
        self.assertEqual(
            _executed(_paged_gate_run_writes_the_gate_var()), (3, 2, 2)
        )
        self.assertEqual(
            _static(_paged_gate_run_writes_the_gate_var()), (3, 2, 3, 7_228)
        )

    def test_body_read_of_a_run_written_var_stays_unmerged(self) -> None:
        # Before and after: 3/2/3/7,232 static, 3/2/3 executed.
        sim = run_sim(_body_reads_the_run_written_var(), 1)
        self.assertEqual(sim["var_values"], [1, 5])
        self.assertEqual(
            _executed(_body_reads_the_run_written_var()), (3, 2, 3)
        )
        self.assertEqual(
            _static(_body_reads_the_run_written_var()), (3, 2, 3, 7_232)
        )

    def test_run_read_of_a_body_written_var_stays_unmerged(self) -> None:
        # Before and after: 3/2/3/7,232 static, 3/2/3 executed.
        sim = run_sim(_run_reads_the_body_written_var(), 1)
        self.assertEqual(sim["var_values"], [1, 5])
        self.assertEqual(
            _executed(_run_reads_the_body_written_var()), (3, 2, 3)
        )
        self.assertEqual(
            _static(_run_reads_the_body_written_var()), (3, 2, 3, 7_232)
        )

    def test_conditional_trigger_between_the_gates_stays_unmerged(self) -> None:
        # Before and after: 3/3/3/7,228 static, 3/3/3 executed.
        sim = run_sim(_conditional_trigger_between_the_gates(), 1)
        self.assertEqual(sim["var_values"], [1, 1, 1])
        self.assertEqual(
            _executed(_conditional_trigger_between_the_gates()), (3, 3, 3)
        )
        self.assertEqual(
            _static(_conditional_trigger_between_the_gates()), (3, 3, 3, 7_228)
        )

    def test_separated_once_siblings_stay_unmerged(self) -> None:
        # Before and after: 3/2/3/7,228 static, 3/2/3 executed.
        sim = run_sim(_two_separated_once_siblings(), 1)
        self.assertEqual(sim["var_values"], [1, 1, 1])
        self.assertEqual(sim["external_epds"].get("9000", 0), 5)
        self.assertEqual(_executed(_two_separated_once_siblings()), (3, 2, 3))
        self.assertEqual(
            _static(_two_separated_once_siblings()), (3, 2, 3, 7_228)
        )

    def test_adjacent_paged_when_control_is_unchanged(self) -> None:
        # The existing forward merge control: 1/1/2/2,412, exec 1/1/2.
        sim = run_sim(_adjacent_paged_when_control(), 1)
        self.assertEqual(sim["var_values"], [1, 1, 1])
        self.assertEqual(_executed(_adjacent_paged_when_control()), (1, 1, 2))
        self.assertEqual(
            _static(_adjacent_paged_when_control()), (1, 1, 2, 2_412)
        )


class Ticket112ExternalGateMerge(unittest.TestCase):
    def test_memory_gate_over_eight_store_bodies_merges(self) -> None:
        self.assertEqual(
            run_sim(_memory_gate_eight_store_bodies(), 1)["var_values"], [3]
        )
        self.assertEqual(
            _static(_memory_gate_eight_store_bodies()),
            (12, 1, 91, 28_896),
        )

    def test_memory_gate_over_two_setmemory_acts_merges(self) -> None:
        self.assertEqual(
            run_sim(_memory_gate_two_setmemory_acts(), 1)["var_values"], []
        )
        self.assertEqual(
            _static(_memory_gate_two_setmemory_acts()), (2, 1, 3, 4_816)
        )

    def test_deaths_gate_over_two_store_bodies_merges(self) -> None:
        self.assertEqual(
            run_sim(_deaths_gate_two_store_bodies(), 1)["var_values"], [3]
        )
        self.assertEqual(
            _static(_deaths_gate_two_store_bodies()), (6, 1, 73, 14_448)
        )

    def test_memory_gate_over_mixed_act_store_pair_merges(self) -> None:
        self.assertEqual(
            run_sim(_mixed_act_store_pair(), 1)["var_values"], [3, 6]
        )
        self.assertEqual(_static(_mixed_act_store_pair()), (6, 1, 75, 14_452))

    def test_switch_gate_over_two_set_switch_acts_merges(self) -> None:
        self.assertEqual(
            run_sim(_switch_gate_two_set_switch_acts(), 1)["var_values"], []
        )
        self.assertEqual(
            _static(_switch_gate_two_set_switch_acts()), (2, 1, 3, 4_816)
        )


class Ticket112ExternalGateHazards(unittest.TestCase):
    def test_same_cell_hazard_stays_unmerged(self) -> None:
        # Naive merge: (5, 1, 72, 12_040) with TABLE == 7. Unmerged, the
        # second gate re-reads FLAG == 0, so TABLE is never written.
        sim = run_sim(_same_cell_hazard_from_gate_clearing_source(), 1)
        self.assertEqual(sim["var_values"], [0, 7])
        self.assertEqual(sim["external_epds"].get(str(TABLE_EPD), 0), 0)
        self.assertEqual(
            _static(_same_cell_hazard_from_gate_clearing_source()),
            (8, 2, 75, 19_264),
        )

    def test_current_player_gate_stays_unmerged(self) -> None:
        # Naive merge: (6, 1, 73, 14_448). CurrentPlayer is a dynamic base,
        # so the gate's key is unknown and must not compare as a dword.
        self.assertEqual(
            run_sim(_current_player_gate_store_pair(), 1)["var_values"], [3]
        )
        self.assertEqual(
            _static(_current_player_gate_store_pair()), (8, 2, 75, 19_264)
        )

    def test_dst_trigger_store_pair_stays_unmerged(self) -> None:
        # Naive merge: (6, 1, 73, 14_448). A trigger field is not an address.
        self.assertEqual(
            run_sim(_dst_trigger_store_pair_under_memory_gate(), 1)["var_values"],
            [3],
        )
        self.assertEqual(
            _static(_dst_trigger_store_pair_under_memory_gate()),
            (8, 2, 75, 19_264),
        )

    def test_set_string_dword_pair_stays_unmerged(self) -> None:
        # Naive merge: (2, 1, 3, 4_816). A string-buffer write carries a
        # target_string reloc at the dest field, not a comparable address.
        self.assertEqual(
            run_sim(_set_string_dword_pair_under_memory_gate(), 1)["var_values"],
            [],
        )
        self.assertEqual(
            _static(_set_string_dword_pair_under_memory_gate()),
            (3, 2, 3, 7_224),
        )


if __name__ == "__main__":
    unittest.main()
