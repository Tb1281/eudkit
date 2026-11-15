"""The Phase 3 contract: one stock row spec, five hand-written special rows.

This module pins the end state of the stock row spec work:

- the retired test-only catalog and its runners stay gone;
- only the five special rows keep hand-written bodies in the Python frontend;
- adding a stock row needs a spec edit plus a regenerate, nothing else.

The docs/LSP/export surfaces have their own spec-driven tests
(`py/tests/test_stock_surfaces.py`, `crates/eudkit-lsp/tests/stock_spec.rs`).
"""

from __future__ import annotations

import copy
import importlib.util
import json
import re
import tempfile
import unittest
from pathlib import Path
from types import ModuleType
from typing import Any

from py import stock_rows_built

REPO_ROOT = Path(__file__).resolve().parents[2]
SPEC_PATH = REPO_ROOT / "schema" / "stock-rows.json"
DOCS_PATH = REPO_ROOT / "docs" / "stock-triggers.md"
GENERATOR_PATH = REPO_ROOT / "scripts" / "generate_stock_surfaces.py"
PY_ROOT = REPO_ROOT / "py"

SPECIAL_ROWS = [
    "CreateUnitWithProperties",
    "Deaths",
    "SetDeaths",
    "SetStringDword",
    "UnitProperty",
]

RETIRED_PATHS = [
    REPO_ROOT / "tests" / "stock-trigger-catalog.json",
    REPO_ROOT / "py" / "tests" / "stock_catalog_runner.py",
    REPO_ROOT / "py" / "tests" / "test_stock_catalog.py",
    REPO_ROOT / "ts" / "tests" / "test_stock_catalog.ts",
]

SYNTHETIC_ROW: dict[str, Any] = {
    "id": "SyntheticProbe",
    "kind": "condition",
    "chkType": 99,
    "class": "stock",
    "defaultFlags": 0,
    "special": False,
    "handWritten": False,
    "doc": "Synthetic probe condition (condition 99).",
    "params": [
        {
            "name": "comparison",
            "field": "comparison",
            "chkField": "comparison",
            "encoding": "u8",
            "validator": {
                "kind": "enum",
                "label": "comparison",
                "values": [0, 1, 10],
            },
            "ts": "comparison",
        }
    ],
}


def _load_spec() -> dict[str, Any]:
    return json.loads(SPEC_PATH.read_text(encoding="utf-8"))


def _load_generator() -> ModuleType:
    module_spec = importlib.util.spec_from_file_location(
        "generate_stock_surfaces", GENERATOR_PATH
    )
    assert module_spec is not None and module_spec.loader is not None
    module = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(module)
    return module


def _helper_rows(spec: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        row
        for section in ("conditions", "actions", "extensions", "records")
        for row in spec["sections"][section]
    ]


def _python_sources(root: Path = PY_ROOT) -> list[Path]:
    excluded_parts = {"tests", "build", "__pycache__"}

    def is_source(path: Path) -> bool:
        parts = path.relative_to(root).parts
        return not any(
            part in excluded_parts or part.endswith(".egg-info") for part in parts
        )

    return sorted(path for path in root.rglob("*.py") if is_source(path))


def _declarations(name: str) -> list[Path]:
    pattern = re.compile(rf"^\s*def {re.escape(name)}\s*\(", re.MULTILINE)
    return [
        path
        for path in _python_sources()
        if pattern.search(path.read_text(encoding="utf-8")) is not None
    ]


class TestStockContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.spec = _load_spec()
        cls.rows = _helper_rows(cls.spec)

    def test_the_test_only_catalog_and_its_runners_are_retired(self) -> None:
        for path in RETIRED_PATHS:
            self.assertFalse(
                path.exists(),
                f"{path.relative_to(REPO_ROOT)} is superseded by "
                "schema/stock-rows.json and tests/stock-row-goldens.json",
            )

    def test_python_sources_ignore_build_and_generated_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            kept = [root / "module.py", root / "pkg" / "nested.py"]
            ignored = [
                root / "build" / "lib" / "built.py",
                root / "py.egg-info" / "metadata.py",
                root / "__pycache__" / "cached.py",
                root / "tests" / "test_module.py",
            ]
            for path in kept + ignored:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("value = 1\n", encoding="utf-8")

            self.assertEqual(_python_sources(root), sorted(kept))

    def test_only_the_five_special_rows_are_hand_written_in_python_source(self) -> None:
        special = sorted(row["id"] for row in self.rows if row["special"])
        hand_written = sorted(row["id"] for row in self.rows if row["handWritten"])
        self.assertEqual(special, SPECIAL_ROWS)
        self.assertEqual(hand_written, special)

        for row in self.rows:
            declarations = _declarations(row["id"])
            if row["special"]:
                self.assertEqual(
                    declarations,
                    [PY_ROOT / "hir.py"],
                    f"{row['id']} is a special row and keeps its hand-written body",
                )
            else:
                self.assertEqual(
                    declarations,
                    [],
                    f"{row['id']} is data-driven; its body must not stay "
                    "hand-written in py/",
                )

    def test_adding_a_stock_row_needs_only_the_spec_and_a_regenerate(self) -> None:
        spec = copy.deepcopy(self.spec)
        spec["sections"]["conditions"].append(copy.deepcopy(SYNTHETIC_ROW))
        spec["counts"]["conditions"] += 1

        helpers = stock_rows_built.build_helpers(spec)
        helper = helpers["SyntheticProbe"]
        self.assertEqual(helper.__name__, "SyntheticProbe")
        self.assertEqual(helper.__doc__, SYNTHETIC_ROW["doc"])
        self.assertEqual(
            [param.name for param in helper.__signature__.parameters.values()],
            ["comparison"],
        )
        emitted = helper(1)
        self.assertEqual(emitted["condtype"], 99)
        self.assertEqual(emitted["comparison"], 1)

        generator = _load_generator()
        docs = generator.render_docs(spec, DOCS_PATH.read_text(encoding="utf-8"))
        self.assertIn(
            "| 99 | `SyntheticProbe` | `comparison` | 0 | stock |",
            docs,
            "the docs reference table must regenerate from the spec alone",
        )
        manifest = generator.render_manifest(spec)
        self.assertIn('    "SyntheticProbe",', manifest)
        self.assertIn("    SyntheticProbe,", manifest)


if __name__ == "__main__":
    unittest.main()
