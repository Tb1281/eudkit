"""The data dumper core helper (issue #114): a port of eudplib's
``_add_datadumper`` / ``_f_datadumper`` (``eudlib/utilf/datadumper.py``) and
euddraft's ``plugins/dataDumper.py``. Mirror of
``ts/tests/test_datadumper.ts``.

The helper injects a raw byte payload as a module data blob (issue #99's
``Db``) and teaches the target memory about it:

* default / ``unpatchable``: ``SetMemory(out_offset, SetTo, db)`` per target,
  inside a ``once`` gate (the source's ``onPluginStart``);
* ``copy``: each target holds the destination's *byte address*; the helper
  reads it at run time (converting to an EPD like eudplib's ``f_epdread_epd``)
  and copies the payload there with ``memcpyEPD``, ``(len + 3) // 4`` dwords.

What is pinned here: the one-blob-per-call layout and its dword padding, the
emitted HIR for each mode, the flag/argument validation, the extended
``memcpyEPD`` blob source, and a simulated ``copy`` run proving the bytes land
at the destination. Cross-frontend byte-identical HIR and the lowered runtime
proof live in ``crates/eudkit-lower/tests/datadumper_parity.rs``.
"""

from __future__ import annotations

import base64
import json
import unittest

from py import (
    Add,
    Db,
    EPD,
    EPD_BASE,
    EPD_CURRENT_PLAYER,
    EudslError,
    FLAG_COPY,
    FLAG_UNPATCHABLE,
    SetMemory,
    SetTo,
    Subtract,
    build_module,
    dataDumper,
    data_dumper,
    memcpyEPD,
    once,
)
from py.cp import CP_ADDR
from py.tests.helpers import EPD_BASE, read_dword, run_sim, run_static

#: eudplib's own test copies a `RequireData` file to the address stored here.
PTR = 0x58D740
#: An external scratch address (`copy` writes the payload here at run time).
DEST = 0x58E000
PAYLOAD = b"ABCDEFGHIJ"
DWORDS = 3  # ceil(10 / 4)

ACT_TYPE_SET_MEMORY = 45
ACT_DEST_OFFSET = 16
ACT_VALUE_OFFSET = 20
#: Byte offsets of the copy trigger's destination/value fields inside its page.
TRIGGER_DEST_BYTE_OFFSET = 344
TRIGGER_VALUE_BYTE_OFFSET = 348


def _epd(addr: int) -> int:
    return ((addr - EPD_BASE) // 4) & 0xFFFFFFFF


def _module(body) -> dict:
    return json.loads(build_module(body).to_json())["module"]


def _var_reloc(var_id: int, field_offset: int = ACT_DEST_OFFSET) -> dict:
    return {
        "field_offset": field_offset,
        "target_var": var_id,
        "target_offset": ACT_VALUE_OFFSET,
        "mode": 1,
    }


class TestDataDumperConstants(unittest.TestCase):
    def test_flag_names_match_the_source(self):
        self.assertEqual((FLAG_COPY, FLAG_UNPATCHABLE), ("copy", "unpatchable"))

    def test_camel_case_alias_is_the_same_helper(self):
        self.assertIs(dataDumper, data_dumper)


class TestSharedEpdReader(unittest.TestCase):
    """The `view.read_epd` / `read_epd_pointer` primitives.

    The pointer read is eudplib's `f_epdread_epd`: the reset is owned by the
    read site, one bit test per set bit of `0xFFFFFFFC`, then the one
    `EPD_BASE // 4` subtraction. Both helpers live in `view` so the data
    dumper and the collision scrubber share one body.
    """

    def test_read_epd_resets_then_tests_the_masked_bits(self):
        from py.view import read_epd

        mod = _module(lambda: read_epd(EPD(PTR), 30, 2, mask=0xFFFFFFFC))
        stmts = mod["stmts"]

        self.assertEqual(len(stmts), 31)
        reset = stmts[0]
        self.assertEqual(reset["kind"], "trigger")
        self.assertEqual(reset["acts"][0]["value"], 0)
        self.assertEqual(reset["acts"][0]["relocs"], [_var_reloc(0)])

        for bit in range(2, 32):
            trigger = stmts[1 + bit - 2]
            self.assertEqual(trigger["kind"], "trigger")
            (cond,) = trigger["conds"]
            self.assertEqual(cond["player"], _epd(PTR))
            self.assertEqual(cond["amount"], 1 << bit)
            self.assertEqual(trigger["acts"][0]["modifier"], Add)
            self.assertEqual(trigger["acts"][0]["value"], 1 << (bit - 2))

    def test_read_epd_pointer_subtracts_the_epd_base(self):
        from py.view import read_epd_pointer

        mod = _module(lambda: once(lambda: read_epd_pointer(EPD(PTR))))
        stmts = mod["stmts"][0]["body"]

        self.assertEqual(
            [stmt["kind"] for stmt in stmts], ["trigger"] * 31 + ["act"]
        )
        subtract = stmts[31]
        self.assertEqual(subtract["modifier"], Add)
        self.assertEqual(
            subtract["value"],
            (0x100000000 - (EPD_BASE >> 2)) & 0xFFFFFFFF,
        )
        self.assertEqual(subtract["relocs"], [_var_reloc(0)])


class TestDefaultModeHir(unittest.TestCase):
    def test_default_mode_writes_the_blob_address_into_the_target(self):
        mod = _module(lambda: data_dumper(PAYLOAD, [PTR]))

        # One blob per call, padded to a whole dword so the `copy` mode's last
        # rep-movsd dword stays in-payload.
        self.assertEqual(
            mod["blobs"],
            [
                {
                    "size": DWORDS * 4,
                    "data": base64.b64encode(PAYLOAD + b"\x00\x00").decode(),
                }
            ],
        )

        self.assertEqual(len(mod["stmts"]), 1)
        once = mod["stmts"][0]
        self.assertEqual(once["kind"], "once")
        self.assertEqual(once["conds"], [])
        self.assertEqual(len(once["body"]), 1)
        act = once["body"][0]
        self.assertEqual(act["kind"], "act")
        self.assertEqual(act["acttype"], ACT_TYPE_SET_MEMORY)
        self.assertEqual(act["modifier"], SetTo)
        self.assertEqual(act["dest"], _epd(PTR))
        self.assertEqual(act["value"], 0)
        self.assertEqual(
            act["relocs"],
            [
                {
                    "field_offset": ACT_VALUE_OFFSET,
                    "target_blob": 0,
                    "target_offset": 0,
                    "mode": 4,
                }
            ],
        )

    def test_every_target_shares_the_one_blob(self):
        second = 0x58D744
        mod = _module(lambda: data_dumper(PAYLOAD, [PTR, second]))

        self.assertEqual(len(mod["blobs"]), 1)
        acts = mod["stmts"][0]["body"]
        self.assertEqual(len(acts), 2)
        self.assertEqual([act["dest"] for act in acts], [_epd(PTR), _epd(second)])
        self.assertEqual(
            [act["relocs"][0]["target_blob"] for act in acts], [0, 0]
        )

    def test_dword_aligned_payload_is_not_padded(self):
        mod = _module(lambda: data_dumper(b"ABCD", [PTR]))
        self.assertEqual(mod["blobs"], [{"size": 4, "data": "QUJDRA=="}])

    def test_unpatchable_mode_emits_the_default_write(self):
        # eudkit never patches memory at build time, so `unpatchable` is the
        # same run-time write as the default mode (documented deviation).
        default = _module(lambda: data_dumper(PAYLOAD, [PTR]))
        unpatchable = _module(
            lambda: data_dumper(PAYLOAD, [PTR], flags=(FLAG_UNPATCHABLE,))
        )
        self.assertEqual(default, unpatchable)


class TestCopyModeHir(unittest.TestCase):
    def setUp(self):
        self.mod = _module(
            lambda: data_dumper(PAYLOAD, [PTR], flags=(FLAG_COPY,))
        )
        self.once = self.mod["stmts"][0]
        self.body = self.once["body"]

    def test_blob_is_padded_to_whole_dwords(self):
        self.assertEqual(
            self.mod["blobs"],
            [
                {
                    "size": DWORDS * 4,
                    "data": base64.b64encode(PAYLOAD + b"\x00\x00").decode(),
                }
            ],
        )

    def test_once_body_is_read_then_seed_then_copy_loop(self):
        self.assertEqual(self.once["kind"], "once")
        self.assertEqual(self.once["conds"], [])
        # `f_epdread_epd`: one accumulator reset + one bit test per bit 2..31,
        # then the EPD_BASE subtraction, the counter setup, the blob-cursor
        # seed trigger, the gated destination/cursor seed and the loop.
        self.assertEqual(len(self.body), 36)
        self.assertEqual(
            [stmt["kind"] for stmt in self.body],
            ["trigger"] * 31 + ["act", "act", "trigger", "when", "while"],
        )

    def test_destination_read_tests_bits_two_and_up(self):
        reset = self.body[0]["acts"][0]
        self.assertEqual(reset["value"], 0)
        self.assertEqual(reset["relocs"], [_var_reloc(0)])

        # The read result is variable 0; bit `bit` of the dword at PTR adds
        # 1 << (bit - 2), i.e. the read accumulates `D >> 2`. Bits 0-1 are
        # dropped because the dword holds a dword-aligned byte address.
        for bit in range(2, 32):
            trigger = self.body[1 + bit - 2]
            self.assertEqual(trigger["kind"], "trigger")
            (cond,) = trigger["conds"]
            self.assertEqual(cond["condtype"], 15)  # Deaths / Memory wire form
            self.assertEqual(cond["player"], _epd(PTR))
            self.assertEqual(cond["amount"], 1 << bit)
            self.assertEqual(cond["comparison"], 0)  # AtLeast
            self.assertEqual(trigger["acts"][0]["modifier"], Add)
            self.assertEqual(trigger["acts"][0]["value"], 1 << (bit - 2))
            self.assertEqual(trigger["acts"][0]["relocs"], [_var_reloc(0)])

    def test_epd_base_subtraction_turns_the_read_into_an_epd(self):
        # `EPD(D) = (D - 0x58A364) // 4` is `(D >> 2) - (0x58A364 >> 2)`, with
        # wrapping subtraction emitted as an add of the two's complement.
        subtract = self.body[31]
        self.assertEqual(subtract["kind"], "act")
        self.assertEqual(subtract["modifier"], Add)
        self.assertEqual(subtract["value"], (0x100000000 - (EPD_BASE >> 2)) & 0xFFFFFFFF)
        self.assertEqual(subtract["relocs"], [_var_reloc(0)])

    def test_blob_seeds_the_source_cursor_through_a_mode_1_reloc(self):
        # Cycle 27 #01: the blob EPD is only known at assembly time, so the
        # source cursor stays a var (id 3) seeded by a mode-1 blob relocation;
        # the destination seed rides in the same trigger's dest field.
        seed = self.body[33]["acts"][0]
        self.assertEqual(seed["modifier"], SetTo)
        self.assertEqual(
            seed["relocs"],
            [
                _var_reloc(3),
                {
                    "field_offset": ACT_VALUE_OFFSET,
                    "target_blob": 0,
                    "target_offset": 0,
                    "mode": 1,
                },
            ],
        )

    def test_destination_and_count_are_seeded_before_the_loop(self):
        # Cycle 27 #01: the counter is a real dword var seeded before the
        # loop, while the destination/cursor seed runs inside a `when
        # (cnt >= 1)` gate so a runtime zero count leaves CP and the scratch
        # destination untouched.
        count = self.body[32]
        self.assertEqual(count["kind"], "act")
        self.assertEqual(count["value"], DWORDS)
        self.assertEqual(count["relocs"], [_var_reloc(1)])

        gate = self.body[34]
        self.assertEqual(gate["kind"], "when")
        self.assertEqual(
            gate["conds"][0]["relocs"], [_var_reloc(1, field_offset=4)]
        )
        destination, cursor, decrement = gate["body"]
        # The EPD read result (var 0) seeds the copy trigger's dest field.
        self.assertEqual(destination["kind"], "varstore")
        self.assertEqual(destination["src_var"], 0)
        self.assertEqual(destination["dst_offset"], TRIGGER_DEST_BYTE_OFFSET)
        # The blob cursor (var 3) lands in CurrentPlayer, stepped back one
        # dword because the loop advances CP before every read.
        self.assertEqual(cursor["kind"], "varstore")
        self.assertEqual(cursor["src_var"], 3)
        self.assertEqual(cursor["dst_addr"], CP_ADDR)
        self.assertEqual(decrement["kind"], "act")
        self.assertEqual(decrement["modifier"], Subtract)
        self.assertEqual(decrement["dest"], EPD_CURRENT_PLAYER)
        self.assertEqual(decrement["value"], 1)

    def test_loop_copies_one_dword_per_pass(self):
        loop = self.body[35]
        self.assertEqual(loop["kind"], "while")
        (cond,) = loop["conds"]
        self.assertEqual(cond["comparison"], 0)  # AtLeast
        self.assertEqual(cond["amount"], 1)
        self.assertEqual(cond["relocs"], [_var_reloc(1, field_offset=4)])
        # #97: the source dword is read through the shared reader body.
        # Cycle 27 #01: the pass advances CurrentPlayer, reads, copies, seeds
        # the trigger's value field and moves the dest field in real memory.
        kinds = [stmt["kind"] for stmt in loop["body"]]
        self.assertEqual(
            kinds,
            ["act", "call", "vartransfer", "varstore", "trigger", "act", "act"],
        )
        (call,) = [stmt for stmt in loop["body"] if stmt["kind"] == "call"]
        reader = next(f for f in self.mod["funcs"] if f["id"] == call["func"])
        self.assertEqual(len(reader["body"]), 33)
        # The copied dword lands in the copy trigger's value field.
        value_store = loop["body"][3]
        self.assertEqual(value_store["kind"], "varstore")
        self.assertEqual(value_store["src_var"], 2)
        self.assertEqual(value_store["dst_offset"], TRIGGER_VALUE_BYTE_OFFSET)
        # dest field += 1, counter -= 1 (masked add of -1); the source cursor
        # is CurrentPlayer itself, so it moves with the EPD read.
        advance_dest, decrement = loop["body"][-2:]
        self.assertEqual(advance_dest["value"], 1)
        self.assertEqual(
            advance_dest["relocs"],
            [
                {
                    "field_offset": ACT_DEST_OFFSET,
                    "target_trigger": 32,
                    "target_offset": TRIGGER_DEST_BYTE_OFFSET,
                    "mode": 1,
                }
            ],
        )
        self.assertEqual(decrement["value"], 0xFFFFFFFF)
        self.assertEqual(decrement["relocs"], [_var_reloc(1)])


class TestCopyModeRuntime(unittest.TestCase):
    def test_payload_is_copied_to_the_address_stored_in_the_target(self):
        def prog():
            # Seed the target dword with a *byte address*, as the game does
            # (e.g. the `stat_txt.tbl` pointer at 0x58D740). The `copy` mode
            # reads it at run time and converts it to its EPD.
            SetMemory(PTR, SetTo, DEST)
            data_dumper(PAYLOAD, [PTR], flags=(FLAG_COPY,))

        report = run_sim(build_module(prog).to_json(), loops=1)
        start = _epd(DEST)
        words = [read_dword(report, start + index) for index in range(DWORDS)]
        copied = b"".join(word.to_bytes(4, "little") for word in words)
        self.assertEqual(copied, PAYLOAD + b"\x00\x00")

    def test_low_address_destination_wraps_like_epd(self):
        # A destination below EPD_BASE needs the wrapping
        # `(D - 0x58A364) // 4` conversion, not a plain shift.
        low = 0x80000
        report = run_sim(
            build_module(
                lambda: (
                    SetMemory(PTR, SetTo, low),
                    data_dumper(PAYLOAD, [PTR], flags=(FLAG_COPY,)),
                )
            ).to_json(),
            loops=1,
        )
        start = _epd(low) & 0xFFFFFFFF
        words = [read_dword(report, start + index) for index in range(DWORDS)]
        copied = b"".join(word.to_bytes(4, "little") for word in words)
        self.assertEqual(copied, PAYLOAD + b"\x00\x00")

    def test_each_target_reads_its_own_destination(self):
        other = 0x58E100

        def prog():
            SetMemory(PTR, SetTo, DEST)
            SetMemory(PTR + 4, SetTo, other)
            data_dumper(PAYLOAD, [PTR, PTR + 4], flags=(FLAG_COPY,))

        report = run_sim(build_module(prog).to_json(), loops=1)
        for dest in (DEST, other):
            start = _epd(dest)
            words = [read_dword(report, start + index) for index in range(DWORDS)]
            copied = b"".join(word.to_bytes(4, "little") for word in words)
            self.assertEqual(copied, PAYLOAD + b"\x00\x00", hex(dest))

    def test_default_mode_stores_the_blob_address_in_the_target(self):
        report = run_sim(
            build_module(lambda: data_dumper(PAYLOAD, [PTR])).to_json(),
            loops=1,
        )
        # The stored value is the blob's payload address (2408 = one 2,408-byte
        # trigger page, the only item before it).
        self.assertEqual(read_dword(report, _epd(PTR)), 2408)

    def test_static_cost_of_one_payload_copy(self):
        report = run_static(
            build_module(
                lambda: data_dumper(PAYLOAD, [PTR], flags=(FLAG_COPY,))
            ).to_json()
        )
        static = report["static_cost"]
        self.assertEqual(
            [static["triggers"], static["conditions"], static["actions"]],
            # 31 EPD-read triggers + counter setup + blob-cursor seed trigger
            # + the gated destination seed + the shared-reader loop. Cycle 27
            # #01's pointer cursors trade two per-pass actions for the seed
            # trigger/condition.
            [77, 64, 150],
        )
        # Payload plus two cell dwords: the once latch/cursor dword and the
        # reader-result + call-destination bit cells (#97).
        self.assertEqual(static["blob_bytes"], DWORDS * 4 + 8)


class TestDataDumperValidation(unittest.TestCase):
    def test_empty_offset_list_allocates_nothing(self):
        self.assertIsNone(data_dumper(PAYLOAD, []))
        self.assertEqual(
            _module(lambda: data_dumper(PAYLOAD, [])).get("blobs", []), []
        )

    def test_empty_payload_is_rejected(self):
        with self.assertRaisesRegex(EudslError, "EUDSL-RANGE"):
            data_dumper(b"", [PTR])

    def test_payload_must_be_bytes(self):
        with self.assertRaisesRegex(EudslError, "EUDSL-TYPE"):
            data_dumper("ABCD", [PTR])  # type: ignore[arg-type]

    def test_offsets_must_be_a_sequence(self):
        with self.assertRaisesRegex(EudslError, "EUDSL-TYPE"):
            data_dumper(PAYLOAD, PTR)  # type: ignore[arg-type]
        with self.assertRaisesRegex(EudslError, "EUDSL-TYPE"):
            data_dumper(PAYLOAD, "0x58D740")  # type: ignore[arg-type]

    def test_offsets_must_be_positive_dword_addresses(self):
        for bad in (0, -4, 0x58D742, 0x58D741, 0x100000000):
            with self.subTest(offset=hex(bad & 0xFFFFFFFF)):
                with self.assertRaisesRegex(EudslError, "EUDSL-BOUNDS"):
                    data_dumper(PAYLOAD, [bad])

    def test_flags_must_be_known_names(self):
        with self.assertRaisesRegex(EudslError, "EUDSL-RANGE"):
            data_dumper(PAYLOAD, [PTR], flags=("bogus",))
        with self.assertRaisesRegex(EudslError, "EUDSL-TYPE"):
            data_dumper(PAYLOAD, [PTR], flags="copy")  # type: ignore[arg-type]

    def test_copy_and_unpatchable_are_mutually_exclusive(self):
        with self.assertRaisesRegex(EudslError, "EUDSL-BOUNDS"):
            data_dumper(
                PAYLOAD, [PTR], flags=(FLAG_COPY, FLAG_UNPATCHABLE)
            )


class TestMemcpyBlobSource(unittest.TestCase):
    def test_blob_source_seeds_the_cursor_through_a_relocation(self):
        mod = _module(lambda: memcpyEPD(PTR, Db(b"ABCDEFGH"), 2))

        # Cycle 27 #01: the counter setup comes first (a plain var store),
        # then the blob-cursor seed trigger; the cursor var (id 2) is seeded
        # through a mode-1 relocation because the blob EPD is only known at
        # assembly time.
        seed = mod["stmts"][1]
        self.assertEqual(seed["kind"], "trigger")
        self.assertEqual(seed["conds"], [])
        self.assertEqual(len(seed["acts"]), 1)
        self.assertEqual(
            seed["acts"][0]["relocs"],
            [
                _var_reloc(2),
                {
                    "field_offset": ACT_VALUE_OFFSET,
                    "target_blob": 0,
                    "target_offset": 0,
                    "mode": 1,
                },
            ],
        )
        self.assertEqual(mod["blobs"], [{"size": 8, "data": "QUJDREVGR0g="}])

    def test_constant_source_keeps_the_old_emission(self):
        mod = _module(lambda: memcpyEPD(PTR, EPD(PTR), 2))
        self.assertEqual(mod.get("blobs", []), [])
        for stmt in mod["stmts"]:
            for act in stmt.get("acts") or []:
                for reloc in act.get("relocs") or []:
                    self.assertNotIn("target_blob", reloc)


if __name__ == "__main__":
    unittest.main()
