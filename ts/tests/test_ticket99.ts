/**
 * Issue #99: HIR Blob/Db seam (`Db(bytes | size)` → `LirItem::Blob`).
 *
 * Mirror of `py/tests/test_ticket99.py`. The probe is the unlimiter's
 * `connectDList` shape (`~/workspace/opencode-cell-test/unlimiter-source.py`):
 * one `Db` per game list and a `SetMemory` action patching that list's head
 * with the blob's payload address. The seam lowers those blobs to LIR blob
 * items appended after the trigger pages / demoted Cell blob, and both action
 * fields and blob-internal dword fields relocate to their payload addresses.
 */

import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

import {
  Always,
  Db,
  DbRef,
  EudslError,
  SetMemory,
  SetTo,
  Trigger,
  Var,
  buildModule,
  db,
} from "../index.ts";
import { runStatic } from "./helpers.ts";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = path.resolve(__dirname, "..", "..");
const EUDKIT_COMPILE = path.join(REPO_ROOT, "target", "debug", "eudkit-compile");

// The unlimiter's four `connectDList` calls: (list head, element size).
const GAME_LISTS: readonly (readonly [number, number])[] = [
  [0x64eed8, 112], // bullet count
  [0x63fe30, 36], // sprite count
  [0x57eb68, 64], // image count
  [0x64b2e0, 20], // order count
];

/** The HIR `dest` field is a u32; EPDs below 0x58A364 wrap. */
function epd(addr: number): number {
  return Math.floor((addr - 0x58a364) / 4) >>> 0;
}

function moduleJson(body: () => void): any {
  return JSON.parse(buildModule(body).toJson());
}

function runLower(hirJson: string): any {
  const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), "eudkit-ts-c99-"));
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

test("unlimiter Dbs patch the four game list heads", () => {
  const module = moduleJson(() => {
    const dbs = GAME_LISTS.map(([, size]) => new Db(size));
    GAME_LISTS.forEach(([head], index) => {
      SetMemory(head, SetTo, dbs[index]!);
    });
  }).module;
  assert.deepEqual(
    module.blobs,
    GAME_LISTS.map(([, size]) => ({ size })),
  );
  const acts = module.stmts[0].acts;
  GAME_LISTS.forEach(([head], index) => {
    const act = acts[index];
    assert.equal(act.dest, epd(head));
    assert.equal(act.value, 0);
    assert.deepEqual(act.relocs, [
      {
        field_offset: 20,
        target_blob: index,
        target_offset: 0,
        mode: 4,
      },
    ]);
  });
});

test("Db bytes are base64 and offsets shift", () => {
  const payload = new Uint8Array([0, 1, 2, 3, 4, 5, 6, 7]);
  const module = moduleJson(() => {
    const blob = new Db(payload);
    SetMemory(blob.at(4), SetTo, 10000);
  }).module;
  assert.equal(module.blobs[0].size, 8);
  assert.deepEqual(
    Buffer.from(module.blobs[0].data, "base64"),
    Buffer.from(payload),
  );
  const acts = module.stmts[0].acts;
  // Destination is the blob's dword at +4 (EPD relocation, mode 1).
  assert.deepEqual(acts[0].relocs, [
    {
      field_offset: 16,
      target_blob: 0,
      target_offset: 4,
      mode: 1,
    },
  ]);
  assert.equal(acts[0].value, 10000);
});

test("blob-internal relocs target blobs and triggers", () => {
  const module = moduleJson(() => {
    const tail = new Db(8);
    new Db(8, [
      { offset: 0, target: tail, target_offset: 0, mode: 4 },
      { offset: 4, target: tail, target_offset: 4, mode: 4 },
    ]);
    new Trigger({
      conds: [Always()],
      acts: [
        {
          mask: 0,
          strid: 0,
          wavid: 0,
          time: 0,
          dest: 0,
          value: 0,
          unit: 0,
          acttype: 45,
          modifier: 7,
          flags: 20,
          padding: 0,
          eudx: 0,
          relocs: [
            {
              field_offset: 20,
              target_blob: 1,
              target_offset: 0,
              mode: 4,
            },
          ],
        },
      ],
    });
    // A blob can also point at a trigger item.
    new Db(4, [{ offset: 0, target_trigger: 0, target_offset: 0, mode: 4 }]);
  }).module;
  assert.deepEqual(
    module.blobs.map((b: any) => b.size),
    [8, 8, 4],
  );
  assert.deepEqual(module.blobs[1].relocs, [
    {
      offset: 0,
      target_blob: 0,
      target_offset: 0,
      mode: 4,
    },
    {
      offset: 4,
      target_blob: 0,
      target_offset: 4,
      mode: 4,
    },
  ]);
  assert.deepEqual(module.blobs[2].relocs, [
    { offset: 0, target_trigger: 0, target_offset: 0, mode: 4 },
  ]);
});

test("native lowering appends blobs and resolves head patches", () => {
  const hir = buildModule(() => {
    const dbs = GAME_LISTS.map(([, size]) => new Db(size));
    GAME_LISTS.forEach(([head], index) => {
      SetMemory(head, SetTo, dbs[index]!);
    });
  }).toJson();
  const items = runLower(hir).module.items;
  assert.equal(items.length, 1 + GAME_LISTS.length);
  assert.equal(items[0].kind, "trigger");
  GAME_LISTS.forEach(([, size], index) => {
    const blob = items[1 + index];
    assert.equal(blob.kind, "blob");
    assert.equal(blob.size, size);
    assert.deepEqual(items[0].acts[index].relocs, [
      {
        field_offset: 20,
        target_item: 1 + index,
        target_offset: 0,
        mode: 4,
      },
    ]);
  });
});

test("lowering places blobs after the demoted Cell blob", () => {
  const hir = buildModule(() => {
    // A scalar that never needs a page is demoted to a 4-byte Cell blob,
    // which must stay ahead of the HIR blobs.
    Var(0);
    const blob = new Db(12);
    SetMemory(0x57eb68, SetTo, blob);
  }).toJson();
  const items = runLower(hir).module.items;
  assert.deepEqual(
    items.map((item: any) => item.kind),
    ["trigger", "blob", "blob"],
  );
  assert.equal(items[2].size, 12);
});

test("static cost counts blob bytes", () => {
  const hir = buildModule(() => {
    const dbs = GAME_LISTS.map(([, size]) => new Db(size));
    GAME_LISTS.forEach(([head], index) => {
      SetMemory(head, SetTo, dbs[index]!);
    });
  }).toJson();
  assert.equal(
    runStatic(hir).static_cost.blob_bytes,
    GAME_LISTS.reduce((sum, [, size]) => sum + size, 0),
  );
});

test("Db outside buildModule is rejected", () => {
  assert.throws(
    () => new Db(16),
    (err: unknown) =>
      err instanceof EudslError && err.code === "EUDSL-SCOPE",
  );
});

test("Db payload validation", () => {
  for (const payload of [-4, 1.5, "bytes", undefined]) {
    assert.throws(() => buildModule(() => new Db(payload as never)), EudslError);
  }
  assert.throws(() => buildModule(() => new Db(16).at(2)), EudslError);
  assert.throws(
    () => buildModule(() => new Db(16, [{ offset: 2, target_blob: 0, mode: 4 }])),
    EudslError,
  );
});

test("DbRef arithmetic", () => {
  buildModule(() => {
    const blob = db(16);
    assert.ok(blob instanceof DbRef);
    assert.ok(blob.at(4) instanceof DbRef);
    assert.equal(blob.at(8).byteOffset, 8);
    assert.equal(blob.at(8).at(4).byteOffset, 12);
    assert.equal(blob.plus(4).byteOffset, 4);
  });
});
