/**
 * Unit-list iteration: walk the global CUnit lists `U` / `T` / `S` / `D`.
 *
 * The body receives `(unitEpd, loop)`. `unitEpd` is a runtime `VarImpl` holding
 * the current unit's EPD in eudkit's absolute space, so `withEpd(unitEpd, ...)`
 * reads `CUnit` fields. Return {@link BREAK} (or call `loop.break_()`) to stop
 * the walk after the current node.
 *
 * `D` (dying) is walked **backward** from the tail dword through `prev`; a
 * forward walk from the same dword is empty on Remastered and silently misses
 * every death. The free list `F` is not a unit list and is rejected.
 */

import { CP_ADDR, readCurrentPlayerRegister } from "./cp.ts";
import { AddrReloc, emitVarStore } from "./hir.ts";
import { when, whileLoop } from "./temporal.ts";
import { Var, VarImpl } from "./var.ts";
import {
  EudslError,
  inCunitPool,
  readCunitPointerSaved,
} from "./view.ts";

export const UNIT_LIST_U = "U";
export const UNIT_LIST_T = "T";
export const UNIT_LIST_S = "S";
export const UNIT_LIST_D = "D";

/** EPD of the dword that holds each list's first node byte pointer. */
const UNIT_LIST_HEADS: Record<string, number> = {
  [UNIT_LIST_U]: 161843, // 0x628430
  [UNIT_LIST_T]: 161841, // 0x628428
  [UNIT_LIST_S]: 161844, // 0x628434
  [UNIT_LIST_D]: 161842, // 0x62842C, the D *tail*; walk prev from here
};

/** Dword offset from a node's own EPD to its forward link. */
const UNIT_LIST_LINK_OFFSET: Record<string, number> = {
  [UNIT_LIST_U]: 1, // next at +4
  [UNIT_LIST_T]: 1,
  [UNIT_LIST_S]: 1,
  [UNIT_LIST_D]: 0, // prev at +0
};

const FREE_LIST_MESSAGE =
  "the free list is not a unit list: walking it enumerates unallocated " +
  "slots; use free_list_empty() / free_list_walk()";

/** Named sentinel: return BREAK from a unit-list body to stop the walk. */
export class Break {
  toString(): string {
    return "BREAK";
  }
}

export const BREAK = new Break();

/**
 * Build-time handle for one in-flight unit-list walk.
 *
 * `break_()` emits a single `done.set(1)` at the current build point, so the
 * author gates it at runtime (`when([...], () => loop.break_())`). Calling it
 * twice emits two acts and still stops the walk once.
 */
export class UnitLoop {
  readonly listName: string;
  readonly epd: VarImpl;
  private readonly done: VarImpl | null;
  private active = false;

  constructor(listName: string, epd: VarImpl, done: VarImpl | null = null) {
    validateListName(listName);
    if (!(epd instanceof VarImpl)) {
      throw new EudslError(
        "EUDSL-TYPE",
        `UnitLoop epd must be a Var; got ${describe(epd)}`,
      );
    }
    this.listName = listName;
    this.epd = epd;
    this.done = done;
  }

  /** Emit `done.set(1)`; valid only while the walk's body is running. */
  break_ = (): void => {
    if (!this.active || this.done === null) {
      throw new EudslError(
        "EUDSL-TYPE",
        "UnitLoop.break_() may only be called inside a forEachUnitList() body",
      );
    }
    this.done.set(1);
  };

  breakLoop = (): void => {
    this.break_();
  };

  /** @internal */ _begin(): void {
    this.active = true;
  }

  /** @internal */ _end(): void {
    this.active = false;
  }

  /** @internal */ _emitBreak(): void {
    if (this.done !== null) this.done.set(1);
  }
}

function describe(value: unknown): string {
  if (typeof value === "string") return JSON.stringify(value);
  return String(value);
}

function validateListName(listName: unknown): asserts listName is string {
  if (typeof listName !== "string") {
    throw new EudslError(
      "EUDSL-TYPE",
      "forEachUnitList listName must be a plain string in " +
        `{"U", "T", "S", "D"}; got ${describe(listName)}`,
    );
  }
  if (listName === "F") {
    throw new EudslError("EUDSL-BOUNDS", FREE_LIST_MESSAGE);
  }
  if (!(listName in UNIT_LIST_HEADS)) {
    throw new EudslError(
      "EUDSL-TYPE",
      'forEachUnitList listName must be one of "U", "T", "S", "D"; ' +
        `got ${describe(listName)}`,
    );
  }
}

function runBody(
  body: (unit: VarImpl, loop: UnitLoop) => Break | void,
  loop: UnitLoop,
): void {
  loop._begin();
  let result: unknown;
  try {
    result = body(loop.epd, loop);
  } finally {
    loop._end();
  }
  if (result === BREAK) {
    loop._emitBreak();
  }
}

/**
 * Walk one global CUnit list, emitting `body(unitEpd, loop)` once.
 *
 * `reentrant: false` reads the successor link **after** the body;
 * `reentrant: true` reads it **before**, so a body that frees the current node
 * still reaches the next one. The ambient CurrentPlayer value is read once per
 * site and restored unconditionally after the loop.
 */
export function forEachUnitList(
  listName: string,
  body: (unit: VarImpl, loop: UnitLoop) => Break | void,
  { reentrant = false }: { reentrant?: boolean } = {},
): void {
  validateListName(listName);
  if (typeof reentrant !== "boolean") {
    throw new EudslError(
      "EUDSL-TYPE",
      `forEachUnitList reentrant must be a plain boolean; got ${describe(reentrant)}`,
    );
  }
  if (typeof body !== "function") {
    throw new EudslError(
      "EUDSL-TYPE",
      `forEachUnitList body must be a function; got ${describe(body)}`,
    );
  }

  const ptr = Var(0);
  const done = Var(0);
  const saved = readCurrentPlayerRegister();
  const linkOff = UNIT_LIST_LINK_OFFSET[listName];

  ptr.set(readCunitPointerSaved(saved, UNIT_LIST_HEADS[listName]));

  whileLoop([...inCunitPool(ptr), done.eq(0)], () => {
    const loop = new UnitLoop(listName, ptr, done);
    const link = linkOff ? ptr.add(linkOff) : ptr;
    let nxt: VarImpl;
    if (reentrant) {
      nxt = readCunitPointerSaved(saved, link);
      runBody(body, loop);
    } else {
      runBody(body, loop);
      nxt = readCunitPointerSaved(saved, link);
    }
    when([done.eq(0)], () => ptr.set(nxt));
  });
  emitVarStore(saved.id, AddrReloc(CP_ADDR));
}

export const for_each_unit_list = forEachUnitList;
