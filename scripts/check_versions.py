#!/usr/bin/env python3
"""Check that every eudkit manifest carries the same unified version.

The Rust workspace, the CLI/LSP crates, both editor extensions, and the Python
and npm packages share one unified version. Rust crates inherit it with
``version.workspace = true`` so there is a single source of truth in
``[workspace.package] version``.

Run from anywhere::

    python3 scripts/check_versions.py
    python3 scripts/check_versions.py --tag v0.1.1

``--tag`` accepts the git tag form ``vMAJOR.MINOR.PATCH`` (or the bare
``MAJOR.MINOR.PATCH``) and must exactly match every manifest version.
Prerelease suffixes must match exactly too, so ``v0.2.0-rc.1`` is only valid
when the manifests also say ``0.2.0-rc.1``.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import tomllib
from pathlib import Path

VERSION_PATTERN = re.compile(r"^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$")
CRATES = ("eudkit-cli", "eudkit-lsp")


class VersionError(Exception):
    """A manifest or tag failed validation."""


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Verify eudkit manifest versions.")
    parser.add_argument(
        "--root",
        default=Path(__file__).resolve().parent.parent,
        type=Path,
        help="Repository root (default: the checkout containing this script)",
    )
    parser.add_argument(
        "--tag",
        help="Release tag to check, e.g. v0.1.1; must equal the manifest version",
    )
    return parser.parse_args(argv)


def read_toml(path: Path) -> dict:
    with path.open("rb") as handle:
        return tomllib.load(handle)


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def require_version(label: str, version: object) -> str:
    if not isinstance(version, str) or not VERSION_PATTERN.match(version):
        raise VersionError(f"{label}: invalid or missing version {version!r}")
    return version


def check_inherited(manifest: dict, label: str) -> None:
    package = manifest.get("package", {})
    if package.get("version") != {"workspace": True}:
        raise VersionError(
            f"{label}: expected `version.workspace = true` so the crate inherits "
            f"the workspace version (found {package.get('version')!r})"
        )


def collect(root: Path) -> dict[str, str]:
    cargo = read_toml(root / "Cargo.toml")
    workspace_version = require_version(
        "Cargo.toml [workspace.package] version",
        cargo.get("workspace", {}).get("package", {}).get("version"),
    )

    crate_manifests = sorted((root / "crates").glob("*/Cargo.toml"))
    found = {path.parent.name for path in crate_manifests}
    missing = [crate for crate in CRATES if crate not in found]
    if missing:
        raise VersionError(f"missing crate manifests: {', '.join(missing)}")
    for path in crate_manifests:
        check_inherited(read_toml(path), path.relative_to(root).as_posix())

    vscode = require_version(
        "editors/vscode/package.json version",
        read_json(root / "editors" / "vscode" / "package.json").get("version"),
    )
    zed_cargo = require_version(
        "editors/zed/Cargo.toml version",
        read_toml(root / "editors" / "zed" / "Cargo.toml").get("package", {}).get("version"),
    )
    zed_extension = require_version(
        "editors/zed/extension.toml version",
        read_toml(root / "editors" / "zed" / "extension.toml").get("version"),
    )

    editors = {"editors/vscode/package.json": vscode, "editors/zed/Cargo.toml": zed_cargo,
               "editors/zed/extension.toml": zed_extension}
    mismatched = {label: value for label, value in editors.items() if value != workspace_version}
    if mismatched:
        details = ", ".join(f"{label}={value}" for label, value in mismatched.items())
        raise VersionError(
            f"editor manifests must match the Rust version {workspace_version!r}: {details}"
        )

    ts = require_version("ts/package.json version", read_json(root / "ts" / "package.json").get("version"))
    py = require_version(
        "py/pyproject.toml version",
        read_toml(root / "py" / "pyproject.toml").get("project", {}).get("version"),
    )
    if ts != py:
        raise VersionError(f"ts/package.json={ts} must match py/pyproject.toml={py}")
    if ts != workspace_version or py != workspace_version:
        raise VersionError(
            f"DSL packages must match the workspace version {workspace_version!r}: "
            f"ts/package.json={ts}, py/pyproject.toml={py}"
        )

    return {
        "rust": workspace_version,
        "vscode": vscode,
        "zed-cargo": zed_cargo,
        "zed-extension": zed_extension,
        "npm": ts,
        "python": py,
    }


def normalize_tag(tag: str) -> str:
    value = tag.strip()
    if value.startswith("v"):
        value = value[1:]
    return require_version(f"--tag {tag!r}", value)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    root = args.root.resolve()
    try:
        versions = collect(root)
        if args.tag:
            tag_version = normalize_tag(args.tag)
            if tag_version != versions["rust"]:
                raise VersionError(
                    f"tag {args.tag!r} points at {tag_version}, but the Rust/CLI/editor "
                    f"version is {versions['rust']}. Tags use the form v<MAJOR.MINOR.PATCH> "
                    "and must match exactly (including any prerelease suffix)."
                )
            print(f"tag {args.tag} matches Rust/CLI/editor version {tag_version}")
    except (VersionError, OSError, KeyError, json.JSONDecodeError, tomllib.TOMLDecodeError) as error:
        print(f"version check failed: {error}", file=sys.stderr)
        return 1

    print("versions are consistent:")
    for label, version in versions.items():
        print(f"  {label:14} {version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
