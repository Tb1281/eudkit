"""Issue #99: HIR Blob/Db seam (`Db(bytes | size)` → `LirItem::Blob`).

Mirror of ``ts/tests/test_ticket99.ts``. The probe is the unlimiter's
``connectDList`` shape (``~/workspace/opencode-cell-test/unlimiter-source.py``):
one ``Db`` per game list and a ``SetMemory`` action patching that list's head
with the blob's payload address. The seam lowers those blobs to LIR blob items
appended after the trigger pages / demoted Cell blob, and both action fields
and blob-internal dword fields relocate to their payload addresses.
"""

from __future__ import annotations

import base64
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from py import (
    Always,
    Db,
    DbRef,
    EudslError,
    SetMemory,
    SetTo,
    Trigger,
    Var,
    build_module,
)
from py.tests.helpers import EUDKIT_COMPILE, run_static

# The unlimiter's four `connectDList` calls: (list head, element size).
GAME_LISTS = [
    (0x64EED8, 112),  # bullet count
    (0x63FE30, 36),  # sprite count
    (0x57EB68, 64),  # image count
    (0x64B2E0, 20),  # order count
]


def _epd(addr: int) -> int:
    # The HIR `dest` field is a u32; EPDs below 0x58A364 wrap (the image
    # list head 0x57EB68 is one of them).
    return ((addr - 0x58A364) // 4) & 0xFFFFFFFF


def _module_json(body) -> dict:
    return json.loads(build_module(body).to_json())


def _run_lower(hir_json: str) -> dict:
    with tempfile.TemporaryDirectory(prefix="eudkit-py-c99-") as tmpdir:
        hir_path = Path(tmpdir) / "prog.hir.json"
        hir_path.write_text(hir_json, encoding="utf-8")
        out = subprocess.check_output(
            [str(EUDKIT_COMPILE), "lower", str(hir_path)], text=True
        ).strip()
    return json.loads(out)


class Ticket99HirBlobTest(unittest.TestCase):
    def test_unlimiter_dbs_patch_four_game_list_heads(self):
        def prog():
            dbs = [Db(size) for _, size in GAME_LISTS]
            for (head, _), db in zip(GAME_LISTS, dbs):
                SetMemory(head, SetTo, db)

        module = _module_json(prog)["module"]
        self.assertEqual(
            module["blobs"], [{"size": size} for _, size in GAME_LISTS]
        )
        acts = module["stmts"][0]["acts"]
        for index, act in enumerate(acts):
            self.assertEqual(act["dest"], _epd(GAME_LISTS[index][0]))
            self.assertEqual(act["value"], 0)
            self.assertEqual(
                act["relocs"],
                [
                    {
                        "field_offset": 20,
                        "target_blob": index,
                        "target_offset": 0,
                        "mode": 4,
                    }
                ],
            )

    def test_db_bytes_are_base64_and_offsets_shift(self):
        payload = bytes(range(8))

        def prog():
            db = Db(payload)
            SetMemory(db + 4, SetTo, 10000)

        module = _module_json(prog)["module"]
        self.assertEqual(module["blobs"][0]["size"], 8)
        self.assertEqual(
            base64.b64decode(module["blobs"][0]["data"]), payload
        )
        acts = module["stmts"][0]["acts"]
        # Destination is the blob's dword at +4 (EPD relocation, mode 1).
        self.assertEqual(
            acts[0]["relocs"],
            [
                {
                    "field_offset": 16,
                    "target_blob": 0,
                    "target_offset": 4,
                    "mode": 1,
                }
            ],
        )
        self.assertEqual(acts[0]["value"], 10000)

    def test_blob_internal_relocs_target_blobs_and_triggers(self):
        def prog():
            tail = Db(8)
            head = Db(
                8,
                relocs=[
                    {"offset": 0, "target": tail, "mode": 4},
                    {"offset": 4, "target": tail, "target_offset": 4, "mode": 4},
                ],
            )
            Trigger(
                conds=[Always()],
                acts=[
                    {
                        "mask": 0,
                        "strid": 0,
                        "wavid": 0,
                        "time": 0,
                        "dest": 0,
                        "value": 0,
                        "unit": 0,
                        "acttype": 45,
                        "modifier": 7,
                        "flags": 20,
                        "padding": 0,
                        "eudx": 0,
                        "relocs": [
                            {
                                "field_offset": 20,
                                "target_blob": 1,
                                "target_offset": 0,
                                "mode": 4,
                            }
                        ],
                    }
                ],
            )
            # A blob can also point at a trigger item.
            Db(4, relocs=[{"offset": 0, "target_trigger": 0, "mode": 4}])
            _ = head

        module = _module_json(prog)["module"]
        self.assertEqual([b["size"] for b in module["blobs"]], [8, 8, 4])
        self.assertEqual(
            module["blobs"][1]["relocs"],
            [
                {
                    "offset": 0,
                    "target_blob": 0,
                    "target_offset": 0,
                    "mode": 4,
                },
                {
                    "offset": 4,
                    "target_blob": 0,
                    "target_offset": 4,
                    "mode": 4,
                },
            ],
        )
        self.assertEqual(
            module["blobs"][2]["relocs"],
            [{"offset": 0, "target_trigger": 0, "target_offset": 0, "mode": 4}],
        )

    def test_native_lowering_appends_blobs_and_resolves_head_patches(self):
        def prog():
            dbs = [Db(size) for _, size in GAME_LISTS]
            for (head, _), db in zip(GAME_LISTS, dbs):
                SetMemory(head, SetTo, db)

        lir = _run_lower(build_module(prog).to_json())
        items = lir["module"]["items"]
        self.assertEqual(len(items), 1 + len(GAME_LISTS))
        trigger = items[0]
        self.assertEqual(trigger["kind"], "trigger")
        for index, (_, size) in enumerate(GAME_LISTS):
            blob = items[1 + index]
            self.assertEqual(blob["kind"], "blob")
            self.assertEqual(blob["size"], size)
            assert trigger["acts"][index]["relocs"] == [
                {
                    "field_offset": 20,
                    "target_item": 1 + index,
                    "target_offset": 0,
                    "mode": 4,
                }
            ]

    def test_lowering_places_blobs_after_the_demoted_cell_blob(self):
        def prog():
            # A scalar that never needs a page is demoted to a 4-byte Cell
            # blob, which must stay ahead of the HIR blobs.
            _ = Var(0)
            db = Db(12)
            SetMemory(0x57EB68, SetTo, db)

        lir = _run_lower(build_module(prog).to_json())
        items = lir["module"]["items"]
        self.assertEqual([item["kind"] for item in items], ["trigger", "blob", "blob"])
        self.assertEqual(items[2]["size"], 12)

    def test_static_cost_counts_blob_bytes(self):
        def prog():
            dbs = [Db(size) for _, size in GAME_LISTS]
            for (head, _), db in zip(GAME_LISTS, dbs):
                SetMemory(head, SetTo, db)

        report = run_static(build_module(prog).to_json())
        self.assertEqual(
            report["static_cost"]["blob_bytes"],
            sum(size for _, size in GAME_LISTS),
        )

    def test_db_outside_build_module_is_rejected(self):
        with self.assertRaises(EudslError) as ctx:
            Db(16)
        self.assertEqual(ctx.exception.code, "EUDSL-SCOPE")

    def test_db_payload_validation(self):
        for payload in (-4, 1.5, "bytes", None):
            with self.assertRaises(EudslError):
                build_module(lambda: Db(payload))
        with self.assertRaises(EudslError):
            build_module(lambda: Db(16).at(2))
        with self.assertRaises(EudslError):
            build_module(lambda: Db(16, relocs=[{"offset": 2, "target_blob": 0}]))

    def test_db_ref_arithmetic(self):
        def prog():
            db = Db(16)
            self.assertIsInstance(db, DbRef)
            self.assertIsInstance(db + 4, DbRef)
            self.assertIsInstance(4 + db, DbRef)
            self.assertEqual((db + 8).byteOffset, 8)
            self.assertEqual((db + 8).at(4).byteOffset, 12)

        build_module(prog)


if __name__ == "__main__":
    unittest.main()
