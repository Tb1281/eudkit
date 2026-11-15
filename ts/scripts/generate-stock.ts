/**
 * Generate `ts/stock.generated.ts` from the stock row spec.
 *
 * `schema/stock-rows.json` describes every stock and eudkit-extension helper
 * row once. This script renders the checked-in data and the explicit helper
 * declarations for every non-special row; the five special rows stay
 * hand-written in `ts/hir.ts`.
 *
 * Run from `ts/`:
 *
 * ```sh
 * npm run generate:stock
 * ```
 */

import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

import {
  ACT_FIELD_ORDER,
  COND_FIELD_ORDER,
  EUDX_MAGIC,
} from "../stock-spec.ts";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = path.resolve(HERE, "..", "..");
const SPEC_PATH = path.join(REPO_ROOT, "schema", "stock-rows.json");

export const GENERATED_FILE = path.join(HERE, "..", "stock.generated.ts");

export interface SpecValidator {
  readonly kind: string;
  readonly label?: string;
  readonly values?: readonly number[];
  readonly min?: number;
  readonly max?: number;
  readonly all?: number;
  readonly multipleOf?: number;
  readonly length?: number;
}

export interface SpecParam {
  readonly name: string;
  readonly ts: string;
  readonly field: string;
  readonly encoding: string;
  readonly validator: SpecValidator;
  readonly default?: number;
}

export interface SpecRow {
  readonly id: string;
  readonly kind: "condition" | "action" | "record";
  readonly chkType: number | null;
  readonly class: "stock" | "eudkit extension";
  readonly defaultFlags: number;
  readonly special: boolean;
  readonly handWritten: boolean;
  readonly doc: string;
  readonly params: readonly SpecParam[];
}

export interface Spec {
  readonly format: number;
  readonly hirVersion: string;
  readonly sections: Readonly<Record<string, readonly SpecRow[]>>;
}

/** Rows whose wire form carries a constant the parameter list cannot name. */
const IMPLICIT_MODIFIERS: Readonly<Record<string, number>> = {
  SetNextPtr: 7, // SetTo
};

const SUPPORTED_ENCODINGS = new Set([
  "u8",
  "u16",
  "u32",
  "u8-truncate",
  "u16-truncate",
  "u32-wrap",
  "mask",
  "epd",
  "epd-from-address",
  "value-or-ptr",
  "ptr",
  "string",
  "string-index",
]);

const SUPPORTED_VALIDATORS = new Set(["none", "range", "enum", "count"]);

/** TypeScript parameter type for one encoding. */
const PARAM_TYPES: Readonly<Record<string, string>> = {
  epd: "PlayerOrFieldTarget",
  "epd-from-address": "number | DbRef",
  "value-or-ptr": "ValueOrPtrTarget",
  ptr: "Trigger | TriggerFieldRef",
  string: "string",
};

function loadSpec(): Spec {
  return JSON.parse(fs.readFileSync(SPEC_PATH, "utf8")) as Spec;
}

function helperRows(spec: Spec): readonly SpecRow[] {
  return [
    ...spec.sections.conditions!,
    ...spec.sections.actions!,
    ...spec.sections.extensions!,
  ];
}

function numberSource(value: number): string {
  if (value === 0xffffffff) return "0xffffffff";
  if (value === EUDX_MAGIC) return "0x4353";
  return String(value);
}

function stringSource(value: string): string {
  return JSON.stringify(value);
}

function validatorSource(validator: SpecValidator): string {
  switch (validator.kind) {
    case "none":
      return `{ kind: "none" }`;
    case "range": {
      const max =
        validator.max === undefined
          ? ""
          : `, max: ${numberSource(validator.max)}`;
      return `{ kind: "range", label: ${stringSource(validator.label!)}, min: ${numberSource(validator.min!)}${max} }`;
    }
    case "enum":
      return `{ kind: "enum", label: ${stringSource(validator.label!)}, values: [${validator.values!.map(numberSource).join(", ")}] }`;
    case "count":
      return `{ kind: "count", label: ${stringSource(validator.label!)}, min: ${numberSource(validator.min!)}, max: ${numberSource(validator.max!)}, all: ${numberSource(validator.all!)} }`;
    default:
      throw new Error(`stock row spec: unsupported validator ${validator.kind}`);
  }
}

function paramSource(param: SpecParam): string {
  return `    { field: ${stringSource(param.field)}, encoding: ${stringSource(param.encoding)}, validator: ${validatorSource(param.validator)} },`;
}

function paramType(param: SpecParam): string {
  return PARAM_TYPES[param.encoding] ?? "number";
}

function validateRow(row: SpecRow): void {
  if (row.kind !== "condition" && row.kind !== "action") {
    throw new Error(`stock row spec: ${row.id} is not a trigger row`);
  }
  if (!Number.isInteger(row.chkType)) {
    throw new Error(`stock row spec: ${row.id} has no CHK type`);
  }
  if (row.special !== row.handWritten) {
    throw new Error(
      `stock row spec: ${row.id} must mark special and handWritten together`,
    );
  }
  const order: readonly string[] =
    row.kind === "condition" ? COND_FIELD_ORDER : ACT_FIELD_ORDER;
  const seen = new Set<string>();
  let sawDefault = false;
  for (const param of row.params) {
    if (!order.includes(param.field)) {
      throw new Error(
        `stock row spec: ${row.id}.${param.name} field ${param.field} has no emit position`,
      );
    }
    if (seen.has(param.field)) {
      throw new Error(
        `stock row spec: ${row.id} repeats HIR field ${param.field}`,
      );
    }
    seen.add(param.field);
    if (!SUPPORTED_ENCODINGS.has(param.encoding)) {
      throw new Error(
        `stock row spec: ${row.id}.${param.name} encoding ${param.encoding}`,
      );
    }
    if (!SUPPORTED_VALIDATORS.has(param.validator.kind)) {
      throw new Error(
        `stock row spec: ${row.id}.${param.name} validator ${param.validator.kind}`,
      );
    }
    if (param.validator.kind === "range" && param.validator.max === undefined) {
      if (param.encoding !== "string-index") {
        throw new Error(
          `stock row spec: ${row.id}.${param.name} range validator needs a max`,
        );
      }
    }
    if (param.validator.kind === "count") {
      // The spec spells the count sentinel (`All`) as `all`.
      if (param.validator.all === undefined) {
        throw new Error(
          `stock row spec: ${row.id}.${param.name} count validator needs a sentinel`,
        );
      }
    }
    if (param.default !== undefined) {
      sawDefault = true;
    } else if (sawDefault) {
      throw new Error(
        `stock row spec: ${row.id} has a required parameter after a defaulted one`,
      );
    }
  }
}

function validateSpec(spec: Spec): readonly SpecRow[] {
  if (spec.format !== 1) {
    throw new Error(`stock row spec: unsupported format ${spec.format}`);
  }
  const rows = helperRows(spec);
  for (const row of rows) {
    if (row.handWritten) {
      if (!row.special) {
        throw new Error(`stock row spec: ${row.id} is hand-written but not special`);
      }
      continue;
    }
    validateRow(row);
  }
  const ids = new Set(rows.map((row) => row.id));
  for (const id of Object.keys(IMPLICIT_MODIFIERS)) {
    if (!ids.has(id)) {
      throw new Error(`stock row spec: implicit modifier names no row: ${id}`);
    }
  }
  return rows;
}

function rowSource(row: SpecRow): string {
  const name = `row${row.id}`;
  const kind = row.kind as "condition" | "action";
  const params = row.params.map(paramSource).join("\n");
  const extras: string[] = [];
  if (row.params.some((param) => param.encoding === "mask")) {
    extras.push(`  eudx: ${numberSource(EUDX_MAGIC)},`);
  }
  const implicitModifier = IMPLICIT_MODIFIERS[row.id];
  if (implicitModifier !== undefined) {
    extras.push(`  modifier: ${numberSource(implicitModifier)},`);
  }
  const extraLines = extras.length === 0 ? "" : `\n${extras.join("\n")}`;
  return `const ${name}: StockRowSpec = {
  id: ${stringSource(row.id)},
  kind: ${stringSource(kind)},
  chkType: ${numberSource(row.chkType as number)},
  flags: ${numberSource(row.defaultFlags)},${extraLines}
  params: [
${params}
  ],
};`;
}

function functionSource(row: SpecRow): string {
  const name = `row${row.id}`;
  const signature = row.params
    .map((param) => {
      const type = paramType(param);
      return param.default === undefined
        ? `${param.ts}: ${type}`
        : `${param.ts}: ${type} = ${numberSource(param.default)}`;
    })
    .join(", ");
  const args = row.params.map((param) => param.ts).join(", ");
  const result = row.kind === "condition" ? "HirCond" : "HirAct";
  const emitter = row.kind === "condition" ? "_emitStockCond" : "_emitStockAct";
  return `/** ${row.doc} */
export function ${row.id}(${signature}): ${result} {
  return ${emitter}(${name}, [${args}]);
}`;
}

/** Render the complete generated module without touching the filesystem. */
export function generateStockSource(spec: Spec = loadSpec()): string {
  const rows = validateSpec(spec).filter((row) => !row.handWritten);
  const data = rows.map((row) => rowSource(row)).join("\n\n");
  const helpers = rows.map(functionSource).join("\n\n");
  return `/**
 * Generated from \`schema/stock-rows.json\` by \`ts/scripts/generate-stock.ts\`.
 * Do not edit by hand. Regenerate with \`npm run generate:stock\` from \`ts/\`.
 *
 * Every non-special stock and eudkit-extension helper is data-driven; the
 * special rows (Deaths, SetDeaths, CreateUnitWithProperties, SetStringDword,
 * UnitProperty) stay hand-written in \`hir.ts\`.
 */

import type { HirAct, HirCond, Trigger } from "./trigger.ts";
import type { TriggerFieldRef } from "./view.ts";
import {
  _emitStockAct,
  _emitStockCond,
  type DbRef,
  type PlayerOrFieldTarget,
  type ValueOrPtrTarget,
} from "./hir.ts";
import type { StockRowSpec } from "./stock-spec.ts";

${data}

${helpers}
`;
}

const invokedPath = process.argv[1] ? path.resolve(process.argv[1]) : "";
if (invokedPath === fileURLToPath(import.meta.url)) {
  const source = generateStockSource();
  fs.writeFileSync(GENERATED_FILE, source);
  process.stdout.write(
    `wrote ${path.relative(process.cwd(), GENERATED_FILE)} (${source.length} bytes)\n`,
  );
}
