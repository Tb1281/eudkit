#!/usr/bin/env python3
"""Unit tests for scripts/package_bundle.py (standard library only)."""

from __future__ import annotations

import sys
import tarfile
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import package_bundle  # noqa: E402


class PackageBundleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "repo"
        self.bin_dir = Path(self.temporary.name) / "bin"
        self.out_dir = Path(self.temporary.name) / "dist"
        self._make_repo()

    def _write(self, relative: str, text: str = "x\n") -> Path:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def _make_repo(self) -> None:
        self._write("LICENSE", "MIT\n")
        self._write("README.md", "# eudkit\n")
        self._write("ts/index.ts", "export {};\n")
        self._write("ts/package.json", '{ "name": "eudkit", "version": "3.0.0" }\n')
        self._write("ts/dist/index.js", "export {};\n")
        self._write("ts/schema/layouts/cunit.json", "{}\n")
        self._write("py/__init__.py", "VERSION = '3.0.0'\n")
        self._write("py/pyproject.toml", '[project]\nname = "eudkit"\nversion = "3.0.0"\n')
        self._write("py/schema/layouts/cunit.json", "{}\n")
        self._write("py/schema/stock-rows.json", "{}\n")
        self.bin_dir.mkdir(parents=True)
        for name in package_bundle.BINARIES:
            binary = self.bin_dir / name
            binary.write_bytes(b"#!/bin/sh\nexit 0\n")
            binary.chmod(0o755)

    def _package(self, *, verify: bool = True) -> Path:
        args = [
            "--version",
            "0.3.0",
            "--target",
            "x86_64-unknown-linux-gnu",
            "--bin-dir",
            str(self.bin_dir),
            "--repo-root",
            str(self.root),
            "--out-dir",
            str(self.out_dir),
        ]
        if verify:
            args.append("--verify")
        self.assertEqual(package_bundle.main(args), 0)
        return self.out_dir / "eudkit-0.3.0-x86_64-unknown-linux-gnu.tar.gz"

    def test_rejects_path_traversal_versions_and_targets(self) -> None:
        for version in ("../escape", "..", "0.3.0/evil", "0.3.0\\evil", "/abs"):
            with self.subTest(version=version):
                with self.assertRaises(package_bundle.BundleError):
                    package_bundle.stage_bundle(
                        self.root, self.bin_dir, self.out_dir, version, "x86_64-unknown-linux-gnu"
                    )
        for target in ("../escape", "x86_64/evil", "x86_64\\evil", "..", "/abs"):
            with self.subTest(target=target):
                with self.assertRaises(package_bundle.BundleError):
                    package_bundle.stage_bundle(
                        self.root, self.bin_dir, self.out_dir, "0.3.0", target
                    )

    def test_verify_accepts_complete_bundle_and_normalizes_tar_metadata(self) -> None:
        archive = self._package(verify=True)
        self.assertTrue(archive.is_file())
        self.assertTrue((archive.parent / f"{archive.name}.sha256").is_file())

        with tarfile.open(archive, "r:gz") as tar:
            members = tar.getmembers()
        self.assertTrue(members)
        for member in members:
            self.assertEqual((member.uid, member.gid, member.mtime), (0, 0, 0))
        names = {member.name for member in members}
        self.assertIn("eudkit-0.3.0-x86_64-unknown-linux-gnu/bin/eudkit-lsp", names)
        self.assertIn(
            "eudkit-0.3.0-x86_64-unknown-linux-gnu/share/eudkit/ts/schema/layouts/cunit.json",
            names,
        )

    def test_verify_rejects_missing_python_schema(self) -> None:
        (self.root / "py/schema/stock-rows.json").unlink()
        archive = self._package(verify=False)
        with self.assertRaisesRegex(package_bundle.BundleError, "stock-rows.json"):
            package_bundle.verify_archive(
                archive, "0.3.0", "x86_64-unknown-linux-gnu"
            )

    def test_verify_rejects_missing_layouts(self) -> None:
        for path in (self.root / "ts/schema/layouts").glob("*.json"):
            path.unlink()
        archive = self._package(verify=False)
        with self.assertRaisesRegex(package_bundle.BundleError, "TypeScript layouts"):
            package_bundle.verify_archive(
                archive, "0.3.0", "x86_64-unknown-linux-gnu"
            )


if __name__ == "__main__":
    unittest.main()
