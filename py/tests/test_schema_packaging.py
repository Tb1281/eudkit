"""Packaging tests: the schema files shipped inside the `eudkit` package.

Two artifacts ship as package data (see `pyproject.toml` `package-data`):

- `schema/layouts/*.json`, the DAT/CUnit/CSprite field layouts that
  `_locate_schema_file` checks first; and
- `schema/stock-rows.json`, the stock/extension row spec that
  `stock_rows_built.locate_spec()` resolves before the repo-root canonical
  copy.

These tests pin that the packaged copies exist, parse, and stay
byte-identical to the repo canonical files (`schema/layouts/` and
`schema/stock-rows.json`). When a canonical copy is absent (installed
package environment), the byte comparison is skipped, but the packaged-copy
checks still run.

`TestWheelInstall` is the end-to-end shipping contract: it builds a wheel
from a temporary copy of `py/` (never in place, so `py/build/` and
`py/*.egg-info/` cannot land in the working tree), asserts the wheel holds
`eudkit/schema/stock-rows.json` with the canonical bytes, installs it into a
fresh venv with no index access, and imports `eudkit` from an empty consumer
directory. It then plants a decoy `schema/stock-rows.json` in that directory
and imports again, so a consumer's working directory cannot poison the
import. Budget is roughly 5-15 s per run; that is the build/install, not a
leak. `TestLocateSpecIgnoresCwd` pins the deleted `Path.cwd()` fallback
directly: with both package-relative candidates absent, `locate_spec()`
raises instead of reading a `schema/stock-rows.json` out of the working
directory (a decoy in a healthy wheel is shadowed by the packaged copy, so
the in-process test is the one that bites).

Compatible with both `pytest` and `python3 -m unittest`.
"""

from __future__ import annotations

import importlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import ModuleType

from py import stock_rows_built
from py.view import _locate_schema_file

PY_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = PY_DIR.parent
PACKAGED_DIR = PY_DIR / "schema" / "layouts"
CANONICAL_DIR = REPO_ROOT / "schema" / "layouts"
PACKAGED_SPEC = PY_DIR / "schema" / "stock-rows.json"
CANONICAL_SPEC = REPO_ROOT / "schema" / "stock-rows.json"

SPEC_SECTIONS = ("conditions", "actions", "extensions", "records")
WHEEL_SPEC_ENTRY = "eudkit/schema/stock-rows.json"

# The import code the wheel test runs inside the venv: the helpers prove
# `py/stock_rows_built.py` loaded the spec and built its rows.
IMPORT_CODE = (
    "import eudkit; from eudkit import Always, CountdownTimer; "
    "print(eudkit.__file__)"
)


def _stdlib_zipfile() -> ModuleType:
    """Import stdlib `zipfile`, sidestepping the `py/` path entry.

    `py/tests/__init__.py` puts `py/` on `sys.path` so tests can import
    sibling fixtures; that also makes same-named siblings shadow stdlib
    modules, and `py/struct.py` breaks `zipfile`'s `import struct`. Drop the
    entry just for this import, then restore the original path list.
    """
    cached = sys.modules.get("zipfile")
    if cached is not None:
        return cached
    saved = list(sys.path)
    sys.path[:] = [
        entry
        for entry in saved
        if entry and os.path.abspath(entry) != str(PY_DIR)
    ]
    try:
        return importlib.import_module("zipfile")
    finally:
        sys.path[:] = saved


class TestSchemaPackaging(unittest.TestCase):
    def test_packaged_layouts_exist_and_parse(self) -> None:
        files = sorted(PACKAGED_DIR.glob("*.json"))
        self.assertTrue(files, "no packaged schema layouts found")
        for f in files:
            data = json.loads(f.read_text(encoding="utf-8"))
            self.assertIsInstance(data.get("fields"), dict, f"{f.name} has no fields")

    def test_packaged_matches_canonical(self) -> None:
        if not CANONICAL_DIR.is_dir():
            self.skipTest("canonical schema/layouts not present (installed env)")
        packaged = {f.name: f.read_bytes() for f in PACKAGED_DIR.glob("*.json")}
        canonical = {f.name: f.read_bytes() for f in CANONICAL_DIR.glob("*.json")}
        self.assertEqual(set(packaged), set(canonical), "packaged/canonical layout sets differ")
        for name, blob in packaged.items():
            self.assertEqual(blob, canonical[name], f"packaged {name} drifted from canonical")

    def test_locate_prefers_packaged_copy(self) -> None:
        resolved = _locate_schema_file("cunit")
        self.assertEqual(resolved, PACKAGED_DIR / "cunit.json")

    def test_packaged_stock_rows_spec_exists_and_parses(self) -> None:
        self.assertTrue(
            PACKAGED_SPEC.is_file(),
            f"missing packaged stock row spec: {PACKAGED_SPEC}",
        )
        data = json.loads(PACKAGED_SPEC.read_text(encoding="utf-8"))
        sections = data.get("sections")
        self.assertIsInstance(sections, dict, "packaged spec has no sections")
        for name in SPEC_SECTIONS:
            self.assertIsInstance(sections.get(name), list, f"no {name} section")
            self.assertTrue(sections[name], f"empty {name} section")

    def test_packaged_stock_rows_spec_matches_canonical(self) -> None:
        if not CANONICAL_SPEC.is_file():
            self.skipTest(
                "canonical schema/stock-rows.json not present (installed env)"
            )
        self.assertEqual(
            PACKAGED_SPEC.read_bytes(),
            CANONICAL_SPEC.read_bytes(),
            "packaged stock row spec drifted from canonical; run "
            "`python3 scripts/generate_stock_surfaces.py`",
        )


class TestLocateSpecIgnoresCwd(unittest.TestCase):
    """`locate_spec()` must never read the process working directory.

    The wheel test below plants a decoy spec in the consumer cwd, but a
    healthy wheel finds its packaged copy first either way. This test removes
    both package-relative candidates (by pointing the module at an empty temp
    package) so a working-directory fallback would be the only way to
    resolve, and pins that it is not consulted.
    """

    def test_locate_spec_does_not_read_the_working_directory(self) -> None:
        with tempfile.TemporaryDirectory(prefix="eudkit-cwd-") as tmp:
            root = Path(tmp)
            module_path = root / "pkg" / "stock_rows_built.py"
            module_path.parent.mkdir()
            consumer = root / "consumer"
            decoy = consumer / "schema" / "stock-rows.json"
            decoy.parent.mkdir(parents=True)
            decoy.write_text('{"sections": {}}', encoding="utf-8")

            previous = os.getcwd()
            original_file = stock_rows_built.__file__
            os.chdir(consumer)
            try:
                stock_rows_built.__file__ = str(module_path)
                with self.assertRaises(FileNotFoundError):
                    stock_rows_built.locate_spec()
            finally:
                stock_rows_built.__file__ = original_file
                os.chdir(previous)


class TestWheelInstall(unittest.TestCase):
    """Build a wheel, install it into a fresh venv, import it elsewhere."""

    def _require_build_tools(self) -> None:
        """Skip only when this interpreter cannot create a venv or run pip."""
        commands = {
            "venv": [sys.executable, "-m", "venv", "--help"],
            "ensurepip": [sys.executable, "-m", "ensurepip", "--version"],
            "pip": [sys.executable, "-m", "pip", "--version"],
        }
        missing = []
        for name, command in commands.items():
            result = subprocess.run(command, capture_output=True, text=True)
            if result.returncode != 0:
                detail = (result.stderr or result.stdout).strip().splitlines()
                missing.append(f"{name} ({detail[0] if detail else 'no output'})")
        if missing:
            self.skipTest(
                "venv/pip unavailable for this interpreter: " + ", ".join(missing)
            )

    def test_wheel_ships_the_stock_row_spec_and_imports_cleanly(self) -> None:
        self._require_build_tools()
        zipfile = _stdlib_zipfile()

        with tempfile.TemporaryDirectory(prefix="eudkit-wheel-") as tmp:
            root = Path(tmp)

            # 1. Build from a copy: building in place would leave py/build/
            # and py/*.egg-info/ in the working tree.
            source = root / "src"
            shutil.copytree(
                PY_DIR,
                source,
                ignore=shutil.ignore_patterns(
                    "__pycache__", "build", "*.egg-info", ".pytest_cache"
                ),
            )
            dist = root / "dist"
            env = dict(
                os.environ,
                PIP_NO_INDEX="1",
                PIP_DISABLE_PIP_VERSION_CHECK="1",
            )
            env.pop("PYTHONPATH", None)

            build = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "pip",
                    "wheel",
                    "--no-deps",
                    "--no-build-isolation",
                    "-w",
                    str(dist),
                    str(source),
                ],
                capture_output=True,
                text=True,
                env=env,
            )
            self.assertEqual(
                build.returncode,
                0,
                "pip wheel failed:\n" + build.stdout + build.stderr,
            )

            wheels = sorted(dist.glob("eudkit-*.whl"))
            self.assertEqual(
                len(wheels),
                1,
                f"expected exactly one eudkit wheel, got {[w.name for w in wheels]}",
            )
            wheel = wheels[0]

            # 2. The spec must be inside the wheel, with canonical bytes.
            with zipfile.ZipFile(wheel) as archive:
                names = archive.namelist()
                self.assertIn(
                    WHEEL_SPEC_ENTRY,
                    names,
                    f"{WHEEL_SPEC_ENTRY} missing from the wheel; entries: {names}",
                )
                packaged_spec = archive.read(WHEEL_SPEC_ENTRY)

            data = json.loads(packaged_spec.decode("utf-8"))
            for name in SPEC_SECTIONS:
                self.assertIsInstance(
                    data["sections"].get(name), list, f"no {name} section"
                )
            if CANONICAL_SPEC.is_file():
                self.assertEqual(
                    packaged_spec,
                    CANONICAL_SPEC.read_bytes(),
                    "wheel stock row spec is not byte-identical to the canonical file",
                )

            # 3. Install into a fresh venv without touching an index.
            venv_dir = root / "venv"
            created = subprocess.run(
                [sys.executable, "-m", "venv", str(venv_dir)],
                capture_output=True,
                text=True,
            )
            self.assertEqual(
                created.returncode,
                0,
                "venv creation failed:\n" + created.stdout + created.stderr,
            )
            venv_python = venv_dir / (
                "Scripts/python.exe" if os.name == "nt" else "bin/python"
            )

            install = subprocess.run(
                [
                    str(venv_python),
                    "-m",
                    "pip",
                    "install",
                    "--no-deps",
                    "--no-index",
                    str(wheel),
                ],
                capture_output=True,
                text=True,
                env=env,
            )
            self.assertEqual(
                install.returncode,
                0,
                "pip install failed:\n" + install.stdout + install.stderr,
            )

            # 4. Import from an empty directory, with a scrubbed environment.
            consumer = root / "consumer"
            consumer.mkdir()
            consumer_env = {
                key: value
                for key, value in env.items()
                if key
                not in ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV", "PYTHONSAFEPATH")
            }

            first = subprocess.run(
                [str(venv_python), "-c", IMPORT_CODE],
                cwd=consumer,
                capture_output=True,
                text=True,
                env=consumer_env,
            )
            self.assertEqual(
                first.returncode,
                0,
                "import from an empty directory failed:\n"
                + first.stdout
                + first.stderr,
            )
            package_file = Path(first.stdout.strip()).resolve()
            self.assertTrue(
                package_file.is_file(), f"no package file at {package_file}"
            )
            self.assertEqual(package_file.name, "__init__.py")
            self.assertEqual(package_file.parent.name, "eudkit")
            self.assertIn(
                str(venv_dir.resolve()),
                str(package_file),
                f"eudkit was imported from outside the test venv: {package_file}",
            )
            self.assertNotIn(
                str(REPO_ROOT.resolve()),
                str(package_file),
                f"eudkit was imported from the repo tree: {package_file}",
            )

            # 5. A decoy spec in the consumer cwd must not be picked up (the
            # packaged copy wins in a healthy wheel; this guards the ordering).
            decoy_dir = consumer / "schema"
            decoy_dir.mkdir()
            (decoy_dir / "stock-rows.json").write_text(
                '{"sections": {}}', encoding="utf-8"
            )
            second = subprocess.run(
                [str(venv_python), "-c", IMPORT_CODE],
                cwd=consumer,
                capture_output=True,
                text=True,
                env=consumer_env,
            )
            self.assertEqual(
                second.returncode,
                0,
                "import failed with a decoy schema/stock-rows.json in cwd "
                "(locate_spec must not read the working directory):\n"
                + second.stdout
                + second.stderr,
            )
            self.assertEqual(second.stdout.strip(), first.stdout.strip())
            shutil.rmtree(decoy_dir)


if __name__ == "__main__":
    unittest.main()
