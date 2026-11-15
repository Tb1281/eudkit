"""Verify the progressive beginner examples in ``examples/quickstart``.

Every Python/TypeScript pair must synthesize with zero frontend warnings,
produce byte-identical compact HIR, keep the simulator's self-target invariant,
and compile to byte-identical ``.scx`` output.
"""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from py.tests.helpers import EUDKIT, REPO_ROOT, run_sim

QUICKSTART_DIR = REPO_ROOT / "examples" / "quickstart"
BASEMAP_SCX = REPO_ROOT / "tests" / "outputmap" / "basemap" / "basemap_silent.scx"

QUICKSTART_SLUGS = [
    "01_hello_world",
    "02_variables_and_strings",
    "03_control_flow_and_temporal",
    "04_units_and_triggers",
]


class TestQuickstartExamples(unittest.TestCase):
    def test_py_and_ts_slugs_match(self) -> None:
        py_slugs = sorted(p.stem for p in (QUICKSTART_DIR / "py").glob("*.py"))
        ts_slugs = sorted(p.stem for p in (QUICKSTART_DIR / "ts").glob("*.ts"))
        self.assertEqual(py_slugs, QUICKSTART_SLUGS)
        self.assertEqual(ts_slugs, QUICKSTART_SLUGS)

    def test_py_and_ts_hir_parity_warnings_and_sim(self) -> None:
        self.assertTrue(EUDKIT.exists(), f"missing compiled binary: {EUDKIT}")
        for slug in QUICKSTART_SLUGS:
            py_path = QUICKSTART_DIR / "py" / f"{slug}.py"
            ts_path = QUICKSTART_DIR / "ts" / f"{slug}.ts"
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
        with tempfile.TemporaryDirectory(prefix="eudkit-quickstart-scx-") as tmpdir:
            tmp = Path(tmpdir)
            for slug in QUICKSTART_SLUGS:
                py_path = QUICKSTART_DIR / "py" / f"{slug}.py"
                ts_path = QUICKSTART_DIR / "ts" / f"{slug}.ts"
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
