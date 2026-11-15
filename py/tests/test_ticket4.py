"""Ticket #4 tests for the Python DSL (`py/`):
TriggerStruct: per-action hardware-masked fields, direct property access (s.hp),
and 1T whole-struct copy.
Compatible with both `pytest` and `python3 -m unittest`.
"""

from __future__ import annotations

import subprocess
import unittest
from pathlib import Path

from py import (
    ACT_COUNT,
    DisplayText,
    EudslError,
    Trigger,
    TriggerStruct,
    Var,
    bit,
    buildModule,
    byte,
    defineStruct,
    dword,
    masked,
    word,
)

from py.tests.helpers import run_sim
REPO_ROOT = Path(__file__).resolve().parent.parent.parent
BASEMAP_SCX = REPO_ROOT / "tests" / "outputmap" / "basemap" / "basemap.scx"
class TestTicket4PythonDsl(unittest.TestCase):
    def test_schema_definition_field_types_and_bounds(self) -> None:
        Hero = TriggerStruct(
            "Hero",
            hp=dword(),
            mp=word(),
            armor=byte(),
            flags=bit(4),
            custom=masked(0x0F00),
            level="byte",
        )

        self.assertEqual(Hero.name, "Hero")
        self.assertEqual(Hero.fieldCount, 6)
        self.assertEqual(Hero.fields["hp"].mask, 0xFFFFFFFF)
        self.assertEqual(Hero.fields["mp"].mask, 0x0000FFFF)
        self.assertEqual(Hero.fields["armor"].mask, 0x000000FF)
        self.assertEqual(Hero.fields["flags"].mask, 0x0000000F)
        self.assertEqual(Hero.fields["custom"].mask, 0x00000F00)
        self.assertEqual(Hero.fields["level"].mask, 0x000000FF)

        # K = 0 rejected with EUDSL-BOUNDS
        with self.assertRaises(EudslError) as ctx:
            TriggerStruct("Empty", {})
        self.assertEqual(ctx.exception.code, "EUDSL-BOUNDS")

        # K = 65 rejected with EUDSL-BOUNDS
        big_spec = {f"f{i}": "dword" for i in range(65)}
        with self.assertRaises(EudslError) as ctx:
            defineStruct("TooBig", big_spec)
        self.assertEqual(ctx.exception.code, "EUDSL-BOUNDS")

        # K = 64 accepted
        max_spec = {f"f{i}": "dword" for i in range(64)}
        MaxStruct = defineStruct("MaxStruct", max_spec)
        self.assertEqual(MaxStruct.fieldCount, 64)

    def test_direct_property_access_mutations_and_operator_overloads(self) -> None:
        Unit = TriggerStruct(
            hp="dword",
            mp="word",
            armor="byte",
        )

        def prog() -> None:
            u = Unit(hp=1000, mp=50, armor=5)

            # Direct property access & methods
            u.hp.set(1200)
            u.mp.iadd(10)
            u.armor.isub(2)

            # Python operator syntax: =, +=, ==
            u.hp = 1500
            u.mp += 5
            cond1 = u.hp >= 500
            cond2 = u.mp == 65
            self.assertEqual(cond1["comparison"], 0)  # AtLeast
            self.assertEqual(cond2["comparison"], 10)  # Exactly

            # Unknown field raises loud AttributeError
            with self.assertRaises(AttributeError):
                _bad = u.shield  # type: ignore[attr-defined]

            with self.assertRaises(AttributeError):
                u.shield = 100  # type: ignore[attr-defined]

        buildModule(prog)

    def test_contiguous_trigger_page_slot_packing_without_page_splitting(self) -> None:
        Small = TriggerStruct(
            "Small",
            a="dword",
            b="word",
            c="byte",
            d="byte",
        )
        Wide = TriggerStruct(
            "Wide",
            **{f"w{i}": "dword" for i in range(8)},
        )

        def prog() -> None:
            # 1. Pack two 4-field structs into slots 0..3 and 4..7 of Page 0
            s1 = Small()
            self.assertEqual(s1.pageIndex, 0)
            self.assertEqual(s1.slotIndex, 0)
            self.assertEqual(s1.a.id, 0)
            self.assertEqual(s1.b.id, 1)
            self.assertEqual(s1.c.id, 2)
            self.assertEqual(s1.d.id, 3)

            s2 = Small()
            self.assertEqual(s2.pageIndex, 0)
            self.assertEqual(s2.slotIndex, 4)
            self.assertEqual(s2.a.id, 4)
            self.assertEqual(s2.b.id, 5)
            self.assertEqual(s2.c.id, 6)
            self.assertEqual(s2.d.id, 7)

            # 2. Allocate 52 standalone scalar Vars. These are cells, not
            # page slots: at lowering they become 2 BitCells (Var(0),
            # Var(1)) and 50 DwordCells, so only the 8 page slots of s1/s2
            # are used on Page 0. The next var id is 60, but the next page
            # slot is 8.
            for i in range(52):
                Var(i)

            # Wide needs 8 contiguous slots, and 8 + 8 = 16 <= 64, so it
            # fits on Page 0 without padding and must not split across
            # pages. It packs at pinned ranks 8..15 (page 0, slots 8..15),
            # with ids 60..67.
            w = Wide()
            self.assertEqual(w.pageIndex, 0)
            self.assertEqual(w.slotIndex, 8)
            self.assertEqual(w.w0.id, 60)
            self.assertEqual(w.w7.id, 67)

        buildModule(prog)

    def test_hardware_bitmasks_cross_mask_transfers_and_dirty_dwords(self) -> None:
        Hero = TriggerStruct(
            "Hero",
            hp=dword(),
            mp=word(),
            armor=byte(),
            flags=bit(4),
        )

        def prog() -> None:
            s1 = Hero(hp=1000, mp=0x1234, armor=0xAB, flags=0x0D)
            s2 = Hero(hp=0, mp=0, armor=0, flags=0)
            raw = Var(0x12345678)
            out_armor = Var(0xDEAD0000)

            # 1) Whole-struct copy s2.set(s1)
            s2.set(s1)

            # 2) Cross-mask transfer: s2.armor.set(raw) -> masks 0x12345678 to 0x78
            s2.armor.set(raw)

            # 3) Read sub-dword field into dirty dword: out_armor.set(s2.armor) -> produces 0x78
            out_armor.set(s2.armor)

        mod = buildModule(prog)
        report = run_sim(mod.to_json())
        self.assertTrue(report["self_targeted_invariant_holds"])
        self.assertEqual(report["var_values"][4], 1000)
        self.assertEqual(report["var_values"][5], 0x1234)
        self.assertEqual(report["var_values"][6], 0x78)
        self.assertEqual(report["var_values"][7], 0x0D)
        self.assertEqual(report["var_values"][9], 0x78)

    def test_1t_whole_struct_copy_fires_source_trigger_page_once_for_64_fields(self) -> None:
        spec = {f"f{i}": "dword" for i in range(64)}
        FullStruct = TriggerStruct("FullStruct", spec)

        def prog() -> None:
            inits = {f"f{i}": 500 + i for i in range(64)}
            src = FullStruct(inits)
            dst = FullStruct()

            # Whole-struct copy
            dst.copyFrom(src)

        mod = buildModule(prog)
        report = run_sim(mod.to_json())
        self.assertTrue(report["self_targeted_invariant_holds"])

        page0_item = report["static_cost"]["triggers"] - 2
        page0_visits = sum(1 for i in report["executed_cost"]["visited_items"] if i == page0_item)
        self.assertEqual(page0_visits, 1)

        for i in range(64):
            self.assertEqual(report["var_values"][64 + i], 500 + i)

    def test_byte_identical_hir_parity_with_typescript(self) -> None:
        Hero = TriggerStruct(
            "Hero",
            hp=dword(),
            mp=word(),
            armor=byte(),
            flags=bit(4),
        )

        def prog() -> None:
            s1 = Hero(hp=1000, mp=0x1234, armor=0xAB, flags=0x0D)
            s2 = Hero()
            out_armor = Var(0)

            s2.set(s1)
            s2.armor += 5
            out_armor.set(s2.armor)

            Trigger(
                conds=[s2.hp >= 500, s2.mp == 0x1234, s2.armor == 0xB0],
                acts=[DisplayText("Ticket 4 Parity!")],
            )

        mod = buildModule(prog)
        py_hir_json = mod.to_json()

        ts_script = """
import { DisplayText, Trigger, TriggerStruct, Var, bit, byte, dword, word, buildModule } from "./ts/index.ts";

const Hero = TriggerStruct("Hero", {
  hp: dword(),
  mp: word(),
  armor: byte(),
  flags: bit(4),
});

const tsMod = buildModule(() => {
  const s1 = Hero({ hp: 1000, mp: 0x1234, armor: 0xab, flags: 0x0d });
  const s2 = Hero();
  const outArmor = Var(0);

  s2.set(s1);
  s2.armor.iadd(5);
  outArmor.set(s2.armor);

  new Trigger({
    conds: [s2.hp.ge(500), s2.mp.eq(0x1234), s2.armor.eq(0xb0)],
    acts: [DisplayText("Ticket 4 Parity!")],
  });
});

process.stdout.write(tsMod.toJson());
"""
        res = subprocess.run(
            ["node", "--input-type=module", "-e", ts_script],
            cwd=str(REPO_ROOT),
            check=True,
            capture_output=True,
            text=True,
        )
        ts_hir_json = res.stdout

        self.assertEqual(py_hir_json, ts_hir_json)

        report = run_sim(py_hir_json)
        self.assertTrue(report["self_targeted_invariant_holds"])
        self.assertEqual(report["var_values"][4], 1000)
        self.assertEqual(report["var_values"][5], 0x1234)
        self.assertEqual(report["var_values"][6], 0xB0)
        self.assertEqual(report["var_values"][7], 0x0D)
        self.assertEqual(report["var_values"][8], 0xB0)

if __name__ == "__main__":
    unittest.main()
