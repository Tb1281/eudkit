"""The unlimiter core helper (issue #94): a port of euddraft's
``plugins/unlimiter.py``.

The source connects four grow-only game lists to module data blobs, then
rewrites one dword every trigger loop:

===============  ==========  =========  ========  =========
list             head        tail       stride    elements
===============  ==========  =========  ========  =========
bullets          0x64EED8    0x64EEDC   112       8192
sprites          0x63FE30    0x63FE34   36        65536
images           0x57EB68    0x57EB70   64        65536
orders           0x64B2E0    0x64B2E4   20        65536
===============  ==========  =========  ========  =========

What is pinned here:

* the four list constants, and that ``unlimiter()`` arms exactly those;
* the emitted HIR shape per list (one ``while`` loop, one write trigger, the
  shared patch block, the optional sprite trigger, the four tail acts), which
  is the same trigger grouping the source's ``EUDLoopN`` / ``DoActions`` /
  ``Trigger`` calls produce;
* the linked-list layout, replayed from the emitted relocations for every
  element: ``prev`` points at ``element - stride``, ``next`` at
  ``element + stride``, element 0's ``prev`` and the last element's ``next``
  are zero, and sprite elements carry ``10000 + k`` at ``+0x10``;
* the per-tick ``0x64DEBC = 40`` write, emitted last.

The runtime proof that the patched loop really fills the blob lives in
``crates/eudkit-lower/tests/unlimiter_parity.rs``: the Python simulator reports
only out-of-payload EPDs, and the list blob is inside the payload.
"""

from __future__ import annotations

import json
import unittest

from py import (
    Add,
    BULLET_ITEM_COUNT,
    BULLET_ITEM_SIZE,
    BULLET_LIST_ADDR,
    BULLET_LIST_END_ADDR,
    IMAGE_ITEM_COUNT,
    IMAGE_ITEM_SIZE,
    IMAGE_LIST_ADDR,
    IMAGE_LIST_END_ADDR,
    MIN_ITEM_SIZE,
    NEXT_FIELD_OFFSET,
    ORDER_ITEM_COUNT,
    ORDER_ITEM_SIZE,
    ORDER_LIST_ADDR,
    ORDER_LIST_END_ADDR,
    PER_TICK_ADDR,
    PER_TICK_VALUE,
    PREV_FIELD_OFFSET,
    SPRITE_FIELD_OFFSET,
    SPRITE_FIELD_SEED,
    SPRITE_ITEM_COUNT,
    SPRITE_ITEM_SIZE,
    SPRITE_LIST_ADDR,
    SPRITE_LIST_END_ADDR,
    EudslError,
    SetTo,
    build_module,
    connectDList,
    connect_dlist,
    unlimiter,
)
from py.tests.helpers import EPD_BASE

#: The four `connectDList` calls of the source's `onPluginStart`, in order:
#: (head, tail, stride, elements, is_sprite).
LISTS = [
    (BULLET_LIST_ADDR, BULLET_LIST_END_ADDR, BULLET_ITEM_SIZE, BULLET_ITEM_COUNT, 0),
    (SPRITE_LIST_ADDR, SPRITE_LIST_END_ADDR, SPRITE_ITEM_SIZE, SPRITE_ITEM_COUNT, 1),
    (IMAGE_LIST_ADDR, IMAGE_LIST_END_ADDR, IMAGE_ITEM_SIZE, IMAGE_ITEM_COUNT, 0),
    (ORDER_LIST_ADDR, ORDER_LIST_END_ADDR, ORDER_ITEM_SIZE, ORDER_ITEM_COUNT, 0),
]

ACT_DEST_OFFSET = 16
ACT_VALUE_OFFSET = 20
#: `ActSlotView` byte offsets inside a trigger (see `py/trigger.py`).
ACT_STRIDE = 32
ACT_BASE = 328


def _epd(addr: int) -> int:
    return ((addr - EPD_BASE) // 4) & 0xFFFFFFFF


def _module(body) -> dict:
    return json.loads(build_module(body).to_json())["module"]


def _act_relocs(act: dict) -> list[dict]:
    return act["relocs"]


def _blob_reloc(offset: int, mode: int, field_offset: int = ACT_DEST_OFFSET) -> dict:
    return {"field_offset": field_offset, "target_blob": 0,
            "target_offset": offset, "mode": mode}


def _trigger_reloc(trigger_id: int, offset: int) -> dict:
    return {"field_offset": ACT_DEST_OFFSET, "target_trigger": trigger_id,
            "target_offset": offset, "mode": 1}


def _flatten_acts(stmts: list[dict]) -> list[dict]:
    """Every action of a module body, in emission order."""

    out: list[dict] = []
    for stmt in stmts:
        if stmt["kind"] == "act":
            out.append(stmt)
        elif stmt["kind"] == "trigger":
            out.extend(stmt["acts"])
        else:
            out.extend(_flatten_acts(stmt.get("body") or []))
    return out


class TestUnlimiterConstants(unittest.TestCase):
    def test_list_constants_match_the_source(self):
        self.assertEqual(LISTS, [
            (0x64EED8, 0x64EEDC, 112, 8192, 0),
            (0x63FE30, 0x63FE34, 36, 65536, 1),
            (0x57EB68, 0x57EB70, 64, 65536, 0),
            (0x64B2E0, 0x64B2E4, 20, 65536, 0),
        ])

    def test_per_tick_write_matches_the_source(self):
        self.assertEqual(PER_TICK_ADDR, 0x64DEBC)
        self.assertEqual(PER_TICK_VALUE, 40)

    def test_node_fields_are_prev0_next4(self):
        self.assertEqual((PREV_FIELD_OFFSET, NEXT_FIELD_OFFSET), (0, 4))
        self.assertEqual(MIN_ITEM_SIZE, 8)

    def test_sprite_field_seed_matches_the_source(self):
        self.assertEqual(SPRITE_FIELD_OFFSET, 0x10)
        self.assertEqual(SPRITE_FIELD_SEED, 10000)

    def test_camel_case_alias_is_the_same_helper(self):
        self.assertIs(connectDList, connect_dlist)


class TestConnectDListHir(unittest.TestCase):
    """One list, four elements, both shapes (see the source's `connectDList`)."""

    item_size = 112
    item_n = 4

    def _list(self, is_sprite: int = 0) -> dict:
        self.mod = _module(
            lambda: connect_dlist(
                0x64EED8, 0x64EEDC, self.item_size, self.item_n, is_sprite
            )
        )
        return self.mod

    def _body(self, is_sprite: int = 0) -> tuple[dict, dict]:
        mod = self._list(is_sprite)
        once = mod["stmts"][0]
        self.assertEqual(once["kind"], "once")
        while_stmt = once["body"][0]
        self.assertEqual(while_stmt["kind"], "while")
        return mod, while_stmt

    def test_one_blob_with_a_padding_element_per_side(self):
        mod, _ = self._body()
        self.assertEqual(
            mod["blobs"], [{"size": self.item_size * (self.item_n + 2)}]
        )

    def test_counter_is_initialized_to_item_n(self):
        mod, while_stmt = self._body()
        self.assertEqual(mod["vars"], [{"id": 0, "init": self.item_n}])
        (cond,) = while_stmt["conds"]
        self.assertEqual(cond["condtype"], 15)  # Deaths / Memory wire form
        self.assertEqual(cond["player"], 0)
        self.assertEqual(cond["amount"], 1)
        self.assertEqual(cond["comparison"], 0)  # AtLeast
        self.assertEqual(cond["relocs"], [
            {"field_offset": 4, "target_var": 0, "target_offset": 20, "mode": 1}
        ])

    def test_write_trigger_writes_neighbour_pointers_of_element_zero(self):
        _, while_stmt = self._body()
        trigger = while_stmt["body"][0]
        self.assertEqual(trigger["kind"], "trigger")
        acts = trigger["acts"]
        self.assertEqual(len(acts), 2)
        self.assertEqual(acts[0]["acttype"], 45)  # SetMemory / SetDeaths
        self.assertEqual(acts[0]["modifier"], SetTo)
        self.assertEqual(_act_relocs(acts[0]), [
            _blob_reloc(self.item_size + PREV_FIELD_OFFSET, 1),
            _blob_reloc(0, 4, ACT_VALUE_OFFSET),
        ])
        self.assertEqual(_act_relocs(acts[1]), [
            _blob_reloc(self.item_size + NEXT_FIELD_OFFSET, 1),
            _blob_reloc(2 * self.item_size, 4, ACT_VALUE_OFFSET),
        ])

    def test_patch_block_advances_both_write_actions(self):
        _, while_stmt = self._body()
        trigger = while_stmt["body"][0]
        patches = while_stmt["body"][1:]
        # Four patches plus the loop-counter decrement (`isub`).
        self.assertEqual(len(patches), 5)
        stride = self.item_size // 4
        expected = [
            (trigger["id"], ACT_BASE + ACT_DEST_OFFSET, stride),
            (trigger["id"], ACT_BASE + ACT_VALUE_OFFSET, self.item_size),
            (trigger["id"], ACT_BASE + ACT_STRIDE + ACT_DEST_OFFSET, stride),
            (trigger["id"], ACT_BASE + ACT_STRIDE + ACT_VALUE_OFFSET, self.item_size),
        ]
        for act, (target, field_offset, delta) in zip(patches, expected):
            self.assertEqual(act["kind"], "act")
            self.assertEqual(act["modifier"], Add)
            self.assertEqual(act["value"], delta)
            self.assertEqual(_act_relocs(act), [_trigger_reloc(target, field_offset)])

    def test_counter_decrement_is_the_last_body_act(self):
        _, while_stmt = self._body()
        decrement = while_stmt["body"][-1]
        self.assertEqual(decrement["kind"], "act")
        self.assertEqual(decrement["modifier"], Add)
        self.assertEqual(decrement["value"], 0xFFFFFFFF)  # -1
        self.assertEqual(decrement["eudx"], 0x4353)  # EUDX_MAGIC: masked add
        self.assertEqual(
            decrement["relocs"],
            [{"field_offset": 16, "target_var": 0, "target_offset": 20, "mode": 1}],
        )

    def test_sprite_list_adds_the_seed_trigger_and_its_patches(self):
        _, while_stmt = self._body(1)
        body = while_stmt["body"]
        self.assertEqual(
            [stmt["kind"] for stmt in body],
            ["trigger", "trigger"] + ["act"] * 7,
        )
        writes, sprite = body[0], body[1]
        self.assertEqual(sprite["kind"], "trigger")
        self.assertEqual(len(sprite["acts"]), 1)
        seed = sprite["acts"][0]
        self.assertEqual(seed["modifier"], SetTo)
        self.assertEqual(seed["value"], SPRITE_FIELD_SEED)
        self.assertEqual(_act_relocs(seed), [
            _blob_reloc(self.item_size + SPRITE_FIELD_OFFSET, 1)
        ])
        # The sprite patches share the write trigger's patch block.
        patches = body[6:8]
        self.assertEqual(patches[0]["value"], self.item_size // 4)
        self.assertEqual(patches[1]["value"], 1)
        self.assertEqual(_act_relocs(patches[0]),
                         [_trigger_reloc(sprite["id"], ACT_BASE + ACT_DEST_OFFSET)])
        self.assertEqual(_act_relocs(patches[1]),
                         [_trigger_reloc(sprite["id"], ACT_BASE + ACT_VALUE_OFFSET)])

    def test_tail_points_head_at_element_zero_and_zeroes_the_dangling_links(self):
        _, while_stmt = self._body()
        tail = self._body()[0]["stmts"][0]["body"][1:]
        self.assertEqual(len(tail), 4)
        head, base = tail[0], tail[1]
        end, last = tail[2], tail[3]
        self.assertEqual(head["modifier"], SetTo)
        self.assertEqual(_act_relocs(head), [
            _blob_reloc(self.item_size, 4, ACT_VALUE_OFFSET)
        ])
        self.assertEqual(_act_relocs(base), [
            _blob_reloc(self.item_size + PREV_FIELD_OFFSET, 1)
        ])
        self.assertEqual(base["value"], 0)
        self.assertEqual(_act_relocs(end), [
            _blob_reloc(self.item_size * self.item_n, 4, ACT_VALUE_OFFSET)
        ])
        self.assertEqual(_act_relocs(last), [
            _blob_reloc(self.item_size * self.item_n + NEXT_FIELD_OFFSET, 1)
        ])
        self.assertEqual(last["value"], 0)


class TestListLayout(unittest.TestCase):
    """Replay the emitted relocations into the source's linked-list layout."""

    def test_prev_next_links_match_the_source_geometry(self):
        # The patch acts target the *write trigger*, not the blob, so the
        # replay starts from the write acts' own relocations.
        item_size, item_n = 112, 5
        mod = _module(lambda: connect_dlist(0x64EED8, 0x64EEDC, item_size, item_n))
        body = mod["stmts"][0]["body"][0]["body"]
        writes = body[0]
        starts = {}
        for name, act in zip(("prev_dest", "prev_value"), writes["acts"][0]["relocs"]):
            starts[name] = act["target_offset"]
        for name, act in zip(("next_dest", "next_value"), writes["acts"][1]["relocs"]):
            starts[name] = act["target_offset"]

        self.assertEqual(starts, {
            "prev_dest": item_size + PREV_FIELD_OFFSET,
            "prev_value": 0,
            "next_dest": item_size + NEXT_FIELD_OFFSET,
            "next_value": 2 * item_size,
        })

        # The tail zeroes element 0's prev and the last element's next, and
        # names element n-1 as the list end.
        tail = mod["stmts"][0]["body"][1:]
        self.assertEqual(tail[1]["relocs"][0]["target_offset"], item_size)
        last = tail[2]["relocs"][0]["target_offset"]
        self.assertEqual(last, item_size * item_n)
        self.assertEqual(
            tail[3]["relocs"][0]["target_offset"], last + NEXT_FIELD_OFFSET
        )

        # Walk the write actions forward with the loop's own patches and pin
        # the last iteration against the tail: element n-1's `prev` must be
        # element n-2, its `next` must be the trailing padding (advanced to
        # n * item_size, then zeroed by the tail), and the two destinations must
        # be that element's link fields. This ties the patch deltas to the tail
        # offsets instead of restating the arithmetic.
        advance = item_size * (item_n - 1)
        self.assertEqual(starts["prev_dest"] + advance, last)
        self.assertEqual(starts["prev_value"] + advance, last - item_size)
        self.assertEqual(starts["next_dest"] + advance, last + NEXT_FIELD_OFFSET)
        # Element n-1's `next` seeds the (unwritten) trailing padding at
        # item_size * (n + 1); the tail then zeroes the link.
        self.assertEqual(starts["next_value"] + advance, item_size * (item_n + 1))

    def test_sprite_field_runs_from_10000_in_list_order(self):
        item_size, item_n = 36, 6
        mod = _module(lambda: connect_dlist(0x63FE30, 0x63FE34, item_size, item_n, 1))
        body = mod["stmts"][0]["body"][0]["body"]
        sprite_trigger = body[1]
        seed = sprite_trigger["acts"][0]
        seed_offset = seed["relocs"][0]["target_offset"]
        self.assertEqual(seed_offset, item_size + SPRITE_FIELD_OFFSET)
        self.assertEqual(seed["value"], SPRITE_FIELD_SEED)

        # The sprite patches advance in lockstep with the write trigger (one
        # stride per element, value +1), so the last seed lands on the final
        # element's +0x10 field and the last value is 10000 + n - 1.
        dest_patch, value_patch = body[6], body[7]
        self.assertEqual(dest_patch["value"], item_size // 4)
        self.assertEqual(
            _act_relocs(dest_patch),
            [_trigger_reloc(sprite_trigger["id"], ACT_BASE + ACT_DEST_OFFSET)],
        )
        self.assertEqual(value_patch["value"], 1)
        self.assertEqual(
            _act_relocs(value_patch),
            [_trigger_reloc(sprite_trigger["id"], ACT_BASE + ACT_VALUE_OFFSET)],
        )
        last = item_size * item_n
        self.assertEqual(
            seed_offset + item_size * (item_n - 1), last + SPRITE_FIELD_OFFSET
        )
        self.assertEqual(seed["value"] + (item_n - 1), SPRITE_FIELD_SEED + item_n - 1)


class TestUnlimiter(unittest.TestCase):
    def setUp(self):
        self.mod = _module(lambda: unlimiter())

    def test_arms_all_four_lists_in_order(self):
        self.assertEqual(
            [blob["size"] for blob in self.mod["blobs"]],
            [stride * (count + 2) for _, _, stride, count, _ in LISTS],
        )
        once_stmts = [s for s in self.mod["stmts"] if s["kind"] == "once"]
        self.assertEqual(len(once_stmts), 4)
        heads = []
        for once in once_stmts:
            tail = once["body"][-4:]
            # The head write is the value of a SetMemory at the list head.
            heads.append(tail[0]["dest"])
        self.assertEqual(heads, [_epd(head) for head, _, _, _, _ in LISTS])

    def test_sprite_list_is_the_only_one_with_the_seed(self):
        seeded = []
        for index, (_, _, stride, _, is_sprite) in enumerate(LISTS):
            once = [s for s in self.mod["stmts"] if s["kind"] == "once"][index]
            body = once["body"][0]["body"]
            has_seed = any(
                act["value"] == SPRITE_FIELD_SEED
                for stmt in body[1:]
                if stmt["kind"] == "trigger"
                for act in stmt["acts"]
            )
            seeded.append(has_seed)
            self.assertEqual(has_seed, bool(is_sprite))

    def test_per_tick_write_is_the_last_action(self):
        acts = _flatten_acts(self.mod["stmts"])
        write = acts[-1]
        self.assertEqual(write["acttype"], 45)
        self.assertEqual(write["modifier"], SetTo)
        self.assertEqual(write["value"], PER_TICK_VALUE)
        self.assertEqual(write["dest"], _epd(PER_TICK_ADDR))
        self.assertEqual(write.get("relocs", []), [])

    def test_per_tick_write_appears_once(self):
        acts = [
            act for act in _flatten_acts(self.mod["stmts"])
            if act["dest"] == _epd(PER_TICK_ADDR)
        ]
        self.assertEqual(len(acts), 1)

    def test_default_item_size_is_the_smallest_node(self):
        with self.assertRaises(EudslError):
            build_module(lambda: connect_dlist(0x64EED8, 0x64EEDC, 4, 1))
        mod = _module(lambda: connect_dlist(0x64EED8, 0x64EEDC, MIN_ITEM_SIZE, 1))
        self.assertEqual(mod["blobs"], [{"size": MIN_ITEM_SIZE * 3}])


class TestValidation(unittest.TestCase):
    def test_list_addresses_must_be_positive_aligned_addresses(self):
        for bad in (0, -4, 0x64EED6, 0x1_0000_0000):
            with self.assertRaises(EudslError):
                build_module(lambda: connect_dlist(bad, 0x64EEDC, 112, 1))

    def test_item_size_must_hold_both_links(self):
        for bad in (0, 4, 6, 9, 0x1_0000):
            with self.assertRaises(EudslError):
                build_module(lambda: connect_dlist(0x64EED8, 0x64EEDC, bad, 1))

    def test_sprite_item_size_must_hold_the_seed_field(self):
        with self.assertRaises(EudslError):
            build_module(lambda: connect_dlist(0x63FE30, 0x63FE34, 8, 1, 1))
        build_module(lambda: connect_dlist(0x63FE30, 0x63FE34, 20, 1, 1))

    def test_item_count_must_be_positive(self):
        with self.assertRaises(EudslError):
            build_module(lambda: connect_dlist(0x64EED8, 0x64EEDC, 112, 0))

    def test_sprite_flag_is_zero_or_one(self):
        with self.assertRaises(EudslError):
            build_module(lambda: connect_dlist(0x64EED8, 0x64EEDC, 112, 1, 2))

    def test_connect_dlist_needs_the_module_scope(self):
        # Db() is the scope gate: a bare call outside build_module must fail
        # rather than allocate into a stale builder.
        with self.assertRaises(EudslError):
            connect_dlist(0x64EED8, 0x64EEDC, 112, 1)


if __name__ == "__main__":
    unittest.main()
