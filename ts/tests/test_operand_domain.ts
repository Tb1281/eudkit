/**
 * Ticket #37: operand domains are validated, not coerced (ADR 0019).
 *
 * Both field-write surfaces and every `Var` operand path accept a *plain
 * integer* only. Everything else raises `EudslError` with code `EUDSL-TYPE`
 * eagerly at the call, with the operand name in the message. Runtime operands
 * (`Var`, `ReadonlyVar`, `Expr`) stay legal; magnitude is not part of the
 * domain (`0x100000000` and `-1` normalise to 32 bits downstream).
 *
 * HEAD (28bbbdf) coerced or died unlabelled:
 * - `CUnit(1000).hp.set(x)` built for all seven bad values, writing
 *   0/0/0/1/1/0/0 at EPD 1002 (`true` and `1.5` wrote a *number*).
 * - `v.eq(1.5)` built HIR byte-identical to `v.eq(1)`; `v.eq(NaN)` to
 *   `v.eq(0)`.
 * - `v.eq("abc")`, `v.set(true)` and `v.ne(undefined)` built a
 *   `DeferredCond`/no-op that later died with the raw `reading 'op'`
 *   TypeError at materialisation.
 *
 * So every reject case here is red on 28bbbdf and green after the guard set;
 * `accepted operands build` passes on both sides by design (the legal domain
 * did not narrow). TypeScript cannot distinguish `1.0` from `1`, which is why
 * the reject row is `1.5` and the accepted list includes `1.0` on purpose.
 *
 * Ticket #40 closes the three families cycle 13 left open. HEAD (ba98fb0):
 * - the twelve field *comparison* bodies truncated through `rhs >>> 0`, so
 *   `CUnit(1000).hp.eq("abc" | undefined | null | NaN | Infinity)` built
 *   `Exactly 0`, `eq(1.5)` built `eq(1)`, `eq("5")` built `eq(5)`;
 * - `v.iand` / `v.ior` took an unguarded numeric fast path, so `iand(1.5)`,
 *   `iand(NaN)`, `ior(Infinity)` ... built silently while every other bitwise
 *   operator raised (they route through `materializeOperand`);
 * - `Var(NaN)` / `Var(Infinity)` built with init `0`, `Var(1.5)` with init
 *   `1`; `S(v=NaN)` built writing 0; `FixedArray.const([NaN])` built `[0]`
 *   and `FixedArray.const([0,1]).get(0).eq("abc")` folded to `Always()`
 *   (`eq(1.5)` to `Never()`).
 * The rows below are red on ba98fb0 and green after the guards; the rows
 * labelled *decision pin* are green on both sides by design.
 *
 * Case names are kept identical to T2's `py/tests/test_operand_domain.py` so
 * the two files can be diffed row by row. The TS table has one extra bad
 * value, `undefined`, which Python does not have.
 *
 * Ticket #49 closes the **death-table seam** on both frontends: the two shared
 * resolve functions (`resolveEpdDest` / `resolveValueOrPtr`) ended in
 * `(x as number) >>> 0` — so `Deaths(1002, Exactly, "abc")` built `amount: 0`
 * and `Deaths(1.5, …)` built `player: 1` — and the record builders masked
 * without validating (`Deaths(1002, 1.5, 1)` built `comparison: 1`). `MemCond`
 * forwarded `comparison` / `sizeOrMask` straight into the records. The rows
 * below pin the guards; the relocation forms (`Var` / `TriggerFieldRef`) stay
 * legal and are pinned as controls.
 *
 * The two S2 rows in the py sibling (`CUnit(Var(0)); u.hp = u.hp`) have **no
 * TS twin**: TS has no `__setattr__` write-back to guard, so this file
 * deliberately does not grow a same-name stub.
 */

import assert from "node:assert/strict";
import test from "node:test";

import {
  Add,
  Always,
  CUnit,
  Deaths,
  DeathsX,
  Exactly,
  FixedArray,
  FixedString,
  MemCond,
  Memory,
  MemoryX,
  Never,
  SetDeaths,
  SetDeathsX,
  SetMemory,
  SetMemoryX,
  SetNextPtr,
  SetStringDword,
  SetTo,
  Trigger,
  TriggerFieldRef,
  TriggerStruct,
  Var,
  asReadonly,
  buildModule,
  dword,
  memcpyEPD,
  memsetEPD,
  when,
  withCP,
} from "../index.ts";

const OPS = ["eq", "ne", "ge", "le", "gt", "lt"] as const;
type Op = (typeof OPS)[number];

const WRITES = ["set", "iadd", "isub", "act_set", "act_add", "act_sub"] as const;
type Write = (typeof WRITES)[number];

/** The non-domain operands, in T2's order (plus `undefined` for JS). */
const BAD_VALUES: Array<[string, unknown]> = [
  ["str", "abc"],
  ["undefined", undefined],
  ["null", null],
  ["bool", true],
  ["float", 1.5],
  ["nan", NaN],
  ["inf", Infinity],
  ["numeric-string", "5"],
];

/**
 * Accepted constants: any integer magnitude is in-domain (ADR 0019 §2).
 *
 * `1.0` is the TS side of the per-frontend decision (T1 §4): JavaScript has no
 * `1.0` — `Number.isInteger(1.0)` is true — so the TS frontend accepts it
 * while the py sibling rejects the float row. Do not "fix" this by narrowing
 * the TS domain.
 */
const GOOD_VALUES = [0, -1, 0xff, 0x100000000, 1.0];

/**
 * `BAD_VALUES` minus `undefined`, for operands that land in a parameter with a
 * JS default (`unit`, `mask`, `sizeOrMask`): an explicit `undefined` triggers
 * the default instead of reaching the guard, exactly as `Var(undefined)` is
 * `Var()` in the sibling tests. py has no such swallowing — `None` is a value.
 */
const NO_DEFAULT_VALUES = BAD_VALUES.filter(([, value]) => value !== undefined);

/** `BAD_VALUES` minus the string rows — `MemCond`'s comparison-*name* branch. */
const NON_STRING_VALUES = BAD_VALUES.filter(
  ([, value]) => typeof value !== "string",
);

/**
 * ... and minus `undefined`, for the *record field* rows: a record key that is
 * absent and one that is `undefined` are the same thing to `buildCond` /
 * `buildAct` (both mean "use the default"), so `undefined` cannot reach the
 * field guard. py distinguishes them (`None` is a value) and keeps the row.
 */
const NON_STRING_NO_DEFAULT_VALUES = NON_STRING_VALUES.filter(
  ([, value]) => value !== undefined,
);

/**
 * Expected message for a `Var`-operand path: a numeric bad value hits the
 * plain-int guard, a non-number never reaches it (the operand-shape check
 * fires first), so the two rows name the domain differently on purpose.
 */
function varOperandMessage(value: unknown): string {
  return typeof value === "number"
    ? "must be a plain integer"
    : "integer constant, a Var, or an Expr";
}

/**
 * Expected message for a `Var` *write*. `set`/`iadd`/`isub` dispatch on the
 * operand shape before the numeric guard, so a non-number names the shape;
 * `act_set`/`act_add`/`act_sub` are numeric-only action constructors, so
 * every bad value names the plain-int domain.
 */
function varWriteMessage(method: Write, value: unknown): string {
  return method.startsWith("act_")
    ? "must be a plain integer"
    : varOperandMessage(value);
}

function expectEudslType(
  fn: () => unknown,
  label: string,
  expected: string,
): void {
  let caught: unknown;
  try {
    fn();
  } catch (error) {
    caught = error;
  }
  assert.ok(
    caught instanceof Error,
    `${label}: expected EUDSL-TYPE, nothing was thrown`,
  );
  assert.equal(
    (caught as { code?: string }).code,
    "EUDSL-TYPE",
    `${label}: ${(caught as Error).message}`,
  );
  assert.ok(
    (caught as Error).message.includes(expected),
    `${label}: message must name the domain (${expected}), got ${(caught as Error).message}`,
  );
}

/**
 * #49: the code, the stable domain substring, and the operand surface — not
 * whole-string equality. The two frontends render the *value* differently
 * (`got a float` vs `got 1.5`, ADR 0019 §1), so the pair pins the code plus
 * the two fragments that must not drift.
 */
function expectPlainIntError(
  fn: () => unknown,
  label: string,
  surface: string,
): void {
  let caught: unknown;
  try {
    fn();
  } catch (error) {
    caught = error;
  }
  assert.ok(
    caught instanceof Error,
    `${label}: expected EUDSL-TYPE, nothing was thrown`,
  );
  const message = (caught as Error).message;
  assert.equal(
    (caught as { code?: string }).code,
    "EUDSL-TYPE",
    `${label}: ${message}`,
  );
  assert.ok(
    message.includes("must be a plain integer"),
    `${label}: message must name the domain, got ${message}`,
  );
  assert.ok(
    message.includes(surface),
    `${label}: message must name the operand surface (${surface}), got ${message}`,
  );
}

const FieldBits = TriggerStruct("OperandDomainBits", {
  v: dword(),
});

test("mem field write rejects bad operand", () => {
  buildModule(() => {
    const field = CUnit(1000).hp;
    for (const [caseName, value] of BAD_VALUES) {
      for (const method of WRITES) {
        // Eagerly at the write call itself: the exception escapes `set` /
        // `act_set` / ... before `buildModule` finishes.
        expectEudslType(
          () => (field as any)[method](value),
          `hp.${method}(${caseName})`,
          "must be a plain integer",
        );
      }
    }
  });
});

test("trigger field write rejects bad operand", () => {
  buildModule(() => {
    const inst = FieldBits.instantiate();
    for (const [caseName, value] of BAD_VALUES) {
      for (const method of WRITES) {
        expectEudslType(
          () => (inst.v as any)[method](value),
          `field.${method}(${caseName})`,
          varWriteMessage(method, value),
        );
      }
    }
  });
});

test("mem field comparison rejects bad operand", () => {
  // Red on ba98fb0: every row built (the constant truncated through
  // `rhs >>> 0`), `hp.eq("abc")` compiling to "hp == 0".
  buildModule(() => {
    const field = CUnit(1000).hp;
    for (const [caseName, value] of BAD_VALUES) {
      for (const op of OPS) {
        expectEudslType(
          () => (field as any)[op](value),
          `hp.${op}(${caseName})`,
          "must be a plain integer",
        );
      }
    }
  });

  // N3: the exception escapes the comparison call itself — no builder in
  // scope, so nothing can be queued behind a deferred validation.
  expectEudslType(
    () => (CUnit(1000).hp as any).eq(1.5),
    "hp.eq(1.5) outside buildModule",
    "must be a plain integer",
  );
});

test("trigger field comparison rejects bad operand", () => {
  // A `TriggerStruct` field is a `VarImpl`, so use a real `TriggerFieldRef`
  // off a bound condition slot (4-byte `player`). Red on ba98fb0: every row
  // built — `player.eq("abc")` folded to `Exactly 0`, `eq("5")` to `eq(5)`.
  buildModule(() => {
    const hit = Var(0);
    const slot = new Trigger({ conds: [], acts: [hit.act_set(1)] }).cond(0);
    const field = slot.player;
    for (const [caseName, value] of BAD_VALUES) {
      for (const op of OPS) {
        expectEudslType(
          () => (field as any)[op](value),
          `cond.player.${op}(${caseName})`,
          "must be a plain integer",
        );
      }
    }
  });

  // N3: eager at the call, with no builder in scope.
  expectEudslType(
    () => new TriggerFieldRef(0, 0, 4).eq(1.5),
    "TriggerFieldRef.eq(1.5) outside buildModule",
    "must be a plain integer",
  );
});

test("var write rejects bad operand", () => {
  buildModule(() => {
    const v = Var(0);
    for (const [caseName, value] of BAD_VALUES) {
      for (const method of WRITES) {
        expectEudslType(
          () => (v as any)[method](value),
          `v.${method}(${caseName})`,
          varWriteMessage(method, value),
        );
      }
    }
  });
});

test("comparison rejects bad operand", () => {
  buildModule(() => {
    const v = Var(0);
    for (const [caseName, value] of BAD_VALUES) {
      for (const op of OPS) {
        expectEudslType(
          () => (v as any)[op](value),
          `v.${op}(${caseName})`,
          varOperandMessage(value),
        );
      }
    }
  });
});

test("iand ior reject bad operand", () => {
  // Red on ba98fb0 for the numeric rows: `iand`/`ior` had an unguarded
  // numeric fast path while `ixor`/`ishl`/`ishr`/`imul`/`idiv`/`imod` all
  // raised through `materializeOperand`. The string/bool rows are green
  // before (they already took the expression path).
  buildModule(() => {
    for (const [caseName, value] of BAD_VALUES) {
      const expected = varOperandMessage(value);
      expectEudslType(
        () => Var(0).iand(value as any),
        `v.iand(${caseName})`,
        expected,
      );
      expectEudslType(
        () => Var(0).ior(value as any),
        `v.ior(${caseName})`,
        expected,
      );
    }

    // Control: legal constants still take the fast path and build.
    const v = Var(0);
    v.iand(0xf0);
    v.ior(0x0f);

    // Internal consistency: the sibling bitwise ops raise the same code.
    expectEudslType(
      () => Var(0).ixor(1.5),
      "v.ixor(1.5)",
      "must be a plain integer",
    );
  });
});

test("var init rejects bad operand", () => {
  // Red on ba98fb0 for the numeric rows: `Var(NaN)`/`Var(Infinity)` built
  // with init 0, `Var(1.5)` with init 1 (`init >>> 0`). The string/bool rows
  // are *decision pins* — they already raised via `set` on HEAD.
  buildModule(() => {
    for (const [caseName, value] of BAD_VALUES) {
      // `Var(undefined)` is `Var()` — the default parameter swallows it
      // before the guard, so it is not an operand surface.
      if (value === undefined) continue;
      expectEudslType(
        () => Var(value as any),
        `Var(${caseName})`,
        varOperandMessage(value),
      );
    }

    // Decision pin (T1 §4): TS accepts `1.0` — JS has no such literal, so
    // `Number.isInteger(1.0)` is true and the init normalises to 1. py
    // rejects the float row.
    assert.equal(Var(1.0).initialValue, 1);
  });
});

test("struct init rejects bad operand", () => {
  // Red on ba98fb0 for the numeric rows: `S({v: NaN})` built writing 0,
  // `S({v: 1.5})` writing 1 (`val & mask`). The non-number rows are decision
  // pins — they already raised through the guarded `VarImpl.set` path.
  const S = TriggerStruct("OperandDomainInit", { v: dword() });
  buildModule(() => {
    for (const [caseName, value] of BAD_VALUES) {
      expectEudslType(
        () => S({ v: value as any }),
        `S({v: ${caseName}})`,
        varOperandMessage(value),
      );
    }
  });
});

test("const array literal rejects bad operand", () => {
  // Red on ba98fb0: `FixedArray.const([NaN])` / `([null])` built `[0]`,
  // `([1.5])` / `(["5"])` / `([true])` built `[1]` / `[5]` / `[1]`.
  for (const [caseName, value] of BAD_VALUES) {
    expectEudslType(
      () => FixedArray.const([value as number]),
      `FixedArray.const([${caseName}])`,
      "const array value must be a plain integer",
    );
  }
});

test("const array comparison rejects bad operand", () => {
  // Red on ba98fb0: the constant truncated, so `eq("abc" | NaN | null |
  // undefined)` folded to `Always()` (0 == stored 0) and `eq(1.5)` to
  // `Never()` (1 != stored 0) — a silent wrong answer about memory.
  const arr = FixedArray.const([0, 1]);
  for (const [caseName, value] of BAD_VALUES) {
    for (const op of OPS) {
      expectEudslType(
        () => (arr.get(0) as any)[op](value),
        `constArray.get(0).${op}(${caseName})`,
        "const array comparison must be a plain integer",
      );
    }
  }

  // Control: the constant fold itself is untouched.
  assert.deepEqual(arr.get(0).eq(0), Always());
  assert.deepEqual(arr.get(1).eq(0), Never());
});

test("cp path error queues no act", () => {
  // S4: the CP target hop used to be queued before the operand guard, so a
  // rejected write left a stray `SetDeaths(CurrentPlayer, ...)` behind.
  // ba98fb0: withError is 978 chars vs 710 control. After the reorder both
  // sides are byte-identical at 710 chars — the same length as py's fixed
  // result, so the HIR is identical across frontends for this fixture.
  for (const method of ["set", "iadd", "isub"] as const) {
    const withError = (): void => {
      try {
        withCP(7000, (cp) => {
          (cp.view(CUnit).hp as any)[method]("abc");
        });
      } catch (error) {
        assert.equal((error as { code?: string }).code, "EUDSL-TYPE");
      }
      const hit = Var(0);
      when([hit.eq(0)], () => hit.iadd(1));
    };
    const control = (): void => {
      withCP(7000, () => {});
      const hit = Var(0);
      when([hit.eq(0)], () => hit.iadd(1));
    };

    const failed = buildModule(withError).toJson();
    const expected = buildModule(control).toJson();
    assert.equal(
      failed.length,
      710,
      `hp.${method}(bad) inside withCP must not queue a CP hop`,
    );
    assert.equal(
      failed,
      expected,
      `hp.${method}(bad) inside withCP must leave the HIR untouched`,
    );
  }
});

test("accepted operands build", () => {
  for (const value of GOOD_VALUES) {
    const mod = buildModule(() => {
      const v = Var(0);
      const hit = Var(0);
      const field = CUnit(1000).hp;
      for (const method of WRITES) {
        (field as any)[method](value);
        (v as any)[method](value);
      }
      for (const op of OPS as readonly Op[]) {
        when([(v as any)[op](value)], () => hit.iadd(1));
      }
    });
    assert.equal(mod.toJson().length > 0, true);
  }

  // Runtime operands stay legal (ADR 0019 §3).
  const mod = buildModule(() => {
    const v = Var(0);
    const w = Var(0);
    const hit = Var(0);
    v.set(w);
    v.iadd(w);
    v.isub(w);
    v.eq(w);
    for (const op of OPS as readonly Op[]) {
      when([(v as any)[op](w)], () => hit.iadd(1));
    }
    when([v.eq(v.add(1))], () => hit.iadd(1));
    when([v.eq(asReadonly(w))], () => hit.iadd(1));
  });
  assert.equal(mod.toJson().length > 0, true);
});

test("operand materializer domains match their adapters", () => {
  // FE4: `toVar` / `toOperand` are the single materializer kernel; each caller
  // keeps its narrowed domain. TS has no runtime `ReadonlyVar` wrapper
  // (`asReadonly` is a type-only cast over `VarImpl`), so the blockio
  // ReadonlyVar row is compile-time only here; the py sibling pins the
  // runtime EUDSL-TYPE message.
  buildModule(() => {
    const v = Var(0);
    const ro = asReadonly(v);

    // var: the full operand domain, including plain constants.
    v.set(1);
    v.set(v);
    v.set(ro);
    v.set(v.add(1));

    // string: runtime position accepts Var, Expr, and ReadonlyVar.
    const s = new FixedString(8);
    s.writeFrom(v, "a");
    s.writeFrom(v.add(1), "b");
    s.writeFrom(ro, "c");

    // blockio: Var and Expr are legal block operands.
    memsetEPD(3000, v.add(1), v.add(1));
    memcpyEPD(4000, 3000, v.add(1));
    memsetEPD(3000, v, 2);

    // @ts-expect-error — blockio's domain is Var | Expr; ReadonlyVar is not a block operand.
    if (false) memcpyEPD(3000, ro, 2);
  });
});

test("bad expr leaf rejects", () => {
  buildModule(() => {
    const v = Var(0);
    const hit = Var(0);
    // A bad leaf inside an `Expr` is caught at materialisation (the shared
    // operand choke point), not at `Expr` construction.
    expectEudslType(
      () => v.set(v.add("abc" as unknown as number)),
      'v.set(v.add("abc"))',
      "integer constant, a Var, or an Expr",
    );
    expectEudslType(
      () => when([v.eq(v.add("abc" as unknown as number))], () => hit.iadd(1)),
      'v.eq(v.add("abc"))',
      "integer constant, a Var, or an Expr",
    );
  });
});

// ---------------------------------------------------------------------------
// #49 — the death-table seam
// ---------------------------------------------------------------------------

test("memcond amount rejects bad operand", () => {
  // Red on 5eb941f: `1.5` / `"5"` / `true` built `amount: 1` / `5` / `1`,
  // `"abc"` / `NaN` / `undefined` built `amount: 0` (`int()`/`>>> 0`).
  buildModule(() => {
    for (const [caseName, value] of BAD_VALUES) {
      expectPlainIntError(
        () => MemCond(1002, "==", value as any),
        `MemCond(1002, "==", ${caseName})`,
        "memory value",
      );
    }
  });
});

test("memcond epd addr rejects bad operand", () => {
  // Red on 5eb941f: `MemCond(1.5, …)` built `player: 1`, `"abc"` built `0`.
  buildModule(() => {
    for (const [caseName, value] of BAD_VALUES) {
      expectPlainIntError(
        () => MemCond(value as any, "==", 1),
        `MemCond(${caseName}, "==", 1)`,
        "memory destination",
      );
    }
  });
});

test("memcond size or mask rejects bad operand", () => {
  // Red on 5eb941f: `mask: 1` for `1.5` / `true`, `mask: 0` for `NaN` /
  // `"abc"` — a silent partial write. `undefined` is skipped: it hits the
  // `sizeOrMask = 4` default (see `NO_DEFAULT_VALUES`).
  buildModule(() => {
    for (const [caseName, value] of NO_DEFAULT_VALUES) {
      expectPlainIntError(
        () => MemCond(1002, "==", 1, value as any),
        `MemCond(1002, "==", 1, ${caseName})`,
        "MemCond size_or_mask",
      );
    }
  });
});

test("memcond comparison rejects bad operand", () => {
  // Red on 5eb941f: `MemCond(1002, 1.5, 1)` built `comparison: 1`, `NaN` and
  // `"abc"`-as-number built `0`. Only the *numeric* branch is guarded: a
  // comparison *name* is the string branch and an unknown name stays
  // `EUDSL-CMP` (control below).
  buildModule(() => {
    for (const [caseName, value] of NON_STRING_VALUES) {
      expectPlainIntError(
        () => MemCond(1002, value as any, 1),
        `MemCond(1002, ${caseName}, 1)`,
        "MemCond comparison",
      );
    }
  });

  for (const name of ["abc", "5"]) {
    let caught: unknown;
    try {
      MemCond(1002, name, 1);
    } catch (error) {
      caught = error;
    }
    assert.equal((caught as { code?: string }).code, "EUDSL-CMP");
    assert.ok((caught as Error).message.includes(`Unknown comparison: ${name}`));
  }
});

test("deaths amount rejects bad operand", () => {
  // Red on 5eb941f: `amount: 1` / `5` / `1`, or `0` for `"abc"` / `NaN`.
  buildModule(() => {
    for (const [caseName, value] of BAD_VALUES) {
      expectPlainIntError(
        () => Deaths(1002, Exactly, value as any),
        `Deaths(1002, Exactly, ${caseName})`,
        "memory value",
      );
    }
  });
});

test("setdeaths value rejects bad operand", () => {
  buildModule(() => {
    for (const [caseName, value] of BAD_VALUES) {
      expectPlainIntError(
        () => SetDeaths(1002, SetTo, value as any),
        `SetDeaths(1002, SetTo, ${caseName})`,
        "memory value",
      );
    }
  });
});

test("memory records reject bad operand", () => {
  // Chokepoint proof — three of the nine generated memory helpers reach the
  // same two resolve functions, so one guard covers the family.
  buildModule(() => {
    const rows: Array<[string, (v: any) => unknown]> = [
      ["Memory", (v) => Memory(3000, Exactly, v)],
      ["SetMemory", (v) => SetMemory(3000, SetTo, v)],
      ["SetMemoryX", (v) => SetMemoryX(3000, SetTo, v, 0xffff)],
    ];
    for (const [caseName, value] of BAD_VALUES) {
      for (const [record, call] of rows) {
        expectPlainIntError(
          () => call(value),
          `${record}(${caseName})`,
          "memory value",
        );
      }
    }
  });
});

test("deaths epd dest rejects bad operand", () => {
  // Red on 5eb941f: `Deaths(1.5, …)` built `player: 1`, `"abc"` built `0`.
  buildModule(() => {
    for (const [caseName, value] of BAD_VALUES) {
      expectPlainIntError(
        () => Deaths(value as any, Exactly, 1),
        `Deaths(${caseName}, Exactly, 1)`,
        "memory destination",
      );
    }
  });
});

test("setdeaths epd dest rejects bad operand", () => {
  buildModule(() => {
    for (const [caseName, value] of BAD_VALUES) {
      expectPlainIntError(
        () => SetDeaths(value as any, SetTo, 1),
        `SetDeaths(${caseName}, SetTo, 1)`,
        "memory destination",
      );
    }
  });
});

test("deaths comparison field rejects bad operand", () => {
  // Red on 5eb941f: `Deaths(1002, 1.5, 1)` built `comparison: 1` (the record
  // masked without validating); `NaN` / `true` built `0` / `1`. `undefined`
  // is skipped: the record reads it as "omitted" (see the table note).
  buildModule(() => {
    for (const [caseName, value] of NON_STRING_NO_DEFAULT_VALUES) {
      expectPlainIntError(
        () => Deaths(1002, value as any, 1),
        `Deaths(1002, ${caseName}, 1)`,
        "condition field comparison",
      );
    }
  });
});

test("setdeaths modifier field rejects bad operand", () => {
  buildModule(() => {
    for (const [caseName, value] of NON_STRING_NO_DEFAULT_VALUES) {
      expectPlainIntError(
        () => SetDeaths(1002, value as any, 1),
        `SetDeaths(1002, ${caseName}, 1)`,
        "action field modifier",
      );
    }
  });
});

test("record unit field rejects bad operand", () => {
  // Red on 5eb941f: `unit: 1` for `1.5` / `true`, `unit: 0` for `"abc"`.
  // `undefined` is skipped: it hits the `unit = 0` default.
  buildModule(() => {
    const rows: Array<[string, (v: any) => unknown, string]> = [
      ["Deaths", (v) => Deaths(1002, Exactly, 1, v), "condition field unit"],
      ["SetDeaths", (v) => SetDeaths(1002, SetTo, 1, v), "action field unit"],
    ];
    for (const [caseName, value] of NO_DEFAULT_VALUES) {
      for (const [record, call, surface] of rows) {
        expectPlainIntError(
          () => call(value),
          `${record}(unit=${caseName})`,
          surface,
        );
      }
    }
  });
});

test("record mask field rejects bad operand", () => {
  // Red on 5eb941f: `SetDeathsX(1002, Add, 1, 0, 1.5)` built `mask: 1`.
  // `undefined` is skipped: it hits the `mask = 0xffffffff` default.
  buildModule(() => {
    for (const [caseName, value] of NO_DEFAULT_VALUES) {
      expectPlainIntError(
        () => SetDeathsX(1002, Add, 1, 0, value as any),
        `SetDeathsX(mask=${caseName})`,
        "action field mask",
      );
    }
  });
});

test("setstringdword fields reject bad operand", () => {
  // cycle 17: the hand-written row masked before the guard. HEAD:
  // `value=1.5` builds `value: 1`, `mask=1.5` builds `mask: 1, eudx: 17235`
  // (a different act classification), and `modifier=true` builds
  // `modifier: 1`; the rest are silent too. `index`/`byteOffset` stay
  // EUDSL-BOUNDS (a different family).
  buildModule(() => {
    for (const [caseName, value] of BAD_VALUES) {
      expectPlainIntError(
        () => SetStringDword(0, 0, value as any),
        `SetStringDword(value=${caseName})`,
        "SetStringDword value",
      );
    }
    // `undefined` hits the JS default (`0xffffffff` / `SetTo`), exactly as
    // `Var(undefined)` is `Var()` elsewhere in this file.
    for (const [caseName, value] of NO_DEFAULT_VALUES) {
      expectPlainIntError(
        () => SetStringDword(0, 0, 1, value as any),
        `SetStringDword(mask=${caseName})`,
        "SetStringDword mask",
      );
      expectPlainIntError(
        () => SetStringDword(0, 0, 1, 0xffffffff, value as any),
        `SetStringDword(modifier=${caseName})`,
        "SetStringDword modifier",
      );
    }
  });
});

test("memory addr rejects bad operand", () => {
  // cycle 17: `EPD()` folded the operand into the address-0 lane. HEAD:
  // every row builds `player: 4293515047` / `dest: 4293515047` silently
  // because the guard ran *after* `EPD()`. Only parameter 1 (`addr`) is
  // under test.
  const rows: Array<[string, (value: unknown) => unknown]> = [
    ["Memory", (value) => Memory(value as any, Exactly, 1)],
    ["MemoryX", (value) => MemoryX(value as any, Exactly, 1, 0xffff)],
    ["SetMemory", (value) => SetMemory(value as any, SetTo, 1)],
    ["SetMemoryX", (value) => SetMemoryX(value as any, SetTo, 1, 0xffff)],
  ];
  buildModule(() => {
    for (const [caseName, value] of BAD_VALUES) {
      for (const [record, call] of rows) {
        expectPlainIntError(
          () => call(value),
          `${record}(addr=${caseName})`,
          "memory address",
        );
      }
    }
  });
});

test("setnextptr src rejects bad operand", () => {
  // cycle 17: `.next` was dereferenced before any type check. HEAD: `null` /
  // `undefined` throw a raw `TypeError` and the rest raise `EUDSL-TYPE`
  // naming `undefined` (the failed lookup), not the operand the author
  // passed. A plain integer is *also* rejected — it has no `.next`; the
  // legal forms are a `Trigger` and a `TriggerFieldRef`.
  buildModule(() => {
    const t = new Trigger({ conds: [], acts: [] });
    for (const [caseName, value] of BAD_VALUES) {
      expectPlainIntError(
        () => SetNextPtr(value as any, t),
        `SetNextPtr(src=${caseName})`,
        "src",
      );
    }
  });
});

test("string and memory operand controls are unchanged", () => {
  // Control — green before and after: guard, not a semantic change. Pinned
  // literally (fields AND `relocs`) and byte-identical to 51c6e2e, so "no
  // HIR movement for legal programs" proves the cycle-17 guards only
  // narrowed the operand domain.
  buildModule(() => {
    assert.equal(Memory(0x58a364, Exactly, 1).player, 0);
    assert.equal(SetMemory(0x58a364, SetTo, 1).dest, 0);
    assert.deepEqual(
      { ...SetStringDword(0, 0, 1, 0xffffffff, SetTo) },
      {
        mask: 0,
        strid: 0,
        wavid: 0,
        time: 0,
        dest: 0,
        value: 1,
        unit: 0,
        acttype: 45,
        modifier: SetTo,
        flags: 20,
        padding: 0,
        eudx: 0,
        relocs: [
          { field_offset: 16, target_string: 0, target_offset: 0, mode: 1 },
        ],
      },
    );
    const t = new Trigger({ conds: [], acts: [] });
    assert.deepEqual(
      { ...SetNextPtr(t, t) },
      {
        mask: 0,
        strid: 0,
        wavid: 0,
        time: 0,
        dest: 0,
        value: 0,
        unit: 0,
        acttype: 45,
        modifier: SetTo,
        flags: 20,
        padding: 0,
        eudx: 0,
        relocs: [
          { field_offset: 16, target_trigger: 0, target_offset: 4, mode: 1 },
          { field_offset: 20, target_trigger: 0, target_offset: 0, mode: 4 },
        ],
      },
    );
  });
});

test("death table runtime operands build", () => {
  // Control — green before and after: `Var.value` is a `TriggerFieldRef`, so
  // the resolve functions consume it *before* the plain-int guard. Pinned
  // literally (fields AND `relocs`) and byte-identical to 5eb941f, so a later
  // "simplification" cannot narrow the runtime domain silently.
  buildModule(() => {
    const t = new Trigger({ conds: [], acts: [] });
    const v = Var(0);
    assert.deepEqual(
      { ...Deaths(1002, Exactly, v) },
      {
        locid: 0,
        player: 1002,
        amount: 0,
        unit: 0,
        comparison: Exactly,
        condtype: 15,
        restype: 0,
        flags: 0,
        eudx: 0,
        relocs: [
          { field_offset: 8, target_var: 0, target_offset: 20, mode: 4 },
        ],
      },
    );
    assert.deepEqual(
      { ...Deaths(v, Exactly, 1) },
      {
        locid: 0,
        player: 0,
        amount: 1,
        unit: 0,
        comparison: Exactly,
        condtype: 15,
        restype: 0,
        flags: 0,
        eudx: 0,
        relocs: [
          { field_offset: 4, target_var: 0, target_offset: 20, mode: 1 },
        ],
      },
    );
    assert.deepEqual(
      { ...SetDeaths(1002, SetTo, v) },
      {
        mask: 0,
        strid: 0,
        wavid: 0,
        time: 0,
        dest: 1002,
        value: 0,
        unit: 0,
        acttype: 45,
        modifier: SetTo,
        flags: 20,
        padding: 0,
        eudx: 0,
        relocs: [
          { field_offset: 20, target_var: 0, target_offset: 20, mode: 4 },
        ],
      },
    );
    assert.deepEqual(
      { ...SetDeaths(t.act(0).value, SetTo, 1) },
      {
        mask: 0,
        strid: 0,
        wavid: 0,
        time: 0,
        dest: 0,
        value: 1,
        unit: 0,
        acttype: 45,
        modifier: SetTo,
        flags: 20,
        padding: 0,
        eudx: 0,
        relocs: [
          { field_offset: 16, target_trigger: 0, target_offset: 348, mode: 1 },
        ],
      },
    );
    SetNextPtr(t, t);
  });
});

