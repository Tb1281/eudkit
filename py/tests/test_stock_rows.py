"""Stock row spec and frozen golden checks for the Python frontend."""

from __future__ import annotations

import inspect
import json
import re
import unittest
from pathlib import Path
from typing import Any

import py as eudsl

REPO_ROOT = Path(__file__).resolve().parents[2]
SPEC_PATH = REPO_ROOT / "schema" / "stock-rows.json"
GOLDENS_PATH = REPO_ROOT / "tests" / "stock-row-goldens.json"
DOCS_PATH = REPO_ROOT / "docs" / "stock-triggers.md"

_REFERENCE_ROW = re.compile(
    r"^\|\s*(\d+)\s*\|\s*`([^`]+)`\s*\|\s*`([^`]*)`\s*\|\s*(\d+)\s*\|\s*([a-z ]+?)\s*\|$"
)
_STRING_ROW = re.compile(
    r"^\|\s*`([^`]+)`\s*\|\s*`([^`]+)`\s*\|\s*`([^`]+)`\s*\|\s*`([^`]+)`\s*\|$"
)


def _load(path: Path) -> Any:
    return json.loads(path.read_text())


def _split_params(raw: str) -> list[str]:
    return [] if raw == "—" else [part.strip() for part in raw.split(",")]


def _section_lines(heading: str) -> list[str]:
    lines = DOCS_PATH.read_text().splitlines()
    start = lines.index(f"## {heading}")
    out: list[str] = []
    for line in lines[start + 1 :]:
        if line.startswith("## "):
            break
        out.append(line)
    return out


def _reference_rows(heading: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line in _section_lines(heading):
        match = _REFERENCE_ROW.match(line)
        if match is not None:
            rows.append(
                {
                    "id": int(match.group(1)),
                    "name": match.group(2),
                    "params": _split_params(match.group(3)),
                    "flags": int(match.group(4)),
                    "klass": match.group(5),
                }
            )
    return rows


def _string_slot_rows() -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for line in _section_lines("String parameter slots"):
        match = _STRING_ROW.match(line)
        if match is not None:
            rows.append(
                {
                    "helper": match.group(1),
                    "param": match.group(2),
                    "hir": match.group(3),
                    "chk": match.group(4),
                }
            )
    return rows


def _extension_helper_rows() -> list[str]:
    pattern = re.compile(r"^\|\s*`([^`]+)`\s*\|.*\|\s*eudkit extension\s*\|$")
    rows: list[str] = []
    for line in _section_lines("Extension helpers"):
        match = pattern.match(line)
        if match is not None:
            rows.append(match.group(1))
    return rows


def _target_form_rows() -> list[tuple[str, list[str]]]:
    pattern = re.compile(r"^\|\s*`([^`]+)`\s*\|\s*`([^`]*)`\s*\|\s*eudkit extension\s*\|$")
    rows: list[tuple[str, list[str]]] = []
    for line in _section_lines("Extension target forms"):
        match = pattern.match(line)
        if match is not None:
            rows.append((match.group(1), _split_params(match.group(2))))
    return rows


def _dump(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)


def _error_detail(error: eudsl.EudslError) -> dict[str, str]:
    return {
        "code": error.code,
        "message": str(error)[len(error.code) + 2 :],
    }


class TestStockRows(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.spec = _load(SPEC_PATH)
        cls.goldens = _load(GOLDENS_PATH)
        cls.docs = DOCS_PATH.read_text()
        cls.rows = [
            row
            for section in ("conditions", "actions", "extensions", "records")
            for row in cls.spec["sections"][section]
        ]
        cls.by_id = {row["id"]: row for row in cls.rows}
        cls.record_row = cls.by_id["UnitProperty"]

    def _build_unit_property(self, options: dict[str, Any]) -> list[int]:
        kwargs = {
            param["name"]: options[param["name"]]
            for param in self.record_row["params"]
            if param["name"] in options
        }
        return eudsl.UnitProperty(**kwargs)

    def _build_arg(self, arg: dict[str, Any]) -> Any:
        kind = arg["kind"]
        if kind in ("int", "str", "bool"):
            return arg["value"]
        if kind == "uprp":
            return self._build_unit_property(arg["options"])
        if kind == "fieldref":
            return eudsl.TriggerFieldRef(
                -1, arg["byteOffset"], arg["size"], 0, arg["varId"]
            )
        if kind == "raw":
            return arg["value"]
        raise AssertionError(f"unknown golden argument kind {kind!r}")

    def _build_args(self, args: list[dict[str, Any]]) -> list[Any]:
        return [self._build_arg(arg) for arg in args]

    def _emit(self, row: dict[str, Any], args: list[dict[str, Any]]) -> Any:
        return getattr(eudsl, row["id"])(*self._build_args(args))

    def _assert_diagnostic(
        self,
        row: dict[str, Any],
        args: list[dict[str, Any]],
        diagnostic: dict[str, str],
    ) -> None:
        try:
            self._emit(row, args)
        except eudsl.EudslError as error:
            self.assertEqual(
                _error_detail(error),
                {"code": diagnostic["code"], "message": diagnostic["message"]},
            )
            return
        self.fail(f"{row['id']} must reject {args}")

    def test_spec_declares_all_stock_and_extension_rows_in_order(self) -> None:
        self.assertEqual(self.spec["format"], 1)
        self.assertEqual(self.spec["hirVersion"], "3.2")
        self.assertEqual(len(self.spec["sections"]["conditions"]), 22)
        self.assertEqual(len(self.spec["sections"]["actions"]), 57)
        self.assertEqual(len(self.spec["sections"]["extensions"]), 13)
        self.assertEqual(len(self.spec["sections"]["records"]), 1)
        self.assertEqual(self.spec["counts"]["conditions"], 22)
        self.assertEqual(self.spec["counts"]["actions"], 57)
        self.assertEqual(self.spec["counts"]["extensions"], 13)
        self.assertEqual(self.spec["counts"]["records"], 1)

        stock = [
            *self.spec["sections"]["conditions"],
            *self.spec["sections"]["actions"],
        ]
        self.assertEqual(len(stock), 79)
        self.assertTrue(all(row["class"] == "stock" for row in stock))
        self.assertTrue(
            all(
                row["class"] == "eudkit extension"
                for row in self.spec["sections"]["extensions"]
            )
        )

        self.assertEqual(
            [row["chkType"] for row in self.spec["sections"]["conditions"]],
            [position for position in range(1, 24) if position != 13],
        )
        self.assertEqual(
            [row["chkType"] for row in self.spec["sections"]["actions"]],
            list(range(1, 58)),
        )
        self.assertEqual(
            [row["id"] for row in self.spec["sections"]["extensions"]],
            _extension_helper_rows(),
        )

    def test_reference_tables_match_the_spec(self) -> None:
        for heading, section in (
            ("Conditions (22)", "conditions"),
            ("Actions (57)", "actions"),
        ):
            doc_rows = _reference_rows(heading)
            spec_rows = self.spec["sections"][section]
            self.assertEqual(
                [row["id"] for row in doc_rows],
                [row["chkType"] for row in spec_rows],
                heading,
            )
            for doc, row in zip(doc_rows, spec_rows):
                self.assertEqual(doc["name"], row["id"], row["id"])
                self.assertEqual(
                    doc["params"],
                    [param["name"] for param in row["params"]],
                    row["id"],
                )
                self.assertEqual(doc["flags"], row["defaultFlags"], row["id"])
                self.assertEqual(doc["klass"], row["class"], row["id"])

    def test_string_slot_table_matches_the_spec(self) -> None:
        expected = [
            {
                "helper": row["id"],
                "param": param["name"],
                "hir": param["field"],
                "chk": param["chkField"],
            }
            for section in ("conditions", "actions", "extensions")
            for row in self.spec["sections"][section]
            for param in row["params"]
            if param["encoding"] == "string"
        ]
        key = lambda row: (row["helper"], row["param"])  # noqa: E731
        self.assertEqual(
            sorted(_string_slot_rows(), key=key),
            sorted(expected, key=key),
            "the generated string-slot table must derive from the spec",
        )

    def test_spec_rows_match_python_helper_signatures(self) -> None:
        for row in self.rows:
            helper = getattr(eudsl, row["id"])
            parameters = list(inspect.signature(helper).parameters.values())
            if row["id"] == "UnitProperty":
                self.assertEqual(
                    [param.name for param in parameters],
                    [param["name"] for param in row["params"]],
                )
                for param in parameters:
                    self.assertIsNone(
                        param.default,
                        f"UnitProperty.{param.name} defaults to None",
                    )
                continue
            self.assertEqual(
                [param.name for param in parameters],
                [param["name"] for param in row["params"]],
                f"{row['id']} Python parameter order",
            )
            for parameter, param in zip(parameters, row["params"]):
                if "default" in param:
                    self.assertEqual(
                        parameter.default,
                        param["default"],
                        f"{row['id']}.{parameter.name} default",
                    )
                else:
                    self.assertIs(
                        parameter.default,
                        inspect.Parameter.empty,
                        f"{row['id']}.{parameter.name} has no default",
                    )

    def test_only_five_special_hand_written_rows(self) -> None:
        special = sorted(row["id"] for row in self.rows if row["special"])
        hand_written = sorted(row["id"] for row in self.rows if row["handWritten"])
        self.assertEqual(
            special,
            [
                "CreateUnitWithProperties",
                "Deaths",
                "SetDeaths",
                "SetStringDword",
                "UnitProperty",
            ],
        )
        self.assertEqual(hand_written, special)
        self.assertLessEqual(len(special), 5)
        for row in self.rows:
            if row["special"]:
                self.assertTrue(row.get("specialReason"), row["id"])
            else:
                self.assertNotIn("specialReason", row, row["id"])

    def test_only_deaths_and_setdeaths_carry_target_forms(self) -> None:
        target_rows = [
            row["id"]
            for row in self.rows
            if any("targetForm" in param for param in row["params"])
        ]
        self.assertEqual(target_rows, ["Deaths", "SetDeaths"])
        self.assertEqual(
            [
                (param["name"], param["targetForm"])
                for param in self.by_id["Deaths"]["params"]
                if "targetForm" in param
            ],
            [("player", "PlayerOrFieldTarget"), ("amount", "ValueOrPtrTarget")],
        )
        self.assertEqual(
            [
                (param["name"], param["targetForm"])
                for param in self.by_id["SetDeaths"]["params"]
                if "targetForm" in param
            ],
            [("player", "PlayerOrFieldTarget"), ("value", "ValueOrPtrTarget")],
        )
        self.assertEqual(
            _target_form_rows(),
            [
                ("Deaths", ["player", "amount"]),
                ("SetDeaths", ["player", "value"]),
            ],
        )

    def test_goldens_cover_the_92_rows_in_order_and_keep_the_frozen_rows(self) -> None:
        self.assertEqual(self.goldens["format"], 1)
        # The oracle is frozen at its capture-time version: a row carries no
        # HIR version field, so the 3.1 -> 3.2 bump (ADR 0021) leaves the
        # payloads valid and the file untouched.
        self.assertEqual(self.goldens["hirVersion"], "3.1")
        self.assertEqual(eudsl.HIR_VERSION, "3.2")
        self.assertEqual(len(self.goldens["rows"]), 92)
        self.assertEqual(
            [row["id"] for row in self.goldens["rows"]],
            [
                *[row["id"] for row in self.spec["sections"]["conditions"]],
                *[row["id"] for row in self.spec["sections"]["actions"]],
                *[row["id"] for row in self.spec["sections"]["extensions"]],
            ],
        )
        for golden in self.goldens["rows"]:
            row = self.by_id[golden["id"]]
            if golden["id"] in {
                candidate["id"] for candidate in self.spec["sections"]["extensions"]
            }:
                expected_section = "extensions"
            elif row["kind"] == "condition":
                expected_section = "conditions"
            else:
                expected_section = "actions"
            self.assertEqual(golden["section"], expected_section, golden["id"])
            self.assertEqual(golden["kind"], row["kind"], golden["id"])
            self.assertEqual(golden["class"], row["class"], golden["id"])

    def test_every_golden_row_reproduces_its_frozen_hir(self) -> None:
        for golden in self.goldens["rows"]:
            row = self.by_id[golden["id"]]
            emitted = self._emit(row, golden["args"])
            self.assertEqual(
                _dump(emitted),
                _dump(golden["hir"]),
                f"{golden['id']} HIR drifted from the frozen golden",
            )

    def test_every_golden_diagnostic_reproduces_code_message_and_order(self) -> None:
        for golden in self.goldens["rows"]:
            row = self.by_id[golden["id"]]
            for diagnostic in golden["diagnostics"]:
                self._assert_diagnostic(row, diagnostic["args"], diagnostic)

    def test_unit_property_record_golden_and_diagnostics_are_frozen(self) -> None:
        self.assertEqual(self.goldens["record"]["id"], "UnitProperty")
        options = self.goldens["record"]["args"][0]
        self.assertEqual(options["kind"], "uprp")
        self.assertEqual(
            self._build_unit_property(options["options"]),
            self.goldens["record"]["record"],
            "UnitProperty record drifted from the frozen golden",
        )
        for diagnostic in self.goldens["record"]["diagnostics"]:
            merged = {**options["options"], **diagnostic["options"]}
            try:
                self._build_unit_property(merged)
            except eudsl.EudslError as error:
                self.assertEqual(
                    _error_detail(error),
                    {
                        "code": diagnostic["code"],
                        "message": diagnostic["message"],
                    },
                )
            else:
                self.fail(
                    "UnitProperty must reject " + json.dumps(diagnostic["options"])
                )

    def test_unit_property_boolean_flags_accept_true_false_and_none(self) -> None:
        for param in self.record_row["params"]:
            if param["validator"]["kind"] != "boolean":
                continue
            for value in (True, False, None):
                eudsl.UnitProperty(**{param["name"]: value})
            with self.assertRaises(eudsl.EudslError):
                eudsl.UnitProperty(**{param["name"]: 1})

    def test_spec_validators_accept_and_reject_their_documented_domains(self) -> None:
        for golden in self.goldens["rows"]:
            row = self.by_id[golden["id"]]
            for index, param in enumerate(row["params"]):
                validator = param["validator"]
                if validator["kind"] == "none":
                    continue
                base = self._build_args(golden["args"])
                helper = getattr(eudsl, row["id"])

                def accepts(value: Any) -> None:
                    args = list(base)
                    args[index] = value
                    helper(*args)

                def rejects(value: Any, code: str) -> None:
                    args = list(base)
                    args[index] = value
                    try:
                        helper(*args)
                    except eudsl.EudslError as error:
                        detail = _error_detail(error)
                        label = validator["label"]
                        self.assertEqual(detail["code"], code, f"{row['id']}.{param['name']}")
                        self.assertIn(
                            f"{label} must be",
                            detail["message"],
                            f"{row['id']}.{param['name']} must use label {label}, "
                            f"got {detail['message']}",
                        )
                        return
                    self.fail(
                        f"{row['id']}.{param['name']} accepted invalid {value!r}"
                    )

                kind = validator["kind"]
                if kind == "enum":
                    for value in validator["values"]:
                        accepts(value)
                    rejects(max(validator["values"]) + 1000, "EUDSL-BOUNDS")
                elif kind == "range":
                    accepts(validator["min"])
                    if "max" in validator:
                        accepts(validator["max"])
                    if validator["min"] > 0:
                        invalid = validator["min"] - 1
                    elif "max" not in validator:
                        invalid = -1
                    else:
                        invalid = validator["max"] + 1
                    rejects(invalid, "EUDSL-BOUNDS")
                elif kind == "count":
                    for value in (0, 1, 255):
                        accepts(value)
                    rejects(256, "EUDSL-BOUNDS")
                elif kind == "multiple":
                    accepts(validator["multipleOf"])
                    accepts(validator["multipleOf"] * 2)
                    rejects(validator["multipleOf"] + 1, "EUDSL-BOUNDS")
                elif kind == "record":
                    rejects([0] * 19, "EUDSL-TYPE")
                else:
                    self.fail(f"{row['id']}.{param['name']} has unknown validator {kind}")

    def test_deaths_and_setdeaths_extension_targets_emit_relocations(self) -> None:
        holder: list[Any] = []

        def prog() -> None:
            source = eudsl.Trigger(conds=[], acts=[], preserved=False)
            holder.append(source)
            eudsl.Trigger(
                conds=[
                    eudsl.Deaths(
                        source.act(0).value,
                        eudsl.AtLeast,
                        source.act(1).dest,
                        0,
                    )
                ],
                acts=[
                    eudsl.SetDeaths(
                        source.act(2).dest,
                        eudsl.SetTo,
                        source.act(3).value,
                        0,
                    )
                ],
                preserved=True,
            )

        mod = eudsl.buildModule(prog)
        source = holder[0]
        target = next(trigger for trigger in mod.triggers if trigger.id != source.id)
        self.assertEqual(
            [
                (reloc["field_offset"], reloc["target_trigger"], reloc["mode"])
                for reloc in target.conds[0]["relocs"]
            ],
            [(4, source.id, 1), (8, source.id, 4)],
            "Deaths relocates its player and amount extension targets",
        )
        self.assertEqual(
            [
                (reloc["field_offset"], reloc["target_trigger"], reloc["mode"])
                for reloc in target.acts[0]["relocs"]
            ],
            [(16, source.id, 1), (20, source.id, 4)],
            "SetDeaths relocates its player and value extension targets",
        )


if __name__ == "__main__":
    unittest.main()
