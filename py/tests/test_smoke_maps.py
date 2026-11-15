"""Smoke-map compilation and documented semantic outputs.

The hand-test maps under ``tests/outputmap/smoke`` and
``tests/outputmap/diagnostics`` are the regression surface for the stock
catalog, temporal lowering, dispatch, divmod, unit groups, camera follow, and
container/math helpers.  This module checks three layers:

* frontend warnings and Python/TypeScript HIR parity for every paired source;
* clean ``.scx`` assembly against the documented basemap; and
* the semantic values the maps display, observed through the native
  ``eudkit-compile sim`` report.

It also runs the quickstart and migration examples through the same
synth -> sim path so their documented outputs stay pinned.
"""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any, Sequence

from py.tests.helpers import (
    EUDKIT,
    EUDKIT_COMPILE,
    REPO_ROOT,
    read_dword,
    read_text,
    run_sim,
)

SMOKE_DIR = REPO_ROOT / "tests" / "outputmap" / "smoke"
DIAGNOSTICS_DIR = REPO_ROOT / "tests" / "outputmap" / "diagnostics"
BASEMAP_DIR = REPO_ROOT / "tests" / "outputmap" / "basemap"
BASEMAP_SILENT = BASEMAP_DIR / "basemap_silent.scx"
BASEMAP_PLAIN = BASEMAP_DIR / "basemap.scx"

EXAMPLES_DIR = REPO_ROOT / "examples"
QUICKSTART_DIR = EXAMPLES_DIR / "quickstart" / "py"

# Sources that ship as a Python/TypeScript pair and must stay identical.
PAIRED_TARGETS = [
    "stock-catalog",
    "sample",
    "else-dispatch",
    "divmod-loop",
    "unitgroup-cunit",
    "camera-follow",
    "hud-containers-math",
]

# Single-source hand-test maps: (source, basemap).
SINGLE_TARGETS = [
    ("dat-tables", BASEMAP_SILENT),
    ("classic-runner-order", BASEMAP_PLAIN),
    ("aligned-string-padding", BASEMAP_SILENT),
    ("setup-display-probe", BASEMAP_SILENT),
    ("displayat_owning_verify", BASEMAP_SILENT),
]


def _require_binaries() -> None:
    assert EUDKIT.exists(), f"missing compiled binary: {EUDKIT}"
    assert EUDKIT_COMPILE.exists(), f"missing compiled binary: {EUDKIT_COMPILE}"


def _synth(source: Path) -> subprocess.CompletedProcess[str]:
    """Run ``eudkit synth --compact`` and return the completed process."""
    return subprocess.run(
        [str(EUDKIT), "synth", str(source), "--compact"],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        check=True,
    )


def _run_parallel(
    commands: Sequence[Sequence[str]],
) -> list[subprocess.CompletedProcess[str]]:
    """Start independent commands together, then collect them in order.

    The smoke builds share no state and each compile is CPU-bound, so
    overlapping the subprocesses keeps this module fast without threads (the
    repo's ``py`` directory shadows the stdlib ``queue`` module on
    ``sys.path``, which makes ``concurrent.futures`` awkward to import here).
    """
    procs = [
        subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=REPO_ROOT,
        )
        for command in commands
    ]
    results: list[subprocess.CompletedProcess[str]] = []
    for proc in procs:
        stdout, stderr = proc.communicate()
        results.append(
            subprocess.CompletedProcess(proc.args, proc.returncode, stdout, stderr)
        )
    return results


def _sim_source(source: Path, loops: int) -> dict[str, Any]:
    """Synthesize ``source`` and simulate its HIR for ``loops`` trigger loops."""
    proc = _synth(source)
    assert proc.stderr.strip() == "", f"{source.name}: synth warnings: {proc.stderr}"
    return run_sim(proc.stdout, loops=loops)


def _strings(report: dict[str, Any], count: int = 8) -> list[str]:
    """Decode the first ``count`` synthetic strings that were written."""
    if not report.get("external_epds"):
        return []
    out: list[str] = []
    for index in range(count):
        text = read_text(report, index)
        if text:
            out.append(text)
    return out


def _read_epd_string(report: dict[str, Any], first_epd: int, dwords: int) -> str:
    """Decode a NUL-terminated ASCII payload stored at ``first_epd``."""
    raw = b"".join(
        read_dword(report, first_epd + index).to_bytes(4, "little")
        for index in range(dwords)
    )
    return raw.split(b"\x00", 1)[0].decode("ascii")


def _assert_contains(test: unittest.TestCase, haystack: list[str], needle: str) -> None:
    test.assertTrue(
        any(needle in item for item in haystack),
        f"missing displayed text {needle!r}; saw {haystack!r}",
    )


class TestSmokeMapCompilation(unittest.TestCase):
    """Warnings, HIR parity, and byte-identical map assembly."""

    @classmethod
    def setUpClass(cls) -> None:
        _require_binaries()
        assert BASEMAP_SILENT.exists(), f"missing basemap: {BASEMAP_SILENT}"
        assert BASEMAP_PLAIN.exists(), f"missing basemap: {BASEMAP_PLAIN}"

    def test_paired_targets_compile_byte_identically(self) -> None:
        with tempfile.TemporaryDirectory(prefix="eudkit-smoke-pairs-") as tmpdir:
            tmp = Path(tmpdir)
            sources = [
                SMOKE_DIR / f"{slug}.{frontend}"
                for slug in PAIRED_TARGETS
                for frontend in ("py", "ts")
            ]

            synth_runs = _run_parallel(
                [
                    [str(EUDKIT), "synth", str(source), "--compact"]
                    for source in sources
                ]
            )
            for source, synth in zip(sources, synth_runs):
                self.assertEqual(
                    synth.stderr.strip(),
                    "",
                    f"{source.name}: synth warnings: {synth.stderr}",
                )

            outputs = [
                tmp / f"{source.stem}-{source.suffix.lstrip('.')}.scx"
                for source in sources
            ]
            compile_runs = _run_parallel(
                [
                    [
                        str(EUDKIT),
                        "compile",
                        str(source),
                        "--basemap",
                        str(BASEMAP_SILENT),
                        "-o",
                        str(out),
                    ]
                    for source, out in zip(sources, outputs)
                ]
            )
            for source, compiled in zip(sources, compile_runs):
                self.assertEqual(
                    compiled.stderr.strip(),
                    "",
                    f"{source.name}: compile warnings: {compiled.stderr}",
                )

            for index, slug in enumerate(PAIRED_TARGETS):
                with self.subTest(slug=slug):
                    py_synth = synth_runs[2 * index]
                    ts_synth = synth_runs[2 * index + 1]
                    py_out = outputs[2 * index]
                    ts_out = outputs[2 * index + 1]
                    self.assertEqual(
                        py_synth.stdout,
                        ts_synth.stdout,
                        f"{slug}: Python and TypeScript compact HIR differ",
                    )
                    self.assertEqual(
                        py_out.read_bytes(),
                        ts_out.read_bytes(),
                        f"{slug}: compiled .scx bytes differ between Python and TypeScript",
                    )

    def test_single_targets_compile_cleanly(self) -> None:
        with tempfile.TemporaryDirectory(prefix="eudkit-smoke-singles-") as tmpdir:
            tmp = Path(tmpdir)
            entries: list[tuple[str, Path, Path]] = []
            for name, basemap in SINGLE_TARGETS:
                directory = (
                    DIAGNOSTICS_DIR
                    if name == "setup-display-probe"
                    else SMOKE_DIR
                )
                entries.append((name, directory / f"{name}.ts", basemap))

            synth_runs = _run_parallel(
                [
                    [str(EUDKIT), "synth", str(source), "--compact"]
                    for _, source, _ in entries
                ]
            )
            for (name, source, _), synth in zip(entries, synth_runs):
                self.assertEqual(
                    synth.stderr.strip(),
                    "",
                    f"{name}: synth warnings: {synth.stderr}",
                )

            outputs = [tmp / f"{name}.scx" for name, _, _ in entries]
            compile_runs = _run_parallel(
                [
                    [
                        str(EUDKIT),
                        "compile",
                        str(source),
                        "--basemap",
                        str(basemap),
                        "-o",
                        str(out),
                    ]
                    for (_, source, basemap), out in zip(entries, outputs)
                ]
            )
            for (name, _, _), out, compiled in zip(entries, outputs, compile_runs):
                with self.subTest(name=name):
                    self.assertEqual(
                        compiled.stderr.strip(),
                        "",
                        f"{name}: compile warnings: {compiled.stderr}",
                    )
                    self.assertTrue(out.exists(), f"{name}: no .scx produced")
                    self.assertGreater(out.stat().st_size, 0, f"{name}: empty .scx")


class TestDocumentedSemantics(unittest.TestCase):
    """Observed simulator outputs pin the README's expected HUD lines."""

    @classmethod
    def setUpClass(cls) -> None:
        _require_binaries()

    def test_sample_decimal_hud(self) -> None:
        report = _sim_source(SMOKE_DIR / "sample.py", loops=3)
        self.assertEqual(_strings(report), ["wave=5 hp=100"])

    def test_aligned_string_padding(self) -> None:
        report = _sim_source(SMOKE_DIR / "aligned-string-padding.ts", loops=3)
        self.assertEqual(read_text(report, 0), "ABCDEFGH")
        self.assertEqual(read_text(report, 1), "ABCDEFGH")

    def test_classic_runner_order(self) -> None:
        report = _sim_source(SMOKE_DIR / "classic-runner-order.ts", loops=3)
        self.assertEqual(_strings(report), ["RUNNER PAYLOAD"])

    def test_displayat_owning_verify(self) -> None:
        report = _sim_source(SMOKE_DIR / "displayat_owning_verify.ts", loops=3)
        strings = _strings(report)
        self.assertIn("AAA", strings)
        self.assertIn("BBB", strings)

    def test_else_dispatch_semantics(self) -> None:
        report = _sim_source(SMOKE_DIR / "else-dispatch.py", loops=14)
        values = report["var_values"]
        self.assertEqual(values[:3], [1099, 1070, 12])
        strings = _strings(report)
        _assert_contains(
            self, strings, "DISPATCH CHAIN=1099/1099 BST=1070/1070"
        )
        _assert_contains(self, strings, "WHEN ELSE CHECK=12/12")
        _assert_contains(self, strings, "STATUS: PASS")

    def test_divmod_loop_semantics(self) -> None:
        report = _sim_source(SMOKE_DIR / "divmod-loop.py", loops=14)
        values = set(report["var_values"])
        self.assertLessEqual({714285, 15, 699678, 2}, values)
        strings = _strings(report)
        _assert_contains(
            self, strings, "DIVMOD QSUM=714285/714285 RSUM=15/15 FAIL=0"
        )
        _assert_contains(self, strings, "MUL=699678/699678 NE_GATE=2/2")
        _assert_contains(self, strings, "STATUS: PASS")

    def test_hud_containers_and_math(self) -> None:
        report = _sim_source(SMOKE_DIR / "hud-containers-math.py", loops=6)
        strings = _strings(report)
        _assert_contains(
            self, strings, "STACK=100/100 QUEUE=140/140 STRUCT+ARR=577/577"
        )
        _assert_contains(self, strings, "ISQRT=1255/1255")
        _assert_contains(self, strings, "STATUS: PASS")


class TestQuickstartAndMigrationExamples(unittest.TestCase):
    """The documented beginner/migration example outputs stay stable."""

    @classmethod
    def setUpClass(cls) -> None:
        _require_binaries()

    def test_quickstart_variables_and_strings(self) -> None:
        report = _sim_source(
            QUICKSTART_DIR / "02_variables_and_strings.py", loops=3
        )
        _assert_contains(self, _strings(report), "COUNT=39 DOUBLE=26")

    def test_quickstart_control_flow_and_temporal(self) -> None:
        report = _sim_source(
            QUICKSTART_DIR / "03_control_flow_and_temporal.py", loops=12
        )
        strings = _strings(report)
        _assert_contains(self, strings, "BOOT PHASE=0")
        _assert_contains(self, strings, "PHASE=2 SCORE=20")

    def test_quickstart_units_and_triggers(self) -> None:
        report = _sim_source(
            QUICKSTART_DIR / "04_units_and_triggers.py", loops=3
        )
        _assert_contains(self, _strings(report), "HP=10240 SCORE=258")
        self.assertEqual(read_dword(report, 19027), 10240)
        self.assertEqual(read_dword(report, 19035), 320)

    def test_migration_subdword_masks(self) -> None:
        report = _sim_source(EXAMPLES_DIR / "py" / "02_subdword_masks.py", loops=3)
        values = set(report["var_values"])
        self.assertLessEqual({4660, 4702, 305397760}, values)

    def test_migration_dat_tables(self) -> None:
        report = _sim_source(EXAMPLES_DIR / "py" / "05_dat_tables.py", loops=3)
        self.assertEqual(read_dword(report, 208825), 50)
        self.assertEqual(read_dword(report, 209619), 8)
        self.assertEqual(read_dword(report, 221179), 15360)

    def test_migration_block_io_and_data(self) -> None:
        report = _sim_source(
            EXAMPLES_DIR / "py" / "07_block_io_and_data.py", loops=3
        )
        self.assertEqual(_read_epd_string(report, 3943, 3), "EUDKITV3DATA")
        self.assertEqual(_read_epd_string(report, 4007, 3), "HELLO_EUD!")

    def test_migration_dynamic_epd_access(self) -> None:
        report = _sim_source(
            EXAMPLES_DIR / "py" / "08_dynamic_epd_access.py", loops=3
        )
        self.assertEqual(read_dword(report, 131), 0x12345678)
        self.assertEqual(read_dword(report, 144), 0x12345678)


if __name__ == "__main__":
    unittest.main()
