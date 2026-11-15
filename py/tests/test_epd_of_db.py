"""`epd_of_db`: materialize a payload blob's runtime EPD into a `Var`.

A `Db` payload address (and therefore its EPD) is only known at assembly
time, so the helper emits the same shape `py/blockio.py:_seed_epd_cursor`
already uses privately: one zero-initialized `Var`, one preserved
zero-condition trigger, and one mode-1 relocation into the fresh `Var`'s
value field (ADR 0029).
"""

from __future__ import annotations

import json
import unittest

from py import (
    Db,
    DbRef,
    EudslError,
    Var,
    buildModule,
    epdOfDb,
    epd_of_db,
)

ACT_DEST_OFFSET = 16
ACT_VALUE_OFFSET = 20


def _module(body) -> dict:
    return json.loads(buildModule(body).to_json())["module"]


def _triggers(mod: dict) -> list[dict]:
    return [stmt for stmt in mod["stmts"] if stmt["kind"] == "trigger"]


class TestEpdOfDbEmission(unittest.TestCase):
    def test_db_emits_one_preserved_zero_condition_trigger(self):
        mod = _module(lambda: epd_of_db(Db(8)))

        self.assertEqual(mod["vars"], [{"id": 0, "init": 0}])
        triggers = _triggers(mod)
        self.assertEqual(len(triggers), 1)

        trig = triggers[0]
        self.assertTrue(trig["preserved"])
        self.assertEqual(trig["conds"], [])
        self.assertEqual(len(trig["acts"]), 1)

    def test_the_value_field_carries_a_mode_1_reloc_to_the_blob(self):
        mod = _module(lambda: epd_of_db(Db(8)))

        act = _triggers(mod)[0]["acts"][0]
        self.assertEqual(
            act["relocs"],
            [
                {
                    "field_offset": ACT_DEST_OFFSET,
                    "target_var": 0,
                    "target_offset": ACT_VALUE_OFFSET,
                    "mode": 1,
                },
                {
                    "field_offset": ACT_VALUE_OFFSET,
                    "target_blob": 0,
                    "target_offset": 0,
                    "mode": 1,
                },
            ],
        )

    def test_dbref_offset_is_carried_through_unchanged(self):
        mod = _module(lambda: epd_of_db(Db(b"\x01\x02\x03\x04\x05\x06\x07\x08") + 4))

        reloc = _triggers(mod)[0]["acts"][0]["relocs"][1]
        self.assertEqual(reloc["target_blob"], 0)
        self.assertEqual(reloc["target_offset"], 4)
        self.assertEqual(reloc["mode"], 1)

    def test_returns_a_var(self):
        holder: dict[str, Var] = {}

        def body() -> None:
            holder["var"] = epd_of_db(Db(8))

        buildModule(body)
        self.assertIsInstance(holder["var"], Var)
        self.assertEqual(holder["var"].id, 0)


class TestEpdOfDbDiagnostics(unittest.TestCase):
    def test_non_db_operands_are_eudsl_type(self):
        for bad in ("x", 0, None, 1.5):
            with self.subTest(bad=bad):
                with self.assertRaisesRegex(
                    EudslError, r"EUDSL-TYPE: epd_of_db\(\): expected Db or DbRef"
                ):
                    epd_of_db(bad)  # type: ignore[arg-type]

    def test_outside_build_module_is_eudsl_scope(self):
        with self.assertRaisesRegex(EudslError, "EUDSL-SCOPE"):
            epd_of_db(DbRef(0))

    def test_camel_case_alias_is_the_same_helper(self):
        self.assertIs(epdOfDb, epd_of_db)
