/**
 * Ticket #11: per-primitive memory costs, pinned as a golden table.
 *
 * The comprehensive cost gate is one module, so its totals cannot attribute a
 * drift to a single primitive. Each case here builds one fresh module and pins
 * that primitive's own static and executed cost. `ne` expands to the deferred
 * flag form (reset + `when(lt)` + `when(gt)` + `flag == 1`) at the five one-shot
 * gate sites (#16), so its row is 4T static / 4T executed; a future change there
 * must show up as an explicit golden update rather than a silent drift.
 *
 * Provenance for the read pin: the scan's 34T figure was the
 * `SetMemory(addr, SetTo, read())` sink form. Reading into a variable is the
 * sound form; it used to cost 36T (one 33T inline read plus 3T for the
 * var-to-var transfer). Since #97 a full-dword `read()` is a per-address shared
 * subroutine (one 32-bit-test body per distinct EPD, no per-call field patch),
 * and a body with a single call site is spliced back in by the optimizer. This
 * probe has one site, so it lowers to the 33T body itself and the fused result
 * copy: 33T static / 33T executed, below the old inline 36T. Multiple sites on
 * the same address share the body, paying the call sequence instead of another
 * 33T copy.
 */

import assert from "node:assert/strict";
import test from "node:test";

import {
  CUnit,
  Db,
  EPD_CURRENT_PLAYER,
  MemCond,
  MemView,
  SetDeaths,
  SetTo,
  Var,
  buildModule,
  defineMemView,
  memcpyEPD,
  memsetEPD,
  when,
  whileLoop,
} from "../index.ts";

import { readDword, runSim } from "./helpers.ts";

// Ticket #20: a dword field with a narrow mask; the read must emit one bit
// test per mask bit (8), not per width bit (32).
const MASKED_DWORD_SCHEMA = defineMemView("MaskedDw", 4, {
  v: { offset: 0, size: 4, mask: 0xff00 },
});

function readIntoVar(): void {
  const readOut = Var(0);
  readOut.set(CUnit(1000).hp.read());
}

function maskedReadIntoVar(): void {
  SetDeaths(3000, SetTo, 0x1201, 0);
  const readOut = Var(0);
  readOut.set(MemView(MASKED_DWORD_SCHEMA, 3000).v.read());
}

function memsetVarCount(): void {
  const n = Var(2);
  memsetEPD(3000, 0x11223344, n);
}

function memcpyVarCount(): void {
  const n = Var(2);
  memcpyEPD(6000, 3000, n);
}

const CASES: {
  name: string;
  prog: () => void;
  triggers: number;
  triggerBytes: number;
  totalBytes: number;
  executed: number;
}[] = [
  {
    name: "memview eq",
    prog: () => {
      when(CUnit(1000).hp.eq(500), () => {
        SetDeaths(9000, SetTo, 1, 0);
      });
    },
    triggers: 1,
    triggerBytes: 2_408,
    totalBytes: 2_408,
    executed: 1,
  },
  {
    name: "memview ne",
    prog: () => {
      when(CUnit(1000).hp.ne(500), () => {
        SetDeaths(9000, SetTo, 1, 0);
      });
    },
    triggers: 4,
    triggerBytes: 9_632,
    totalBytes: 9_636,
    executed: 4,
  },
  {
    name: "memview read into a variable",
    prog: readIntoVar,
    triggers: 33,
    triggerBytes: 79_464,
    totalBytes: 79_472,
    executed: 33,
  },
  {
    name: "memview masked read into a variable",
    prog: maskedReadIntoVar,
    triggers: 12,
    triggerBytes: 28_896,
    totalBytes: 28_900,
    executed: 12,
  },
  {
    name: "MemCond",
    prog: () => {
      when(MemCond(3000, "atleast", 5), () => {
        SetDeaths(9000, SetTo, 1, 0);
      });
    },
    triggers: 1,
    triggerBytes: 2_408,
    totalBytes: 2_408,
    executed: 1,
  },
  {
    name: "memsetEPD constant value and count",
    prog: () => memsetEPD(3000, 0xaabbccdd, 2),
    triggers: 1,
    triggerBytes: 2_408,
    totalBytes: 2_408,
    executed: 1,
  },
  {
    name: "memcpyEPD constant count 2",
    prog: () => memcpyEPD(4000, 3000, 2),
    triggers: 41,
    triggerBytes: 98_728,
    totalBytes: 98_736,
    executed: 81,
  },
  {
    name: "memsetEPD variable count",
    prog: memsetVarCount,
    triggers: 8,
    triggerBytes: 19_264,
    totalBytes: 19_268,
    executed: 13,
  },
  {
    name: "memcpyEPD variable count",
    prog: memcpyVarCount,
    triggers: 42,
    triggerBytes: 101_136,
    totalBytes: 101_144,
    executed: 83,
  },
];

test("each probe pins its own static and executed cost", () => {
  for (const c of CASES) {
    const report = runSim(buildModule(c.prog).toJson(), 1);
    assert.equal(report.static_cost.triggers, c.triggers, `${c.name} triggers`);
    assert.equal(
      report.static_cost.trigger_bytes,
      c.triggerBytes,
      `${c.name} trigger bytes`,
    );
    assert.equal(
      report.static_cost.total_bytes,
      c.totalBytes,
      `${c.name} total bytes`,
    );
    assert.equal(
      report.executed_cost.triggers,
      c.executed,
      `${c.name} executed triggers`,
    );
  }
});

test("read returns the memory value", () => {
  const report = runSim(
    buildModule(() => {
      memsetEPD(3000, 0xaabbccdd, 1);
      readIntoVar();
    }).toJson(),
    1,
  );
  assert.equal(readDword(report, 3000), 0xaabbccdd);
});

test("block probes move the expected dwords", () => {
  const report = runSim(
    buildModule(() => {
      memsetEPD(3100, 0xdeadbeef, 4);
      memcpyEPD(4100, 3100, 2);
      memsetVarCount();
    }).toJson(),
    1,
  );
  assert.equal(readDword(report, 4100), 0xdeadbeef);
  assert.equal(readDword(report, 4101), 0xdeadbeef);
  assert.equal(readDword(report, 4102), 0);
  assert.equal(readDword(report, 3000), 0x11223344);
  assert.equal(readDword(report, 3001), 0x11223344);
  assert.equal(readDword(report, 3002), 0);
});

function cpOf(report: ReturnType<typeof runSim>): number | undefined {
  return report.external_epds[`${EPD_CURRENT_PLAYER}`];
}

test("constant and runtime copies end CP on the last source dword", () => {
  for (const n of [1, 2, 16]) {
    const report = runSim(
      buildModule(() => {
        memsetEPD(3000, 0xaabbccdd, 16);
        SetDeaths(EPD_CURRENT_PLAYER, SetTo, 1234, 0);
        memcpyEPD(5000, 3000, n);
      }).toJson(),
      1,
    );
    for (let i = 0; i < n; i += 1) {
      assert.equal(readDword(report, 5000 + i), 0xaabbccdd, `constant dword ${i}`);
    }
    assert.equal(readDword(report, 5000 + n), 0);
    assert.equal(cpOf(report), 3000 + n - 1, `constant CP N=${n}`);
  }

  for (const n of [1, 2, 16]) {
    const report = runSim(
      buildModule(() => {
        memsetEPD(3000, 0xaabbccdd, 16);
        SetDeaths(EPD_CURRENT_PLAYER, SetTo, 1234, 0);
        memcpyEPD(5000, 3000, Var(n));
      }).toJson(),
      1,
    );
    for (let i = 0; i < n; i += 1) {
      assert.equal(readDword(report, 5000 + i), 0xaabbccdd, `runtime dword ${i}`);
    }
    assert.equal(cpOf(report), 3000 + n - 1, `runtime CP N=${n}`);
  }
});

test("runtime zero count leaves CP and destination untouched", () => {
  const report = runSim(
    buildModule(() => {
      memsetEPD(3000, 0xaabbccdd, 16);
      SetDeaths(EPD_CURRENT_PLAYER, SetTo, 1234, 0);
      memcpyEPD(5000, 3000, Var(0));
    }).toJson(),
    1,
  );
  assert.equal(cpOf(report), 1234);
  assert.equal(readDword(report, 5000), 0);
});

test("blob source copies the payload", () => {
  const payload = Uint8Array.from({ length: 64 }, (_, i) => i);
  for (const n of [1, 2, 16]) {
    const report = runSim(
      buildModule(() => {
        memcpyEPD(5000, new Db(payload), n);
      }).toJson(),
      1,
    );
    const expected = Array.from({ length: n }, (_, i) =>
      new DataView(payload.buffer, i * 4, 4).getUint32(0, true),
    );
    for (let i = 0; i < n; i += 1) {
      assert.equal(readDword(report, 5000 + i), expected[i], `blob dword ${i}`);
    }
    assert.equal(readDword(report, 5000 + n), 0);
  }
});

test("two copies in sequence do not drift", () => {
  const report = runSim(
    buildModule(() => {
      memsetEPD(3000, 0x11223344, 16);
      memsetEPD(3100, 0x55667788, 16);
      SetDeaths(EPD_CURRENT_PLAYER, SetTo, 1234, 0);
      memcpyEPD(5000, 3000, 2);
      memcpyEPD(5100, 3100, 3);
    }).toJson(),
    1,
  );
  assert.equal(readDword(report, 5000), 0x11223344);
  assert.equal(readDword(report, 5001), 0x11223344);
  assert.equal(readDword(report, 5100), 0x55667788);
  assert.equal(readDword(report, 5101), 0x55667788);
  assert.equal(readDword(report, 5102), 0x55667788);
  assert.equal(readDword(report, 5103), 0);
  assert.equal(cpOf(report), 3102);
});

test("copy inside when and while keeps CP correct", () => {
  const inWhen = runSim(
    buildModule(() => {
      memsetEPD(3000, 0xaabbccdd, 16);
      SetDeaths(EPD_CURRENT_PLAYER, SetTo, 1234, 0);
      when(Var(1).eq(1), () => {
        memcpyEPD(5000, 3000, 2);
      });
    }).toJson(),
    1,
  );
  assert.equal(readDword(inWhen, 5000), 0xaabbccdd);
  assert.equal(readDword(inWhen, 5001), 0xaabbccdd);
  assert.equal(cpOf(inWhen), 3001);

  const inWhile = runSim(
    buildModule(() => {
      memsetEPD(3000, 0xaabbccdd, 16);
      const tick = Var(0);
      SetDeaths(EPD_CURRENT_PLAYER, SetTo, 1234, 0);
      whileLoop(tick.lt(2), () => {
        memcpyEPD(5000, 3000, 2);
        tick.iadd(1);
      });
    }).toJson(),
    1,
  );
  assert.equal(readDword(inWhile, 5000), 0xaabbccdd);
  assert.equal(readDword(inWhile, 5001), 0xaabbccdd);
  assert.equal(cpOf(inWhile), 3001);
});

test("constant-count runtime fill re-executes every loop", () => {
  // A constant count with a runtime fill value keeps the runtime loop, so the
  // counter must be re-stored every trigger loop (not just at module init).
  for (const loops of [1, 2, 3]) {
    const report = runSim(
      buildModule(() => {
        const v = Var(0);
        v.iadd(1);
        memsetEPD(3000, v, 2);
      }).toJson(),
      loops,
    );
    assert.equal(readDword(report, 3000), loops, `loop ${loops} dword 0`);
    assert.equal(readDword(report, 3001), loops, `loop ${loops} dword 1`);
    assert.equal(readDword(report, 3002), 0);
  }
});

test("constant-count runtime fill re-enters a while loop", () => {
  // Pass 1 writes the seed value, pass 2 writes its increment. An init-only
  // counter would leave pass 1's bytes.
  const report = runSim(
    buildModule(() => {
      const v = Var(0x11111111);
      const i = Var(0);
      whileLoop(i.lt(2), () => {
        memsetEPD(3000, v, 2);
        v.iadd(0x11111111);
        i.iadd(1);
      });
    }).toJson(),
    1,
  );
  assert.equal(readDword(report, 3000), 0x22222222);
  assert.equal(readDword(report, 3001), 0x22222222);
});

test("constant-count fill and copy repeat in one loop body", () => {
  // A non-folded constant-count fill and a constant-count copy in the same
  // repeated body must both re-seed on every pass: no stale counter, no
  // synthetic-id collision between the fill and copy triggers.
  const report = runSim(
    buildModule(() => {
      const v = Var(0x11111111);
      const i = Var(0);
      whileLoop(i.lt(2), () => {
        memsetEPD(3000, v, 2);
        memcpyEPD(4000, 3000, 2);
        v.iadd(0x11111111);
        i.iadd(1);
      });
    }).toJson(),
    1,
  );
  assert.equal(readDword(report, 3000), 0x22222222);
  assert.equal(readDword(report, 3001), 0x22222222);
  assert.equal(readDword(report, 4000), 0x22222222);
  assert.equal(readDword(report, 4001), 0x22222222);
});

test("fills write exactly the requested dwords", () => {
  for (const n of [1, 2, 16]) {
    const constFill = runSim(
      buildModule(() => memsetEPD(3000, 0x11223344, n)).toJson(),
      1,
    );
    for (let i = 0; i < n; i += 1) {
      assert.equal(readDword(constFill, 3000 + i), 0x11223344, `const fill ${i}`);
    }
    assert.equal(readDword(constFill, 3000 + n), 0);

    const varFill = runSim(
      buildModule(() => memsetEPD(3000, Var(0x11223344), n)).toJson(),
      1,
    );
    for (let i = 0; i < n; i += 1) {
      assert.equal(readDword(varFill, 3000 + i), 0x11223344, `var fill ${i}`);
    }
    assert.equal(readDword(varFill, 3000 + n), 0);
  }
});
