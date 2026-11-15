"""Issue #76 (C4 follow-up): dynamic container Var writes go through `VarStore`.

A `FixedArray` dynamic set and a `Queue`/`Deque` push of a `Var` used to
decompose the value into 32 bit-test triggers (38T/37T for `N = 8`; 552T/548T
for a 16-slot push). Both now emit one `emitVarStore` into the container's
pointer action `.value` field, so the source variable's `TriggerPage` firing
delivers the whole dword in one out-of-band transfer:

- array dynamic set: `log2(N) + 5` triggers (8T/8T at `N = 8`, no flag gate);
- queue push: one pointer plus one store, 32T/29T at capacity 16.

The marginal cost of one `VarStore` into a trigger field measures +3T (setup
trigger + out-of-band `TriggerPage` firing + restore), not the review's
projected 2T; nothing here claims 2T. Provenance: spike #74 on `b299f27`,
re-measured on this branch's base `73214aa`.

The runtime proof is the repo's LIR payload simulator (`eudkit-compile sim`),
compared against the pre-change frontend on the same probe; an in-game run
remains open because this environment has no StarCraft install.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from py import (
    Deque,
    FixedArray,
    Queue,
    Stack,
    TriggerStruct,
    Var,
    arrayByte,
    arrayDword,
    arrayWord,
    build_module,
    byte,
    dword,
    when,
)
from py.trigger import TRIGGER_STRIDE_EPDS

from py.tests.helpers import run_sim

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
EUDKIT_COMPILE = REPO_ROOT / "target" / "debug" / "eudkit-compile"

ACTION_VALUE_OFFSET = 328 + 20
ACTION_DEST_OFFSET = 328 + 16
SLOT_ZERO_VALUE = ACTION_VALUE_OFFSET


def run_lower(hir_json: str) -> dict:
    with tempfile.TemporaryDirectory(prefix="eudkit-py-c4-") as tmpdir:
        hir_path = Path(tmpdir) / "prog.hir.json"
        hir_path.write_text(hir_json, encoding="utf-8")
        out = subprocess.check_output(
            [str(EUDKIT_COMPILE), "lower", str(hir_path)],
            text=True,
        ).strip()
        return json.loads(out)


def trigger_items(lir: dict) -> list[dict]:
    return [item for item in lir["module"]["items"] if item["kind"] == "trigger"]


def _array_set_var(length: int):
    def prog() -> None:
        idx = Var(3)
        val = Var(7)
        arr = FixedArray(arrayDword, length)
        arr.set(idx, val)

    return prog


def _array_set_const(length: int):
    def prog() -> None:
        idx = Var(3)
        arr = FixedArray(arrayDword, length)
        arr.set(idx, 7)

    return prog


def _queue_push_var():
    def prog() -> None:
        val = Var(7)
        Queue("dword", 16).push(val)

    return prog


def _queue_push_two():
    def prog() -> None:
        first = Var(7)
        second = Var(9)
        q = Queue("dword", 16)
        q.push(first)
        q.push(second)

    return prog


def _queue_push_const():
    def prog() -> None:
        Queue("dword", 16).push(7)

    return prog


def _queue_push_struct():
    def prog() -> None:
        Point = TriggerStruct("Point", x=dword(), y=dword())
        Queue(Point, 16).push(Point(x=7, y=9))

    return prog


def _deque_append_var():
    def prog() -> None:
        val = Var(7)
        Deque("dword", 4).append(val)

    return prog


def _stack_push_var():
    def prog() -> None:
        val = Var(7)
        Stack(4).push(val)

    return prog


# --- cost cases: (name, builder, static T, trigger bytes, total bytes, exec T)
COST_CASES = [
    ("array dynamic set Var N=8", lambda: _array_set_var(8), 8, 19_264, 19_272, 8),
    ("array dynamic set Var N=16", lambda: _array_set_var(16), 9, 21_672, 21_680, 9),
    ("array dynamic set Var N=32", lambda: _array_set_var(32), 10, 24_080, 24_088, 10),
    ("array dynamic set const N=8", lambda: _array_set_const(8), 6, 14_448, 14_456, 5),
    ("queue push Var cap16", _queue_push_var, 32, 77_056, 77_072, 29),
    ("queue push two Vars cap16", _queue_push_two, 47, 113_176, 113_192, 42),
    ("queue push const cap16", _queue_push_const, 40, 96_320, 96_332, 36),
    ("queue push struct cap16", _queue_push_struct, 40, 96_320, 96_336, 38),
    ("deque append Var cap4", _deque_append_var, 28, 67_424, 67_440, 26),
    ("stack push Var cap4", _stack_push_var, 8, 19_264, 19_272, 8),
]


class ContainerVarWriteCostTests(unittest.TestCase):
    def test_each_primitive_pins_its_own_static_and_executed_cost(self):
        for name, prog, triggers, trigger_bytes, total_bytes, executed in COST_CASES:
            with self.subTest(probe=name):
                report = run_sim(build_module(prog()).to_json(), loops=1)
                static = report["static_cost"]
                self.assertEqual(static["triggers"], triggers)
                self.assertEqual(static["trigger_bytes"], trigger_bytes)
                self.assertEqual(static["total_bytes"], total_bytes)
                self.assertEqual(report["executed_cost"]["triggers"], executed)


class ArrayDynamicWriteTests(unittest.TestCase):
    def test_dynamic_set_writes_the_zero_mid_and_last_element(self):
        for index in (0, 3, 7):
            with self.subTest(index=index):
                ids: dict[str, object] = {}

                def prog() -> None:
                    idx = Var(index)
                    val = Var(7)
                    arr = FixedArray(arrayDword, 8)
                    arr.set(idx, val)
                    ids["elems"] = [v.id for v in arr.vars]

                report = run_sim(build_module(prog).to_json(), loops=1)
                values = [report["var_values"][i] for i in ids["elems"]]
                expected = [7 if i == index else 0 for i in range(8)]
                self.assertEqual(values, expected)

    def test_dynamic_set_keeps_the_previous_frontends_byte_and_word_behaviour(self):
        """The dynamic path stores and reads back the raw dword.

        The element mask narrows only the static ``arr.set(i, const)`` path;
        the dynamic const write already behaved this way, and the Var path now
        matches the pre-change 32-bit-trigger frontend bit for bit (checked
        against a baseline snapshot of the frontend). This is a parity pin,
        not a new masking decision.
        """
        for elem, source, stored in (
            (arrayByte, 0x1FF, 0x1FF),
            (arrayWord, 0x1_2345, 0x1_2345),
        ):
            with self.subTest(elem=elem):
                ids: dict[str, object] = {}

                def prog() -> None:
                    idx = Var(3)
                    val = Var(source)
                    arr = FixedArray(elem, 8)
                    arr.set(idx, val)
                    out = Var(0)
                    out.set(arr.get(3))
                    ids["elems"] = [v.id for v in arr.vars]
                    ids["out"] = out.id

                report = run_sim(build_module(prog).to_json(), loops=1)
                values = [report["var_values"][i] for i in ids["elems"]]
                self.assertEqual(
                    values, [stored if i == 3 else 0 for i in range(8)]
                )
                self.assertEqual(report["var_values"][ids["out"]], stored)


class QueueVarWriteTests(unittest.TestCase):
    def test_fifo_round_trip_lands_on_every_tail_slot(self):
        """16 pushes of distinct Vars: each write must hit the addressed slot.

        This exercises the whole `TRIGGER_STRIDE_EPDS << bit` address range,
        including single-bit slots and their combinations (tail 0..15), so a
        broken slot-block adjacency shows up as a wrong FIFO order.
        """
        ids: dict[str, object] = {}

        def prog() -> None:
            q = Queue("dword", 16)
            phase = Var(0)
            values = [Var(100 + i) for i in range(16)]
            outs = [Var(0) for _ in range(16)]
            for i, value in enumerate(values):
                def push(value=value) -> None:
                    q.push(value)

                when([phase.eq(i)], push)
            for i, out in enumerate(outs):
                def pop(out=out) -> None:
                    out.set(q.popleft())

                when([phase.eq(16 + i)], pop)
            phase.iadd(1)
            ids["outs"] = [v.id for v in outs]
            ids["length"] = q.length.id
            ids["slot_ids"] = [slot.id for slot in q.slots]

        slot_ids = None
        report = run_sim(build_module(prog).to_json(), loops=40)
        slot_ids = ids["slot_ids"]
        self.assertEqual(report["var_values"][ids["length"]], 0)
        self.assertEqual(
            [report["var_values"][i] for i in ids["outs"]],
            [100 + i for i in range(16)],
        )
        self.assertEqual(
            slot_ids, list(range(slot_ids[0], slot_ids[0] + 16)), "ring slots non-contiguous"
        )

    def test_two_pushes_and_a_wrap_keep_fifo_order(self):
        ids: dict[str, object] = {}

        def prog() -> None:
            q = Queue("dword", 4)
            phase = Var(0)
            values = [Var(11), Var(22), Var(33), Var(44), Var(55)]
            outs = [Var(0) for _ in range(5)]
            for i in range(4):
                def push(i=i) -> None:
                    q.push(values[i])

                when([phase.eq(i)], push)
            for i in range(2):
                def pop(i=i) -> None:
                    outs[i].set(q.popleft())

                when([phase.eq(4 + i)], pop)
            when([phase.eq(6)], lambda: q.push(values[4]))
            for i in range(3):
                def pop(i=i) -> None:
                    outs[2 + i].set(q.popleft())

                when([phase.eq(7 + i)], pop)
            phase.iadd(1)
            ids["outs"] = [v.id for v in outs]
            ids["head"] = q.head.id
            ids["tail"] = q.tail.id
            ids["length"] = q.length.id

        report = run_sim(build_module(prog).to_json(), loops=12)
        self.assertEqual(
            [report["var_values"][i] for i in ids["outs"]],
            [11, 22, 33, 44, 55],
        )
        self.assertEqual(report["var_values"][ids["length"]], 0)
        # Two pushes after two pops wrapped the ring: tail went 3 -> 0 -> 1.
        self.assertEqual(report["var_values"][ids["head"]], 1)
        self.assertEqual(report["var_values"][ids["tail"]], 1)

    def test_struct_element_writes_every_field(self):
        """A per-field store must target the pointer's action 0 value field.

        Regression for a store offset taken from the *slot's* field k: for
        k > 0 that lands on the pointer's `flag.act_set(0)` action and every
        field but the first is silently dropped (the simulator showed 0).
        """
        ids: dict[str, object] = {}

        def prog() -> None:
            Point = TriggerStruct("Point", x=dword(), y=dword(), z=byte())
            q = Queue(Point, 4)
            phase = Var(0)
            ox = Var(0)
            oy = Var(0)
            oz = Var(0)
            when(
                [phase.eq(0)],
                lambda: q.push(Point(x=7, y=9, z=0x1FF)),
            )

            def pop() -> None:
                got = q.popleft()
                ox.set(got.x)
                oy.set(got.y)
                oz.set(got.z)

            when([phase.eq(1)], pop)
            phase.iadd(1)
            ids.update(ox=ox.id, oy=oy.id, oz=oz.id, length=q.length.id)

        report = run_sim(build_module(prog).to_json(), loops=3)
        values = report["var_values"]
        self.assertEqual(values[ids["ox"]], 7)
        self.assertEqual(values[ids["oy"]], 9)
        self.assertEqual(values[ids["oz"]], 0xFF, "byte field mask not applied")
        self.assertEqual(values[ids["length"]], 0)

    def test_same_page_var_written_in_the_same_loop_survives_the_store(self):
        """The store fires the source page out of band; other slots self-write.

        A page-backed variable written by a normal transfer in the same loop
        shares the `TriggerPage` with the store source and the array elements.
        Both statement orders must leave every value intact.
        """
        for order in ("transfer_first", "store_first"):
            with self.subTest(order=order):
                ids: dict[str, object] = {}

                def prog() -> None:
                    a = Var(11)
                    b = Var(22)
                    idx = Var(3)
                    arr = FixedArray(arrayDword, 8)
                    phase = Var(0)
                    if order == "transfer_first":
                        def body() -> None:
                            a.set(b)
                            arr.set(idx, a)
                    else:
                        def body() -> None:
                            arr.set(idx, a)
                            a.set(b)

                    when([phase.eq(0)], body)
                    phase.iadd(1)
                    ids.update(a=a.id, b=b.id)
                    ids["elems"] = [v.id for v in arr.vars]

                report = run_sim(build_module(prog).to_json(), loops=2)
                values = report["var_values"]
                self.assertEqual(values[ids["a"]], 22)
                self.assertEqual(values[ids["b"]], 22)
                # The store reads the source at its program position, exactly
                # like the baseline bit-test frontend: when the array statement
                # comes first it captures the pre-transfer value (11).
                expected = 22 if order == "transfer_first" else 11
                self.assertEqual(values[ids["elems"][3]], expected)
                self.assertEqual(
                    [values[i] for j, i in enumerate(ids["elems"]) if j != 3],
                    [0] * 7,
                )


class DequeVarWriteTests(unittest.TestCase):
    def test_append_and_appendleft_of_vars_round_trip(self):
        """Both ring views take the same pointer path (write index == value)."""
        ids: dict[str, object] = {}

        def prog() -> None:
            q = Deque("dword", 4)
            phase = Var(0)
            first = Var(7)
            second = Var(9)
            outs = [Var(0) for _ in range(3)]
            when([phase.eq(0)], lambda: q.append(first))
            when([phase.eq(1)], lambda: q.appendleft(second))
            when([phase.eq(2)], lambda: outs[0].set(q.popleft()))
            when([phase.eq(3)], lambda: outs[1].set(q.pop()))
            when([phase.eq(4)], lambda: q.append(first))
            when([phase.eq(5)], lambda: outs[2].set(q.pop()))
            phase.iadd(1)
            ids["outs"] = [v.id for v in outs]
            ids["length"] = q.length.id
            ids["left_ids"] = [slot.id for slot in q.leftSlots]
            ids["right_ids"] = [slot.id for slot in q.rightSlots]

        report = run_sim(build_module(prog).to_json(), loops=8)
        self.assertEqual([report["var_values"][i] for i in ids["outs"]], [9, 7, 7])
        self.assertEqual(report["var_values"][ids["length"]], 0)
        for key in ("left_ids", "right_ids"):
            slot_ids = ids[key]
            self.assertEqual(slot_ids, list(range(slot_ids[0], slot_ids[0] + 4)))


class StoreLayoutTests(unittest.TestCase):
    """Pin the emitted pointer shape, not just its cost.

    The address math assumes the ring slot triggers are one contiguous block
    at `TRIGGER_STRIDE_EPDS` (602 EPDs = 2408 bytes) per slot, and that the
    store hits the pointer's *action 0* value field. Both are invisible in the
    cost table, so they are pinned structurally here.
    """

    def _queue_lir(self, capacity: int = 16, fields: int = 1) -> list[dict]:
        def prog() -> None:
            if fields == 1:
                Queue("dword", capacity).push(Var(7))
                return
            Point = TriggerStruct("Point", x=dword(), y=dword())
            Queue(Point, capacity).push(Point(x=7, y=9))

        return trigger_items(run_lower(build_module(prog).to_json()))

    def test_ring_slots_are_the_leading_contiguous_trigger_block(self):
        items = self._queue_lir()
        for i in range(16):
            item = items[i]
            self.assertEqual(len(item["conds"]), 1, f"item {i} is not a ring slot")
            # The slot read gate is `head == i` (Deaths, Exactly).
            self.assertEqual(item["conds"][0]["amount"], i)
            self.assertEqual(item["conds"][0]["comparison"], 10)
            self.assertEqual(item["size"], TRIGGER_STRIDE_EPDS * 4)

    def test_reset_and_store_address_slot_zero_and_the_pointer_value_field(self):
        items = self._queue_lir(fields=2)
        resets = []
        for idx, item in enumerate(items):
            for act in item["acts"] or []:
                relocs = act["relocs"]
                dest = [r for r in relocs if r["field_offset"] == 16]
                value = [r for r in relocs if r["field_offset"] == 20]
                if (
                    len(dest) == 1
                    and dest[0]["target_offset"] == ACTION_DEST_OFFSET
                    and len(value) == 1
                    and value[0]["target_item"] == 0
                ):
                    resets.append((dest[0]["target_item"], value[0]["target_offset"], act))
        self.assertEqual(len(resets), 2, "one reset per element field")
        pointer_items = {pointer for pointer, _, _ in resets}
        self.assertEqual(len(pointer_items), 2)
        # Field k's reset reads slot 0's action k value field (0x15c + 32k).
        self.assertEqual(
            sorted(offset for _, offset, _ in resets),
            [SLOT_ZERO_VALUE, SLOT_ZERO_VALUE + 32],
        )

        for pointer in sorted(pointer_items):
            setups = [
                item
                for item in items
                if item["acts"]
                and any(
                    r["field_offset"] == 20
                    and r["target_item"] == pointer
                    and r["target_offset"] == ACTION_VALUE_OFFSET
                    for act in item["acts"]
                    for r in act["relocs"]
                )
            ]
            self.assertEqual(len(setups), 1, "one VarStore setup per pointer")
            # Every reloc into the pointer itself must address its action 0
            # `.dest` (address walk) or `.value` (store target); the flag
            # lives in action 1, whose value field is 0x17c.
            pointer_relocs = [
                r
                for item in items
                for act in item["acts"] or []
                for r in act["relocs"]
                if r["target_item"] == pointer
            ]
            self.assertTrue(pointer_relocs)
            self.assertEqual(
                {
                    r["target_offset"]
                    for r in pointer_relocs
                },
                {ACTION_DEST_OFFSET, ACTION_VALUE_OFFSET},
            )

    def test_addr_weights_are_trigger_stride_shifts(self):
        items = self._queue_lir()
        weights = {
            act["value"]
            for item in items
            for act in item["acts"]
            if act["modifier"] == 8  # modifier: Add
            and act["value"] in {602, 1204, 2408, 4816}
        }
        self.assertEqual(
            weights,
            {TRIGGER_STRIDE_EPDS << b for b in range(4)},
        )


if __name__ == "__main__":
    unittest.main()
