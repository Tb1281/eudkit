"""Python stock helpers are built from the stock row spec at import time.

`schema/stock-rows.json` is the single source of truth for the non-special
stock and eudkit-extension helper rows. Unlike TypeScript, which checks in a
generated module (ADR 0009), Python constructs the functions at import. The
constructed surface must keep the documented name, docstring, and ordered
signature so `inspect.signature` still reports the spec parameters.
"""

from __future__ import annotations

import inspect
import json
import unittest
from pathlib import Path
from typing import Any

import py as eudsl

REPO_ROOT = Path(__file__).resolve().parents[2]
SPEC_PATH = REPO_ROOT / "schema" / "stock-rows.json"


def _spec() -> dict[str, Any]:
    return json.loads(SPEC_PATH.read_text())


def _helper_rows(spec: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        row
        for section in ("conditions", "actions", "extensions", "records")
        for row in spec["sections"][section]
    ]


class TestBuiltHelperSurface(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.spec = _spec()
        cls.rows = _helper_rows(cls.spec)

    def test_every_non_special_helper_is_named_documented_and_signed_from_the_spec(
        self,
    ) -> None:
        for row in self.rows:
            if row["handWritten"]:
                continue
            helper = getattr(eudsl, row["id"])
            self.assertEqual(helper.__name__, row["id"], row["id"])
            self.assertEqual(helper.__doc__, row["doc"], row["id"])

            signature = getattr(helper, "__signature__", None)
            self.assertIsInstance(
                signature,
                inspect.Signature,
                f"{row['id']} must expose a spec-built __signature__",
            )
            reported = inspect.signature(helper)
            self.assertEqual(
                [param.name for param in reported.parameters.values()],
                [param["name"] for param in row["params"]],
                f"{row['id']} ordered parameters",
            )
            for parameter, param in zip(reported.parameters.values(), row["params"]):
                expected = (
                    param["default"]
                    if "default" in param
                    else inspect.Parameter.empty
                )
                self.assertEqual(
                    parameter.default,
                    expected,
                    f"{row['id']}.{param['name']} default",
                )

    def test_special_rows_stay_hand_written(self) -> None:
        special = [row for row in self.rows if row["handWritten"]]
        self.assertEqual(
            sorted(row["id"] for row in special),
            [
                "CreateUnitWithProperties",
                "Deaths",
                "SetDeaths",
                "SetStringDword",
                "UnitProperty",
            ],
        )
        for row in special:
            # The special rows keep their explicit hand-written bodies, so
            # their documented behaviour (and docstrings) is not rebuilt.
            self.assertTrue(callable(getattr(eudsl, row["id"])), row["id"])

    def test_late_validation_failure_queues_nothing(self) -> None:
        def body() -> None:
            try:
                eudsl.SetCountdownTimer(eudsl.Add, -1)
            except eudsl.EudslError as error:
                self.assertEqual(error.code, "EUDSL-BOUNDS")
            eudsl.Victory()

        module = eudsl.buildModule(body)
        self.assertEqual(len(module.triggers), 1)
        self.assertEqual(len(module.triggers[0].acts), 1)
        self.assertEqual(module.triggers[0].acts[0]["acttype"], 1)

    def test_invalid_late_field_queues_no_partial_action(self) -> None:
        def body() -> None:
            try:
                eudsl.KillUnitAt(1, 1, 1, 999)
            except eudsl.EudslError as error:
                self.assertEqual(error.code, "EUDSL-BOUNDS")

        module = eudsl.buildModule(body)
        self.assertEqual(len(module.triggers), 0)


if __name__ == "__main__":
    unittest.main()
