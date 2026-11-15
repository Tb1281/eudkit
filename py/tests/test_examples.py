"""Verify side-by-side migration examples (`examples/eudplib`, `examples/py`, `examples/ts`)."""

from __future__ import annotations

import ast
import subprocess
import tempfile
import unittest
from pathlib import Path

from py.tests.helpers import EUDKIT, REPO_ROOT, run_sim

EXAMPLES_DIR = REPO_ROOT / "examples"
BASEMAP_SCX = REPO_ROOT / "tests" / "outputmap" / "basemap" / "basemap_silent.scx"
EUDPLIB_SRC = REPO_ROOT.parent / "eudplib" / "src" / "eudplib"


def _example_slugs() -> list[str]:
    return sorted(p.stem for p in (EXAMPLES_DIR / "py").glob("*.py"))


class TestMigrationExamples(unittest.TestCase):
    def test_triplet_inventory_matches(self) -> None:
        py_slugs = sorted(p.stem for p in (EXAMPLES_DIR / "py").glob("*.py"))
        ts_slugs = sorted(p.stem for p in (EXAMPLES_DIR / "ts").glob("*.ts"))
        eudplib_slugs = sorted(p.stem for p in (EXAMPLES_DIR / "eudplib").glob("*.py"))
        self.assertGreaterEqual(len(py_slugs), 8)
        self.assertEqual(py_slugs, ts_slugs)
        self.assertEqual(py_slugs, eudplib_slugs)

    def test_eudplib_baselines_syntax_and_imports(self) -> None:
        for path in sorted((EXAMPLES_DIR / "eudplib").glob("*.py")):
            src = path.read_text(encoding="utf-8")
            tree = ast.parse(src, filename=str(path))
            compile(tree, str(path), "exec")
            if not EUDPLIB_SRC.exists():
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("eudplib."):
                    rel = Path(*node.module.split(".")[1:])
                    mod_file = EUDPLIB_SRC / f"{rel}.py"
                    pkg_init = EUDPLIB_SRC / rel / "__init__.py"
                    self.assertTrue(
                        mod_file.exists() or pkg_init.exists(),
                        f"{path.name}: unknown eudplib submodule {node.module}",
                    )

    def test_py_and_ts_hir_parity_warnings_and_sim(self) -> None:
        self.assertTrue(EUDKIT.exists(), f"missing compiled binary: {EUDKIT}")
        for slug in _example_slugs():
            py_path = EXAMPLES_DIR / "py" / f"{slug}.py"
            ts_path = EXAMPLES_DIR / "ts" / f"{slug}.ts"
            with self.subTest(slug=slug):
                py_proc = subprocess.run(
                    [str(EUDKIT), "synth", str(py_path), "--compact"],
                    capture_output=True,
                    text=True,
                    cwd=REPO_ROOT,
                    check=True,
                )
                ts_proc = subprocess.run(
                    [str(EUDKIT), "synth", str(ts_path), "--compact"],
                    capture_output=True,
                    text=True,
                    cwd=REPO_ROOT,
                    check=True,
                )
                self.assertEqual(py_proc.stderr.strip(), "", f"{slug} py stderr warnings")
                self.assertEqual(ts_proc.stderr.strip(), "", f"{slug} ts stderr warnings")
                self.assertEqual(
                    py_proc.stdout,
                    ts_proc.stdout,
                    f"{slug}: Python and TS compact HIR must be byte-identical",
                )
                sim_report = run_sim(py_proc.stdout, loops=2)
                self.assertTrue(
                    sim_report.get("self_targeted_invariant_holds", False),
                    f"{slug}: self_targeted_invariant_holds failed",
                )

    def test_py_and_ts_scx_compile_parity(self) -> None:
        self.assertTrue(EUDKIT.exists(), f"missing compiled binary: {EUDKIT}")
        self.assertTrue(BASEMAP_SCX.exists(), f"missing basemap: {BASEMAP_SCX}")
        with tempfile.TemporaryDirectory(prefix="eudkit-examples-scx-") as tmpdir:
            tmp = Path(tmpdir)
            for slug in _example_slugs():
                py_path = EXAMPLES_DIR / "py" / f"{slug}.py"
                ts_path = EXAMPLES_DIR / "ts" / f"{slug}.ts"
                py_scx = tmp / f"{slug}-py.scx"
                ts_scx = tmp / f"{slug}-ts.scx"
                with self.subTest(slug=slug):
                    subprocess.run(
                        [
                            str(EUDKIT),
                            "compile",
                            str(py_path),
                            "--basemap",
                            str(BASEMAP_SCX),
                            "-o",
                            str(py_scx),
                        ],
                        capture_output=True,
                        text=True,
                        cwd=REPO_ROOT,
                        check=True,
                    )
                    subprocess.run(
                        [
                            str(EUDKIT),
                            "compile",
                            str(ts_path),
                            "--basemap",
                            str(BASEMAP_SCX),
                            "-o",
                            str(ts_scx),
                        ],
                        capture_output=True,
                        text=True,
                        cwd=REPO_ROOT,
                        check=True,
                    )
                    self.assertEqual(
                        py_scx.read_bytes(),
                        ts_scx.read_bytes(),
                        f"{slug}: compiled .scx bytes differ between Py and TS",
                    )


if __name__ == "__main__":
    unittest.main()
