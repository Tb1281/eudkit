/**
 * Builder seam contract tests (candidate 8, shape C).
 *
 * These tests pin the builder seam interface directly — scope entry/exit
 * flushing, flush ordering, scope-serial invalidation, explicit-registration
 * ordering, and outside-buildModule fallback — instead of covering it only
 * indirectly via whole-program String/temporal tests. They are pins, not
 * bug fixes: every behaviour asserted here is the current behaviour, and
 * the two mirror asymmetries (flusher dedup, root pop) are pinned AS-IS
 * per language, explicitly NOT unified (see the per-language tests below).
 *
 * One pin was revised, not merely updated: the outside-buildModule var
 * fallback (candidate-8 shape C) turned out to be the corruption mechanism
 * behind the aligned-string-padding bug, so the var-scope fix revises that
 * contract to throwing EUDSL-SCOPE. The Trigger fallback pin is unchanged.
 */

import assert from "node:assert/strict";
import test from "node:test";

import {
  EPD,
  SetDeathsX,
  SetTo,
  EudslError,
  FixedString,
  Trigger,
  _getActiveModuleBuilder,
  _registerTrigger,
  allocVarSlot,
  buildModule,
  getNextPageSlot,
  getNextVarSlot,
  getScopeSerial,
  type HirAct,
  type ScopeBuilderHook,
} from "../index.ts";

function activeBuilder(): ScopeBuilderHook {
  const builder = _getActiveModuleBuilder() as unknown as ScopeBuilderHook;
  assert.ok(builder, "expected an active module builder");
  return builder;
}

function deathAct(value: number): HirAct {
  return SetDeathsX(EPD(0x58a368), SetTo, value, 0, 0xffffffff);
}

test("builder seam: flushers run in registration order at pushScope, popScope, and module end", () => {
  const log: string[] = [];
  const aligns: boolean[] = [];
  buildModule(() => {
    const builder = activeBuilder();
    assert.equal(typeof builder.registerFlusher, "function");
    builder.registerFlusher!((align) => {
      log.push("first");
      aligns.push(align);
    });
    builder.registerFlusher!((align) => {
      log.push("second");
      aligns.push(align);
    });
    builder.pushScope();
    assert.deepEqual(log, ["first", "second"]);
    builder.popScope();
    assert.deepEqual(log, ["first", "second", "first", "second"]);
  });
  // Module-end flush runs the flushers a third time.
  assert.deepEqual(log, [
    "first",
    "second",
    "first",
    "second",
    "first",
    "second",
  ]);
  // Scope-boundary flushes pass align=true; the module-end flush
  // passes align=false.
  assert.deepEqual(aligns, [true, true, true, true, false, false]);
});

test("builder seam: _registerTrigger places a trigger at registration time, after its patching acts", () => {
  const patchAct = deathAct(0x11111111);
  let t0id = -1;
  let t1id = -1;
  let ptrId = -1;
  const mod = buildModule(() => {
    const builder = activeBuilder();
    const t0 = new Trigger({
      conds: [],
      acts: [deathAct(1)],
      preserved: true,
    });
    t0id = t0.id;
    // Constructed before t1 but registered after it: construction order
    // must NOT determine HIR position.
    const ptr = new Trigger({
      conds: [],
      acts: [deathAct(2)],
      preserved: true,
      register: false,
    });
    ptrId = ptr.id;
    builder.queueAct(patchAct);
    const t1 = new Trigger({
      conds: [],
      acts: [deathAct(3)],
      preserved: true,
    });
    t1id = t1.id;
    _registerTrigger(ptr);
  });
  assert.ok(ptrId < t1id, "ptr id allocated before t1 id");
  const ids = mod.triggers.map((t) => t.id);
  // t0 and t1 register immediately, but the patch act queued between
  // them flushes into a bin-packed trigger BEFORE t1 at assembly (pending
  // acts always flush before the following trigger entry); ptr, which
  // registers last via _registerTrigger, still comes last — after both
  // t1 and its own patching act.
  assert.equal(ids.length, 4);
  assert.equal(ids[0], t0id);
  assert.equal(ids[2], t1id);
  assert.equal(ids[3], ptrId);
  const packed = mod.triggers[1];
  assert.equal(packed.acts.length, 1);
  assert.equal(packed.acts[0], patchAct);
});

test("builder seam: crossing a scope boundary invalidates String static positions via the scope serial", () => {
  let before = -1;
  let after = -1;
  let recorded = -1;
  buildModule(() => {
    const s = new FixedString(8);
    recorded = (s as unknown as { staticSerial: number }).staticSerial;
    before = getScopeSerial();
    assert.equal(recorded, before);
    const builder = activeBuilder();
    builder.pushScope();
    builder.popScope();
    after = getScopeSerial();
  });
  // One serial bump per scope change: pushScope AND popScope.
  assert.equal(after, before + 2);
  assert.notEqual(recorded, after);
});

test("builder seam (TS): registering the same flusher twice runs it once per flush (Set dedup)", () => {
  let count = 0;
  const fn = (): void => {
    count++;
  };
  buildModule(() => {
    const builder = activeBuilder();
    builder.registerFlusher!(fn);
    builder.registerFlusher!(fn);
    builder.pushScope();
    assert.equal(count, 1);
  });
});

test("builder seam (TS): popScope on the root scope returns the root entries and empties the stack", () => {
  // NOTE: act factories auto-queue into the active builder (buildAct),
  // so the act is created OUTSIDE buildModule; queueAct inside then
  // queues it exactly once instead of twice.
  const act = deathAct(9);
  let poppedLength = -1;
  let poppedKind = "";
  let poppedAct: unknown = null;
  const mod = buildModule(() => {
    const builder = activeBuilder();
    builder.queueAct(act);
    const popped = builder.popScope();
    poppedLength = popped.length;
    poppedKind = popped[0].kind;
    poppedAct = (popped[0] as { act: unknown }).act;
    // Restore a stack frame so final assembly still has a root scope.
    builder.pushScope();
  });
  assert.equal(poppedLength, 1);
  assert.equal(poppedKind, "act");
  assert.equal(poppedAct, act);
  // The popped act was discarded with the root scope: nothing survives.
  assert.equal(mod.triggers.length, 0);
});

test("builder seam: two Strings flush in construction order at a scope boundary", () => {
  const mod = buildModule(() => {
    const s1 = new FixedString(8);
    const s2 = new FixedString(8);
    // write() only marks the terminator as needed. At the next scope
    // flush, main's FixedString emits TWO acts per string: one reloc
    // at target_offset 0 (content dword) and one at target_offset 4
    // (terminator dword). The order of these acts in the final module
    // IS the flusher order; this test pins that order, not act count.
    s1.write("A");
    s2.write("B");
    const builder = activeBuilder();
    builder.pushScope();
    builder.popScope();
  });
  const order: number[] = [];
  for (const t of mod.triggers) {
    for (const act of t.acts) {
      const relocs = (
        act as unknown as {
          relocs?: Array<{ target_string?: number }>;
        }
      ).relocs;
      for (const reloc of relocs ?? []) {
        if (reloc.target_string !== undefined) {
          order.push(reloc.target_string);
        }
      }
    }
  }
  // Order only: all acts of the first-constructed string precede all
  // acts of the second-constructed string. The literal sequence is
  // [0, 0, 1, 1] on main's FixedString (two acts per string), but
  // pinning that would pin incidental act-splitting, which is
  // FixedString's business, not the seam's.
  const firstOccurrence = [...new Set(order)];
  assert.deepEqual(firstOccurrence, [0, 1]);
  assert.ok(order.lastIndexOf(0) < order.indexOf(1));
});

test("builder seam (TS): a flusher registered mid-scope runs at popScope; a flusher registered during a flush runs in the same flush", () => {
  const log: string[] = [];
  buildModule(() => {
    const builder = activeBuilder();
    builder.pushScope();
    builder.registerFlusher!(() => {
      log.push("mid");
    });
    builder.popScope();
    assert.deepEqual(log, ["mid"]);
    // Live Set iteration visits a flusher added during the same flush.
    // NOTE: flushers persist module-wide, so "mid" re-runs at every
    // later flush too; the assertions below include those re-runs.
    let added = false;
    const second = (): void => {
      log.push("second");
    };
    const first = (): void => {
      log.push("first");
      if (!added) {
        added = true;
        builder.registerFlusher!(second);
      }
    };
    builder.registerFlusher!(first);
    builder.pushScope();
    assert.deepEqual(log, ["mid", "mid", "first", "second"]);
  });
  // Module-end flush runs all three flushers once more.
  assert.deepEqual(log, [
    "mid",
    "mid",
    "first",
    "second",
    "mid",
    "first",
    "second",
  ]);
});

// CONTRACT REVISION (var-scope fix): the outside-buildModule var fallback
// below was a candidate-8 pin, but the fallback id collides with the first
// in-module var and silently corrupts output, so outside var allocation now
// throws EUDSL-SCOPE like allocString already does. Trigger fallback is
// unchanged (report-only, out of the var fix scope) and keeps its own pin.
test("builder seam: outside buildModule, var allocation throws EUDSL-SCOPE", () => {
  assert.throws(
    () => allocVarSlot(0),
    (e: unknown) => e instanceof EudslError && e.code === "EUDSL-SCOPE",
  );
  assert.throws(
    () => getNextVarSlot(),
    (e: unknown) => e instanceof EudslError && e.code === "EUDSL-SCOPE",
  );
  assert.throws(
    () => getNextPageSlot(),
    (e: unknown) => e instanceof EudslError && e.code === "EUDSL-SCOPE",
  );
});

test("builder seam: outside buildModule, Trigger ids still fall back to the module-independent counter (unchanged)", () => {
  const t0 = new Trigger({ conds: [], acts: [] });
  const t1 = new Trigger({ conds: [], acts: [] });
  assert.equal(t1.id, t0.id + 1);
});
