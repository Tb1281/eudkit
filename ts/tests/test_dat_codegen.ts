/**
 * The checked-in DAT codegen must match what `generate:dat` renders now.
 *
 * This is a freshness test, not a golden test: it runs the real generator
 * against `schema/layouts/dat-*.json`, so editing a schema without
 * regenerating fails here instead of drifting.
 */
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

import {
  DAT_TABLES,
  GENERATED_FILE,
  generateDatSource,
} from "../scripts/generate-dat.ts";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const REPO_SCHEMA_DIR = path.join(HERE, "..", "..", "schema", "layouts");

test("ts/dat.generated.ts is up to date", () => {
  const checkedIn = fs.readFileSync(GENERATED_FILE, "utf-8");
  assert.equal(
    checkedIn,
    generateDatSource(),
    "ts/dat.generated.ts drifted; run `npm run generate:dat` from ts/",
  );
});

test("every canonical DAT schema has a generated table", () => {
  const schemaFiles = fs
    .readdirSync(REPO_SCHEMA_DIR)
    .filter((file) => file.startsWith("dat-") && file.endsWith(".json"))
    .sort();
  const generatedFiles = DAT_TABLES.map((table) => `${table.schema}.json`).sort();
  assert.deepEqual(generatedFiles, schemaFiles);
});

test("generated symbols expose the typed accessor surface", () => {
  const source = generateDatSource();
  for (const table of DAT_TABLES) {
    assert.match(source, new RegExp(`export type ${table.fieldsType} =`));
    assert.match(source, new RegExp(`export interface ${table.entryType}`));
    assert.match(source, new RegExp(`export const ${table.symbolType} =`));
  }
});
