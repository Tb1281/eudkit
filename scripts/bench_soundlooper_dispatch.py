#!/usr/bin/env python3
"""Soundlooper `dispatch` benchmark: sibling `when` vs `dispatch`.

This is a measurement spike for RT3 (wave 5).  It builds the *shape* of the
current soundlooper play dispatch — one loop gate ``when`` whose body contains
one ``when(value.eq(i), body_i)`` per segment — and the same shape with the
public ``dispatch(value, cases)`` surface.  Production code is untouched.

The fixture
-----------
* ``gate``  — a page ``Var`` initialised to ``0`` so the loop gate is open.
  In soundlooper the gate is ``current_loop.eq(loop.index)`` and the whole
  block is only reached on a due pass; here one simulator loop with the gate
  open is one due pass.
* ``value`` — the runtime segment selector.  Each measurement re-initialises
  it to a target key so the executed path is deterministic.
* ``sink``  — a page ``Var``.  Each arm does exactly one ``sink.set(1)`` so
  every body has the same one-action cost and the measurement isolates the
  dispatch structure (a ``PlayWAV`` placeholder would add a string per arm and
  swamp the comparison).
* Targets — first key (0), middle key (N // 2), last key (N - 1), and a miss
  (N + 5).  ``dispatch`` is called without a fallback, so a miss is the
  documented no-op.  The "worst" row is the target with the largest
  (triggers, conditions, actions) tuple.

Static cost comes from ``eudkit dump-ir --json --no-exec``.  Executed cost is
one trigger loop of ``eudkit-compile sim`` (the native simulator).  Both are
the same counters the test suite pins.

Reproduce from the repository root (build the native tools first)::

    cargo build
    python3 scripts/bench_soundlooper_dispatch.py

    node ts/scripts/bench_soundlooper_dispatch.ts

Add ``--boundary`` to include the construct's N=9/10 lowering boundary::

    python3 scripts/bench_soundlooper_dispatch.py --boundary
    node ts/scripts/bench_soundlooper_dispatch.ts --boundary

Recorded result (2026-11-14, N=16/64/256)
-----------------------------------------
Static T/C/A/B and worst-case executed T/C/A per due pass (gate included)::

    N   strategy  static T/C/A/B      worst exec T/C/A (key)   B/seg  C/seg
    16  when      19/17/82/45,756     18/17/3 (key 15)        2,860  1.06
    16  dispatch  49/32/112/117,996   11/6/11 (key 15)        7,375  0.38
    64  when      67/65/130/161,340   66/65/3 (key 63)        2,521  1.02
    64  dispatch  193/128/256/464,748 15/8/15 (key 63)        7,262  0.13
    256 when      259/257/322/623,676 258/257/3 (key 255)     2,436  1.00
    256 dispatch  769/512/832/1,851,756 19/10/19 (key 255)   7,233  0.04

The tree cuts the due-pass condition path from ``N + 1`` to ``log2(N) + 2``
(17 -> 6, 65 -> 8, 257 -> 10) and the evaluated trigger path from ``N + 2`` to
``2*log2(N) + 3`` (18 -> 11, 66 -> 15, 258 -> 19), but adds the same
``2*log2(N)`` executed actions (3 -> 11/15/19) and ``2N - 2`` static triggers
(about 4.8 KB per saved worst-case condition).  The
hybrid recommendation is therefore: keep the sibling-``when`` shape for small
N, switch to ``dispatch`` once the tree's due-pass win is material.  Dispatch
itself switches to the tree at N=10 (ADR-0025); ``--boundary`` reproduces the
N=9/10 edge.  For soundlooper the measured win is clear at N >= 16; a 1,000
segment loop would add about 1,998 static triggers (roughly 4.8 MB), so any
adoption needs a map-size budget.  This is a follow-up decision: the script
does not change production dispatch.
"""

from __future__ import annotations

import argparse
import math
import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from py import Var, build_module, dispatch, when  # noqa: E402
from py.tests.helpers import run_sim, run_static  # noqa: E402


DEFAULT_N = (16, 64, 256)
BOUNDARY_N = (9, 10)
STRATEGIES = ("when", "dispatch")
TARGET_LABELS = ("first", "mid", "last", "miss")


@dataclass(frozen=True)
class StaticCost:
    triggers: int
    conditions: int
    actions: int
    total_bytes: int


@dataclass(frozen=True)
class ExecCost:
    triggers: int
    conditions: int
    actions: int


@dataclass(frozen=True)
class Measurement:
    n: int
    strategy: str
    static: StaticCost
    executed: dict[str, ExecCost]


def _build(n: int, target: int, strategy: str) -> object:
    """Build the soundlooper-shaped gate + one strategy over ``value``."""

    def prog() -> None:
        gate = Var(0, storage="page")
        value = Var(target)
        sink = Var(0, storage="page")

        def body() -> None:
            sink.set(1)

        cases = [(key, body) for key in range(n)]

        def play() -> None:
            if strategy == "when":
                for key, arm in cases:
                    when(value.eq(key), arm)
            else:
                dispatch(value, cases)

        when(gate.eq(0), play)

    return build_module(prog)


def _targets(n: int) -> dict[str, int]:
    return {
        "first": 0,
        "mid": n // 2,
        "last": n - 1,
        "miss": n + 5,
    }


def _measure(n: int, strategy: str) -> Measurement:
    module = _build(n, 0, strategy)
    cost = run_static(module.to_json())["static_cost"]
    static = StaticCost(
        triggers=cost["triggers"],
        conditions=cost["conditions"],
        actions=cost["actions"],
        total_bytes=cost["total_bytes"],
    )
    executed: dict[str, ExecCost] = {}
    for label, target in _targets(n).items():
        report = run_sim(_build(n, target, strategy).to_json(), 1)
        exec_cost = report["executed_cost"]
        executed[label] = ExecCost(
            triggers=exec_cost["triggers"],
            conditions=exec_cost["conditions"],
            actions=exec_cost["actions"],
        )
    return Measurement(n=n, strategy=strategy, static=static, executed=executed)


def _worst(measure: Measurement) -> tuple[str, ExecCost]:
    """Worst target by (triggers, conditions, actions), tie-break last->first."""
    best = max(
        (measure.executed[label].triggers, measure.executed[label].conditions,
         measure.executed[label].actions)
        for label in TARGET_LABELS
    )
    for label in ("last", "miss", "mid", "first"):
        cost = measure.executed[label]
        if (cost.triggers, cost.conditions, cost.actions) == best:
            return label, cost
    raise AssertionError("unreachable")


def _fmt_static(cost: StaticCost) -> str:
    return (
        f"{cost.triggers}/{cost.conditions}/{cost.actions}/{cost.total_bytes:,}"
    )


def _fmt_exec(cost: ExecCost) -> str:
    return f"{cost.triggers}/{cost.conditions}/{cost.actions}"


def _fmt2(value: float) -> str:
    # Round half up so the Python table matches the TS `toFixed(2)` table.
    return f"{math.floor(value * 100 + 0.5) / 100:.2f}"


def _print_table(
    measurements: dict[tuple[int, str], Measurement], n_values: list[int]
) -> None:
    print(
        "static T/C/A/Bytes and worst-case executed T/C/A per due pass "
        "(gate included)"
    )
    print(
        f"{'N':>4}  {'strategy':<9} {'static T/C/A/B':<24} "
        f"{'worst exec T/C/A':<16} {'key':<9} {'B/seg':>8} {'C/seg':>7}"
    )
    for n in n_values:
        for strategy in STRATEGIES:
            m = measurements[(n, strategy)]
            label, cost = _worst(m)
            key = _targets(n)[label]
            print(
                f"{n:>4}  {strategy:<9} {_fmt_static(m.static):<24} "
                f"{_fmt_exec(cost):<16} {f'{label} ({key})':<9} "
                f"{m.static.total_bytes / n:>8,.0f} {_fmt2(cost.conditions / n):>7}"
            )


def _print_comparison(
    measurements: dict[tuple[int, str], Measurement], n_values: list[int]
) -> None:
    print()
    print("dispatch - when (negative exec delta is better; static is one-time)")
    print(
        f"{'N':>4}  {'lowering':<8} {'dT':>5} {'dB':>10} "
        f"{'dExecT':>7} {'dExecC':>7} {'dExecA':>7} "
        f"{'saved C':>8} {'B/savedC':>10}"
    )
    for n in n_values:
        base = measurements[(n, "when")]
        cand = measurements[(n, "dispatch")]
        _, base_worst = _worst(base)
        _, cand_worst = _worst(cand)
        saved = base_worst.conditions - cand_worst.conditions
        lowering = "tree" if n >= 10 else "chain"
        per_saved = (
            f"{(cand.static.total_bytes - base.static.total_bytes) / saved:,.0f}"
            if saved > 0
            else "-"
        )
        print(
            f"{n:>4}  {lowering:<8} {cand.static.triggers - base.static.triggers:>5} "
            f"{cand.static.total_bytes - base.static.total_bytes:>10,} "
            f"{cand_worst.triggers - base_worst.triggers:>7} "
            f"{cand_worst.conditions - base_worst.conditions:>7} "
            f"{cand_worst.actions - base_worst.actions:>7} "
            f"{saved:>8} {per_saved:>10}"
        )


def _recommendation(
    measurements: dict[tuple[int, str], Measurement], n_values: list[int]
) -> str:
    wins = [
        n
        for n in n_values
        if _worst(measurements[(n, "dispatch")])[1].conditions
        < _worst(measurements[(n, "when")])[1].conditions
    ]
    first_tree_win = min(wins) if wins else None
    boundary = 9 in n_values and 10 in n_values
    if boundary:
        chain_when = _worst(measurements[(9, "when")])[1]
        chain_dispatch = _worst(measurements[(9, "dispatch")])[1]
        tree_when = _worst(measurements[(10, "when")])[1]
        tree_dispatch = _worst(measurements[(10, "dispatch")])[1]
        boundary_text = (
            "At N=9 dispatch still uses the else-chain: executed "
            f"{_fmt_exec(chain_dispatch)} vs sibling when {_fmt_exec(chain_when)} "
            "(same shape, more static). At N=10 the tree first wins the "
            f"due-pass conditions ({_fmt_exec(tree_dispatch)} vs "
            f"{_fmt_exec(tree_when)}), which is ADR-0025's threshold. "
        )
    else:
        boundary_text = (
            "The construct itself switches from the else-chain to the tree at "
            "N=10 (ADR-0025); rerun with --boundary to reproduce N=9/10. "
        )
    first = (
        f"The first measured tree win is N={first_tree_win}. "
        if first_tree_win is not None and first_tree_win not in BOUNDARY_N
        else ""
    )
    return (
        "Recommendation: hybrid — sibling `when` below the crossover, "
        "`dispatch`'s BST above it. Keep the current sibling-`when` play "
        "dispatch for small loops (N <= 9), where the chain and `dispatch` have "
        "the same due-pass path but `dispatch` pays extra static bytes. Use the "
        "BST once its O(log N) due-pass path is material; in the measured "
        "soundlooper shape that is N >= 16, with N=10..15 a gray band (the tree "
        "saves a few conditions but adds static triggers and ~8 executed "
        "actions). "
        + boundary_text
        + first
        + "Static cost is one-time and runtime cost is per due pass: the tree "
        "saves N - log2(N) - 1 worst-case conditions and N - 2*log2(N) - 1 "
        "evaluated triggers at N >= 16, but adds 2N - 2 static triggers "
        "(roughly 4.8 KB per saved condition). For very large loops (N "
        "approaching 1,000) the static "
        "premium approaches 4.8 MB, so adoption needs a map-size budget. "
        "No production change is made here; an adoption would be a follow-up "
        "that amends ADR-0023 section 7."
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--n",
        type=int,
        nargs="+",
        default=list(DEFAULT_N),
        help="segment counts to measure (default: 16 64 256)",
    )
    parser.add_argument(
        "--boundary",
        action="store_true",
        help="also measure the N=9/10 chain->tree boundary",
    )
    args = parser.parse_args(argv)

    n_values = sorted(set(args.n) | (set(BOUNDARY_N) if args.boundary else set()))
    if not n_values:
        parser.error("--n must contain at least one positive integer")
    if any(n < 1 for n in n_values):
        parser.error("--n values must be >= 1")

    print(f"soundlooper dispatch benchmark: N={n_values}")
    measurements = {
        (n, strategy): _measure(n, strategy)
        for n in n_values
        for strategy in STRATEGIES
    }
    _print_table(measurements, n_values)
    _print_comparison(measurements, n_values)
    print()
    print(_recommendation(measurements, n_values))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
