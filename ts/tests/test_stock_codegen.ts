/**
 * The checked-in stock emitter must be exactly what `generate:stock` renders
 * now, and it must be the only home for the non-special helper bodies.
 *
 * This is a freshness + provenance test, not a golden test: it runs the real
 * generator against `schema/stock-rows.json`, so editing the spec without
 * regenerating fails here instead of drifting.
 */
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

import * as ts from "../index.ts";
import { GENERATED_FILE, generateStockSource } from "../scripts/generate-stock.ts";
import type { Spec, SpecRow } from "../scripts/generate-stock.ts";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = path.resolve(HERE, "..", "..");
const SPEC_PATH = path.join(REPO_ROOT, "schema", "stock-rows.json");
const TS_ROOT = path.join(HERE, "..");
const HIR_PATH = path.join(TS_ROOT, "hir.ts");
const GENERATED_NAME = "stock.generated.ts";

const spec = JSON.parse(fs.readFileSync(SPEC_PATH, "utf8")) as Spec;
const helperRows = (): readonly SpecRow[] => [
  ...spec.sections.conditions!,
  ...spec.sections.actions!,
  ...spec.sections.extensions!,
];
const specialRows = (): readonly SpecRow[] => helperRows().filter((row) => row.special);
const generatedRows = (): readonly SpecRow[] => helperRows().filter((row) => !row.handWritten);

function declares(source: string, rowId: string): boolean {
  return new RegExp(`export function ${rowId}\\(`).test(source);
}

/** Every checked-in TypeScript module except the generated stock surface. */
function handWrittenModules(): string[] {
  return fs
    .readdirSync(TS_ROOT)
    .filter((name) => name.endsWith(".ts") && name !== GENERATED_NAME)
    .sort();
}

function declarations(rowId: string): string[] {
  return handWrittenModules().filter((name) =>
    declares(fs.readFileSync(path.join(TS_ROOT, name), "utf-8"), rowId),
  );
}

const SYNTHETIC_ROW: SpecRow = {
  id: "SyntheticProbe",
  kind: "condition",
  chkType: 99,
  class: "stock",
  defaultFlags: 0,
  special: false,
  handWritten: false,
  doc: "Synthetic probe condition (condition 99).",
  params: [
    {
      name: "comparison",
      ts: "comparison",
      field: "comparison",
      encoding: "u8",
      validator: { kind: "enum", label: "comparison", values: [0, 1, 10] },
    },
  ],
};

test("ts/stock.generated.ts is up to date", () => {
  const checkedIn = fs.readFileSync(GENERATED_FILE, "utf-8");
  assert.equal(
    checkedIn,
    generateStockSource(),
    "ts/stock.generated.ts drifted; run `npm run generate:stock` from ts/",
  );
});

test("the generated module emits every non-special row exactly once", () => {
  const source = fs.readFileSync(GENERATED_FILE, "utf-8");
  assert.match(source, /Generated from `schema\/stock-rows\.json`/);
  assert.match(source, /Do not edit by hand/);
  assert.equal(generatedRows().length, 88, "88 of the 92 rows are data-driven");
  for (const row of generatedRows()) {
    assert.ok(declares(source, row.id), `${row.id} must be emitted by the generator`);
  }
  for (const row of specialRows()) {
    if (row.kind === "record") continue;
    assert.equal(
      declares(source, row.id),
      false,
      `${row.id} is a special row and must stay hand-written`,
    );
  }
});

test("only special rows remain hand-written in the TypeScript frontend", () => {
  for (const row of generatedRows()) {
    assert.deepEqual(
      declarations(row.id),
      [],
      `${row.id} is data-driven; its body must not stay hand-written in ts/`,
    );
  }
  for (const row of specialRows()) {
    assert.deepEqual(
      declarations(row.id),
      [path.basename(HIR_PATH)],
      `${row.id} is special and stays hand-written in hir.ts`,
    );
  }
});

test("adding a stock row needs only a spec edit and a regenerate", () => {
  const specWithRow: Spec = {
    ...spec,
    sections: {
      ...spec.sections,
      conditions: [...spec.sections.conditions!, SYNTHETIC_ROW],
    },
  };
  const source = generateStockSource(specWithRow);
  assert.match(
    source,
    /export function SyntheticProbe\(comparison: number\): HirCond/,
    "the generator must declare the new helper",
  );
  assert.match(source, /id: "SyntheticProbe"/);
  assert.match(source, /chkType: 99/);
});

test("a failing generated action validates before it queues anything", () => {
  const compiled = ts.buildModule(() => {
    assert.throws(
      () => ts.KillUnitAt(1, 1, 1, 999),
      (error: unknown) => error instanceof ts.EudslError && error.code === "EUDSL-BOUNDS",
    );
    ts.Victory();
  });
  assert.equal(compiled.triggers.length, 1, "the failed row must not emit a trigger");
  assert.equal(compiled.triggers[0]!.acts.length, 1, "only Victory is queued");
  assert.equal(compiled.triggers[0]!.acts[0]!.acttype, 1);
});

test("a late validation failure still leaves the builder empty", () => {
  const compiled = ts.buildModule(() => {
    assert.throws(
      () => ts.SetCountdownTimer(ts.SetTo, -1),
      (error: unknown) => error instanceof ts.EudslError && error.code === "EUDSL-BOUNDS",
    );
  });
  assert.equal(compiled.triggers.length, 0, "SetCountdownTimer must not emit a partial action");
});
