"""Ticket #8: fixed-out-box ``Queue``/``Deque``/``Stack``/``UnitGroup`` tests."""

from __future__ import annotations

import json
import subprocess
import unittest
from pathlib import Path

from py import (
    Deque,
    Queue,
    Stack,
    TriggerStruct,
    UnitGroup,
    Var,
    build_module,
    dword,
    when,
)

from py.tests.helpers import run_sim
REPO_ROOT = Path(__file__).resolve().parent.parent.parent
def module_stmts(hir_json: str) -> list[dict]:
    return json.loads(hir_json)["module"]["stmts"]

def when_body(stmts: list[dict], index: int) -> list[dict]:
    whens = [s for s in stmts if s["kind"] == "when"]
    body = whens[index]["body"]
    assert body, f"expected when statement #{index}"
    return body

def body_action_count(body: list[dict]) -> int:
    total = 0
    for s in body:
        if s["kind"] == "trigger":
            total += len(s["acts"])
        elif s["kind"] == "act":
            total += 1
    return total

class Ticket8Tests(unittest.TestCase):
    def test_queue_is_fifo_and_round_trips_scalars_across_loops(self):
        ids: dict[str, int] = {}

        def prog() -> None:
            q = Queue("dword", 4)
            phase = Var(0)
            out1 = Var(0)
            out2 = Var(0)
            when([phase.eq(0)], lambda: (q.append(11), q.append(22)))
            when([phase.eq(1)], lambda: out1.set(q.popleft()))
            when([phase.eq(2)], lambda: out2.set(q.popleft()))
            phase.iadd(1)
            ids.update(
                length=q.length.id,
                head=q.head.id,
                tail=q.tail.id,
                out1=out1.id,
                out2=out2.id,
            )

        report = run_sim(build_module(prog).to_json(), 5)
        self.assertEqual(report["var_values"][ids["out1"]], 11)
        self.assertEqual(report["var_values"][ids["out2"]], 22)
        self.assertEqual(report["var_values"][ids["length"]], 0)
        self.assertEqual(report["var_values"][ids["head"]], 2)
        self.assertEqual(report["var_values"][ids["tail"]], 2)

    def test_deque_supports_appendleft_popleft_and_pop(self):
        ids: dict[str, int] = {}

        def prog() -> None:
            q = Deque("dword", 4)
            phase = Var(0)
            out1 = Var(0)
            out2 = Var(0)
            out3 = Var(0)
            when([phase.eq(0)], lambda: (q.append(1), q.append(2), q.appendleft(0)))
            when([phase.eq(1)], lambda: out1.set(q.popleft()))
            when([phase.eq(2)], lambda: out2.set(q.pop()))
            when([phase.eq(3)], lambda: out3.set(q.popleft()))
            phase.iadd(1)
            ids.update(length=q.length.id, out1=out1.id, out2=out2.id, out3=out3.id)

        report = run_sim(build_module(prog).to_json(), 6)
        self.assertEqual(report["var_values"][ids["out1"]], 0)
        self.assertEqual(report["var_values"][ids["out2"]], 2)
        self.assertEqual(report["var_values"][ids["out3"]], 1)
        self.assertEqual(report["var_values"][ids["length"]], 0)

    def test_stack_is_lifo_over_page_backed_array_storage(self):
        ids: dict[str, int] = {}

        def prog() -> None:
            s = Stack(4)
            phase = Var(0)
            out1 = Var(0)
            out2 = Var(0)
            out3 = Var(0)
            when([phase.eq(0)], lambda: (s.push(11), s.push(22), s.push(33)))
            when([phase.eq(1)], lambda: out1.set(s.pop()))
            when([phase.eq(2)], lambda: out2.set(s.pop()))
            when([phase.eq(3)], lambda: out3.set(s.pop()))
            phase.iadd(1)
            ids.update(pos=s.pos.id, out1=out1.id, out2=out2.id, out3=out3.id)

        report = run_sim(build_module(prog).to_json(), 5)
        self.assertEqual(report["var_values"][ids["out1"]], 33)
        self.assertEqual(report["var_values"][ids["out2"]], 22)
        self.assertEqual(report["var_values"][ids["out3"]], 11)
        self.assertEqual(report["var_values"][ids["pos"]], 0)

    def test_queue_round_trips_struct_and_binds_all_field_destinations(self):
        ids: dict[str, int] = {}

        def prog() -> None:
            Point = TriggerStruct("Point", x=dword(), y=dword())
            q = Queue(Point, 2)
            phase = Var(0)
            ox1 = Var(0)
            oy1 = Var(0)
            ox2 = Var(0)
            oy2 = Var(0)
            when(
                [phase.eq(0)],
                lambda: (q.append(Point(x=7, y=9)), q.append(Point(x=1, y=2))),
            )

            def pop_to(ox: Var, oy: Var) -> None:
                got = q.popleft()
                ox.set(got.x)
                oy.set(got.y)

            when([phase.eq(1)], lambda: pop_to(ox1, oy1))
            when([phase.eq(2)], lambda: pop_to(ox2, oy2))
            phase.iadd(1)
            ids.update(
                length=q.length.id,
                ox1=ox1.id,
                oy1=oy1.id,
                ox2=ox2.id,
                oy2=oy2.id,
                out_x=q.outBox.fields[0].id,
                out_y=q.outBox.fields[1].id,
            )

        hir_json = build_module(prog).to_json()
        parsed = module_stmts(hir_json)
        slot_triggers = [
            s
            for s in parsed
            if s["kind"] == "trigger"
            and len(s["acts"]) == 2
            and all(
                a["relocs"][0]["target_var"] in (ids["out_x"], ids["out_y"])
                for a in s["acts"]
            )
        ]
        self.assertEqual(len(slot_triggers), 2)
        for slot in slot_triggers:
            self.assertEqual(slot["acts"][0]["relocs"][0]["target_var"], ids["out_x"])
            self.assertEqual(slot["acts"][1]["relocs"][0]["target_var"], ids["out_y"])

        report = run_sim(hir_json, 5)
        self.assertEqual(report["var_values"][ids["ox1"]], 7)
        self.assertEqual(report["var_values"][ids["oy1"]], 9)
        self.assertEqual(report["var_values"][ids["ox2"]], 1)
        self.assertEqual(report["var_values"][ids["oy2"]], 2)
        self.assertEqual(report["var_values"][ids["length"]], 0)

    def _pop_site_cost(self, kind: str, capacity: int) -> dict:
        def prog() -> None:
            phase = Var(0)
            out = Var(0)
            if kind == "queue":
                q = Queue("dword", capacity)
            else:
                q = Deque("dword", capacity)
            when([phase.eq(1)], lambda: out.set(q.popleft()))

        body = when_body(module_stmts(build_module(prog).to_json()), 0)
        return {
            "stmts": len(body),
            "triggers": sum(1 for s in body if s["kind"] == "trigger"),
            "acts": sum(1 for s in body if s["kind"] == "act"),
            "transfers": sum(1 for s in body if s["kind"] == "vartransfer"),
            "actions": body_action_count(body),
        }

    def test_queue_and_deque_pop_sites_are_o1_at_capacity_16_and_128(self):
        for kind in ("queue", "deque"):
            small = self._pop_site_cost(kind, 16)
            large = self._pop_site_cost(kind, 128)
            self.assertEqual(large, small, f"{kind} pop site grew with capacity")
            self.assertEqual(
                small,
                {"stmts": 4, "triggers": 2, "acts": 1, "transfers": 1, "actions": 3},
            )

    def test_queue_ring_slot_destinations_are_statically_bound_to_out_box(self):
        ids: dict[str, int] = {}

        def prog() -> None:
            q = Queue("dword", 16)
            ids.update(head=q.head.id, out=q.outBox.id)

        parsed = module_stmts(build_module(prog).to_json())
        slot_triggers = [
            s
            for s in parsed
            if s["kind"] == "trigger"
            and s["conds"][0]["relocs"][0]["target_var"] == ids["head"]
        ]
        self.assertEqual(len(slot_triggers), 16)
        for slot in slot_triggers:
            self.assertEqual(slot["acts"][0]["relocs"][0]["target_var"], ids["out"])
            self.assertNotIn("target_trigger", slot["acts"][0]["relocs"][0])

    def test_unit_group_auto_prune_sweeps_dead_members_through_varstore(self):
        ids: dict[str, int] = {}

        def prog() -> None:
            g = UnitGroup(2)
            phase = Var(0)
            when([phase.eq(0)], lambda: (g.add(1000), g.add(2000)))
            phase.iadd(1)
            ids.update(length=g.length.id, slot0=g.slots[0].id, slot1=g.slots[1].id)

        hir_json = build_module(prog).to_json()
        self.assertIn('"kind":"varstore"', hir_json)

        # The CLI simulator has no external memory, so both unit EPD fields
        # read 0 and the per-frame sweep clears the members.
        report = run_sim(hir_json, 2)
        self.assertEqual(report["var_values"][ids["length"]], 0)
        self.assertEqual(report["var_values"][ids["slot0"]], 0)
        self.assertEqual(report["var_values"][ids["slot1"]], 0)

    def test_byte_identical_hir_parity_with_typescript_containers(self):
        ts_script = r"""
import {
  Deque, Queue, Stack, TriggerStruct, UnitGroup, Var,
  buildModule, dword,
} from "./ts/index.ts";

const mod = buildModule(() => {
  const P = TriggerStruct("P", { x: dword(), y: dword() });
  const q = new Queue("dword", 4);
  q.append(1);
  q.append(2);
  const out = Var(0);
  out.set(q.popleft());

  const qs = new Queue(P, 2);
  qs.append(P({ x: 7, y: 9 }));
  const got = qs.popleft();
  out.set(got.x);
  out.set(got.y);

  const dq = new Deque("word", 4);
  dq.append(1);
  dq.appendleft(2);
  out.set(dq.pop());
  out.set(dq.popleft());

  const st = new Stack(4);
  st.push(1);
  st.push(2);
  out.set(st.pop());

  const g = new UnitGroup(2);
  g.add(1000);
  g.add(2000);
  g.remove(1000);
});
process.stdout.write(mod.toJson());
"""

        def prog() -> None:
            P = TriggerStruct("P", x=dword(), y=dword())
            q = Queue("dword", 4)
            q.append(1)
            q.append(2)
            out = Var(0)
            out.set(q.popleft())

            qs = Queue(P, 2)
            qs.append(P(x=7, y=9))
            got = qs.popleft()
            out.set(got.x)
            out.set(got.y)

            dq = Deque("word", 4)
            dq.append(1)
            dq.appendleft(2)
            out.set(dq.pop())
            out.set(dq.popleft())

            st = Stack(4)
            st.push(1)
            st.push(2)
            out.set(st.pop())

            g = UnitGroup(2)
            g.add(1000)
            g.add(2000)
            g.remove(1000)

        ts_output = subprocess.check_output(
            ["node", "--input-type=module", "-e", ts_script],
            cwd=REPO_ROOT,
            text=True,
        )
        self.assertEqual(build_module(prog).to_json(), ts_output)

if __name__ == "__main__":
    unittest.main()
