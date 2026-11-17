#!/usr/bin/env python3
"""Assemble a cross-platform eudkit release bundle.

The bundle layout is the distribution contract the CLI resolves at runtime:

    eudkit-<version>-<target>/
        bin/eudkit(.exe)
        bin/eudkit-lsp(.exe)
        share/eudkit/ts/        dist/, *.ts, package.json, schema/
        share/eudkit/py/        *.py, README.md, schema/, pyproject.toml
        LICENSE
        README.md

Use ``--verify`` to check the archive after it is written. The script only
uses the Python standard library so it runs unchanged on Linux, macOS, and
Windows runners.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import stat
import sys
import tarfile
import zipfile
from pathlib import Path, PurePosixPath

BINARIES = ("eudkit", "eudkit-lsp")
SHA256_CHUNK = 1024 * 1024


class BundleError(Exception):
    """A packaging or verification failure."""


def safe_component(value: str, label: str) -> str:
    """Reject values that could escape the output directory."""
    if not value or value != value.strip():
        raise BundleError(f"invalid {label}: {value!r}")
    if value in {".", ".."} or "/" in value or "\\" in value or "\0" in value:
        raise BundleError(f"invalid {label} (path separators/traversal): {value!r}")
    if Path(value).is_absolute() or (len(value) > 1 and value[1] == ":"):
        raise BundleError(f"invalid {label} (absolute path): {value!r}")
    return value


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(SHA256_CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Assemble an eudkit release bundle from a release build.",
    )
    parser.add_argument("--version", required=True, help="Bundle version, e.g. 0.1.1")
    parser.add_argument(
        "--target",
        required=True,
        help="Rust target triple, e.g. x86_64-unknown-linux-gnu",
    )
    parser.add_argument(
        "--bin-dir",
        required=True,
        help="Directory containing the release eudkit/eudkit-lsp binaries",
    )
    parser.add_argument(
        "--repo-root",
        default=".",
        help="Repository checkout root (default: current directory)",
    )
    parser.add_argument(
        "--out-dir",
        default="dist",
        help="Directory for the archive and checksum (default: dist)",
    )
    parser.add_argument(
        "--verify",
        action="store_true",
        help="Verify the archive layout after writing it",
    )
    return parser.parse_args(argv)


def require_file(path: Path, label: str) -> None:
    if not path.is_file():
        raise BundleError(f"missing {label}: {path}")


def require_dir(path: Path, label: str) -> None:
    if not path.is_dir():
        raise BundleError(f"missing {label}: {path}")


def copy_file(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)


def copy_tree(src: Path, dst: Path) -> None:
    def ignore(_directory: str, names: list[str]) -> set[str]:
        return {
            name
            for name in names
            if name in {"node_modules", "__pycache__", "tests", "build"}
            or name.endswith(".egg-info")
        }

    shutil.copytree(src, dst, ignore=ignore)


def stage_ts(repo_root: Path, bundle: Path) -> None:
    ts = repo_root / "ts"
    dist = ts / "dist"
    require_dir(dist, "built TypeScript output (run `npm run build` in ts/)")
    require_file(ts / "index.ts", "TypeScript runtime entry")

    out = bundle / "share" / "eudkit" / "ts"
    out.mkdir(parents=True, exist_ok=True)
    copy_tree(dist, out / "dist")

    sources = sorted(ts.glob("*.ts"))
    if not sources:
        raise BundleError(f"no TypeScript sources found in {ts}")
    for source in sources:
        copy_file(source, out / source.name)

    copy_file(ts / "package.json", out / "package.json")
    require_dir(ts / "schema", "TypeScript schema directory")
    copy_tree(ts / "schema", out / "schema")


def stage_py(repo_root: Path, bundle: Path) -> None:
    py = repo_root / "py"
    require_file(py / "__init__.py", "Python runtime entry")

    out = bundle / "share" / "eudkit" / "py"
    out.mkdir(parents=True, exist_ok=True)

    sources = sorted(py.glob("*.py"))
    if not sources:
        raise BundleError(f"no Python sources found in {py}")
    for source in sources:
        copy_file(source, out / source.name)

    require_dir(py / "schema", "Python schema directory")
    copy_tree(py / "schema", out / "schema")
    # pyproject.toml declares readme = "README.md", so keep the tree buildable.
    if (py / "README.md").is_file():
        copy_file(py / "README.md", out / "README.md")
    copy_file(py / "pyproject.toml", out / "pyproject.toml")


def stage_binaries(bin_dir: Path, bundle: Path, target: str) -> None:
    suffix = ".exe" if "windows" in target else ""
    out = bundle / "bin"
    out.mkdir(parents=True, exist_ok=True)
    for name in BINARIES:
        source = bin_dir / f"{name}{suffix}"
        require_file(source, f"release binary {name}{suffix}")
        destination = out / source.name
        copy_file(source, destination)
        if os.name != "nt" and "windows" not in target:
            mode = destination.stat().st_mode
            destination.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def stage_bundle(repo_root: Path, bin_dir: Path, out_dir: Path, version: str, target: str) -> Path:
    safe_component(version, "version")
    safe_component(target, "target")
    bundle_name = f"eudkit-{version}-{target}"
    bundle = out_dir / bundle_name
    if bundle.is_dir():
        shutil.rmtree(bundle)
    elif bundle.exists():
        bundle.unlink()
    bundle.mkdir(parents=True)

    for name in ("LICENSE", "README.md"):
        source = repo_root / name
        require_file(source, name)
        copy_file(source, bundle / name)

    stage_binaries(bin_dir, bundle, target)
    stage_ts(repo_root, bundle)
    stage_py(repo_root, bundle)
    return bundle


def write_archive(bundle: Path, out_dir: Path, target: str) -> Path:
    if "windows" in target:
        archive = out_dir / f"{bundle.name}.zip"
        if archive.exists():
            archive.unlink()
        with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for path in sorted(bundle.rglob("*")):
                if path.is_file():
                    zf.write(path, path.relative_to(out_dir).as_posix())
        return archive

    archive = out_dir / f"{bundle.name}.tar.gz"
    if archive.exists():
        archive.unlink()
    with tarfile.open(archive, "w:gz") as tar:
        # Deterministic metadata: the same inputs produce the same archive
        # regardless of the checkout user, host, or build time.
        for path in sorted(bundle.rglob("*")):
            relative = path.relative_to(out_dir).as_posix()
            info = tarfile.TarInfo(relative)
            info.uid = 0
            info.gid = 0
            info.uname = ""
            info.gname = ""
            info.mtime = 0
            if path.is_dir():
                info.type = tarfile.DIRTYPE
                info.mode = 0o755
                tar.addfile(info)
            elif path.is_file():
                mode = path.stat().st_mode
                info.size = path.stat().st_size
                info.mode = 0o755 if mode & 0o111 else 0o644
                with path.open("rb") as handle:
                    tar.addfile(info, handle)
    return archive


def write_checksum(archive: Path) -> Path:
    digest = sha256_file(archive)
    checksum = archive.parent / f"{archive.name}.sha256"
    checksum.write_text(f"{digest}  {archive.name}\n", encoding="utf-8")
    return checksum


def archive_entries(archive: Path) -> dict[str, tuple[int, int]]:
    """Return ``{name: (size, mode)}`` for regular files in the archive."""
    entries: dict[str, tuple[int, int]] = {}
    if archive.name.endswith(".zip"):
        with zipfile.ZipFile(archive) as zf:
            for info in zf.infolist():
                if not info.is_dir():
                    mode = (info.external_attr >> 16) & 0o777
                    entries[info.filename] = (info.file_size, mode)
        return entries

    with tarfile.open(archive, "r:gz") as tar:
        for member in tar.getmembers():
            if member.isfile():
                entries[member.name] = (member.size, member.mode & 0o777)
    return entries


def verify_archive(archive: Path, version: str, target: str) -> None:
    root = f"eudkit-{version}-{target}"
    suffix = ".exe" if "windows" in target else ""
    entries = archive_entries(archive)

    required = [
        f"{root}/bin/eudkit{suffix}",
        f"{root}/bin/eudkit-lsp{suffix}",
        f"{root}/share/eudkit/ts/index.ts",
        f"{root}/share/eudkit/ts/dist/index.js",
        f"{root}/share/eudkit/ts/package.json",
        f"{root}/share/eudkit/py/__init__.py",
        f"{root}/share/eudkit/py/pyproject.toml",
        f"{root}/share/eudkit/py/schema/stock-rows.json",
        f"{root}/LICENSE",
        f"{root}/README.md",
    ]
    problems = [f"missing {name}" for name in required if name not in entries]
    problems.extend(
        f"empty {name}" for name in required if name in entries and entries[name][0] == 0
    )

    # The runtimes load their schemas at import time; an archive without them
    # installs cleanly but cannot compile anything.
    schema_prefixes = {
        "TypeScript layouts": f"{root}/share/eudkit/ts/schema/layouts/",
        "Python layouts": f"{root}/share/eudkit/py/schema/layouts/",
    }
    for label, prefix in schema_prefixes.items():
        matches = [
            name
            for name in entries
            if name.startswith(prefix) and name.endswith(".json") and entries[name][0] > 0
        ]
        if not matches:
            problems.append(f"missing non-empty {label} JSON files under {prefix}")

    forbidden_components = {"node_modules", "tests", "__pycache__", "build"}
    for name in entries:
        parts = PurePosixPath(name).parts
        if any(part in forbidden_components for part in parts):
            problems.append(f"forbidden path in archive: {name}")
        if any(part.endswith(".egg-info") for part in parts):
            problems.append(f"forbidden egg-info path in archive: {name}")

    if "windows" not in target:
        for name in (f"{root}/bin/eudkit", f"{root}/bin/eudkit-lsp"):
            if name in entries and not entries[name][1] & 0o111:
                problems.append(f"binary is not executable: {name}")

    checksum = archive.parent / f"{archive.name}.sha256"
    if not checksum.is_file():
        problems.append(f"missing checksum file: {checksum}")
    else:
        expected = sha256_file(archive)
        recorded = checksum.read_text(encoding="utf-8").split()[0]
        if recorded != expected:
            problems.append(f"checksum mismatch: {checksum}")

    if problems:
        raise BundleError("archive verification failed:\n  " + "\n  ".join(problems))
    print(f"verified {archive.name}: {len(entries)} files")


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    repo_root = Path(args.repo_root).resolve()
    bin_dir = Path(args.bin_dir)
    if not bin_dir.is_absolute():
        bin_dir = repo_root / bin_dir
    out_dir = Path(args.out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    try:
        if not repo_root.is_dir():
            raise BundleError(f"repository root is not a directory: {repo_root}")
        bundle = stage_bundle(repo_root, bin_dir, out_dir, args.version, args.target)
        archive = write_archive(bundle, out_dir, args.target)
        checksum = write_checksum(archive)
        # Keep releases tidy: only the archive and checksum belong in out-dir.
        shutil.rmtree(bundle)
        print(f"wrote {archive}")
        print(f"wrote {checksum}")
        if args.verify:
            verify_archive(archive, args.version, args.target)
    except (BundleError, OSError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
