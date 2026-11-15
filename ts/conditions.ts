/**
 * ADR 0028: composable condition combinators and the unified `GatePlan`
 * prelude.
 *
 * This module is a leaf: `view`, `var`, `temporal` and `trigger` all import it
 * without creating a new frontend cycle. It owns the public `condAll` /
 * `condAny` / `condNot` vocabulary plus the canonical plan emitter used by
 * every evaluation site.
 *
 * A `GatePlan` is a small boolean tree. `and` is spliced into the caller's
 * condition list; `or` and `not_opaque` need a runtime flag and therefore emit
 * a prelude (preserved reset trigger + one `when` per branch) immediately
 * before the consuming trigger.
 */

import { Always, Never, type HirAct } from "./hir.ts";
import { EudslError } from "./errors.ts";
import type {
  CompositeCond,
  CondLike,
  HirCond,
  MaterializableCond,
} from "./trigger.ts";

/** `condtype` of the always-true HIR condition. */
export const COND_ALWAYS = 22;
/** `condtype` of the always-false HIR condition. */
export const COND_NEVER = 23;

/** One node of a condition plan. */
export type GatePlan =
  | { kind: "lit"; cond: HirCond | MaterializableCond }
  | { kind: "and"; nodes: GatePlan[] }
  | { kind: "or"; nodes: GatePlan[] }
  | { kind: "not_opaque"; inner: CondLike };

/**
 * Structural flag handle supplied by the caller.
 *
 * `temporal` and `trigger` pass a `Var`; keeping the shape local avoids a
 * runtime import of `var` here.
 */
export interface GateFlag {
  act_set(value: number): HirAct;
  set(value: number): unknown;
  eq(value: number): HirCond;
}

/** Emission hooks owned by the consuming construct (`when` / `Trigger` / ...). */
export interface PlanHooks {
  /** True while a module builder is active (needed for flag allocation). */
  hasBuilder(): boolean;
  /** Allocate a fresh `Var(0)` flag. */
  allocFlag(): GateFlag;
  /** Emit the preserved empty-condition reset trigger. */
  emitReset(flag: GateFlag, value: number): void;
  /** Emit `when(conds, () => flag.set(value))`. */
  emitSetWhen(
    conds: readonly HirCond[],
    flag: GateFlag,
    value: number,
  ): void;
}

/**
 * Build the hook object shared by `temporal` and `trigger`.
 *
 * The emitting constructs live in other modules, so the caller injects the
 * four primitives; this helper owns the one object shape both sites use.
 */
export function makePlanHooks(
  hasBuilder: () => boolean,
  allocFlag: () => GateFlag,
  emitReset: (flag: GateFlag, value: number) => void,
  emitSetWhen: (
    conds: readonly HirCond[],
    flag: GateFlag,
    value: number,
  ) => void,
): PlanHooks {
  return { hasBuilder, allocFlag, emitReset, emitSetWhen };
}

/**
 * Plans whose detached (no-builder) fallback is the owner's legacy
 * `materialize()` path. `DeferredNe` registers its `or` plan here so a
 * detached raw `Trigger` keeps the pre-ADR error path verbatim.
 */
const detachedFallbacks = new WeakMap<GatePlan, MaterializableCond>();

/** Register the legacy detached fallback for a gate plan. @internal */
export function registerDetachedPlan(
  plan: GatePlan,
  cond: MaterializableCond,
): GatePlan {
  detachedFallbacks.set(plan, cond);
  return plan;
}

function describeArg(value: unknown): string {
  if (value === null) return "null";
  if (value === undefined) return "undefined";
  if (typeof value === "string") return JSON.stringify(value);
  if (typeof value === "number") return String(value);
  return `a ${typeof value}`;
}

function isConditionLike(value: unknown): value is CondLike {
  if (typeof value !== "object" || value === null) return false;
  const candidate = value as {
    condtype?: unknown;
    materialize?: unknown;
    gatePlan?: unknown;
  };
  return (
    typeof candidate.condtype === "number" ||
    typeof candidate.materialize === "function" ||
    typeof candidate.gatePlan === "function"
  );
}

function validateArg(value: unknown, where: string): CondLike {
  if (!isConditionLike(value)) {
    throw new EudslError(
      "EUDSL-TYPE",
      `${where} expects a condition, got ${describeArg(value)}`,
    );
  }
  return value;
}

/** Return the plan for a leaf condition, or `null` when it has none. */
function gatePlanOf(cond: CondLike): GatePlan | null {
  const candidate = cond as { gatePlan?: () => GatePlan | null };
  if (typeof candidate.gatePlan === "function") {
    return candidate.gatePlan();
  }
  return null;
}

function planOf(cond: CondLike): GatePlan {
  const plan = gatePlanOf(cond);
  // A `CompositeCond` always exposes `gatePlan()`, so the fallback only ever
  // receives a real leaf here.
  return plan ?? { kind: "lit", cond: cond as HirCond | MaterializableCond };
}

function resolveLit(cond: HirCond | MaterializableCond): HirCond;
function resolveLit(cond: CondLike): HirCond;
function resolveLit(cond: CondLike): HirCond {
  const candidate = cond as Partial<MaterializableCond>;
  return typeof candidate.materialize === "function"
    ? candidate.materialize()
    : (cond as HirCond);
}

function validatePlan(plan: GatePlan, seen: Set<GatePlan> = new Set()): void {
  if (seen.has(plan)) return;
  seen.add(plan);
  switch (plan.kind) {
    case "lit":
      validateArg(plan.cond, "condition");
      return;
    case "and":
    case "or":
      if (!Array.isArray(plan.nodes)) {
        throw new EudslError(
          "EUDSL-TYPE",
          `condition ${plan.kind} node expects a node list`,
        );
      }
      for (const node of plan.nodes) validatePlan(node, seen);
      return;
    case "not_opaque":
      validateArg(plan.inner, "condition complement");
      return;
    default: {
      const exhaustive: never = plan;
      throw new EudslError(
        "EUDSL-TYPE",
        `unknown condition plan ${describeArg(exhaustive)}`,
      );
    }
  }
}

/** Canonical flag/context for one evaluation site (reused by `whileLoop`). */
export class PlanContext {
  private readonly hooks: PlanHooks;
  private readonly flags = new Map<GatePlan, GateFlag>();

  constructor(hooks: PlanHooks) {
    this.hooks = hooks;
  }

  /** True while the consuming site has an active module builder. */
  hasBuilder(): boolean {
    return this.hooks.hasBuilder();
  }

  /**
   * Allocate the flag for `node` once, or return the flag already assigned to
   * it. This is what makes `whileLoop`'s entry and tail emissions share ids.
   */
  getOrAllocFlag(node: GatePlan): GateFlag {
    const existing = this.flags.get(node);
    if (existing !== undefined) return existing;
    if (!this.hooks.hasBuilder()) {
      throw new EudslError(
        "EUDSL-SCOPE",
        "condition combinators that need a flag must be used inside buildModule()",
      );
    }
    const flag = this.hooks.allocFlag();
    this.flags.set(node, flag);
    return flag;
  }

  emitReset(flag: GateFlag, value: number): void {
    this.hooks.emitReset(flag, value);
  }

  emitSetWhen(conds: readonly HirCond[], flag: GateFlag, value: number): void {
    this.hooks.emitSetWhen(conds, flag, value);
  }
}

/**
 * Emit one planned condition and return the literal conditions to splice into
 * the caller's gate.
 *
 * `and` is transparent. `or`/`not_opaque` emit their prelude after allocating
 * their flag and return `flag == 1`.
 */
function flattenPlan(
  plan: GatePlan,
  ctx: PlanContext,
  seen: Set<GatePlan> = new Set(),
): HirCond[] {
  if (seen.has(plan)) {
    throw new EudslError(
      "EUDSL-TYPE",
      "condition plan is cyclic and cannot be expanded",
    );
  }
  seen.add(plan);
  try {
    switch (plan.kind) {
      case "lit":
        return [resolveLit(plan.cond)];
      case "and": {
        const out: HirCond[] = [];
        for (const node of plan.nodes) out.push(...flattenPlan(node, ctx, seen));
        return out;
      }
      case "or": {
        // `condAny()` is statically false: emit Never() and allocate nothing.
        if (plan.nodes.length === 0) return [Never()];
        // Detached `DeferredNe` keeps its historical `materialize()` fallback;
        // new composites have no fallback and fail in `getOrAllocFlag`.
        if (!ctx.hasBuilder()) {
          const fallback = detachedFallbacks.get(plan);
          if (fallback !== undefined) return [fallback.materialize()];
        }
        const flag = ctx.getOrAllocFlag(plan);
        ctx.emitReset(flag, 0);
        for (const branch of plan.nodes) {
          // Nested `or`/`not_opaque` preludes run here, before this branch's
          // set-when, so var ids stay in canonical pre-order.
          const branchConds = flattenPlan(branch, ctx, seen);
          ctx.emitSetWhen(branchConds, flag, 1);
        }
        return [flag.eq(1)];
      }
      case "not_opaque": {
        const flag = ctx.getOrAllocFlag(plan);
        ctx.emitReset(flag, 1);
        ctx.emitSetWhen([resolveLit(plan.inner)], flag, 0);
        return [flag.eq(1)];
      }
    }
  } finally {
    seen.delete(plan);
  }
}

/**
 * Number of condition slots a plan occupies in its consumer.
 *
 * `and` splices its children; every other plan (including `condAny()`'s
 * `Never()` and a flag gate) occupies exactly one slot. Raw `Trigger` uses
 * this for its 16-condition cap *before* emitting any prelude.
 */
export function plannedCondCount(plan: GatePlan): number {
  if (plan.kind !== "and") return 1;
  let total = 0;
  for (const node of plan.nodes) total += plannedCondCount(node);
  return total;
}

/** Expand one already-built plan, validating it before any flag allocation. */
export function emitPlan(plan: GatePlan, ctx: PlanContext): HirCond[] {
  validatePlan(plan);
  return flattenPlan(plan, ctx);
}

/**
 * Build and validate the plan tree for a raw condition list.
 *
 * Callers that emit at more than one point (`whileLoop`'s entry and tail)
 * build once and pass the same plan objects to {@link emitCondPlans}, so a
 * `PlanContext` maps each plan to exactly one flag.
 *
 * All arguments and plans are validated here, before any flag allocation, so
 * a malformed later item cannot leave a half-emitted prelude behind.
 */
export function buildCondPlans(
  rawConds: readonly CondLike[],
): GatePlan[] {
  const plans: GatePlan[] = [];
  for (const cond of rawConds) {
    plans.push(planOf(validateArg(cond, "condition")));
  }
  for (const plan of plans) validatePlan(plan);
  return plans;
}

/** Emit a pre-built plan list at one evaluation site. */
export function emitCondPlans(
  plans: readonly GatePlan[],
  ctx: PlanContext,
): HirCond[] {
  for (const plan of plans) validatePlan(plan);
  const out: HirCond[] = [];
  for (const plan of plans) out.push(...flattenPlan(plan, ctx));
  return out;
}

/**
 * Emit a pre-built plan list with all top-level literal leaves materialized
 * before any top-level gate prelude.
 *
 * `whileLoop` must keep the pre-ADR emission order on mixed lists such as
 * `[DeferredNe, DeferredCond]`: the deferred temp transfers are queued first,
 * then the gate reset/set-whens. The resulting condition list keeps the
 * caller's order; only the prelude acts move.
 */
export function emitCondPlansLeafFirst(
  plans: readonly GatePlan[],
  ctx: PlanContext,
): HirCond[] {
  for (const plan of plans) validatePlan(plan);
  type Slot =
    | { readonly kind: "cond"; readonly cond: HirCond }
    | { readonly kind: "gate"; readonly plan: GatePlan; cond?: HirCond };
  const slots: Slot[] = [];
  const prepare = (plan: GatePlan): void => {
    if (plan.kind === "and") {
      for (const node of plan.nodes) prepare(node);
      return;
    }
    if (plan.kind === "lit") {
      slots.push({ kind: "cond", cond: resolveLit(plan.cond) });
      return;
    }
    slots.push({ kind: "gate", plan });
  };
  for (const plan of plans) prepare(plan);
  for (const slot of slots) {
    if (slot.kind === "gate") {
      slot.cond = flattenPlan(slot.plan, ctx)[0];
    }
  }
  return slots.map((slot) =>
    slot.kind === "cond" ? slot.cond : slot.cond!,
  );
}

/**
 * Expand a raw condition list at one evaluation site.
 *
 * All arguments and plans are validated first, so a malformed later item
 * cannot leave a half-emitted prelude behind.
 */
export function expandCondList(
  rawConds: readonly CondLike[],
  ctx: PlanContext,
): HirCond[] {
  return emitCondPlans(buildCondPlans(rawConds), ctx);
}

/** Abstract base for the three user-facing combinator markers. */
abstract class CompositeMarker implements CompositeCond {
  abstract gatePlan(): GatePlan;
  abstract complement(): CondLike;

  /** Reject implicit numeric/boolean coercion of a runtime condition marker. */
  valueOf(): never {
    throw new EudslError(
      "EUDSL-TYPE",
      "condition combinators are runtime conditions, not JS booleans",
    );
  }

  [Symbol.toPrimitive](): never {
    throw new EudslError(
      "EUDSL-TYPE",
      "condition combinators are runtime conditions, not JS booleans",
    );
  }
}

class ConditionAll extends CompositeMarker {
  readonly conds: readonly CondLike[];

  constructor(conds: readonly CondLike[]) {
    super();
    this.conds = conds;
  }

  gatePlan(): GatePlan {
    return { kind: "and", nodes: this.conds.map(planOf) };
  }

  complement(): CondLike {
    return condAny(...this.conds.map(complementCond));
  }
}

class ConditionAny extends CompositeMarker {
  readonly conds: readonly CondLike[];

  constructor(conds: readonly CondLike[]) {
    super();
    this.conds = conds;
  }

  gatePlan(): GatePlan {
    return { kind: "or", nodes: this.conds.map(planOf) };
  }

  complement(): CondLike {
    return condAll(...this.conds.map(complementCond));
  }
}

class ConditionNot extends CompositeMarker {
  readonly cond: CondLike;

  constructor(cond: CondLike) {
    super();
    this.cond = cond;
  }

  gatePlan(): GatePlan {
    return planOf(complementCond(this.cond));
  }

  complement(): CondLike {
    return this.cond;
  }
}

/**
 * `not_opaque(cond)` marker: a negated condition with no structural
 * complement. It emits a flag that starts at 1 and is cleared when `cond`
 * holds.
 */
class OpaqueNot implements CompositeCond {
  readonly cond: CondLike;

  constructor(cond: CondLike) {
    this.cond = cond;
  }

  gatePlan(): GatePlan {
    return { kind: "not_opaque", inner: this.cond };
  }

  complement(): CondLike {
    return this.cond;
  }

  valueOf(): never {
    throw new EudslError(
      "EUDSL-TYPE",
      "condition combinators are runtime conditions, not JS booleans",
    );
  }

  [Symbol.toPrimitive](): never {
    throw new EudslError(
      "EUDSL-TYPE",
      "condition combinators are runtime conditions, not JS booleans",
    );
  }
}

/**
 * Structural complement of one condition.
 *
 * Leaf comparisons complement to a direct condition where possible
 * (`ne(k)` -> `eq(k)`, `ge` <-> `lt`, ...); `Always`/`Never` swap; every other
 * opaque condition takes the generic `not_opaque` flag gate.
 */
export function complementCond(cond: CondLike): CondLike {
  const candidate = cond as { complement?: () => CondLike };
  if (typeof candidate.complement === "function") {
    return candidate.complement();
  }
  if (typeof (cond as HirCond).condtype === "number") {
    const condtype = (cond as HirCond).condtype;
    if (condtype === COND_ALWAYS) return Never();
    if (condtype === COND_NEVER) return Always();
  }
  return new OpaqueNot(cond);
}

/** Flatten direct same-kind conjunctions into one child list. */
function flattenAll(conds: readonly CondLike[]): CondLike[] {
  const flat: CondLike[] = [];
  for (const cond of conds) {
    if (cond instanceof ConditionAll) {
      flat.push(...cond.conds);
    } else {
      flat.push(cond);
    }
  }
  return flat;
}

/** Flatten direct same-kind disjunctions into one child list. */
function flattenAny(conds: readonly CondLike[]): CondLike[] {
  const flat: CondLike[] = [];
  for (const cond of conds) {
    if (cond instanceof ConditionAny) {
      flat.push(...cond.conds);
    } else {
      flat.push(cond);
    }
  }
  return flat;
}

/**
 * Conjunction: `and` splices its members into the consuming gate.
 *
 * Nested `condAll` children are flattened and a one-child result is the child
 * itself (ADR 0028 §2). `condAll()` stays an empty `and` (0 conds).
 */
export function condAll(...conds: CondLike[]): CondLike {
  for (const cond of conds) validateArg(cond, "condAll()");
  const flat = flattenAll(conds);
  if (flat.length === 1) return flat[0];
  return new ConditionAll(flat);
}

export const cond_all = condAll;

/**
 * Disjunction: lowered through one shared flag.
 *
 * Nested `condAny` children are flattened into the same flag, a one-child
 * result is the child itself, and with zero branches it expands to `Never()`.
 */
export function condAny(...conds: CondLike[]): CondLike {
  for (const cond of conds) validateArg(cond, "condAny()");
  const flat = flattenAny(conds);
  if (flat.length === 1) return flat[0];
  return new ConditionAny(flat);
}

export const cond_any = condAny;

/** Negation; `condNot(condNot(x))` returns `x` unchanged. */
export function condNot(cond: CondLike): CondLike {
  if (arguments.length !== 1) {
    throw new EudslError(
      "EUDSL-TYPE",
      `condNot() expects exactly one condition, got ${arguments.length}`,
    );
  }
  validateArg(cond, "condNot()");
  if (cond instanceof ConditionNot) return cond.cond;
  return new ConditionNot(cond);
}

export const cond_not = condNot;
