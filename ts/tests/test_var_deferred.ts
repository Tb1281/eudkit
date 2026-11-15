/**
 * Deferred-mode `VarImpl` tests (globals P1-01, internal prefactor).
 *
 * These tests use the INTERNAL deferred constructor flag (test-only; the
 * flag is not public API and stays undocumented for users). A deferred
 * handle allocates nothing at construction and resolves its identity on
 * first use through a per-builder binding, so the same handle can serve
 * several modules with separate bindings.
 */
import assert from "node:assert/strict";
import test from "node:test";
import { execFileSync } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { Var, VarImpl, buildModule } from "../index.ts";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = path.resolve(__dirname, "..", "..");
const EUDKIT_COMPILE = path.join(REPO_ROOT, "target", "debug", "eudkit-compile");

function runLower(hirJson: string): any {
  const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), "eudkit-ts-p101-"));
  try {
    const hirPath = path.join(tmpDir, "prog.hir.json");
    fs.writeFileSync(hirPath, hirJson, "utf-8");
    const out = execFileSync(EUDKIT_COMPILE, ["lower", hirPath], {
      encoding: "utf-8",
    }).trim();
    return JSON.parse(out);
  } finally {
    fs.rmSync(tmpDir, { recursive: true, force: true });
  }
}

function hirJson(mod: { toJson(): string }): any {
  return JSON.parse(mod.toJson());
}

function decls(mod: { toJson(): string }): Array<[number, number]> {
  return hirJson(mod).module.vars.map((v: any) => [v.id, v.init]);
}

/** Test-only helper: construct a deferred handle via the internal flag. */
function deferred(init: number, storage?: "page"): VarImpl {
  return new VarImpl(init, undefined, undefined, storage, true);
}

function deferredMasked(init: number, mask: number): VarImpl {
  return new VarImpl(init, undefined, mask, undefined, true);
}

function assertCode(fn: () => unknown, code: string): void {
  assert.throws(fn, (e: any) => {
    assert.equal(e.code, code);
    return true;
  });
}

function assertCodeMessage(
  fn: () => unknown,
  code: string,
  messagePart: string,
): void {
  assert.throws(fn, (e: any) => {
    assert.equal(e.code, code);
    assert.match(e.message, new RegExp(messagePart));
    return true;
  });
}

test("P1-01: deferred construction outside a builder allocates nothing", () => {
  const handle = deferred(7);
  const withHandle = buildModule(() => {
    const v = Var(0);
    v.set(1);
  });
  const withoutHandle = buildModule(() => {
    const v = Var(0);
    v.set(1);
  });
  // Identical output proves the unused handle consumed no id, emitted no
  // decl, and emitted no statement (any of those would shift ids or stmts).
  assert.deepEqual(hirJson(withHandle), hirJson(withoutHandle));
  assert.deepEqual(decls(withHandle), [[0, 0]]);
});

test("P1-01: pure reads never resolve and never allocate", () => {
  const handle = deferred(7);
  const masked = deferredMasked(3, 0xff);
  assert.equal(handle.init, 7);
  assert.equal(handle.initialValue, 7);
  assert.equal(handle.bitmask, 0xffffffff);
  assert.equal(masked.init, 3);
  assert.equal(masked.bitmask, 0xff);
  const mod = buildModule(() => {
    const v = Var(0);
    v.set(1);
  });
  assert.deepEqual(decls(mod), [[0, 0]]);
});

test("P1-01: first resolving use with no active builder throws EUDSL-SCOPE", () => {
  const handle = deferred(7);
  for (const read of [
    () => handle.id,
    () => handle.pageIndex,
    () => handle.slotIndex,
    () => handle.mask,
    () => handle.dest,
    () => handle.value,
    () => handle.modifier,
  ]) {
    assertCode(read, "EUDSL-SCOPE");
  }
  assertCode(() => handle.set(1), "EUDSL-SCOPE");
});

test("P1-01: same handle under two builders creates separate bindings", () => {
  const handle = deferred(7);
  let idA = -1;
  const modA = buildModule(() => {
    idA = handle.id;
    handle.set(1);
    const eager = Var(0);
    eager.set(9);
  });
  let idB = -1;
  const modB = buildModule(() => {
    const eager = Var(0);
    eager.set(9);
    idB = handle.id;
    handle.set(2);
  });
  assert.equal(idA, 0);
  assert.equal(idB, 1);
  assert.deepEqual(decls(modA), [
    [0, 7],
    [1, 0],
  ]);
  assert.deepEqual(decls(modB), [
    [0, 0],
    [1, 7],
  ]);
  // Byte-identity against eager equivalents proves neither module's
  // handle-use statements reference the other module's binding id.
  const eagerA = buildModule(() => {
    const a = Var(7);
    a.set(1);
    const b = Var(0);
    b.set(9);
  });
  const eagerB = buildModule(() => {
    const a = Var(0);
    a.set(9);
    const b = Var(7);
    b.set(2);
  });
  assert.deepEqual(runLower(modA.toJson()), runLower(eagerA.toJson()));
  assert.deepEqual(runLower(modB.toJson()), runLower(eagerB.toJson()));
});

test("P1-01: post-build single-builder read returns the cached record", () => {
  const handle = deferred(7);
  let recordedId = -1;
  let recordedPage = -1;
  let recordedSlot = -1;
  let recordedValue: unknown = null;
  buildModule(() => {
    recordedId = handle.id;
    recordedPage = handle.pageIndex;
    recordedSlot = handle.slotIndex;
    recordedValue = handle.value;
    handle.set(3);
  });
  assert.equal(handle.id, recordedId);
  assert.equal(handle.pageIndex, recordedPage);
  assert.equal(handle.slotIndex, recordedSlot);
  assert.equal(handle.value, recordedValue);
  // Repeated post-build reads stay cached and identical.
  assert.equal(handle.id, recordedId);
  assert.equal(handle.value, recordedValue);
});

test("P1-01: post-build multi-builder read throws ambiguous", () => {
  const handle = deferred(7);
  buildModule(() => {
    handle.set(1);
  });
  buildModule(() => {
    handle.set(2);
  });
  assertCodeMessage(() => handle.id, "EUDSL-SCOPE", "ambiguous");
  assertCodeMessage(() => handle.value, "EUDSL-SCOPE", "ambiguous");
});

test("P1-01: post-build never-resolved read throws EUDSL-SCOPE", () => {
  const handle = deferred(7);
  assert.throws(
    () => handle.id,
    (e: any) => {
      assert.equal(e.code, "EUDSL-SCOPE");
      assert.doesNotMatch(e.message, /ambiguous/);
      return true;
    },
  );
});

test("P1-01: deferred non-const init throws EUDSL-TYPE eagerly (D4)", () => {
  buildModule(() => {
    const e = Var(0);
    assertCode(
      () => new VarImpl(e, undefined, undefined, undefined, true),
      "EUDSL-TYPE",
    );
    assertCode(
      () => new VarImpl(e.add(1), undefined, undefined, undefined, true),
      "EUDSL-TYPE",
    );
  });
});

test("P1-01: deferred handle with explicit id throws EUDSL-TYPE", () => {
  assertCode(
    () => new VarImpl(0, 5, undefined, undefined, true),
    "EUDSL-TYPE",
  );
});

test("P1-01: deferred page-pinned var resolves page coords per builder", () => {
  const handle = deferred(0, "page");
  let pageA = -1;
  let slotA = -1;
  buildModule(() => {
    pageA = handle.pageIndex;
    slotA = handle.slotIndex;
  });
  let pageB = -1;
  let slotB = -1;
  buildModule(() => {
    const pad = Var(0, undefined, undefined, "page");
    pad.set(0);
    pageB = handle.pageIndex;
    slotB = handle.slotIndex;
  });
  assert.deepEqual([pageA, slotA], [0, 0]);
  assert.deepEqual([pageB, slotB], [0, 1]);
});
