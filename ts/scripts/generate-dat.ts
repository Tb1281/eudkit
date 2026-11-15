/**
 * Generate `ts/dat.generated.ts` from the canonical DAT schemas.
 *
 * The runtime reads field metadata from `schema/layouts/dat-*.json`; this
 * script only generates the TypeScript surface that needs to be static:
 * field-name unions, typed entry interfaces, and `UnitType`-style entry
 * indices. Keeping the symbol rules in `ts/dat-symbols.ts` means the checked
 * file and the runtime `symbols` property share one implementation.
 *
 * Run from `ts/`:
 *
 * ```sh
 * npm run generate:dat
 * ```
 */

import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { datSymbolsFor } from "../dat-symbols.ts";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = path.resolve(HERE, "..", "..");
const SCHEMA_DIR = path.join(REPO_ROOT, "schema", "layouts");
export const GENERATED_FILE = path.join(HERE, "..", "dat.generated.ts");

/** One DAT table exposed by the DSL. */
export interface DatTableCodegen {
  /** Schema basename under `schema/layouts/`. */
  readonly schema: string;
  /** Game data file, used in doc comments. */
  readonly dataFile: string;
  readonly fieldsType: string;
  readonly entryType: string;
  readonly symbolType: string;
}

export const DAT_TABLES: readonly DatTableCodegen[] = [
  {
    schema: "dat-flingy",
    dataFile: "flingy.dat",
    fieldsType: "FlingyFields",
    entryType: "FlingyEntry",
    symbolType: "FlingyType",
  },
  {
    schema: "dat-tech",
    dataFile: "techdata.dat",
    fieldsType: "TechFields",
    entryType: "TechEntry",
    symbolType: "TechType",
  },
  {
    schema: "dat-trgunit",
    dataFile: "units.dat",
    fieldsType: "TrgUnitFields",
    entryType: "TrgUnitEntry",
    symbolType: "UnitType",
  },
  {
    schema: "dat-upgrade",
    dataFile: "upgrades.dat",
    fieldsType: "UpgradeFields",
    entryType: "UpgradeEntry",
    symbolType: "UpgradeType",
  },
  {
    schema: "dat-weapon",
    dataFile: "weapons.dat",
    fieldsType: "WeaponFields",
    entryType: "WeaponEntry",
    symbolType: "WeaponType",
  },
];

interface RawDatField {
  base: number;
  size: number;
  stride: number;
  aliases?: string[];
}

interface RawDatSchema {
  table: string;
  kind: string;
  count: number;
  fields: Record<string, RawDatField>;
  names: Record<string, number>;
}

function loadSchema(table: DatTableCodegen): RawDatSchema {
  const file = path.join(SCHEMA_DIR, `${table.schema}.json`);
  const data = JSON.parse(fs.readFileSync(file, "utf-8")) as RawDatSchema;
  if (data.kind !== "soa") {
    throw new Error(`${table.schema}.json: expected kind 'soa'`);
  }
  if (!Number.isInteger(data.count) || data.count <= 0) {
    throw new Error(`${table.schema}.json: invalid count`);
  }
  if (!data.fields || typeof data.fields !== "object") {
    throw new Error(`${table.schema}.json: missing fields`);
  }
  if (!data.names || typeof data.names !== "object") {
    throw new Error(`${table.schema}.json: missing names`);
  }
  return data;
}

/** `maxHp` -> `max_hp`; `SCV` -> `scv`. */
function snakeCase(name: string): string {
  return name.replace(/(?<!^)(?=[A-Z])/g, "_").toLowerCase();
}

function quote(value: string): string {
  return JSON.stringify(value);
}

function fieldNames(schema: RawDatSchema): string[] {
  return Object.keys(schema.fields);
}

function generateFieldsType(table: DatTableCodegen, schema: RawDatSchema): string {
  const names = fieldNames(schema);
  const lines = names.map((name) => `  | ${quote(name)}`).join("\n");
  return `/** \`${table.dataFile}\` field names (\`schema/layouts/${table.schema}.json\`). */
export type ${table.fieldsType} =
${lines};`;
}

function generateEntryType(table: DatTableCodegen, schema: RawDatSchema): string {
  const names = fieldNames(schema);
  const declared = new Set(names);
  const extraFields: string[] = [];
  const add = (name: string) => {
    if (!declared.has(name)) {
      declared.add(name);
      extraFields.push(name);
    }
  };
  for (const name of names) {
    add(snakeCase(name));
  }
  for (const name of names) {
    for (const alias of schema.fields[name].aliases ?? []) {
      add(alias);
      add(snakeCase(alias));
    }
  }
  const body = extraFields.map((name) => `  readonly ${name}: DatFieldHandle;`).join("\n");
  const summary =
    `/** \`${table.dataFile}\` typed entry. Canonical field names come from ` +
    `\`DatEntry\`; snake_case spellings and schema aliases are listed explicitly. */`;
  if (body.length === 0) {
    return `${summary}\nexport interface ${table.entryType} extends DatEntry<${table.fieldsType}> {}`;
  }
  return `${summary}\nexport interface ${table.entryType} extends DatEntry<${table.fieldsType}> {\n${body}\n}`;
}

function generateSymbols(table: DatTableCodegen, schema: RawDatSchema): string {
  const symbols = datSymbolsFor(schema.names);
  const body = Object.entries(symbols)
    .map(([name, index]) => `  ${name}: ${index},`)
    .join("\n");
  return `/** \`${table.dataFile}\` entry indices (\`schema/layouts/${table.schema}.json\`). */
export const ${table.symbolType} = {
${body}
} as const;

/** Numeric union of every \`${table.dataFile}\` entry index. */
export type ${table.symbolType} =
  (typeof ${table.symbolType})[keyof typeof ${table.symbolType}];`;
}

/** Render the complete generated module without touching the filesystem. */
export function generateDatSource(): string {
  const sections = DAT_TABLES.map((table) => {
    const schema = loadSchema(table);
    return [
      generateFieldsType(table, schema),
      generateEntryType(table, schema),
      generateSymbols(table, schema),
    ].join("\n\n");
  });

  return `/**
 * Generated by \`ts/scripts/generate-dat.ts\` from \`schema/layouts/dat-*.json\`.
 * Do not edit by hand. Regenerate with \`npm run generate:dat\` from \`ts/\`.
 */

import type { DatEntry, DatFieldHandle } from "./dat.ts";

${sections.join("\n\n")}
`;
}

const invokedPath = process.argv[1] ? path.resolve(process.argv[1]) : "";
if (invokedPath === fileURLToPath(import.meta.url)) {
  const source = generateDatSource();
  fs.writeFileSync(GENERATED_FILE, source);
  process.stdout.write(
    `wrote ${path.relative(process.cwd(), GENERATED_FILE)} (${source.length} bytes)\n`,
  );
}
