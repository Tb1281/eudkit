import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

import * as ts from "../index.ts";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = path.resolve(__dirname, "..", "..");
const SPEC_PATH = path.join(REPO_ROOT, "schema", "stock-rows.json");
const GOLDENS_PATH = path.join(REPO_ROOT, "tests", "stock-row-goldens.json");
const DOCS_PATH = path.join(REPO_ROOT, "docs", "stock-triggers.md");

type SectionName = "conditions" | "actions" | "extensions" | "records";

interface Validator {
  readonly kind: string;
  readonly label?: string;
  readonly values?: readonly number[];
  readonly min?: number;
  readonly max?: number;
  readonly multipleOf?: number;
  readonly length?: number;
}

interface SpecParam {
  readonly name: string;
  readonly ts: string;
  readonly field: string;
  readonly chkField: string | null;
  readonly encoding: string;
  readonly validator: Validator;
  readonly default?: number;
  readonly targetForm?: string;
}

interface SpecRow {
  readonly id: string;
  readonly kind: "condition" | "action" | "record";
  readonly chkType: number | null;
  readonly class: "stock" | "eudkit extension";
  readonly defaultFlags: number | null;
  readonly special: boolean;
  readonly handWritten: boolean;
  readonly specialReason?: string;
  readonly doc: string;
  readonly params: readonly SpecParam[];
}

interface Spec {
  readonly format: number;
  readonly name: string;
  readonly hirVersion: string;
  readonly sections: Readonly<Record<SectionName, readonly SpecRow[]>>;
  readonly counts: Readonly<Record<SectionName, number>>;
  readonly assumptions: readonly string[];
}

type ArgSpec =
  | { readonly kind: "int"; readonly value: number }
  | { readonly kind: "str"; readonly value: string }
  | { readonly kind: "bool"; readonly value: boolean }
  | { readonly kind: "uprp"; readonly options: Readonly<Record<string, number | boolean>> }
  | {
      readonly kind: "fieldref";
      readonly varId: number;
      readonly byteOffset: number;
      readonly size: number;
    }
  | { readonly kind: "raw"; readonly value: readonly number[] };

interface Diagnostic {
  readonly case: string;
  readonly args: readonly ArgSpec[];
  readonly code: string;
  readonly message: string;
}

interface GoldenRow {
  readonly id: string;
  readonly section: "conditions" | "actions" | "extensions";
  readonly kind: "condition" | "action";
  readonly class: "stock" | "eudkit extension";
  readonly args: readonly ArgSpec[];
  readonly hir: Record<string, unknown>;
  readonly diagnostics: readonly Diagnostic[];
}

interface GoldenFile {
  readonly format: number;
  readonly hirVersion: string;
  readonly capturedFrom: string;
  readonly note: string;
  readonly rows: readonly GoldenRow[];
  readonly record: {
    readonly id: string;
    readonly args: readonly ArgSpec[];
    readonly record: readonly number[];
    readonly diagnostics: readonly {
      readonly case: string;
      readonly options: Readonly<Record<string, number | boolean>>;
      readonly code: string;
      readonly message: string;
    }[];
  };
}

const spec = JSON.parse(fs.readFileSync(SPEC_PATH, "utf8")) as Spec;
const goldens = JSON.parse(fs.readFileSync(GOLDENS_PATH, "utf8")) as GoldenFile;
const docs = fs.readFileSync(DOCS_PATH, "utf8");

const tsApi = ts as unknown as Record<string, unknown>;
const allRows = (): readonly SpecRow[] => [
  ...spec.sections.conditions,
  ...spec.sections.actions,
  ...spec.sections.extensions,
  ...spec.sections.records,
];

function helper(name: string): (...args: unknown[]) => unknown {
  const fn = tsApi[name];
  assert.equal(typeof fn, "function", `${name} must be a TypeScript export`);
  return fn as (...args: unknown[]) => unknown;
}

const recordRow = (): SpecRow => {
  const row = spec.sections.records.find((candidate) => candidate.id === "UnitProperty");
  assert.ok(row, "the spec must declare the UnitProperty record");
  return row;
};

function buildUnitProperty(options: Readonly<Record<string, number | boolean>>): unknown {
  const row = recordRow();
  const tsOptions: Record<string, number | boolean> = {};
  for (const param of row.params) {
    if (param.name in options) {
      tsOptions[param.ts] = options[param.name]!;
    }
  }
  return ts.UnitProperty(tsOptions);
}

function buildArg(arg: ArgSpec): unknown {
  switch (arg.kind) {
    case "int":
    case "str":
    case "bool":
      return arg.value;
    case "uprp":
      return buildUnitProperty(arg.options);
    case "fieldref":
      return new ts.TriggerFieldRef(
        -1,
        arg.byteOffset,
        arg.size as 1 | 2 | 4,
        0,
        arg.varId,
      );
    case "raw":
      return arg.value;
  }
}

function buildArgs(args: readonly ArgSpec[]): unknown[] {
  return args.map(buildArg);
}

function emit(row: SpecRow, args: readonly ArgSpec[]): unknown {
  return helper(row.id)(...buildArgs(args));
}

function errorDetail(error: unknown): { code: string; message: string } {
  assert.ok(error instanceof ts.EudslError, `expected EudslError, got ${String(error)}`);
  return {
    code: error.code,
    message: error.message.slice(error.code.length + 2),
  };
}

function expectDiagnostic(
  row: SpecRow,
  args: readonly ArgSpec[],
  diagnostic: { readonly code: string; readonly message: string },
): void {
  try {
    emit(row, args);
  } catch (error) {
    assert.deepEqual(errorDetail(error), {
      code: diagnostic.code,
      message: diagnostic.message,
    });
    return;
  }
  assert.fail(`${row.id} must reject ${JSON.stringify(args)}`);
}

function sectionLines(heading: string): string[] {
  const lines = docs.split("\n");
  const start = lines.findIndex((line) => line === `## ${heading}`);
  assert.notEqual(start, -1, `docs must have a "${heading}" section`);
  const out: string[] = [];
  for (let i = start + 1; i < lines.length && !lines[i]!.startsWith("## "); i++) {
    out.push(lines[i]!);
  }
  return out;
}

function extensionHelperRows(): string[] {
  return sectionLines("Extension helpers").flatMap((line) => {
    const match = /^\|\s*`([^`]+)`\s*\|.*\|\s*eudkit extension\s*\|$/.exec(line);
    return match === null ? [] : [match[1]!];
  });
}

function targetFormRows(): Array<{ helper: string; params: string[] }> {
  return sectionLines("Extension target forms").flatMap((line) => {
    const match = /^\|\s*`([^`]+)`\s*\|\s*`([^`]*)`\s*\|\s*eudkit extension\s*\|$/.exec(line);
    if (match === null) {
      return [];
    }
    const raw = match[2]!;
    return [
      {
        helper: match[1]!,
        params: raw === "—" ? [] : raw.split(",").map((part) => part.trim()),
      },
    ];
  });
}

function splitParams(raw: string): string[] {
  return raw === "—" ? [] : raw.split(",").map((part) => part.trim());
}

function referenceRows(heading: string): Array<{
  id: number;
  name: string;
  params: string[];
  flags: number;
  klass: string;
}> {
  return sectionLines(heading).flatMap((line) => {
    const match =
      /^\|\s*(\d+)\s*\|\s*`([^`]+)`\s*\|\s*`([^`]*)`\s*\|\s*(\d+)\s*\|\s*([a-z ]+?)\s*\|$/.exec(
        line,
      );
    return match === null
      ? []
      : [
          {
            id: Number(match[1]),
            name: match[2]!,
            params: splitParams(match[3]!),
            flags: Number(match[4]),
            klass: match[5]!,
          },
        ];
  });
}

function stringSlotRows(): Array<{
  helper: string;
  param: string;
  hir: string;
  chk: string;
}> {
  return sectionLines("String parameter slots").flatMap((line) => {
    const match =
      /^\|\s*`([^`]+)`\s*\|\s*`([^`]+)`\s*\|\s*`([^`]+)`\s*\|\s*`([^`]+)`\s*\|$/.exec(
        line,
      );
    return match === null
      ? []
      : [
          {
            helper: match[1]!,
            param: match[2]!,
            hir: match[3]!,
            chk: match[4]!,
          },
        ];
  });
}

function functionParams(fn: (...args: unknown[]) => unknown): Array<{
  name: string;
  default?: string;
}> {
  const source = fn.toString();
  const open = source.indexOf("(");
  assert.notEqual(open, -1, `${fn.name} must print a parameter list`);
  let depth = 0;
  let close = open;
  for (let i = open; i < source.length; i++) {
    if (source[i] === "(") depth++;
    if (source[i] === ")") {
      depth--;
      if (depth === 0) {
        close = i;
        break;
      }
    }
  }
  const raw = source.slice(open + 1, close);
  const parts: string[] = [];
  let current = "";
  depth = 0;
  for (const char of raw) {
    if ("([{<".includes(char)) depth++;
    if (")]}>".includes(char)) depth--;
    if (char === "," && depth === 0) {
      parts.push(current);
      current = "";
    } else {
      current += char;
    }
  }
  if (current.trim() !== "") parts.push(current);
  return parts.map((part) => {
    const match = /^(\w+)(?:\s*=\s*(.+))?$/.exec(part.trim());
    assert.ok(match, `${fn.name} parameter ${part}`);
    return match[2] === undefined
      ? { name: match[1]! }
      : { name: match[1]!, default: match[2]!.trim() };
  });
}

test("spec declares all 22 stock conditions and 57 stock actions in row order", () => {
  assert.equal(spec.format, 1);
  assert.equal(spec.hirVersion, "3.2");
  assert.equal(spec.sections.conditions.length, 22);
  assert.equal(spec.sections.actions.length, 57);
  assert.equal(spec.sections.extensions.length, 13);
  assert.equal(spec.sections.records.length, 1);
  assert.equal(spec.counts.conditions, 22);
  assert.equal(spec.counts.actions, 57);
  assert.equal(spec.counts.extensions, 13);
  assert.equal(spec.counts.records, 1);

  const stock = [...spec.sections.conditions, ...spec.sections.actions];
  assert.equal(stock.length, 79);
  assert.equal(stock.filter((row) => row.class === "stock").length, 79);
  assert.equal(spec.sections.extensions.filter((row) => row.class === "eudkit extension").length, 13);

  assert.deepEqual(
    spec.sections.conditions.map((row) => row.chkType),
    Array.from({ length: 23 }, (_, index) => index + 1).filter((id) => id !== 13),
    "condition types are exactly 1..23 without 13",
  );
  assert.deepEqual(
    spec.sections.actions.map((row) => row.chkType),
    Array.from({ length: 57 }, (_, index) => index + 1),
    "action types are exactly 1..57",
  );
  assert.deepEqual(
    spec.sections.extensions.map((row) => row.id),
    extensionHelperRows(),
    "extension spec order must match the documented extension helpers",
  );
});

test("spec rows match the current TypeScript helper signatures", () => {
  for (const row of allRows()) {
    const fn = helper(row.id);
    if (row.id === "UnitProperty") {
      assert.equal(fn.length, 0, "UnitProperty takes one defaulted options object");
      continue;
    }
    const params = functionParams(fn);
    assert.deepEqual(
      params.map((param) => param.name),
      row.params.map((param) => param.ts),
      `${row.id} TypeScript parameter order`,
    );
    for (const [index, param] of row.params.entries()) {
      const actualDefault = params[index]!.default;
      if (param.default === undefined) {
        assert.equal(actualDefault, undefined, `${row.id}.${param.ts} default`);
        continue;
      }
      assert.ok(actualDefault !== undefined, `${row.id}.${param.ts} default`);
      const trimmed = actualDefault.trim();
      const resolved = /^[+-]?(?:0[xX][0-9a-fA-F]+|\d+(?:\.\d+)?)$/.test(trimmed)
        ? Number(trimmed)
        : tsApi[trimmed];
      assert.equal(resolved, param.default, `${row.id}.${param.ts} default`);
    }
  }
});

test("the generated reference tables match the spec row for row", () => {
  for (const [heading, section] of [
    ["Conditions (22)", spec.sections.conditions],
    ["Actions (57)", spec.sections.actions],
  ] as const) {
    const docRows = referenceRows(heading);
    assert.deepEqual(
      docRows.map((row) => row.id),
      section.map((row) => row.chkType),
      `${heading} row order and IDs`,
    );
    for (const [index, doc] of docRows.entries()) {
      const row = section[index]!;
      assert.equal(doc.name, row.id, `${row.id} docs helper name`);
      assert.deepEqual(
        doc.params,
        row.params.map((param) => param.name),
        `${row.id} parameter metadata`,
      );
      assert.equal(doc.flags, row.defaultFlags, `${row.id} default flags`);
      assert.equal(doc.klass, row.class, `${row.id} class`);
    }
  }
});

test("the generated string-slot table matches the spec", () => {
  const expected = [
    ...spec.sections.conditions,
    ...spec.sections.actions,
    ...spec.sections.extensions,
  ].flatMap((row) =>
    row.params
      .filter((param) => param.encoding === "string")
      .map((param) => ({
        helper: row.id,
        param: param.name,
        hir: param.field,
        chk: param.chkField,
      })),
  );
  const byKey = (row: { helper: string; param: string }) =>
    `${row.helper}.${row.param}`;
  assert.deepEqual(
    stringSlotRows().sort((a, b) => byKey(a).localeCompare(byKey(b))),
    expected.sort((a, b) => byKey(a).localeCompare(byKey(b))),
    "the docs string-slot table must derive from the spec",
  );
});

test("only five special rows exist and all are marked hand-written", () => {
  const special = allRows()
    .filter((row) => row.special)
    .map((row) => row.id)
    .sort();
  const handWritten = allRows()
    .filter((row) => row.handWritten)
    .map((row) => row.id)
    .sort();
  assert.deepEqual(special, [
    "CreateUnitWithProperties",
    "Deaths",
    "SetDeaths",
    "SetStringDword",
    "UnitProperty",
  ]);
  assert.deepEqual(handWritten, special);
  assert.ok(special.length <= 5, "at most five special rows");
  for (const row of allRows()) {
    if (row.special) {
      assert.ok(row.specialReason, `${row.id} must explain why it is special`);
    } else {
      assert.equal(row.specialReason, undefined, `${row.id} is not special`);
    }
  }
});

test("only Deaths and SetDeaths carry an extension target form", () => {
  const targetRows = allRows()
    .filter((row) => row.params.some((param) => param.targetForm !== undefined))
    .map((row) => row.id);
  assert.deepEqual(targetRows, ["Deaths", "SetDeaths"]);
  assert.deepEqual(
    spec.sections.conditions
      .find((row) => row.id === "Deaths")!
      .params.filter((param) => param.targetForm !== undefined)
      .map((param) => [param.name, param.targetForm]),
    [
      ["player", "PlayerOrFieldTarget"],
      ["amount", "ValueOrPtrTarget"],
    ],
  );
  assert.deepEqual(
    spec.sections.actions
      .find((row) => row.id === "SetDeaths")!
      .params.filter((param) => param.targetForm !== undefined)
      .map((param) => [param.name, param.targetForm]),
    [
      ["player", "PlayerOrFieldTarget"],
      ["value", "ValueOrPtrTarget"],
    ],
  );
  assert.deepEqual(
    targetFormRows().map((row) => [row.helper, row.params]),
    [
      ["Deaths", ["player", "amount"]],
      ["SetDeaths", ["player", "value"]],
    ],
    "docs target-form table must match the spec",
  );
});

test("spec CHK fields match the documented HIR-to-CHK alias map", () => {
  const conditionAliases: Record<string, string> = {
    locid: "locid",
    player: "player",
    amount: "amount",
    unit: "unit",
    comparison: "comparison",
    condtype: "condtype",
    restype: "restype",
    flags: "flags",
    eudx: "eudx",
    padding: "padding",
  };
  const actionAliases: Record<string, string | null> = {
    mask: "locid1",
    strid: "strid",
    wavid: "wavid",
    time: "time",
    dest: "player1",
    value: "player2",
    modifier: "amount",
    unit: "unit",
    flags: "flags",
    padding: "padding",
    eudx: "eudx",
    text: "strid",
    wav: "wavid",
    uprp: null,
    str_index: "strid",
    "relocs.target_string": "strid",
    "relocs.target_offset": "strid",
    relocs: "player1",
  };
  for (const row of allRows()) {
    if (row.kind === "record") {
      assert.equal(row.chkType, null, "record rows have no CHK type");
      continue;
    }
    assert.ok(Number.isInteger(row.chkType), `${row.id} CHK type`);
    assert.ok(Number.isInteger(row.defaultFlags), `${row.id} default flags`);
    const aliases = row.kind === "action" ? actionAliases : conditionAliases;
    for (const param of row.params) {
      assert.ok(param.name in aliases || param.field in aliases, `${row.id}.${param.name} field`);
      assert.equal(
        param.chkField,
        aliases[param.field] ?? null,
        `${row.id}.${param.name} CHK field for ${param.field}`,
      );
    }
  }
});

test("goldens cover exactly the 92 spec rows in order and keep the frozen rows", () => {
  assert.equal(goldens.format, 1);
  // The oracle is frozen at its capture-time version: a row carries no HIR
  // version field, so the 3.1 -> 3.2 bump (ADR 0021) leaves the payloads
  // valid and the file untouched.
  assert.equal(goldens.hirVersion, "3.1");
  assert.equal(ts.HIR_VERSION, "3.2");
  assert.equal(goldens.rows.length, 92);
  assert.deepEqual(
    goldens.rows.map((row) => row.id),
    [
      ...spec.sections.conditions.map((row) => row.id),
      ...spec.sections.actions.map((row) => row.id),
      ...spec.sections.extensions.map((row) => row.id),
    ],
    "golden row order must match the spec",
  );
  for (const row of goldens.rows) {
    const specRow = allRows().find((candidate) => candidate.id === row.id);
    assert.ok(specRow, `${row.id} must be in the spec`);
    const expectedSection = spec.sections.extensions.includes(specRow)
      ? "extensions"
      : specRow.kind === "condition"
        ? "conditions"
        : "actions";
    assert.equal(row.section, expectedSection, `${row.id} section`);
    assert.equal(row.kind, specRow.kind, `${row.id} kind`);
    assert.equal(row.class, specRow.class, `${row.id} class`);
  }
});

test("every golden row reproduces its frozen HIR byte for byte", () => {
  for (const golden of goldens.rows) {
    const row = allRows().find((candidate) => candidate.id === golden.id)!;
    const emitted = emit(row, golden.args);
    assert.equal(
      JSON.stringify(emitted),
      JSON.stringify(golden.hir),
      `${golden.id} HIR drifted from the frozen golden`,
    );
  }
});

test("every golden diagnostic reproduces its code, message, and validation order", () => {
  for (const golden of goldens.rows) {
    const row = allRows().find((candidate) => candidate.id === golden.id)!;
    for (const diagnostic of golden.diagnostics) {
      expectDiagnostic(row, diagnostic.args, diagnostic);
    }
  }
});

test("UnitProperty record golden and diagnostics are frozen", () => {
  recordRow();
  assert.equal(goldens.record.id, "UnitProperty");
  const options = goldens.record.args[0]!;
  assert.equal(options.kind, "uprp");
  assert.ok(options.kind === "uprp");
  assert.deepEqual(
    buildUnitProperty(options.options),
    goldens.record.record,
    "UnitProperty record drifted from the frozen golden",
  );
  for (const diagnostic of goldens.record.diagnostics) {
    const merged = { ...options.options, ...diagnostic.options };
    try {
      buildUnitProperty(merged);
      assert.fail(`UnitProperty must reject ${JSON.stringify(diagnostic.options)}`);
    } catch (error) {
      assert.deepEqual(errorDetail(error), {
        code: diagnostic.code,
        message: diagnostic.message,
      });
    }
  }
});

test("UnitProperty boolean flags accept true, false, and undefined", () => {
  for (const param of recordRow().params) {
    if (param.validator.kind !== "boolean") continue;
    for (const value of [true, false, undefined]) {
      assert.doesNotThrow(
        () => ts.UnitProperty({ [param.ts]: value }),
        `UnitProperty.${param.ts} must accept ${String(value)}`,
      );
    }
    assert.throws(
      () => ts.UnitProperty({ [param.ts]: 1 as unknown as boolean }),
      ts.EudslError,
      `UnitProperty.${param.ts} must reject a non-boolean`,
    );
  }
});

test("spec validators accept their documented domains and reject outside them", () => {
  for (const golden of goldens.rows) {
    const row = allRows().find((candidate) => candidate.id === golden.id)!;
    for (const [index, param] of row.params.entries()) {
      const validator = param.validator;
      if (validator.kind === "none") continue;
      const base = golden.args.slice();
      const replace = (value: unknown): unknown[] => {
        const args = buildArgs(base);
        args[index] = value;
        return args;
      };
      const accepts = (value: unknown): void => {
        helper(row.id)(...replace(value));
      };
      const rejects = (value: unknown, code: string): void => {
        try {
          helper(row.id)(...replace(value));
        } catch (error) {
          const detail = errorDetail(error);
          assert.equal(detail.code, code, `${row.id}.${param.name}`);
          assert.ok(
            detail.message.includes(`${validator.label} must be`),
            `${row.id}.${param.name} must use label ${validator.label}, got ${detail.message}`,
          );
          return;
        }
        assert.fail(`${row.id}.${param.name} accepted invalid ${String(value)}`);
      };
      switch (validator.kind) {
        case "enum":
          for (const value of validator.values!) accepts(value);
          rejects(Math.max(...validator.values!) + 1000, "EUDSL-BOUNDS");
          break;
        case "range": {
          accepts(validator.min);
          if (validator.max !== undefined) accepts(validator.max);
          const invalid =
            validator.min! > 0
              ? validator.min! - 1
              : validator.max === undefined
                ? -1
                : validator.max + 1;
          rejects(invalid, "EUDSL-BOUNDS");
          break;
        }
        case "count":
          for (const value of [0, 1, 255]) accepts(value);
          rejects(256, "EUDSL-BOUNDS");
          break;
        case "multiple":
          accepts(validator.multipleOf!);
          accepts(validator.multipleOf! * 2);
          rejects(validator.multipleOf! + 1, "EUDSL-BOUNDS");
          break;
        case "record":
          rejects([0, ...Array(18).fill(0)], "EUDSL-TYPE");
          break;
        case "boolean":
          accepts(true);
          accepts(false);
          rejects(1, "EUDSL-TYPE");
          break;
        default:
          assert.fail(`${row.id}.${param.name} has unknown validator ${validator.kind}`);
      }
    }
  }
});

test("Deaths and SetDeaths extension target forms emit relocations", () => {
  let sourceId = -1;
  const mod = ts.buildModule(() => {
    const source = new ts.Trigger({ conds: [], acts: [], preserved: false });
    sourceId = source.id;
    new ts.Trigger({
      conds: [ts.Deaths(source.act(0).value, ts.AtLeast, source.act(1).dest, 0)],
      acts: [ts.SetDeaths(source.act(2).dest, ts.SetTo, source.act(3).value, 0)],
      preserved: true,
    });
  });
  assert.notEqual(sourceId, -1);
  const target = mod.triggers.find((trigger) => trigger.id !== sourceId);
  assert.ok(target, "the second trigger must be emitted");
  assert.deepEqual(
    target.conds[0]!.relocs?.map((reloc) => [
      reloc.field_offset,
      reloc.target_trigger,
      reloc.mode,
    ]),
    [
      [4, sourceId, 1],
      [8, sourceId, 4],
    ],
    "Deaths relocates its player and amount extension targets",
  );
  assert.deepEqual(
    target.acts[0]!.relocs?.map((reloc) => [
      reloc.field_offset,
      reloc.target_trigger,
      reloc.mode,
    ]),
    [
      [16, sourceId, 1],
      [20, sourceId, 4],
    ],
    "SetDeaths relocates its player and value extension targets",
  );
});
