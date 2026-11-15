/**
 * Temporal and structured control flow primitives (`when`, `once`, `until`,
 * `onRising`, `onFalling`, `every`, `after`, `whileLoop`, `Func`, `dispatch`).
 */

import {
  flushEntriesToStmts,
  Never,
  SetTo,
  type HirCallArgNode,
  type HirCond,
  type HirFuncNode,
  type HirStmtNode,
  type ScopeBuilderHook,
} from "./hir.ts";
import {
  buildCondPlans,
  emitCondPlansLeafFirst,
  expandCondList,
  makePlanHooks,
  PlanContext,
  type PlanHooks,
} from "./conditions.ts";
import {
  _getActiveModuleBuilder,
  Trigger,
  type CondLike,
} from "./trigger.ts";
import { Expr, Var, VarImpl, emitVarTransfer, type VarOperand } from "./var.ts";
import { EudslError, _registerFuncFactory, describeValue } from "./view.ts";

export {
  condAll,
  cond_all,
  condAny,
  cond_any,
  condNot,
  cond_not,
} from "./conditions.ts";

export type CondInput = CondLike | readonly CondLike[];

function rawConds(conds: CondInput): readonly CondLike[] {
  if (Array.isArray(conds)) {
    return conds;
  }
  // Only `undefined` / `null` mean "no conds". A bare boolean (notably
  // `false`) must reach the guard below as an item, matching py, instead of
  // silently becoming an unconditional form.
  if (conds !== undefined && conds !== null) {
    return [conds as CondLike];
  }
  return [];
}

/** Reject a bare boolean in an already-flattened cond list. */
function rejectBooleanConds(raw: readonly CondLike[]): void {
  for (const c of raw) {
    if (typeof c === "boolean") {
      throw new EudslError(
        "EUDSL-TYPE",
        `condition list must not contain a boolean; got ${describeValue(c)}`,
      );
    }
  }
}

/**
 * Emission hooks for one structured-control evaluation site.
 *
 * The reset is a preserved empty-condition `Trigger` and each branch is a
 * plain `when`, exactly as the historical deferred gate emitted them.
 */
function planHooks(): PlanHooks {
  return makePlanHooks(
    () => _getActiveModuleBuilder() !== null,
    () => Var(0),
    (flag, value) => {
      new Trigger({
        conds: [],
        acts: [flag.act_set(value)],
        preserved: true,
      });
    },
    (conds, flag, value) => {
      when(conds, () => {
        flag.set(value);
      });
    },
  );
}

/**
 * Expand a condition list once at its use site.
 *
 * The single `PlanContext` gives every plan at this site one flag; a fresh
 * context is created for each one-shot construct.
 */
function normalizeConds(conds: CondInput): readonly HirCond[] {
  const raw = rawConds(conds);
  rejectBooleanConds(raw);
  return expandCondList(raw, new PlanContext(planHooks()));
}

function getBuilder(): ScopeBuilderHook {
  const builder = _getActiveModuleBuilder() as ScopeBuilderHook | null;
  if (!builder || typeof builder.pushScope !== "function") {
    throw new EudslError(
      "EUDSL-SCOPE",
      "Control flow constructs must be called inside buildModule()",
    );
  }
  return builder;
}

type ResolvedWhenArgs =
  | { drop: true }
  | { drop: false; conds: readonly CondLike[]; body: () => void };

/**
 * Resolve when/once arguments. A single function argument is the
 * body-only form (unconditional, 0 conds). Boolean true maps to 0
 * conds (NOT an Always cond). Boolean false drops the whole block: it
 * short-circuits BEFORE normalizeConds. Lists are normalised eagerly,
 * so a boolean inside a cond list raises EUDSL-TYPE.
 */
function resolveWhenArgs(
  condsOrBody: CondInput | boolean | (() => void),
  body: (() => void) | undefined,
  who: "when" | "once",
): ResolvedWhenArgs {
  if (typeof condsOrBody === "function") {
    if (body !== undefined) {
      throw new EudslError(
        "EUDSL-TYPE",
        `${who}(): first argument is a body function, but a second argument was also given`,
      );
    }
    return { drop: false, conds: [], body: condsOrBody };
  }
  if (typeof body !== "function") {
    throw new EudslError("EUDSL-TYPE", `${who}() requires a body function`);
  }
  if (condsOrBody === false) {
    return { drop: true };
  }
  const raw = condsOrBody === true ? [] : rawConds(condsOrBody);
  rejectBooleanConds(raw);
  return { drop: false, conds: raw, body };
}

/**
 * Queue a `when` node with the same scope discipline as py's `_WhenCM`:
 * push/run/pop/flush the then arm, then optionally push/run/pop/flush the
 * else arm in a fresh scope. The `else_body` key is omitted when the else
 * arm emits no statements, so then-only HIR stays byte-identical.
 */
function queueWhen(
  builder: ScopeBuilderHook,
  conds: readonly HirCond[],
  body: () => void,
  other?: () => void,
): void {
  builder.pushScope();
  try {
    body();
  } finally {
    const entries = builder.popScope();
    const stmts = flushEntriesToStmts(entries);
    const node: {
      kind: "when";
      conds: readonly HirCond[];
      body: HirStmtNode[];
      else_body?: HirStmtNode[];
    } = { kind: "when", conds, body: stmts };
    if (other !== undefined) {
      builder.pushScope();
      try {
        other();
      } finally {
        const elseEntries = builder.popScope();
        const elseStmts = flushEntriesToStmts(elseEntries);
        if (elseStmts.length > 0) node.else_body = elseStmts;
      }
    }
    builder.queueStmt(node);
  }
}

/**
 * Queue an unconditional `when([], body)`, omitting the node when the body
 * emits no statements. Mirrors py's dropped-branch `__exit__`, which only
 * queues the else arm when its flush is non-empty.
 */
function queueUnconditionalWhen(
  builder: ScopeBuilderHook,
  body: () => void,
): void {
  builder.pushScope();
  try {
    body();
  } finally {
    const entries = builder.popScope();
    const stmts = flushEntriesToStmts(entries);
    if (stmts.length > 0) {
      builder.queueStmt({ kind: "when", conds: [], body: stmts });
    }
  }
}

/**
 * Execute `body` on every trigger loop in which `conds` hold.
 * Body-only form when(body) is unconditional (0 conds); when(true, body)
 * is identical to it; when(false, body) drops the block entirely.
 *
 * The optional third argument is a runtime else arm: it runs iff
 * NOT(all conds). `when(false, body, other)` queues `other`
 * unconditionally, while `when(true, body, other)` / `when([], body, other)`
 * never run the else arm.
 */
export function when(body: () => void): void;
export function when(conds: CondInput | boolean, then: () => void): void;
export function when(
  conds: CondInput | boolean,
  then: () => void,
  other: () => void,
): void;
export function when(
  condsOrBody: CondInput | boolean | (() => void),
  then?: () => void,
  other?: () => void,
): void {
  const builder = getBuilder();
  if (typeof condsOrBody === "function") {
    if (then !== undefined || other !== undefined) {
      throw new EudslError(
        "EUDSL-TYPE",
        "when(): first argument is a body function, but a second argument was also given",
      );
    }
    queueWhen(builder, [], condsOrBody);
    return;
  }
  if (typeof then !== "function") {
    throw new EudslError("EUDSL-TYPE", "when() requires a body function");
  }
  if (other !== undefined && typeof other !== "function") {
    throw new EudslError(
      "EUDSL-TYPE",
      "when() else_body must be callable",
    );
  }
  if (condsOrBody === false) {
    if (other !== undefined) queueUnconditionalWhen(builder, other);
    return;
  }
  const conds = condsOrBody === true ? [] : normalizeConds(condsOrBody);
  if (conds.length === 0) {
    // The gate is statically open: the else arm is unreachable.
    queueWhen(builder, [], then);
    return;
  }
  queueWhen(builder, conds, then, other);
}

const DISPATCH_ELSE_CHAIN_MAX = 9;

interface DispatchValidation {
  builder: ScopeBuilderHook;
  value: VarOperand;
  normalized: Array<readonly [number, () => void]>;
  fallback: (() => void) | undefined;
}

/**
 * Validate a `dispatch` call in the documented deterministic order.
 *
 * Scope first (no arm or fallback body may run outside `buildModule`), then the
 * value domain, the cases container, the fallback callable, the non-empty
 * bound, and finally each `(key, body)` pair in the caller's order. Keys
 * normalise to their unsigned 32-bit value; duplicates are `EUDSL-BOUNDS`.
 */
function dispatchValidate(
  value: VarOperand,
  cases: readonly (readonly [number, () => void])[],
  fallback: (() => void) | undefined,
): DispatchValidation {
  const builder = _getActiveModuleBuilder() as ScopeBuilderHook | null;
  if (!builder || typeof builder.pushScope !== "function") {
    throw new EudslError(
      "EUDSL-SCOPE",
      "dispatch() must be called inside buildModule()",
    );
  }
  const valueIsOperand =
    typeof value === "number"
      ? Number.isInteger(value)
      : value instanceof VarImpl || value instanceof Expr;
  if (!valueIsOperand) {
    throw new EudslError(
      "EUDSL-TYPE",
      "dispatch(): value must be a Var, Expr, ReadonlyVar, or plain integer, " +
        `got ${describeValue(value)}`,
    );
  }
  if (!Array.isArray(cases)) {
    throw new EudslError(
      "EUDSL-TYPE",
      "dispatch(): cases must be an array of (key, body) pairs",
    );
  }
  if (fallback !== undefined && typeof fallback !== "function") {
    throw new EudslError("EUDSL-TYPE", "dispatch(): fallback must be callable");
  }
  if (cases.length === 0) {
    throw new EudslError(
      "EUDSL-BOUNDS",
      "dispatch(): cases must contain at least one (key, body) pair",
    );
  }
  const normalized: Array<readonly [number, () => void]> = [];
  const seen = new Set<number>();
  for (const item of cases) {
    if (!Array.isArray(item) || item.length !== 2) {
      throw new EudslError(
        "EUDSL-TYPE",
        "dispatch(): each case must be a (key, body) pair",
      );
    }
    const [key, body] = item as unknown as readonly [unknown, unknown];
    if (key instanceof VarImpl || key instanceof Expr) {
      throw new EudslError(
        "EUDSL-TYPE",
        "dispatch(): runtime keys are not supported; keys must be " +
          "compile-time plain integers",
      );
    }
    if (typeof key !== "number" || !Number.isInteger(key)) {
      throw new EudslError(
        "EUDSL-TYPE",
        "dispatch(): case keys must be plain integers, got " +
          `${describeValue(key)}`,
      );
    }
    if (typeof body !== "function") {
      throw new EudslError(
        "EUDSL-TYPE",
        `dispatch(): body for key ${key} must be callable`,
      );
    }
    // `>>> 0` forces unsigned 32-bit; `& 0xFFFFFFFF` would stay signed.
    const normKey = key >>> 0;
    if (seen.has(normKey)) {
      const hex = normKey.toString(16).toUpperCase().padStart(8, "0");
      throw new EudslError(
        "EUDSL-BOUNDS",
        `dispatch(): duplicate key 0x${hex} after 32-bit normalization`,
      );
    }
    seen.add(normKey);
    normalized.push([normKey, body as () => void]);
  }
  return { builder, value, normalized, fallback };
}

/** Run one zero-argument arm body in a fresh scope and flush its statements. */
function dispatchCapture(
  builder: ScopeBuilderHook,
  body: () => void,
): HirStmtNode[] {
  builder.pushScope();
  let stmts: HirStmtNode[] = [];
  try {
    body();
  } finally {
    stmts = flushEntriesToStmts(builder.popScope());
  }
  return stmts;
}

/** Build one `when` node; omit `else_body` when the arm is empty. */
function dispatchWhen(
  value: VarImpl,
  key: number,
  body: HirStmtNode[],
  elseStmts?: HirStmtNode[],
): HirStmtNode {
  const node: {
    kind: "when";
    conds: HirCond[];
    body: HirStmtNode[];
    else_body?: HirStmtNode[];
  } = { kind: "when", conds: [value.eq(key)], body };
  if (elseStmts !== undefined && elseStmts.length > 0) {
    node.else_body = elseStmts;
  }
  return node as unknown as HirStmtNode;
}

/**
 * Lower a one-of-N integer choice to the existing HIR.
 *
 * `value` is a plain number, `Var`, `Expr`, or `ReadonlyVar`. A plain integer
 * is folded at build time: only the selected arm, the fallback, or nothing is
 * emitted, with no `when` node. An `Expr` is materialized once into a fresh
 * `Var`; a `Var`/`ReadonlyVar` is used directly.
 *
 * `cases` is a non-empty ordered list of `(key, body)` pairs with compile-time
 * plain-integer keys, normalised to 32 bits and unique after normalisation.
 * Bodies are zero-argument builders. `fallback` (named, not `default`) is an
 * optional zero-argument builder; a miss without one is a documented no-op.
 *
 * Runtime `N <= 9` lowers to a nested `when(..., else_body)` chain built from
 * the last case backwards, so the first matching key wins. Runtime `N >= 10`
 * lowers to a balanced `HirWhen` tree (`ge(mid)` internal nodes, `eq(key)`
 * leaves); with a fallback, the same fallback statements are attached to every
 * leaf's `else_body` because only one leaf is reachable. No new HIR node and no
 * dense jump table are involved.
 */
export function dispatch(
  value: VarOperand,
  cases: readonly (readonly [number, () => void])[],
  fallback?: () => void,
): void {
  const validated = dispatchValidate(value, cases, fallback);
  const builder = validated.builder;
  const normalized = validated.normalized;
  const fallbackBody = validated.fallback;

  if (typeof value === "number") {
    const target = value >>> 0;
    let selected: (() => void) | undefined;
    for (const [key, body] of normalized) {
      if (key === target) {
        selected = body;
        break;
      }
    }
    selected = selected ?? fallbackBody;
    if (selected !== undefined) {
      for (const stmt of dispatchCapture(builder, selected)) {
        builder.queueStmt(stmt);
      }
    }
    return;
  }

  const operand = (value instanceof Expr ? new VarImpl(value) : value) as VarImpl;
  const bodies = new Map<number, HirStmtNode[]>();
  for (const [key, body] of normalized) {
    bodies.set(key, dispatchCapture(builder, body));
  }
  const fallbackStmts =
    fallbackBody === undefined ? [] : dispatchCapture(builder, fallbackBody);
  const count = normalized.length;
  if (count <= DISPATCH_ELSE_CHAIN_MAX) {
    let node: HirStmtNode | undefined;
    for (let index = count - 1; index >= 0; index--) {
      const [key] = normalized[index];
      const elseStmts =
        index === count - 1
          ? fallbackStmts
          : node === undefined
            ? undefined
            : [node];
      node = dispatchWhen(operand, key, bodies.get(key)!, elseStmts);
    }
    builder.queueStmt(node!);
    return;
  }

  const keys = normalized.map(([key]) => key).sort((a, b) => a - b);
  const buildTree = (subkeys: readonly number[]): HirStmtNode => {
    if (subkeys.length === 1) {
      const key = subkeys[0];
      return dispatchWhen(operand, key, bodies.get(key)!, fallbackStmts);
    }
    const mid = Math.floor(subkeys.length / 2);
    const key = subkeys[mid];
    const right = buildTree(subkeys.slice(mid));
    const left = buildTree(subkeys.slice(0, mid));
    return {
      kind: "when",
      conds: [operand.ge(key)],
      body: [right],
      else_body: [left],
    } as unknown as HirStmtNode;
  };
  builder.queueStmt(buildTree(keys));
}

/**
 * Execute `body` on the first trigger loop in which `conds` hold and permanently
 * self-disable afterwards (0T executed on subsequent loops).
 * Body-only form once(body) runs once unconditionally; once(true, body)
 * is identical to it; once(false, body) drops the block entirely.
 */
export function once(body: () => void): void;
export function once(conds: CondInput | boolean, body: () => void): void;
export function once(
  condsOrBody: CondInput | boolean | (() => void),
  body?: () => void,
): void {
  const builder = getBuilder();
  const resolved = resolveWhenArgs(condsOrBody, body, "once");
  if (resolved.drop) {
    return;
  }
  const conds = expandCondList(
    resolved.conds,
    new PlanContext(planHooks()),
  );
  builder.pushScope();
  try {
    resolved.body();
  } finally {
    const entries = builder.popScope();
    const stmts = flushEntriesToStmts(entries);
    builder.queueStmt({
      kind: "once",
      conds,
      body: stmts,
    });
  }
}

/**
 * Run `body` while `conds` are false; permanently disable once `conds` hold.
 * Conds stay REQUIRED (no body-only overload): until(true, body) is 0
 * conds, so the latch trips immediately and the body never runs (opt
 * emits only the reset trip-when for an unconditional gate — exclusive
 * semantics, issue #72; a flat leaf-act body no longer rides along in
 * a merged trip+body trigger);
 * until(false, body) maps to a Never cond, so the trip-when never
 * fires, the latch stays 1, and the body runs every loop.
 *
 * C11-02: emits a `HirUntil` node instead of desugaring. The latch
 * variable is still allocated here, before the body runs, in exactly
 * the position the old desugar allocated `active`, and is carried on
 * the node as `latch_var`; the opt temporal pass desugars the node to
 * the old 2-`when` latch shape (exactly, for gated `until`; only the
 * reset trip-when for an unconditional gate, whose body never runs)
 * using that id verbatim, so the node is the single home for the until
 * desugar and lowering never sees it. Allocating before pushScope/body
 * preserves the old id order (latch first, then body-allocated vars).
 */
export function until(conds: CondInput | boolean, body: () => void): void {
  const builder = getBuilder();
  // Boolean false maps to a Never cond (checked with === before any
  // int/cond handling): the trip-when `when(latch==1){ when(Never){
  // latch.set(0) } }` never fires, so the latch stays 1 and the
  // body-when runs every loop — exactly the decided until(false)
  // semantics, identical to until(Never(), body). Dropping false from
  // the list would instead give the until(true) shape (latch trips
  // immediately, body never runs), which is why false is NOT dropped.
  const normConds =
    conds === false ? [Never()] : conds === true ? [] : normalizeConds(conds);
  const active = Var(1);
  builder.pushScope();
  try {
    body();
  } finally {
    const entries = builder.popScope();
    const stmts = flushEntriesToStmts(entries);
    builder.queueStmt({
      kind: "until",
      conds: normConds,
      body: stmts,
      latch_var: active.id,
    });
  }
}

/**
 * Run `body` on the trigger loop where `conds` transition from false -> true.
 * True on loop 1 is considered a rising edge from initial state 0.
 *
 * C11-03: emits a `HirEdge` node instead of desugaring. The two state
 * variables are still allocated here, in the same order as the old
 * desugar allocated them, so their declarations (and therefore their
 * StoragePlan ranks) are identical to the baseline; the opt desugar
 * expands the node using these reserved ids.
 */
export function onRising(conds: CondInput, body: () => void): void {
  const builder = getBuilder();
  const normConds = normalizeConds(conds);
  const curr = Var(0);
  const prev = Var(0);
  builder.pushScope();
  try {
    body();
  } finally {
    const entries = builder.popScope();
    const stmts = flushEntriesToStmts(entries);
    builder.queueStmt({
      kind: "edge",
      direction: "rising",
      conds: normConds,
      body: stmts,
      curr_var: curr.id,
      prev_var: prev.id,
    });
  }
}

export const on_rising = onRising;

/**
 * Run `body` on the trigger loop where `conds` transition from true -> false.
 *
 * C11-03: emits a `HirEdge` node (direction `falling`) instead of
 * desugaring; state variables are reserved as in `onRising`.
 */
export function onFalling(conds: CondInput, body: () => void): void {
  const builder = getBuilder();
  const normConds = normalizeConds(conds);
  const curr = Var(0);
  const prev = Var(0);
  builder.pushScope();
  try {
    body();
  } finally {
    const entries = builder.popScope();
    const stmts = flushEntriesToStmts(entries);
    builder.queueStmt({
      kind: "edge",
      direction: "falling",
      conds: normConds,
      body: stmts,
      curr_var: curr.id,
      prev_var: prev.id,
    });
  }
}

export const on_falling = onFalling;

function validateCount(n: unknown, who: string): number {
  if (typeof n !== "number" || !Number.isInteger(n)) {
    throw new EudslError(
      "EUDSL-TYPE",
      `${who}: count must be a build-time integer constant, got ${n}`,
    );
  }
  if (n <= 0) {
    throw new EudslError(
      "EUDSL-RANGE",
      `${who}: count must be a positive integer, got ${n}`,
    );
  }
  return n;
}

/**
 * Run `body` every `n` trigger loops.
 *
 * C11-04: emits a `HirEvery` node instead of desugaring. The counter
 * variable is allocated here, at emission time in the old order (before
 * the body runs, exactly where the old desugar allocated it), and
 * carried on the node as `counter_var`; opt desugars using that id
 * verbatim and never mints var ids, so compiled output is byte-
 * identical to the old DSL desugar.
 */
export function every(n: number, body: () => void): void {
  const count = validateCount(n, "every");
  const builder = getBuilder();
  const cnt = Var(0);
  builder.pushScope();
  try {
    body();
  } finally {
    const entries = builder.popScope();
    const stmts = flushEntriesToStmts(entries);
    builder.queueStmt({
      kind: "every",
      n: count,
      counter_var: cnt.id,
      body: stmts,
    });
  }
}

/**
 * Delayed one-shot: arms an `n`-loop countdown when reached and fires once at 0.
 *
 * C11-04: emits a `HirAfter` node instead of desugaring. The counter
 * is allocated at emission time like `every`'s, and opt desugars to
 * exactly today's 3-`when` arm/decrement/fire shape.
 */
export function after(n: number, body: () => void): void {
  const count = validateCount(n, "after");
  const builder = getBuilder();
  const cnt = Var(0);
  builder.pushScope();
  try {
    body();
  } finally {
    const entries = builder.popScope();
    const stmts = flushEntriesToStmts(entries);
    builder.queueStmt({
      kind: "after",
      n: count,
      counter_var: cnt.id,
      body: stmts,
    });
  }
}

/**
 * Structured intra-frame loop repeating `body` as long as `conds` hold.
 */
export function whileLoop(conds: CondInput, body: () => void): void {
  const builder = getBuilder();
  const raw = rawConds(conds);
  rejectBooleanConds(raw);
  // Build one plan tree and one context for the whole loop: entry and tail
  // emissions share the same plan objects and flag ids, and the tail re-emits
  // the same plans from live memory.
  const plans = buildCondPlans(raw);
  const ctx = new PlanContext(planHooks());
  // Entry: preludes before the while gate -> first check reads live values.
  // Top-level literal leaves (deferred temp transfers) come first, then the
  // gate preludes, preserving master's emission order on mixed lists.
  const normConds = emitCondPlansLeafFirst(plans, ctx);
  builder.pushScope();
  try {
    body();
  } finally {
    // Tail: re-emit every plan with the SAME context as the last body
    // statements. Deferred temps materialize into their cached slots first,
    // gate flags are then rebuilt from live memory, and the restored jump
    // lands on a refreshed gate.
    emitCondPlansLeafFirst(plans, ctx);
    const entries = builder.popScope();
    const stmts = flushEntriesToStmts(entries);
    builder.queueStmt({
      kind: "while",
      conds: normConds,
      body: stmts,
    });
  }
}

export const while_loop = whileLoop;

/**
 * Declare a reusable subroutine and return its call handle.
 *
 * The body runs exactly once, at definition time, with one `Var` per
 * positional parameter; its statements are captured as a module-level `func`
 * and emitted once no matter how many sites call it. To return a value,
 * `return` a `Var`; the call then yields a fresh `Var` holding a copy of that
 * result (`return` nothing for a side-effecting subroutine).
 *
 * Parameters are counted by the function's `length`, so default and rest
 * parameters are not supported. Subroutines cannot recurse (directly or
 * indirectly): the return path is a single pointer patched per call site, so
 * a nested self-call would lose the outer return address. Cycles are rejected
 * at HIR validation.
 */
export type FuncHandle = ((...args: readonly (Var | number)[]) => Var | null) & {
  readonly funcId: number;
};

export function Func(
  body: (...params: Var[]) => Var | void | null,
): FuncHandle {
  const builder = getBuilder();
  if (
    typeof builder.captureEntries !== "function" ||
    typeof builder.allocFuncId !== "function" ||
    typeof builder.queueFunc !== "function" ||
    typeof builder.queueCall !== "function"
  ) {
    throw new EudslError(
      "EUDSL-SCOPE",
      "func() requires a module builder with subroutine support",
    );
  }
  if (typeof body !== "function") {
    throw new EudslError("EUDSL-TYPE", "func(): body must be a function");
  }
  const arity = body.length;
  const paramVars: Var[] = [];
  for (let i = 0; i < arity; i++) paramVars.push(Var(0));
  let resultId: number | null = null;

  const stmts = builder.captureEntries(() => {
    const result = body(...paramVars);
    if (result === undefined || result === null) return;
    if (!(result instanceof Var)) {
      throw new EudslError(
        "EUDSL-TYPE",
        `func(): body must return a Var or None, got ${describeValue(result)}`,
      );
    }
    // The returned var *is* the result slot: the call site copies it into a
    // fresh var, so a second copy here would only add a value move (a 64-act
    // TriggerPage firing) per executed call.
    resultId = result.id;
  });
  const funcId = builder.allocFuncId();
  const node: {
    id: number;
    params: number[];
    result?: number;
    body: HirStmtNode[];
  } = {
    id: funcId,
    params: paramVars.map((v) => v.id),
    body: stmts,
  };
  if (resultId !== null) node.result = resultId;
  builder.queueFunc(node as unknown as HirFuncNode);

  const queueCall = builder.queueCall;
  const handle = ((...args: readonly (Var | number)[]): Var | null => {
    if (args.length !== arity) {
      throw new EudslError(
        "EUDSL-BOUNDS",
        `func call expects ${arity} argument(s), got ${args.length}`,
      );
    }
    const encoded: HirCallArgNode[] = [];
    for (const arg of args) {
      if (arg instanceof Var) {
        encoded.push({ kind: "var", id: arg.id });
      } else if (typeof arg === "number" && Number.isInteger(arg)) {
        encoded.push({ kind: "const", value: arg >>> 0 });
      } else {
        throw new EudslError(
          "EUDSL-TYPE",
          `func arguments must be a Var or a constant integer, got ${describeValue(arg)}`,
        );
      }
    }
    const dst = resultId === null ? null : Var(0);
    queueCall.call(builder, funcId, encoded, dst === null ? null : dst.id);
    return dst;
  }) as FuncHandle;
  (handle as { funcId: number }).funcId = funcId;
  return handle;
}

export const func = Func;

// The shared full-dword reader in `view` declares its body through `Func`;
// `view` cannot import `temporal` (temporal imports view), so hand it over.
_registerFuncFactory(Func as Parameters<typeof _registerFuncFactory>[0]);
