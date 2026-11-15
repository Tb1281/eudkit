import test from "node:test";
import assert from "node:assert/strict";

import {
  buildModule,
  Var,
  VarImpl,
  lazyVar,
  asReadonly,
  FixedString,
  when,
  type ReadonlyVar,
} from "../index.ts";

// Ticket #97: the public ReadonlyVar surface must not expose raw field refs
// (`value`/`dest`/`mask`/`modifier`), `copy()`, or mutation methods.
// TypeScript enforcement is type-level only: negative assertions must use
// `@ts-expect-error`; there is no runtime guard to assert against.
type HasKey<T, K extends PropertyKey> = K extends keyof T ? true : false;
type Absent<T, K extends PropertyKey> = HasKey<T, K> extends false ? true : never;

type _NoValue = Absent<ReadonlyVar, "value">;
type _NoDest = Absent<ReadonlyVar, "dest">;
type _NoMask = Absent<ReadonlyVar, "mask">;
type _NoModifier = Absent<ReadonlyVar, "modifier">;
type _NoCopy = Absent<ReadonlyVar, "copy">;
type _NoSet = Absent<ReadonlyVar, "set">;
type _NoIadd = Absent<ReadonlyVar, "iadd">;
type _NoIsub = Absent<ReadonlyVar, "isub">;
type _NoImul = Absent<ReadonlyVar, "imul">;
type _NoIdiv = Absent<ReadonlyVar, "idiv">;
type _NoImod = Absent<ReadonlyVar, "imod">;
type _NoIand = Absent<ReadonlyVar, "iand">;
type _NoIor = Absent<ReadonlyVar, "ior">;
type _NoIxor = Absent<ReadonlyVar, "ixor">;
type _NoIshl = Absent<ReadonlyVar, "ishl">;
type _NoIshr = Absent<ReadonlyVar, "ishr">;
type _NoActSet = Absent<ReadonlyVar, "act_set">;
type _NoActAdd = Absent<ReadonlyVar, "act_add">;
type _NoActSub = Absent<ReadonlyVar, "act_sub">;

const _typeCheckAssertions: [
  _NoValue,
  _NoDest,
  _NoMask,
  _NoModifier,
  _NoCopy,
  _NoSet,
  _NoIadd,
  _NoIsub,
  _NoImul,
  _NoIdiv,
  _NoImod,
  _NoIand,
  _NoIor,
  _NoIxor,
  _NoIshl,
  _NoIshr,
  _NoActSet,
  _NoActAdd,
  _NoActSub,
] = [
  true,
  true,
  true,
  true,
  true,
  true,
  true,
  true,
  true,
  true,
  true,
  true,
  true,
  true,
  true,
  true,
  true,
  true,
  true,
];

void _typeCheckAssertions;

test("Ticket 97: ReadonlyVar reads, conditions, expressions, and formatting still compile", () => {
  const wave = lazyVar(3);
  const rv: ReadonlyVar = asReadonly(wave);

  assert.equal(rv.init, 3);
  assert.equal(rv.bitmask, 0xffffffff);

  buildModule(() => {
    assert.ok(rv.exactly(3));
    assert.ok(rv.gt(2));
    assert.ok(rv.lt(4));

    const expr = rv.add(1).bitXor(3);
    assert.ok(expr);
    const target = Var(0);
    target.set(rv.add(1));
    target.iadd(expr);
    const str = new FixedString(16);
    str.write("wave={:d}", rv);
    when(rv.atLeast(1), () => {});
  });
});

test("Ticket 97: raw field refs, copy, and mutators are rejected by the type", () => {
  buildModule(() => {
    const rv: ReadonlyVar = asReadonly(Var(1));

    // @ts-expect-error - .value is not part of the read-only surface
    rv.value;
    // @ts-expect-error - .value.set() has no field-ref escape hatch
    rv.value.set(1);
    // @ts-expect-error - .dest is not part of the read-only surface
    rv.dest;
    // @ts-expect-error - .mask is not part of the read-only surface
    rv.mask;
    // @ts-expect-error - .modifier is not part of the read-only surface
    rv.modifier;
    // @ts-expect-error - .copy() is not part of the read-only surface
    rv.copy();

    // @ts-expect-error - .set() does not exist on ReadonlyVar
    rv.set(1);
    // @ts-expect-error - .iadd() does not exist on ReadonlyVar
    rv.iadd(1);
    // @ts-expect-error - .isub() does not exist on ReadonlyVar
    rv.isub(1);
    // @ts-expect-error - .imul() does not exist on ReadonlyVar
    rv.imul(1);
    // @ts-expect-error - .idiv() does not exist on ReadonlyVar
    rv.idiv(1);
    // @ts-expect-error - .imod() does not exist on ReadonlyVar
    rv.imod(1);
    // @ts-expect-error - .iand() does not exist on ReadonlyVar
    rv.iand(1);
    // @ts-expect-error - .ior() does not exist on ReadonlyVar
    rv.ior(1);
    // @ts-expect-error - .ixor() does not exist on ReadonlyVar
    rv.ixor(1);
    // @ts-expect-error - .ishl() does not exist on ReadonlyVar
    rv.ishl(1);
    // @ts-expect-error - .ishr() does not exist on ReadonlyVar
    rv.ishr(1);
    // @ts-expect-error - .act_set() does not exist on ReadonlyVar
    rv.act_set(1);
    // @ts-expect-error - .act_add() does not exist on ReadonlyVar
    rv.act_add(1);
    // @ts-expect-error - .act_sub() does not exist on ReadonlyVar
    rv.act_sub(1);
  });
});

test("Ticket 97: TypeScript enforcement is type-level only", () => {
  const owner = lazyVar(1);
  const rv = asReadonly(owner);

  // The runtime value is still the owner's VarImpl; only the static type is
  // narrowed. This is the documented TS/Python enforcement difference.
  assert.equal(rv, owner);
  assert.ok(rv instanceof VarImpl);
});
