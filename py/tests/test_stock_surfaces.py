"""Generated stock/extension surfaces: Python exports and docs tables.

The stock row spec drives three non-emitter surfaces. The LSP vocabulary is
covered by `crates/eudkit-lsp/tests/stock_spec.rs`; this module pins the
Python root export manifest and the generated docs regions.
"""

from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path
from typing import Any

import py as eudsl
from py import stock_rows_generated

REPO_ROOT = Path(__file__).resolve().parents[2]
SPEC_PATH = REPO_ROOT / "schema" / "stock-rows.json"
GENERATOR = REPO_ROOT / "scripts" / "generate_stock_surfaces.py"


def _spec_ids() -> list[str]:
    spec: dict[str, Any] = json.loads(SPEC_PATH.read_text())
    return [
        row["id"]
        for section in ("conditions", "actions", "extensions")
        for row in spec["sections"][section]
    ]


class TestPythonExportManifest(unittest.TestCase):
    def test_manifest_lists_every_stock_and_extension_helper_in_spec_order(self) -> None:
        self.assertEqual(list(stock_rows_generated.STOCK_EXPORTS), _spec_ids())
        self.assertEqual(len(stock_rows_generated.STOCK_EXPORTS), 92)

    def test_root_exports_follow_the_manifest(self) -> None:
        for name in stock_rows_generated.STOCK_EXPORTS:
            self.assertIn(name, eudsl.__all__, name)
            self.assertTrue(callable(getattr(eudsl, name)), name)
        self.assertEqual(
            list(eudsl.__all__[-len(stock_rows_generated.STOCK_EXPORTS) :]),
            list(stock_rows_generated.STOCK_EXPORTS),
            "the generated manifest is appended to the hand-written exports",
        )

    def test_hand_written_special_rows_stay_outside_the_manifest(self) -> None:
        for name in ("UnitProperty", "buildModule", "when", "Var"):
            self.assertNotIn(name, stock_rows_generated.STOCK_EXPORTS, name)
            self.assertIn(name, eudsl.__all__, name)


class TestGeneratedSurfacesAreInSync(unittest.TestCase):
    def test_generator_check_passes(self) -> None:
        result = subprocess.run(
            [sys.executable, str(GENERATOR), "--check"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
        )
        self.assertEqual(
            result.returncode,
            0,
            "generated surfaces drifted from the stock row spec:\n"
            + result.stdout
            + result.stderr,
        )


if __name__ == "__main__":
    unittest.main()
