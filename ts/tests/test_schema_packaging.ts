/**
 * Packaging tests: the schema layouts shipped inside the `eudkit` package.
 *
 * The installed npm tarball must work outside the repo tree, so
 * `ts/schema/layouts/` ships in the tarball (included by the `files`
 * allowlist in package.json) and `locateSchemaFile` checks it first. These tests pin that: the
 * packaged copies exist, parse, and stay byte-identical to the repo
 * canonical files in `schema/layouts/` (the LSP build script's source of
 * truth). When the canonical directory is absent (installed-package
 * environment), the comparison is skipped, but the packaged-copy checks
 * still run.
 */
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

const __dirname_here = path.dirname(fileURLToPath(import.meta.url));
const PACKAGED_DIR = path.join(__dirname_here, "..", "schema", "layouts");
const CANONICAL_DIR = path.join(__dirname_here, "..", "..", "schema", "layouts");

test("packaged layouts exist and parse", () => {
  const files = fs.readdirSync(PACKAGED_DIR).filter((f) => f.endsWith(".json")).sort();
  assert.ok(files.length > 0, "no packaged schema layouts found");
  for (const f of files) {
    const data = JSON.parse(fs.readFileSync(path.join(PACKAGED_DIR, f), "utf-8"));
    assert.equal(typeof data.fields, "object", `${f} has no fields`);
  }
});

test("packaged layouts match canonical", () => {
  // Canonical absent (installed env): nothing to compare against; the
  // existence/parse test above still guards the packaged copy.
  if (!fs.existsSync(CANONICAL_DIR)) {
    return;
  }
  const packaged = fs.readdirSync(PACKAGED_DIR).filter((f) => f.endsWith(".json")).sort();
  const canonical = fs.readdirSync(CANONICAL_DIR).filter((f) => f.endsWith(".json")).sort();
  assert.deepEqual(packaged, canonical, "packaged/canonical layout sets differ");
  for (const f of packaged) {
    assert.deepEqual(
      fs.readFileSync(path.join(PACKAGED_DIR, f)),
      fs.readFileSync(path.join(CANONICAL_DIR, f)),
      `packaged ${f} drifted from canonical`,
    );
  }
});
