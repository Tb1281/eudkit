"""Ticket #1 tests for the Python DSL (`py/`).
Compatible with both `pytest` and `python3 -m unittest`.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from py import (
    ACT_COUNT,
    COND_COUNT,
    EPD,
    P1,
    Add,
    Always,
    AtLeast,
    Deaths,
    DeathsX,
    DisplayText,
    EudslError,
    Exactly,
    Memory,
    SetDeaths,
    SetDeathsX,
    SetMemory,
    SetMemoryX,
    SetNextPtr,
    SetTo,
    TRIGGER_SIZE_BYTES,
    TRIGGER_STRIDE_EPDS,
    Trigger,
    buildModule,
)

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
EUDKIT_COMPILE = REPO_ROOT / "target" / "debug" / "eudkit-compile"
BASEMAP_SCX = REPO_ROOT / "tests" / "outputmap" / "basemap" / "basemap.scx"


class TestTicket1PythonDsl(unittest.TestCase):
    def test_trigger_slot_inspection_and_geometry(self) -> None:
        def prog() -> None:
            t0 = Trigger(
                {
                    "conds": [
                        Always(),
                        DeathsX(P1, Exactly, 42, 0, 0xFF00FF00),
                    ],
                    "acts": [
                        SetDeathsX(EPD(0x58A368), SetTo, 0x12345678, 0, 0x00FF00FF),
                        DisplayText("Hello from Python!"),
                    ],
                    "preserved": True,
                }
            )
            self.assertEqual(t0.prev.byteOffset, 0)
            self.assertEqual(t0.prev.epdOffset, 0)
            self.assertEqual(t0.next.byteOffset, 4)
            self.assertEqual(t0.next.epdOffset, 1)
            self.assertEqual(t0.flagsField.byteOffset, 2376)

            # Condition slot inspection (5-EPD = 20B stride starting at +8)
            c0 = t0.cond(0)
            self.assertEqual(c0.byteOffset, 8)
            self.assertEqual(c0.epdOffset, 2)
            self.assertEqual(c0.condtype.initialValue, 22)

            c1 = t0.cond(1)
            self.assertEqual(c1.byteOffset, 28)
            self.assertEqual(c1.epdOffset, 7)
            self.assertEqual(c1.mask.byteOffset, 28)
            self.assertEqual(c1.mask.initialValue, 0xFF00FF00)
            self.assertEqual(c1.player.byteOffset, 32)
            self.assertEqual(c1.amount.byteOffset, 36)
            self.assertEqual(c1.amount.initialValue, 42)
            self.assertEqual(c1.comparison.byteOffset, 42)
            self.assertEqual(c1.condtype.byteOffset, 43)
            self.assertEqual(c1.eudx.byteOffset, 46)
            self.assertEqual(c1.eudx.initialValue, 0x4353)

            c15 = t0.cond(15)
            self.assertEqual(c15.byteOffset, 8 + 15 * 20)
            self.assertEqual(c15.epdOffset, 2 + 15 * 5)

            # Action slot inspection (8-EPD = 32B stride starting at +328)
            a0 = t0.act(0)
            self.assertEqual(a0.byteOffset, 328)
            self.assertEqual(a0.epdOffset, 82)
            self.assertEqual(a0.mask.byteOffset, 328)
            self.assertEqual(a0.mask.initialValue, 0x00FF00FF)
            self.assertEqual(a0.dest.byteOffset, 344)
            self.assertEqual(a0.dest.initialValue, 1)
            self.assertEqual(a0.value.byteOffset, 348)
            self.assertEqual(a0.value.initialValue, 0x12345678)
            self.assertEqual(a0.acttype.byteOffset, 354)
            self.assertEqual(a0.acttype.initialValue, 45)
            self.assertEqual(a0.modifier.byteOffset, 355)
            self.assertEqual(a0.modifier.initialValue, 7)
            self.assertEqual(a0.flags.byteOffset, 356)
            self.assertEqual(a0.flags.initialValue, 20)
            self.assertEqual(a0.eudx.byteOffset, 358)
            self.assertEqual(a0.eudx.initialValue, 0x4353)

            a63 = t0.act(63)
            self.assertEqual(a63.byteOffset, 328 + 63 * 32)
            self.assertEqual(a63.epdOffset, 82 + 63 * 8)

        mod = buildModule(prog)
        self.assertEqual(len(mod.triggers), 1)
        self.assertEqual(TRIGGER_SIZE_BYTES, 2408)
        self.assertEqual(TRIGGER_STRIDE_EPDS, 602)

    def test_trigger_bounds_errors_are_loud_eudsl_bounds(self) -> None:
        with self.assertRaisesRegex(EudslError, "EUDSL-BOUNDS") as ctx1:
            Trigger(conds=[Always() for _ in range(COND_COUNT + 1)], acts=[])
        self.assertEqual(ctx1.exception.code, "EUDSL-BOUNDS")

        with self.assertRaisesRegex(EudslError, "EUDSL-BOUNDS") as ctx2:
            Trigger(
                conds=[Always()],
                acts=[SetDeaths(P1, SetTo, i, 0) for i in range(ACT_COUNT + 1)],
            )
        self.assertEqual(ctx2.exception.code, "EUDSL-BOUNDS")

        t = Trigger(conds=[Always()], acts=[SetDeaths(P1, SetTo, 1, 0)])
        with self.assertRaisesRegex(EudslError, "EUDSL-BOUNDS"):
            t.cond(-1)
        with self.assertRaisesRegex(EudslError, "EUDSL-BOUNDS"):
            t.cond(16)
        with self.assertRaisesRegex(EudslError, "EUDSL-BOUNDS"):
            t.act(-1)
        with self.assertRaisesRegex(EudslError, "EUDSL-BOUNDS"):
            t.act(64)

    def test_top_level_unconditional_actions_bin_pack_to_64_per_trigger(self) -> None:
        def prog() -> None:
            for i in range(130):
                SetDeaths(P1, Add, i, 0)

        mod = buildModule(prog)
        self.assertEqual(len(mod.triggers), 3)
        self.assertEqual([len(t.acts) for t in mod.triggers], [64, 64, 2])

    def test_end_to_end_lower_and_scx_map_compilation(self) -> None:
        def prog() -> None:
            t0 = Trigger(
                conds=[Always(), Deaths(P1, AtLeast, 1, 0)],
                acts=[
                    SetMemory(0x58A364, SetTo, 100),
                    SetMemoryX(0x58A368, SetTo, 0xABCD, 0xFFFF),
                    DisplayText("Tracer bullet SCX test"),
                ],
                preserved=True,
            )
            t1 = Trigger(
                conds=[Memory(0x58A364, Exactly, 100)],
                acts=[
                    SetDeaths(t0.act(0).value, SetTo, 200, 0),
                    SetNextPtr(t0, t0),
                ],
                preserved=True,
            )
            _ = t1

        mod = buildModule(prog)
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            hir_path = tmp_path / "prog.hir.json"
            out_scx = tmp_path / "out.scx"
            hir_path.write_text(mod.to_json(), encoding="utf-8")

            lower_res = subprocess.run(
                [str(EUDKIT_COMPILE), "lower", str(hir_path)],
                check=True,
                capture_output=True,
                text=True,
            )
            lir_doc = json.loads(lower_res.stdout)
            self.assertEqual(lir_doc["version"], "3.0")
            self.assertEqual(len(lir_doc["module"]["items"]), 2)
            for item in lir_doc["module"]["items"]:
                self.assertEqual(item["kind"], "trigger")
                self.assertEqual(item["size"], 2408)

            map_res = subprocess.run(
                [str(EUDKIT_COMPILE), "map", str(hir_path), str(BASEMAP_SCX), str(out_scx)],
                check=True,
                capture_output=True,
                text=True,
            )
            cost = json.loads(map_res.stdout)
            # 2 program triggers + the 35-trigger once-per-tick gate. The
            # gate contributes 33 conditions (branch + 32 bit copies) and
            # 35 actions.
            self.assertEqual(
                cost,
                {
                    "triggers": 37,
                    "conditions": 36,
                    "actions": 40,
                    "trigger_bytes": 37 * 2408,
                    "blob_bytes": 0,
                    "total_bytes": 37 * 2408,
                },
            )
            self.assertTrue(out_scx.is_file())
            self.assertGreater(out_scx.stat().st_size, 0)


if __name__ == "__main__":
    unittest.main()
