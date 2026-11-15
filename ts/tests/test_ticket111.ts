/**
 * Issue #111: reject unaligned absolute dword addresses at the DSL/lower seam.
 *
 * Mirror of `py/tests/test_ticket111.py`.
 *
 * `Memory(addr)` family conditions/actions fold the byte address into a signed
 * EPD with `ts/view.ts` `EPD()` (floor), while an
 * `emitVarStore(..., { addr })` keeps the raw byte address in HIR and lowers it
 * through `eudkit_lir::epd_u32` (truncating). For an unaligned address the two
 * disagree silently — `0x57F0B2` gated dword `0x57F0B0` but wrote `0x57F0B4` —
 * so the frontend rejects `addr & 3 !== 0` at the call instead.
 *
 * The guard deliberately does **not** live in `EPD()`: `DatFieldHandle` folds
 * an unaligned DAT *field* address into the containing dword plus a byte lane,
 * which is a byte-lane access, not a dword read/write. The controls below pin
 * that distinction.
 */

import assert from "node:assert/strict";
import test from "node:test";

import {
  AtLeast,
  Deaths,
  EPD,
  EudslError,
  Memory,
  SetDeaths,
  SetMemory,
  SetTo,
  Trigger,
  UnitType,
  Var,
  buildModule,
  emitVarStore,
  unitsDat,
  when,
} from "../index.ts";
import { runStatic } from "./helpers.ts";

const FLAG = 0x57f0b4;
const UNALIGNED = 0x57f0b2;

function staticCost(hirJson: string): number[] {
  const cost = runStatic(hirJson).static_cost;
  return [cost.triggers, cost.conditions, cost.actions, cost.total_bytes];
}

// Byte-identical HIR pinned on `0a5f851` (pre-#111); both frontends emit these.
const MEM_ALIGNED_HIR =
  '{"version":"3.2","module":{"stmts":[{"kind":"trigger","id":0,"conds":[],' +
  '"acts":[{"mask":0,"strid":0,"wavid":0,"time":0,"dest":4294955860,' +
  '"value":1,"unit":0,"acttype":45,"modifier":7,"flags":20,"padding":0,' +
  '"eudx":0}],"preserved":true,"flags":4},{"kind":"when","conds":' +
  '[{"locid":0,"player":4294955860,"amount":1,"unit":0,"comparison":0,' +
  '"condtype":15,"restype":0,"flags":0,"eudx":0}],"body":[]}]}}';

const STORE_ALIGNED_HIR =
  '{"version":"3.2","module":{"vars":[{"id":0,"init":3}],"stmts":' +
  '[{"kind":"varstore","src_var":0,"modifier":7,"dst_addr":5763252}]}}';

const BASE_HIR =
  '{"version":"3.2","module":{"stmts":[{"kind":"trigger","id":0,"conds":[],' +
  '"acts":[{"mask":0,"strid":0,"wavid":0,"time":0,"dest":0,"value":1,' +
  '"unit":0,"acttype":45,"modifier":7,"flags":20,"padding":0,"eudx":0}],' +
  '"preserved":true,"flags":4},{"kind":"when","conds":[{"locid":0,' +
  '"player":0,"amount":1,"unit":0,"comparison":0,"condtype":15,' +
  '"restype":0,"flags":0,"eudx":0}],"body":[]}]}}';

const DEATHS_HIR =
  '{"version":"3.2","module":{"stmts":[{"kind":"trigger","id":0,"conds":[],' +
  '"acts":[{"mask":0,"strid":0,"wavid":0,"time":0,"dest":4294955860,' +
  '"value":0,"unit":0,"acttype":45,"modifier":7,"flags":20,"padding":0,' +
  '"eudx":0}],"preserved":true,"flags":4},{"kind":"when","conds":' +
  '[{"locid":0,"player":4294955860,"amount":1,"unit":0,"comparison":0,' +
  '"condtype":15,"restype":0,"flags":0,"eudx":0}],"body":[]}]}}';

// `unitsDat.at(1).armor` sits at 6_684_361 — unaligned by one byte — and the
// marine's `maxHp` at 6_693_712 is aligned. Both still build; the armor write
// floors to dword EPD 218_841 with the byte-lane mask 0xFF00.
const DAT_HIR =
  '{"version":"3.2","module":{"stmts":[{"kind":"trigger","id":0,"conds":[],' +
  '"acts":[{"mask":65280,"strid":0,"wavid":0,"time":0,"dest":218841,' +
  '"value":256,"unit":0,"acttype":45,"modifier":7,"flags":20,"padding":0,' +
  '"eudx":17235},{"mask":4294967295,"strid":0,"wavid":0,"time":0,' +
  '"dest":221179,"value":40,"unit":0,"acttype":45,"modifier":7,"flags":20,' +
  '"padding":0,"eudx":17235}],"preserved":true,"flags":4}]}}';

test("Memory and SetMemory reject an unaligned address at the call", () => {
  buildModule(() => {
    assert.throws(
      () => {
        when([Memory(UNALIGNED, AtLeast, 1)], () => {});
      },
      (e: unknown) => {
        assert.ok(e instanceof EudslError);
        assert.equal(e.code, "EUDSL-BOUNDS");
        assert.equal(
          e.message,
          "EUDSL-BOUNDS: memory address 0x57f0b2 is not 4-byte aligned",
        );
        return true;
      },
    );
    assert.throws(
      () => {
        new Trigger({ conds: [], acts: [SetMemory(UNALIGNED, SetTo, 1)] });
      },
      (e: unknown) => {
        assert.ok(e instanceof EudslError);
        assert.equal(e.code, "EUDSL-BOUNDS");
        assert.equal(
          e.message,
          "EUDSL-BOUNDS: memory address 0x57f0b2 is not 4-byte aligned",
        );
        return true;
      },
    );
  });
});

test("emitVarStore rejects an unaligned address at the call", () => {
  buildModule(() => {
    const v = Var(3);
    assert.throws(
      () => {
        emitVarStore(v.id, { addr: UNALIGNED });
      },
      (e: unknown) => {
        assert.ok(e instanceof EudslError);
        assert.equal(e.code, "EUDSL-BOUNDS");
        assert.equal(
          e.message,
          "EUDSL-BOUNDS: VarStore address 0x57f0b2 is not 4-byte aligned",
        );
        return true;
      },
    );
  });
});

test("aligned Memory HIR and static cost are unchanged", () => {
  const hirJson = buildModule(() => {
    new Trigger({ conds: [], acts: [SetMemory(FLAG, SetTo, 1)] });
    when([Memory(FLAG, AtLeast, 1)], () => {});
  }).toJson();
  assert.equal(hirJson, MEM_ALIGNED_HIR);
  // The empty `when` is a dead gate; the post-schedule dead-gate pass drops it
  // (Cycle 28 (Cycle 3) #03), so only the SetMemory trigger remains.
  assert.deepEqual(staticCost(hirJson), [1, 0, 1, 2408]);
});

test("aligned emitVarStore HIR and static cost are unchanged", () => {
  const hirJson = buildModule(() => {
    const v = Var(3);
    emitVarStore(v.id, { addr: FLAG });
  }).toJson();
  assert.equal(hirJson, STORE_ALIGNED_HIR);
  assert.deepEqual(staticCost(hirJson), [3, 0, 67, 7224]);
});

test("EPD base address is unchanged", () => {
  const hirJson = buildModule(() => {
    new Trigger({ conds: [], acts: [SetMemory(0x58a364, SetTo, 1)] });
    when([Memory(0x58a364, AtLeast, 1)], () => {});
  }).toJson();
  assert.equal(hirJson, BASE_HIR);
  assert.ok(hirJson.includes('"player":0'));
});

test("the EPD wire form is not guarded", () => {
  const hirJson = buildModule(() => {
    new Trigger({ conds: [], acts: [SetDeaths(EPD(FLAG), SetTo, 0)] });
    when([Deaths(EPD(FLAG), AtLeast, 1)], () => {});
  }).toJson();
  assert.equal(hirJson, DEATHS_HIR);
});

test("an unaligned DAT field still reads and writes", () => {
  // The regression pin for not guarding `EPD()`: a DAT field address is a byte
  // lane on the containing dword, not a whole-dword access.
  const armor = unitsDat.at(1).armor;
  assert.notEqual(armor.address % 4, 0);

  const hirJson = buildModule(() => {
    unitsDat.at(1).armor.set(1);
    unitsDat[UnitType.TerranMarine].maxHp.set(40);
  }).toJson();
  assert.equal(hirJson, DAT_HIR);
  assert.deepEqual(staticCost(hirJson), [1, 0, 2, 2408]);
});
