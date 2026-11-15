#!/usr/bin/env node
/**
 * Soundlooper `dispatch` benchmark: sibling `when` vs `dispatch`.
 *
 * This is the TypeScript half of the RT3 measurement spike. It mirrors
 * `scripts/bench_soundlooper_dispatch.py` exactly (same fixture, same targets,
 * same counters) and builds the *shape* of the current soundlooper play
 * dispatch — one loop gate `when` whose body contains one
 * `when(value.eq(i), body_i)` per segment — and the same shape with the public
 * `dispatch(value, cases)` surface. Production code is untouched.
 *
 * The fixture
 * -----------
 * - `gate`  — a page `Var` initialised to 0 so the loop gate is open. In
 *   soundlooper the gate is `currentLoop.eq(loop.index)` and the whole block
 *   is only reached on a due pass; here one simulator loop with the gate open
 *   is one due pass.
 * - `value` — the runtime segment selector. Each measurement re-initialises it
 *   to a target key so the executed path is deterministic.
 * - `sink`  — a page `Var`. Each arm does exactly one `sink.set(1)` so every
 *   body has the same one-action cost and the measurement isolates the
 *   dispatch structure (a `PlayWAV` placeholder would add a string per arm and
 *   swamp the comparison).
 * - Targets — first key (0), middle key (N >> 1), last key (N - 1), and a miss
 *   (N + 5). `dispatch` is called without a fallback, so a miss is the
 *   documented no-op. The "worst" row is the target with the largest
 *   (triggers, conditions, actions) tuple.
 *
 * Static cost comes from `eudkit dump-ir --json --no-exec`. Executed cost is
 * one trigger loop of `eudkit-compile sim` (the native simulator). Both are
 * the same counters the test suite pins.
 *
 * Reproduce from the repository root (build the native tools first):
 *
 *     cargo build
 *     node ts/scripts/bench_soundlooper_dispatch.ts
 *
 *     python3 scripts/bench_soundlooper_dispatch.py
 *
 * Add `--boundary` to include the construct's N=9/10 lowering boundary:
 *
 *     node ts/scripts/bench_soundlooper_dispatch.ts --boundary
 *     python3 scripts/bench_soundlooper_dispatch.py --boundary
 *
 * Recorded result (2026-11-14, N=16/64/256)
 * -----------------------------------------
 * Static T/C/A/B and worst-case executed T/C/A per due pass (gate included):
 *
 *     N   strategy  static T/C/A/B      worst exec T/C/A (key)  B/seg  C/seg
 *     16  when      19/17/82/45,756     18/17/3 (key 15)       2,860  1.06
 *     16  dispatch  49/32/112/117,996   11/6/11 (key 15)       7,375  0.38
 *     64  when      67/65/130/161,340   66/65/3 (key 63)       2,521  1.02
 *     64  dispatch  193/128/256/464,748 15/8/15 (key 63)       7,262  0.13
 *     256 when      259/257/322/623,676 258/257/3 (key 255)    2,436  1.00
 *     256 dispatch  769/512/832/1,851,756 19/10/19 (key 255)   7,233  0.04
 *
 * The tree cuts the due-pass condition path from `N + 1` to `log2(N) + 2`
 * (17 -> 6, 65 -> 8, 257 -> 10) and the evaluated trigger path from `N + 2` to
 * `2*log2(N) + 3` (18 -> 11, 66 -> 15, 258 -> 19), but adds the same
 * `2*log2(N)` executed actions (3 -> 11/15/19) and `2N - 2` static triggers
 * (about 4.8 KB per saved worst-case condition). The hybrid recommendation is
 * therefore: keep the sibling-`when` shape for small N, switch to `dispatch`
 * once the tree's due-pass win is material. Dispatch itself switches to the
 * tree at N=10 (ADR-0025); `--boundary` reproduces the N=9/10 edge. For
 * soundlooper the measured win is clear at N >= 16; a 1,000-segment loop would
 * add about 1,998 static triggers (roughly 4.8 MB), so any adoption needs a
 * map-size budget. This is a follow-up decision: the script does not change
 * production dispatch.
 */

import path from "node:path";
import { fileURLToPath } from "node:url";

import { Var, buildModule, dispatch, when } from "../index.ts";
import { runSim, runStatic } from "../tests/helpers.ts";

const DEFAULT_N = [16, 64, 256];
const BOUNDARY_N = [9, 10];
const STRATEGIES = ["when", "dispatch"] as const;
const TARGET_LABELS = ["first", "mid", "last", "miss"] as const;

type Strategy = (typeof STRATEGIES)[number];
type TargetLabel = (typeof TARGET_LABELS)[number];

interface StaticCost {
  triggers: number;
  conditions: number;
  actions: number;
  totalBytes: number;
}

interface ExecCost {
  triggers: number;
  conditions: number;
  actions: number;
}

interface Measurement {
  n: number;
  strategy: Strategy;
  static: StaticCost;
  executed: Record<TargetLabel, ExecCost>;
}

function build(n: number, target: number, strategy: Strategy) {
  return buildModule(() => {
    const gate = Var(0, undefined, undefined, "page");
    const value = Var(target);
    const sink = Var(0, undefined, undefined, "page");

    const body = (): void => {
      sink.set(1);
    };
    const cases: Array<readonly [number, () => void]> = Array.from(
      { length: n },
      (_, key) => [key, body] as const,
    );

    when(gate.eq(0), () => {
      if (strategy === "when") {
        for (const [key, arm] of cases) when(value.eq(key), arm);
      } else {
        dispatch(value, cases);
      }
    });
  });
}

function targets(n: number): Record<TargetLabel, number> {
  return {
    first: 0,
    mid: n >> 1,
    last: n - 1,
    miss: n + 5,
  };
}

function measure(n: number, strategy: Strategy): Measurement {
  const mod = build(n, 0, strategy);
  const cost = runStatic(mod.toJson()).static_cost;
  const executed = {} as Record<TargetLabel, ExecCost>;
  for (const label of TARGET_LABELS) {
    const report = runSim(build(n, targets(n)[label], strategy).toJson(), 1);
    const exec = report.executed_cost;
    executed[label] = {
      triggers: exec.triggers,
      conditions: exec.conditions,
      actions: exec.actions,
    };
  }
  return {
    n,
    strategy,
    static: {
      triggers: cost.triggers,
      conditions: cost.conditions,
      actions: cost.actions,
      totalBytes: cost.total_bytes,
    },
    executed,
  };
}

function compareExec(a: ExecCost, b: ExecCost): number {
  if (a.triggers !== b.triggers) return a.triggers - b.triggers;
  if (a.conditions !== b.conditions) return a.conditions - b.conditions;
  return a.actions - b.actions;
}

function worst(measurement: Measurement): [TargetLabel, ExecCost] {
  const ordered: TargetLabel[] = ["last", "miss", "mid", "first"];
  let best = measurement.executed[TARGET_LABELS[0]];
  for (const label of TARGET_LABELS) {
    const cost = measurement.executed[label];
    if (compareExec(cost, best) > 0) best = cost;
  }
  for (const label of ordered) {
    const cost = measurement.executed[label];
    if (compareExec(cost, best) === 0) return [label, cost];
  }
  throw new Error("unreachable");
}

function formatInt(value: number): string {
  return String(value).replace(/\B(?=(\d{3})+(?!\d))/g, ",");
}

function format2(value: number): string {
  return (Math.round(value * 100) / 100).toFixed(2);
}

function fmtStatic(cost: StaticCost): string {
  return `${cost.triggers}/${cost.conditions}/${cost.actions}/${formatInt(cost.totalBytes)}`;
}

function fmtExec(cost: ExecCost): string {
  return `${cost.triggers}/${cost.conditions}/${cost.actions}`;
}

function padEnd(value: string | number, width: number): string {
  return String(value).padEnd(width);
}

function padStart(value: string | number, width: number): string {
  return String(value).padStart(width);
}

function printTable(
  measurements: Map<string, Measurement>,
  nValues: number[],
): void {
  console.log(
    "static T/C/A/Bytes and worst-case executed T/C/A per due pass (gate included)",
  );
  console.log(
    `${padStart("N", 4)}  ${padEnd("strategy", 9)} ${padEnd("static T/C/A/B", 24)} ` +
      `${padEnd("worst exec T/C/A", 16)} ${padEnd("key", 9)} ` +
      `${padStart("B/seg", 8)} ${padStart("C/seg", 7)}`,
  );
  for (const n of nValues) {
    for (const strategy of STRATEGIES) {
      const m = measurements.get(`${n}:${strategy}`)!;
      const [label, cost] = worst(m);
      const key = targets(n)[label];
      console.log(
        `${padStart(n, 4)}  ${padEnd(strategy, 9)} ${padEnd(fmtStatic(m.static), 24)} ` +
          `${padEnd(fmtExec(cost), 16)} ${padEnd(`${label} (${key})`, 9)} ` +
          `${padStart(formatInt(Math.round(m.static.totalBytes / n)), 8)} ` +
          `${padStart(format2(cost.conditions / n), 7)}`,
      );
    }
  }
}

function printComparison(
  measurements: Map<string, Measurement>,
  nValues: number[],
): void {
  console.log();
  console.log(
    "dispatch - when (negative exec delta is better; static is one-time)",
  );
  console.log(
    `${padStart("N", 4)}  ${padEnd("lowering", 8)} ${padStart("dT", 5)} ` +
      `${padStart("dB", 10)} ${padStart("dExecT", 7)} ${padStart("dExecC", 7)} ` +
      `${padStart("dExecA", 7)} ${padStart("saved C", 8)} ${padStart("B/savedC", 10)}`,
  );
  for (const n of nValues) {
    const base = measurements.get(`${n}:when`)!;
    const cand = measurements.get(`${n}:dispatch`)!;
    const baseWorst = worst(base)[1];
    const candWorst = worst(cand)[1];
    const saved = baseWorst.conditions - candWorst.conditions;
    const lowering = n >= 10 ? "tree" : "chain";
    const perSaved =
      saved > 0
        ? formatInt(
            Math.round((cand.static.totalBytes - base.static.totalBytes) / saved),
          )
        : "-";
    console.log(
      `${padStart(n, 4)}  ${padEnd(lowering, 8)} ` +
        `${padStart(cand.static.triggers - base.static.triggers, 5)} ` +
        `${padStart(formatInt(cand.static.totalBytes - base.static.totalBytes), 10)} ` +
        `${padStart(candWorst.triggers - baseWorst.triggers, 7)} ` +
        `${padStart(candWorst.conditions - baseWorst.conditions, 7)} ` +
        `${padStart(candWorst.actions - baseWorst.actions, 7)} ` +
        `${padStart(saved, 8)} ${padStart(perSaved, 10)}`,
    );
  }
}

function recommendation(
  measurements: Map<string, Measurement>,
  nValues: number[],
): string {
  const wins = nValues.filter(
    (n) =>
      worst(measurements.get(`${n}:dispatch`)!)[1].conditions <
      worst(measurements.get(`${n}:when`)!)[1].conditions,
  );
  const firstTreeWin = wins.length > 0 ? Math.min(...wins) : null;
  const boundary = nValues.includes(9) && nValues.includes(10);
  let boundaryText: string;
  if (boundary) {
    const chainWhen = worst(measurements.get("9:when")!)[1];
    const chainDispatch = worst(measurements.get("9:dispatch")!)[1];
    const treeWhen = worst(measurements.get("10:when")!)[1];
    const treeDispatch = worst(measurements.get("10:dispatch")!)[1];
    boundaryText =
      "At N=9 dispatch still uses the else-chain: executed " +
      `${fmtExec(chainDispatch)} vs sibling when ${fmtExec(chainWhen)} ` +
      "(same shape, more static). At N=10 the tree first wins the " +
      `due-pass conditions (${fmtExec(treeDispatch)} vs ` +
      `${fmtExec(treeWhen)}), which is ADR-0025's threshold. `;
  } else {
    boundaryText =
      "The construct itself switches from the else-chain to the tree at " +
      "N=10 (ADR-0025); rerun with --boundary to reproduce N=9/10. ";
  }
  const first =
    firstTreeWin !== null && !BOUNDARY_N.includes(firstTreeWin)
      ? `The first measured tree win is N=${firstTreeWin}. `
      : "";
  return (
    "Recommendation: hybrid — sibling `when` below the crossover, " +
    "`dispatch`'s BST above it. Keep the current sibling-`when` play " +
    "dispatch for small loops (N <= 9), where the chain and `dispatch` have " +
    "the same due-pass path but `dispatch` pays extra static bytes. Use the " +
    "BST once its O(log N) due-pass path is material; in the measured " +
    "soundlooper shape that is N >= 16, with N=10..15 a gray band (the tree " +
    "saves a few conditions but adds static triggers and ~8 executed " +
    "actions). " +
    boundaryText +
    first +
    "Static cost is one-time and runtime cost is per due pass: the tree " +
    "saves N - log2(N) - 1 worst-case conditions and N - 2*log2(N) - 1 " +
    "evaluated triggers at N >= 16, but adds 2N - 2 static triggers " +
    "(roughly 4.8 KB per saved condition). For very large loops (N " +
    "approaching 1,000) the static premium approaches 4.8 MB, so adoption " +
    "needs a map-size budget. No production change is made here; an " +
    "adoption would be a follow-up that amends ADR-0023 section 7."
  );
}

function parseArgs(argv: string[]): { nValues: number[]; boundary: boolean } {
  let nValues = [...DEFAULT_N];
  let boundary = false;
  for (let i = 0; i < argv.length; i++) {
    const arg = argv[i];
    if (arg === "--boundary") {
      boundary = true;
      continue;
    }
    if (arg === "--n") {
      const values: number[] = [];
      while (i + 1 < argv.length && !argv[i + 1].startsWith("--")) {
        const value = Number(argv[++i]);
        if (!Number.isInteger(value) || value < 1) {
          throw new Error("--n values must be integers >= 1");
        }
        values.push(value);
      }
      if (values.length === 0) throw new Error("--n needs at least one value");
      nValues = values;
      continue;
    }
    if (arg === "--help" || arg === "-h") {
      console.log(
        "usage: node ts/scripts/bench_soundlooper_dispatch.ts " +
          "[--n N [N ...]] [--boundary]",
      );
      process.exit(0);
    }
    throw new Error(`unknown argument: ${arg}`);
  }
  const merged = new Set(nValues);
  if (boundary) for (const n of BOUNDARY_N) merged.add(n);
  return { nValues: [...merged].sort((a, b) => a - b), boundary };
}

function main(): void {
  const { nValues } = parseArgs(process.argv.slice(2));
  console.log(`soundlooper dispatch benchmark: N=[${nValues.join(", ")}]`);
  const measurements = new Map<string, Measurement>();
  for (const n of nValues) {
    for (const strategy of STRATEGIES) {
      measurements.set(`${n}:${strategy}`, measure(n, strategy));
    }
  }
  printTable(measurements, nValues);
  printComparison(measurements, nValues);
  console.log();
  console.log(recommendation(measurements, nValues));
}

// Only run when executed directly; importing this module (e.g. from a test or
// another benchmark) must not start a measurement run.
const invokedDirectly =
  process.argv[1] !== undefined &&
  path.resolve(process.argv[1]) === fileURLToPath(import.meta.url);
if (invokedDirectly) {
  main();
}
