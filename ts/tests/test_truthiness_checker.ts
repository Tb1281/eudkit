/**
 * Issue #50: the opt-in TypeScript truthiness checker.
 *
 * `checkSource` is a parser-only walk; `checkProject` pins the repo baseline
 * at zero diagnostics. The CLI tests spawn the real script so the
 * `--project` path and exit codes are covered.
 */
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";

import {
  checkProject,
  checkSource,
  type TruthinessDiagnostic,
} from "../scripts/check-truthiness.ts";

const SCRIPT = path.join(import.meta.dirname, "..", "scripts", "check-truthiness.ts");

function assertOneDiagnostic(
  diagnostics: TruthinessDiagnostic[],
  source: string,
): TruthinessDiagnostic {
  assert.equal(diagnostics.length, 1, `expected one diagnostic for ${JSON.stringify(source)}`);
  const diagnostic = diagnostics[0];
  assert.equal(diagnostic.code, "EUDSL-TS-TRUTHIFY");
  return diagnostic;
}

function spawnChecker(args: string[]): ReturnType<typeof spawnSync> {
  return spawnSync(
    process.execPath,
    ["--experimental-strip-types", "--no-warnings", SCRIPT, ...args],
    { encoding: "utf8" },
  );
}

test("test_flags_condition_call", () => {
  const source = "function f(v: any) { if (v.eq(1)) {} }";
  const diagnostic = assertOneDiagnostic(checkSource("fixture.ts", source), source);
  assert.equal(diagnostic.line, 1);
  assert.equal(
    diagnostic.message,
    "runtime condition/value used as a JS boolean; use when([cond], body) / whileLoop([cond], body)",
  );
});

test("test_flags_initial_value_comparison", () => {
  const source = "function f(u: any) { if (u.hp == u.hp.initialValue) {} }";
  assertOneDiagnostic(checkSource("fixture.ts", source), source);
});

test("test_flags_view_field_read", () => {
  const source = "function f() { const u = CUnit(1000); if (u.hp) {} }";
  assertOneDiagnostic(checkSource("fixture.ts", source), source);
});

test("test_flags_not_operand", () => {
  const source = "function f(v: any) { if (!v.eq(1)) {} }";
  assertOneDiagnostic(checkSource("fixture.ts", source), source);
});

test("test_flags_ternary", () => {
  const source = "function f(v: any) { return v.eq(1) ? 1 : 2; }";
  assertOneDiagnostic(checkSource("fixture.ts", source), source);
});

test("test_sanctioned_when_is_not_flagged", () => {
  assert.deepEqual(checkSource("fixture.ts", "when([v.eq(1)], () => {});"), []);
});

test("test_sanctioned_while_loop_is_not_flagged", () => {
  assert.deepEqual(checkSource("fixture.ts", "whileLoop([tick.lt(2)], () => {});"), []);
});

test("test_plain_boolean_is_not_flagged", () => {
  const source = "if (flag) {} if (count > 0) {} if (arr.length) {}";
  assert.deepEqual(checkSource("fixture.ts", source), []);
});

test("test_initial_value_argument_is_not_a_test", () => {
  const source = "when([u.hp == u.hp.initialValue], () => {});";
  assert.deepEqual(checkSource("fixture.ts", source), []);
});

test("test_initial_value_in_a_callback_body_is_not_a_test", () => {
  const source =
    "when([v.eq(1)], () => { u.hp == u.hp.initialValue; });";
  assert.deepEqual(checkSource("fixture.ts", source), []);
});

test("test_ignore_comment_suppresses_the_statement", () => {
  const source = "if (v.eq(1)) {} // eudsl-truthiness: ignore";
  assert.deepEqual(checkSource("fixture.ts", source), []);
});

test("test_ignore_comment_does_not_leak_to_the_next_statement", () => {
  const source =
    "if (v.eq(1)) {} // eudsl-truthiness: ignore\nif (v.ne(2)) {}";
  assertOneDiagnostic(checkSource("fixture.ts", source), source);
});

test("test_project_is_clean", () => {
  assert.deepEqual(checkProject("tsconfig.json"), []);
});

test("test_cli_exits_nonzero_on_a_bad_fixture", () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "eudsl-truthiness-bad-"));
  try {
    fs.writeFileSync(path.join(dir, "fixture.ts"), "if (v.eq(1)) {}\n");
    fs.writeFileSync(
      path.join(dir, "tsconfig.json"),
      JSON.stringify({ compilerOptions: { target: "ES2022" }, include: ["fixture.ts"] }),
    );
    const result = spawnChecker(["--project", path.join(dir, "tsconfig.json")]);
    assert.equal(result.status, 1);
    assert.match(String(result.stdout), /EUDSL-TS-TRUTHIFY/);
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

test("test_cli_exits_zero_on_a_good_fixture", () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "eudsl-truthiness-good-"));
  try {
    fs.writeFileSync(path.join(dir, "fixture.ts"), "if (flag) {}\n");
    fs.writeFileSync(
      path.join(dir, "tsconfig.json"),
      JSON.stringify({ compilerOptions: { target: "ES2022" }, include: ["fixture.ts"] }),
    );
    const result = spawnChecker(["--project", path.join(dir, "tsconfig.json")]);
    assert.equal(result.status, 0);
    assert.match(String(result.stdout), /truthiness: clean/);
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});
