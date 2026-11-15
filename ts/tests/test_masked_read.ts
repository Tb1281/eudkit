/**
 * Ticket #20: field `read()` honours the field's `bitMask`.
 *
 * `read()` used to emit one bit test per bit of the *nominal width window*
 * (`size * 8` bits from `shift`), ignoring a custom `bitMask`. A masked read
 * then returned the raw window shifted instead of `(raw & bitMask) >>> shift`
 * — a silent wrong answer whenever the mask does not cover that window, and one
 * trigger per width bit instead of one per mask bit. These tests pin the
 * mask-aware read on both field surfaces (`MemFieldHandle` and
 * `TriggerFieldRef`), the resulting 12T cost for a masked dword read,
 * agreement with `eq`, and the rejection of a mask with set bits below
 * `shift`.
 */

import { execFileSync } from "node:child_process";
import assert from "node:assert/strict";
import crypto from "node:crypto";
import test from "node:test";

import { REPO_ROOT, runSim } from "./helpers.ts";

import {
  CUnit,
  EudslError,
  MemView,
  SetDeaths,
  SetTo,
  TriggerFieldRef,
  Var,
  buildModule,
  defineMemView,
  when,
  type MemViewSchema,
} from "../index.ts";

// `v` sits at byte 0 (shift 0); the 0xff00 mask covers the second byte only.
const MASKED_BYTE_SCHEMA = defineMemView("MaskedByte", 4, {
  v: { offset: 0, size: 1, mask: 0xff00 },
});
// A dword field with a narrow mask: the window has 32 bits, the mask 8.
const MASKED_DWORD_SCHEMA = defineMemView("MaskedDw", 4, {
  v: { offset: 0, size: 4, mask: 0xff00 },
});
// `shift = 16`, mask bits (0x00ff) live below it: unrepresentable in field units.
const LOW_MASK_SCHEMA = defineMemView("LowMask", 4, {
  v: { offset: 2, size: 2, mask: 0x00ff },
});

// Byte-field rows measured on HEAD ee2ae1c; the second element is the expected
// `(raw & bitMask) >>> shift`.
const BYTE_ROWS: Array<[number, number]> = [
  [0x00001200, 0x00001200],
  [0x00001201, 0x00001200],
  [0x00000000, 0x00000000],
  [0x0000ff00, 0x0000ff00],
  [0x0000ff12, 0x0000ff00],
];

// Dword-field rows: the pre-fix width window returned the low bits too.
const DWORD_ROWS: Array<[number, number]> = [
  [0x00001200, 0x00001200],
  [0x00001201, 0x00001200],
  [0x0000ff12, 0x0000ff00],
];

function readMasked(schema: MemViewSchema, raw: number): number {
  const ids = { out: -1 };
  const mod = buildModule(() => {
    SetDeaths(3000, SetTo, raw, 0);
    const view = MemView(schema, 3000);
    const out = Var(0);
    ids.out = out.id;
    out.set(view.v.read());
  });
  return runSim(mod.toJson()).var_values[ids.out];
}

function maskedDwordReadProg(): void {
  SetDeaths(3000, SetTo, 0x1201, 0);
  const view = MemView(MASKED_DWORD_SCHEMA, 3000);
  const out = Var(0);
  out.set(view.v.read());
}

test("masked byte read returns mask units", () => {
  for (const [raw, want] of BYTE_ROWS) {
    assert.equal(readMasked(MASKED_BYTE_SCHEMA, raw), want, `raw=${raw.toString(16)}`);
  }
});

test("masked dword read returns mask units", () => {
  for (const [raw, want] of DWORD_ROWS) {
    assert.equal(
      readMasked(MASKED_DWORD_SCHEMA, raw),
      want,
      `raw=${raw.toString(16)}`,
    );
  }
});

test("read agrees with eq in mask units", () => {
  // raw 0x1201 differs from the masked value only outside the mask.
  const ids = { out: -1, hit: -1, miss: -1 };
  const mod = buildModule(() => {
    SetDeaths(3000, SetTo, 0x1201, 0);
    const view = MemView(MASKED_BYTE_SCHEMA, 3000);
    const out = Var(0);
    ids.out = out.id;
    out.set(view.v.read());
    const hit = Var(0);
    ids.hit = hit.id;
    const miss = Var(0);
    ids.miss = miss.id;
    when(view.v.eq(0x1200), () => hit.iadd(1));
    when(view.v.eq(0x1201), () => miss.iadd(1));
  });
  const values = runSim(mod.toJson()).var_values;
  assert.equal(values[ids.out], 0x1200);
  assert.equal(values[ids.hit], 1);
  assert.equal(values[ids.miss], 0);
});

test("masked dword read costs 12T", () => {
  const report = runSim(buildModule(maskedDwordReadProg).toJson(), 1);
  assert.equal(report.static_cost.triggers, 12);
  assert.equal(report.static_cost.conditions, 8);
  assert.equal(report.static_cost.actions, 77);
  assert.equal(report.static_cost.trigger_bytes, 28_896);
  assert.equal(report.static_cost.total_bytes, 28_900);
  assert.equal(report.executed_cost.triggers, 12);
});

function readRef(size: 1 | 2 | 4): { value: number; triggers: number } {
  const ids = { out: -1 };
  const mod = buildModule(() => {
    const value = Var(0x1234);
    const out = Var(0);
    ids.out = out.id;
    const ref = new TriggerFieldRef(-1, 20, size, 0, value.id, 0xff00);
    out.set(ref.read());
  });
  const report = runSim(mod.toJson(), 1);
  return {
    value: report.var_values[ids.out],
    triggers: report.static_cost.triggers,
  };
}

test("TriggerFieldRef masked read uses the custom mask", () => {
  for (const size of [4, 1] as const) {
    const { value, triggers } = readRef(size);
    assert.equal(value, 0x1200, `size=${size}`);
    assert.equal(triggers, 12, `size=${size}`);
  }
});

test("masked read rejects mask bits below shift", () => {
  assert.throws(
    () =>
      buildModule(() => {
        const view = MemView(LOW_MASK_SCHEMA, 3000);
        const out = Var(0);
        out.set(view.v.read());
      }),
    (e: any) => {
      assert.ok(e instanceof EudslError);
      assert.equal(e.code, "EUDSL-BOUNDS");
      return true;
    },
  );
});

test("default-mask read HIR is byte identical", () => {
  // The cost-gate probe; `mask` omitted must stay the frozen-golden path.
  // #97 re-blessed the hash: a full-dword `mask=None` read now compiles to
  // the shared reader (`trigger` patch + `call` + result transfer) instead of
  // 32 inlined bit-test triggers. The *masked* partial-width paths below keep
  // the inline reader and are unchanged.
  const mod = buildModule(() => {
    const out = Var(0);
    out.set(CUnit(1000).hp.read());
  });
  const digest = crypto.createHash("sha256").update(mod.toJson()).digest("hex");
  assert.equal(
    digest,
    "72b36d7ddbefabfb72ec7d51ec03fb2e921ffeda790a1005e92efc0a3a405e8c",
  );
});

test("py-ts HIR parity for every new case", () => {
  const pyHir = execFileSync(
    "python3",
    [
      "-c",
      [
        "from py.tests.test_masked_read import _build_masked_read_parity_module",
        "print(_build_masked_read_parity_module().to_json(), end='')",
      ].join("\n"),
    ],
    { cwd: REPO_ROOT, encoding: "utf8", env: { ...process.env, PYTHONPATH: "." } },
  );
  const tsHir = buildMaskedReadParityModule().toJson();
  assert.equal(tsHir, pyHir);
});

function buildMaskedReadParityModule() {
  return buildModule(() => {
    SetDeaths(3000, SetTo, 0x1201, 0);

    const byteView = MemView(MASKED_BYTE_SCHEMA, 3000);
    const byteOut = Var(0);
    byteOut.set(byteView.v.read());

    const dwordView = MemView(MASKED_DWORD_SCHEMA, 3000);
    const dwordOut = Var(0);
    dwordOut.set(dwordView.v.read());

    const value = Var(0x1234);
    const ref = new TriggerFieldRef(-1, 20, 4, 0, value.id, 0xff00);
    const refOut = Var(0);
    refOut.set(ref.read());
    const narrow = new TriggerFieldRef(-1, 20, 1, 0, value.id, 0xff00);
    const narrowOut = Var(0);
    narrowOut.set(narrow.read());

    const hit = Var(0);
    const miss = Var(0);
    when(byteView.v.eq(0x1200), () => hit.iadd(1));
    when(byteView.v.eq(0x1201), () => miss.iadd(1));
  });
}
