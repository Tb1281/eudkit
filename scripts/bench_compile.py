#!/usr/bin/env python3
"""Repeatable compile-stage benchmark for large eudkit maps.

The harness is manual by design: pass the source and basemap explicitly so a
large fixture never has to live in the repository or normal CI.

Examples:
    python3 scripts/bench_compile.py \\
        --source /path/to/large.ts \\
        --basemap /path/to/base.scx

    python3 scripts/bench_compile.py ... --runs 5 --json
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import os
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

STAGE_MARKER = "__EUDKIT_STAGE__ "
STAGE_ENV = "EUDKIT_STAGE_TIMINGS"


@dataclasses.dataclass
class RunResult:
    stages: dict[str, float]
    total_ms: float
    bytes: int
    sha256: str
    static: int | None


def repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def release_binary(root: Path) -> Path:
    return root / "target" / "release" / "eudkit"


def build_release(root: Path) -> Path:
    binary = release_binary(root)
    print(f"building {binary} ...", file=sys.stderr)
    subprocess.run(
        ["cargo", "build", "--release", "-p", "eudkit"],
        cwd=root,
        check=True,
    )
    return binary


def parse_stages(stderr: str) -> dict[str, float]:
    stages: dict[str, float] = {}
    for line in stderr.splitlines():
        if not line.startswith(STAGE_MARKER):
            continue
        try:
            payload = json.loads(line[len(STAGE_MARKER) :])
            name = str(payload["name"])
            stages[name] = stages.get(name, 0.0) + float(payload["ms"])
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            raise SystemExit(f"malformed stage line: {line!r}")
    return stages


def run_once(
    eudkit: Path,
    source: Path,
    basemap: Path,
    out: Path,
    root: Path,
) -> RunResult:
    env = os.environ.copy()
    env[STAGE_ENV] = "1"
    env.setdefault("EUDKIT_ROOT", str(root))
    cmd = [
        str(eudkit),
        "compile",
        str(source),
        "--basemap",
        str(basemap),
        "--out",
        str(out),
    ]
    started = time.perf_counter()
    proc = subprocess.run(cmd, env=env, capture_output=True, text=True)
    total_ms = (time.perf_counter() - started) * 1000.0
    if proc.returncode != 0:
        print("command failed:", " ".join(cmd), file=sys.stderr)
        print(proc.stdout, file=sys.stderr, end="")
        print(proc.stderr, file=sys.stderr, end="")
        raise SystemExit(proc.returncode)

    stages = parse_stages(proc.stderr)
    if not stages:
        raise SystemExit(
            "no stage lines found; is EUDKIT_STAGE_TIMINGS supported by this binary?"
        )

    data = out.read_bytes()
    static = re.search(r"\((\d+)T static\)", proc.stdout)
    return RunResult(
        stages=stages,
        total_ms=total_ms,
        bytes=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
        static=int(static.group(1)) if static else None,
    )


def summarize(values: list[float]) -> dict[str, float]:
    return {
        "median_ms": statistics.median(values),
        "min_ms": min(values),
        "max_ms": max(values),
    }


def stage_summaries(runs: list[RunResult]) -> dict[str, dict[str, float]]:
    order: list[str] = []
    for run in runs:
        for name in run.stages:
            if name not in order:
                order.append(name)
    return {
        name: summarize([run.stages[name] for run in runs if name in run.stages])
        for name in order
    }


def check_deterministic(runs: list[RunResult]) -> None:
    hashes = {run.sha256 for run in runs}
    statics = {run.static for run in runs}
    sizes = {run.bytes for run in runs}
    if len(hashes) != 1 or len(statics) != 1 or len(sizes) != 1:
        print("error: benchmark output is not deterministic", file=sys.stderr)
        print(f"  hashes:  {sorted(hashes)}", file=sys.stderr)
        print(f"  statics: {sorted(statics, key=str)}", file=sys.stderr)
        print(f"  sizes:   {sorted(sizes)}", file=sys.stderr)
        raise SystemExit(1)


def print_table(
    runs: list[RunResult],
    total: dict[str, float],
    stages: dict[str, dict[str, float]],
    source: Path,
    basemap: Path,
    eudkit: Path,
    warmup: int,
) -> None:
    print("compile stage benchmark")
    print(f"  source:  {source}")
    print(f"  basemap: {basemap}")
    print(f"  binary:  {eudkit}")
    print(f"  runs:    {len(runs)} measured, {warmup} warmup")
    print()
    print(
        f"{'stage':<24} {'median ms':>10} {'min ms':>10} {'max ms':>10} {'share':>8}"
    )
    print("-" * 66)
    medians_sum = 0.0
    for name, summary in stages.items():
        medians_sum += summary["median_ms"]
        share = summary["median_ms"] / total["median_ms"] * 100.0
        print(
            f"{name:<24} {summary['median_ms']:>10.1f} {summary['min_ms']:>10.1f} "
            f"{summary['max_ms']:>10.1f} {share:>7.1f}%"
        )
    print("-" * 66)
    sum_share = medians_sum / total["median_ms"] * 100.0
    print(
        f"{'sum(stages)':<24} {medians_sum:>10.1f} {'':>10} {'':>10} "
        f"{sum_share:>7.1f}%"
    )
    print(
        f"{'total (wall clock)':<24} {total['median_ms']:>10.1f} "
        f"{total['min_ms']:>10.1f} {total['max_ms']:>10.1f} {100.0:>7.1f}%"
    )

    first = runs[0]
    print()
    print(
        f"output: {first.bytes:,} bytes; identical sha256 {first.sha256}"
    )
    print(f"static: {first.static}T; identical across runs")


def main() -> None:
    root = repo_root()
    parser = argparse.ArgumentParser(
        description="Repeatable compile-stage benchmark for large eudkit maps."
    )
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--basemap", required=True, type=Path)
    parser.add_argument(
        "--eudkit",
        type=Path,
        default=None,
        help="CLI binary to run (default: target/release/eudkit)",
    )
    parser.add_argument(
        "--no-build",
        action="store_true",
        help="skip the release build and use the existing binary",
    )
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--warmup", type=int, default=1)
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="output map path (default: a temporary directory)",
    )
    parser.add_argument(
        "--keep",
        action="store_true",
        help="keep the temporary output directory",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="print machine-readable JSON instead of the table",
    )
    args = parser.parse_args()

    if args.runs < 1:
        parser.error("--runs must be at least 1")
    if args.warmup < 0:
        parser.error("--warmup must be non-negative")
    if not args.source.is_file():
        parser.error(f"source does not exist: {args.source}")
    if not args.basemap.is_file():
        parser.error(f"basemap does not exist: {args.basemap}")
    if args.eudkit is not None and not args.eudkit.is_file():
        parser.error(f"eudkit binary does not exist: {args.eudkit}")

    if args.eudkit is not None:
        eudkit = args.eudkit
    else:
        eudkit = release_binary(root)
        if not args.no_build:
            eudkit = build_release(root)
        elif not eudkit.is_file():
            parser.error(
                f"{eudkit} does not exist; run without --no-build or pass --eudkit"
            )

    if args.out is not None:
        out = args.out
        out.parent.mkdir(parents=True, exist_ok=True)
        temp_dir = None
    else:
        temp_dir = Path(tempfile.mkdtemp(prefix="eudkit-bench-"))
        out = temp_dir / "bench.scx"

    try:
        for index in range(args.warmup):
            print(f"warmup {index + 1}/{args.warmup} ...", file=sys.stderr)
            run_once(eudkit, args.source, args.basemap, out, root)

        runs = []
        for index in range(args.runs):
            print(f"run {index + 1}/{args.runs} ...", file=sys.stderr)
            runs.append(run_once(eudkit, args.source, args.basemap, out, root))

        total = summarize([run.total_ms for run in runs])
        stages = stage_summaries(runs)
        check_deterministic(runs)

        if args.json:
            payload = {
                "source": str(args.source),
                "basemap": str(args.basemap),
                "binary": str(eudkit),
                "runs": args.runs,
                "warmup": args.warmup,
                "total": total,
                "stages": stages,
                "output": {
                    "bytes": runs[0].bytes,
                    "sha256": runs[0].sha256,
                    "deterministic": True,
                },
                "static": runs[0].static,
            }
            print(json.dumps(payload, indent=2))
        else:
            print_table(
                runs, total, stages, args.source, args.basemap, eudkit, args.warmup
            )

        if args.keep:
            print(f"kept output: {out}", file=sys.stderr)
    finally:
        if temp_dir is not None and not args.keep:
            shutil.rmtree(temp_dir, ignore_errors=True)


if __name__ == "__main__":
    main()
