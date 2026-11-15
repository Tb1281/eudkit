/**
 * T-WP5: `withEpd` runtime CP binding, ambient save/restore, blockio parity.
 *
 * `withEpd` is the runtime-EPD sibling of `withCP`: the CP register is seeded
 * from a `Var` (or a `Db`/`DbRef` routed through `epdOfDb`) instead of a
 * build-time constant, so cursor hops are relative `Add` deltas. The ambient
 * CurrentPlayer value is saved with the shared 32-bit reader and restored
 * unconditionally, including on the exception path and when the body clobbers
 * CP. `withCP` remains int-only.
 *
 * The blockio snapshot pins the pre-refactor `memcpyEPD`/`memsetEPD`/
 * `connectDList` HIR byte-for-byte (SHA-256 captured before `ts/blockio.ts`
 * was refactored onto the shared seed helpers). The Python mirror pins the
 * same digest.
 */
import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import test from "node:test";

import {
  AddrReloc,
  CP_ADDR,
  Db,
  EPD_CURRENT_PLAYER,
  Expr,
  SetMemory,
  SetTo,
  Var,
  asReadonly,
  buildModule,
  connectDList,
  emitVarStore,
  memcpyEPD,
  memsetEPD,
  readCurrentPlayerRegister,
  withCP,
  withEpd,
  with_epd,
} from "../index.ts";
import { EPD_BASE, flattenStmts, readDword, runSim, type HirActNode, type HirStmtNode } from "./helpers.ts";

const SET_DEATHS = 45;
const SET_TO = 7;
const ACT_ADD = 8;

/** External observation dwords (outside the synthetic payload). */
const OBSERVE = 0x6d0000;

interface HirModule {
  stmts: HirStmtNode[];
  vars?: unknown[];
  funcs?: { body: HirStmtNode[] }[];
}

function epd(addr: number): number {
  return (addr - EPD_BASE) / 4;
}

function moduleFor(prog: () => void): HirModule {
  const hir = JSON.parse(buildModule(prog).toJson()) as { module: HirModule };
  return hir.module;
}

function acts(module: HirModule): HirActNode[] {
  const out: HirActNode[] = [];
  for (const stmt of flattenStmts(module.stmts)) {
    if (stmt.acts) out.push(...stmt.acts);
    if (stmt.kind === "act") out.push(stmt);
  }
  return out;
}

function cpHops(module: HirModule): number[] {
  return acts(module)
    .filter(
      (act) =>
        act.acttype === SET_DEATHS &&
        act.dest === EPD_CURRENT_PLAYER &&
        act.modifier === ACT_ADD,
    )
    .map((act) => act.value as number);
}

/** Store the live CurrentPlayer value into an external observation dword. */
function observe(addr: number): void {
  const saved = readCurrentPlayerRegister();
  emitVarStore(saved.id, AddrReloc(addr), SetTo);
}

test("ambient CP is restored after the body", () => {
  const report = runSim(
    buildModule(() => {
      SetMemory(CP_ADDR, SetTo, 77);
      withEpd(1000, (cp) => cp.at(2).set(7));
      observe(OBSERVE);
    }).toJson(),
  );
  assert.equal(readDword(report, epd(OBSERVE)), 77);
});

test("restore runs even when the body clobbers CP", () => {
  const report = runSim(
    buildModule(() => {
      SetMemory(CP_ADDR, SetTo, 77);
      withEpd(1000, () => SetMemory(CP_ADDR, SetTo, 999));
      observe(OBSERVE);
    }).toJson(),
  );
  assert.equal(readDword(report, epd(OBSERVE)), 77);
});

test("restore runs on the exception path", () => {
  const report = runSim(
    buildModule(() => {
      SetMemory(CP_ADDR, SetTo, 77);
      try {
        withEpd(1000, (cp) => {
          cp.at(1).set(9);
          throw new Error("boom");
        });
      } catch (err) {
        if (!(err instanceof Error) || err.message !== "boom") throw err;
      }
      observe(OBSERVE);
    }).toJson(),
  );
  assert.equal(readDword(report, epd(OBSERVE)), 77);
});

test("nested calls restore in LIFO order", () => {
  const report = runSim(
    buildModule(() => {
      SetMemory(CP_ADDR, SetTo, 77);
      withEpd(1000, (outer) => {
        observe(OBSERVE);
        withEpd(2000, (inner) => {
          observe(OBSERVE + 8);
          inner.at(0).set(2);
        });
        observe(OBSERVE + 4);
      });
      observe(OBSERVE + 12);
    }).toJson(),
  );
  assert.equal(readDword(report, epd(OBSERVE)), 1000);
  assert.equal(readDword(report, epd(OBSERVE + 4)), 1000);
  assert.equal(readDword(report, epd(OBSERVE + 8)), 2000);
  assert.equal(readDword(report, epd(OBSERVE + 12)), 77);
});

test("zero offset emits no pre-hop", () => {
  const module = moduleFor(() => withEpd(1000, (cp) => cp.at(0).set(1)));
  assert.deepEqual(cpHops(module), []);
});

test("offset pre-hops the private scratch copy", () => {
  const module = moduleFor(() => withEpd(1000, () => {}, 3));
  const adds = acts(module)
    .filter((act) => act.acttype === SET_DEATHS && act.modifier === ACT_ADD)
    .map((act) => act.value as number);
  assert.deepEqual(adds, [3], "offset=3 must be one scratch Add");
});

test("at uses a relative hop not an absolute base", () => {
  const module = moduleFor(() => withEpd(1000, (cp) => cp.at(2).set(7)));
  assert.deepEqual(cpHops(module), [2]);
  assert.ok(
    !acts(module).some(
      (act) =>
        act.acttype === SET_DEATHS &&
        act.dest === EPD_CURRENT_PLAYER &&
        act.modifier === SET_TO,
    ),
    "a runtime base must never emit SetTo base+target",
  );
});

test("repeating the offset never hops twice", () => {
  const module = moduleFor(() =>
    withEpd(1000, (cp) => {
      cp.at(2).set(7);
      cp.at(2).set(8);
      cp.moveTo(3);
      cp.at(3).set(9);
    }),
  );
  assert.deepEqual(cpHops(module), [2, 1]);
});

test("offset zero emits no hop even with a body hop", () => {
  const module = moduleFor(() => withEpd(1000, (cp) => cp.at(2).set(7)));
  assert.deepEqual(
    cpHops(module).filter((value) => value === 3),
    [],
    "the default offset must not add a pre-hop act",
  );
});

test("dbref base routes through epdOfDb", () => {
  const module = moduleFor(() =>
    withEpd(new Db(Buffer.from([1, 2, 3, 4, 5, 6, 7, 8])), () => {}),
  );
  const triggers = module.stmts.filter((stmt) => stmt.kind === "trigger");
  const relocs = (triggers[0].acts?.[0].relocs ?? []) as NonNullable<
    HirActNode["relocs"]
  >;
  const blobReloc = relocs.find((reloc) => "target_blob" in reloc) as
    | { mode?: number }
    | undefined;
  assert.equal(blobReloc?.mode, 1);
  const baseId = relocs.find((reloc) => reloc.field_offset === 16)
    ?.target_var as number;
  const transfers = module.stmts.filter(
    (stmt) => stmt.kind === "vartransfer" && stmt.src_var === baseId,
  );
  assert.equal(transfers.length, 1, "the blob EPD must be copied once");
  assert.notEqual(transfers[0].dst_var, baseId);
});

test("DbRef offset routes through epdOfDb", () => {
  const module = moduleFor(() =>
    withEpd(new Db(Buffer.from([1, 2, 3, 4, 5, 6, 7, 8])).at(4), () => {}),
  );
  const triggers = module.stmts.filter((stmt) => stmt.kind === "trigger");
  const relocs = (triggers[0].acts?.[0].relocs ?? []) as NonNullable<
    HirActNode["relocs"]
  >;
  const blobReloc = relocs.find((reloc) => "target_blob" in reloc) as
    | { mode?: number; target_offset?: number }
    | undefined;
  assert.equal(blobReloc?.mode, 1);
  assert.equal(blobReloc?.target_offset, 4);
});

test("readonly Var materializes without an extra temp", () => {
  const holder: { value?: Var } = {};
  const module = moduleFor(() => {
    const value = new Var(1000);
    holder.value = value;
    withEpd(asReadonly(value), () => {});
  });
  const transfers = module.stmts.filter(
    (stmt) => stmt.kind === "vartransfer" && stmt.src_var === holder.value?.id,
  );
  assert.equal(transfers.length, 1);
});

test("Expr base materializes exactly once", () => {
  const holder: { value?: Var } = {};
  const module = moduleFor(() => {
    const value = new Var(1000);
    holder.value = value;
    withEpd(new Expr("add", value, 5), () => {});
  });
  const transfers = module.stmts.filter(
    (stmt) => stmt.kind === "vartransfer" && stmt.src_var === holder.value?.id,
  );
  assert.equal(transfers.length, 1, "the Expr operand must be evaluated exactly once");
});

test("non-EPD operands are EUDSL-TYPE", () => {
  for (const bad of ["x", 1.5, null, true, {}] as unknown[]) {
    assert.throws(
      () => buildModule(() => withEpd(bad as never, () => {})),
      /EUDSL-TYPE/,
      String(bad),
    );
  }
});

test("offset outside the signed 32-bit range is EUDSL-RANGE", () => {
  for (const bad of [2 ** 31, -(2 ** 31) - 1, true, 1.5, "3", null] as unknown[]) {
    assert.throws(
      () => buildModule(() => withEpd(1000, () => {}, bad as number)),
      /EUDSL-RANGE/,
      String(bad),
    );
  }
});

test("invalid offset queues no act", () => {
  const module = moduleFor(() => {
    try {
      withEpd(1000, () => {}, 2 ** 31);
    } catch {
      // expected: validated before materializing anything
    }
  });
  assert.deepEqual(module.stmts, []);
});

test("invalid epd queues no act", () => {
  const module = moduleFor(() => {
    try {
      withEpd("not an epd" as never, () => {});
    } catch {
      // expected
    }
  });
  assert.deepEqual(module.stmts, []);
});

test("withCP still rejects a dynamic base before any act", () => {
  const module = moduleFor(() => {
    try {
      withCP(new Var(0), () => {});
    } catch {
      // expected
    }
  });
  assert.deepEqual(module.stmts, []);
  assert.ok(module.vars && module.vars.length > 0, "the argument Var itself still allocates");
});

test("camelCase alias is the same helper", () => {
  assert.equal(with_epd, withEpd);
});

// SHA-256 of the pre-refactor HIR for `blockioProgram` (captured before
// `ts/blockio.ts` was refactored onto `emitEpdRelocInto` / `_setCpFromVar`).
// The Python mirror pins the same digest.
const BLOCKIO_HIR_SHA256 =
  "04160598ab5c3038e6743eafe1ee44f4405f9b1caf89af0a96b53edec209aa60";

function blockioProgram(): void {
  const d = new Db(Buffer.from([1, 2, 3, 4, 5, 6, 7, 8]));
  memsetEPD(1000, 0xaabbccdd, 3);
  memcpyEPD(2000, 1000, 3);
  const n = new Var(2);
  memsetEPD(3000, 0x11223344, n);
  const fill = new Var(9);
  memsetEPD(3100, fill, 2);
  memcpyEPD(4000, d, 2);
  memcpyEPD(5000, d, 1);
  memcpyEPD(6000, 1000, 1);
  const src = new Var(1000);
  memcpyEPD(7000, src, 3);
  memcpyEPD(8000, 1000, n);
  const dst = new Var(9000);
  memcpyEPD(dst, src, n);
  connectDList(0x64eed8, 0x64eedc, 112, 3, 0);
}

test("blockio HIR is byte-identical after the refactor", () => {
  const hir = buildModule(blockioProgram).toJson();
  assert.equal(
    createHash("sha256").update(hir, "utf8").digest("hex"),
    BLOCKIO_HIR_SHA256,
    "the blockio seed refactor must not change a single HIR byte",
  );
});
