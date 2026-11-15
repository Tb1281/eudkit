/**
 * `buildModule` compiler entry point, stock conditions, stock actions,
 * and HIR 3.0 serializer for the TypeScript DSL (`ts/`).
 */

import {
  ACT_COUNT,
  type HirAct,
  type HirBlobRelocInput,
  type HirBlobRelocNode,
  type HirCond,
  type HirReloc,
  type HirTriggerPtr,
  type ModuleBuilderHook,
  Trigger,
  _getActiveModuleBuilder,
  _setActiveModuleBuilder,
} from "./trigger.ts";
import {
  EPD,
  EudslError,
  TriggerFieldRef,
  describeValue,
  requireAlignedDwordAddress,
  requirePlainInt,
} from "./view.ts";
import {
  ACT_FIELD_OFFSETS,
  ACT_FIELD_ORDER,
  COND_FIELD_OFFSETS,
  COND_FIELD_ORDER,
  EUDX_MAGIC,
  type StockParamSpec,
  type StockRowSpec,
  type StockValidator,
} from "./stock-spec.ts";
// `var.ts` imports this module for the stock action builders, so this is a
// deliberate cycle: `epdOfDb` reads `VarImpl` only at call time, after both
// modules finish evaluating.
import { VarImpl } from "./var.ts";

export type { HirAct, HirCond, HirReloc, HirTriggerPtr };
export type { StockParamSpec, StockRowSpec, StockValidator } from "./stock-spec.ts";
export { EUDX_MAGIC } from "./stock-spec.ts";

// Non-special stock and extension helpers are generated from
// `schema/stock-rows.json`; only the marked special rows live below.
export * from "./stock.generated.ts";

export const HIR_VERSION = "3.2";

// Player constants
export const P1 = 0;
export const P2 = 1;
export const P3 = 2;
export const P4 = 3;
export const P5 = 4;
export const P6 = 5;
export const P7 = 6;
export const P8 = 7;
export const P9 = 8;
export const P10 = 9;
export const P11 = 10;
export const P12 = 11;

export const Player1 = 0;
export const Player2 = 1;
export const Player3 = 2;
export const Player4 = 3;
export const Player5 = 4;
export const Player6 = 5;
export const Player7 = 6;
export const Player8 = 7;
export const Player9 = 8;
export const Player10 = 9;
export const Player11 = 10;
export const Player12 = 11;

export const CurrentPlayer = 13;
export const AllPlayers = 17;
export const Foes = 14;
export const Allies = 15;
export const NeutralPlayers = 16;
export const Force1 = 18;
export const Force2 = 19;
export const Force3 = 20;
export const Force4 = 21;
export const NonAlliedVictoryPlayers = 26;

// Comparison constants
export const AtLeast = 0;
export const AtMost = 1;
export const Exactly = 10;

// Modifier constants
export const SetTo = 7;
export const Add = 8;
export const Subtract = 9;

// Stock trigger vocabulary -------------------------------------------------

/** StarEdit's standard `Anywhere` trigger location slot (`locid 64`). */
export const Anywhere = 64;

/** `All` is the zero count sentinel for count-taking stock actions. */
export const All = 0;

export const Ore = 0;
export const Gas = 1;
export const OreAndGas = 2;

export const ScoreTotal = 0;
export const ScoreUnits = 1;
export const ScoreBuildings = 2;
export const ScoreUnitsAndBuildings = 3;
export const ScoreKills = 4;
export const ScoreRazings = 5;
export const ScoreKillsAndRazings = 6;
export const ScoreCustom = 7;

export const SwitchSet = 2;
export const SwitchCleared = 3;

export const SwitchActionSet = 4;
export const SwitchActionClear = 5;
export const SwitchActionToggle = 6;
export const SwitchActionRandom = 11;

export const PropEnable = 4;
export const PropDisable = 5;
export const PropToggle = 6;

export const OrderMove = 0;
export const OrderPatrol = 1;
export const OrderAttack = 2;

export const Enemy = 0;
export const Ally = 1;
export const AlliedVictory = 2;

const PLAYER_VALUES: readonly number[] = [
  P1, P2, P3, P4, P5, P6, P7, P8, P9, P10, P11, P12,
  CurrentPlayer, Foes, Allies, NeutralPlayers, AllPlayers,
  Force1, Force2, Force3, Force4, NonAlliedVictoryPlayers,
];
function stockInt(name: string, value: number, min: number, max: number): number {
  if (!Number.isInteger(value) || value < min || value > max) {
    throw new EudslError(
      "EUDSL-BOUNDS",
      `${name} must be an integer in ${min}..${max}, got ${String(value)}`,
    );
  }
  return value;
}

function stockEnum(name: string, value: number, allowed: readonly number[]): number {
  if (!Number.isInteger(value) || !allowed.includes(value)) {
    throw new EudslError(
      "EUDSL-BOUNDS",
      `${name} must be one of ${allowed.join(", ")}, got ${String(value)}`,
    );
  }
  return value;
}

function stockPlayer(player: number): number {
  return stockEnum("player", player, PLAYER_VALUES);
}

function stockLocation(location: number): number {
  return stockInt("location", location, 0, 255);
}

function stockUnit(unit: number): number {
  return stockInt("unit", unit, 0, 227);
}

export type PlayerOrFieldTarget =
  | number
  | TriggerFieldRef
  | { readonly value: TriggerFieldRef }
  | DbRef;
export type ValueOrPtrTarget =
  | number
  | Trigger
  | TriggerFieldRef
  | { readonly value: TriggerFieldRef }
  | DbRef;

/**
 * A pointer into a module data blob (`Db`), usable as a memory target or value.
 *
 * `blobId` is the 0-based module blob index and `byteOffset` the byte offset
 * inside the blob. `db.at(16)` shifts the reference. `SetMemory(head, SetTo,
 * db)` relocates the action's value field to the blob's payload address
 * (mode 4); `SetMemory(db.at(0x10), ...)` relocates the destination field to
 * the blob's EPD (mode 1).
 */
export class DbRef {
  readonly blobId: number;
  readonly byteOffset: number;

  constructor(blobId: number, byteOffset = 0) {
    if (!Number.isInteger(blobId) || blobId < 0) {
      throw new EudslError(
        "EUDSL-BOUNDS",
        `Db blob id must be a non-negative integer, got ${String(blobId)}`,
      );
    }
    if (!Number.isInteger(byteOffset) || byteOffset < 0 || byteOffset % 4 !== 0) {
      throw new EudslError(
        "EUDSL-BOUNDS",
        `Db offset must be a non-negative multiple of 4, got ${String(byteOffset)}`,
      );
    }
    this.blobId = blobId;
    this.byteOffset = byteOffset;
  }

  /** A reference to the 4-byte dword `byteOffset` bytes into the blob. */
  at(byteOffset: number): DbRef {
    if (!Number.isInteger(byteOffset)) {
      throw new EudslError(
        "EUDSL-BOUNDS",
        `Db offset must be a non-negative multiple of 4, got ${String(byteOffset)}`,
      );
    }
    return new DbRef(this.blobId, this.byteOffset + byteOffset);
  }

  /** Alias of {@link DbRef.at} (TypeScript cannot overload `+`). */
  plus(byteOffset: number): DbRef {
    return this.at(byteOffset);
  }
}

/**
 * Author-facing blob relocation: the shared input shape plus `target`, which
 * names a `Db`/`DbRef` or a trigger handle instead of a raw index.
 */
export type DbRelocInput = HirBlobRelocInput & {
  readonly target?: DbRef | Trigger;
};

/**
 * A relocatable raw data blob (`Db(bytes | size)`, HIR #99).
 *
 * `new Db(16)` reserves 16 zero-filled bytes; `new Db(bytes)` stores the bytes
 * verbatim. The blob is appended to the map payload (after the trigger pages
 * and the demoted Cell blob) and can be pointed at from any action or
 * condition field. Must be created inside {@link buildModule}.
 */
export class Db extends DbRef {
  readonly size: number;

  constructor(
    payload: number | Uint8Array,
    relocs: readonly DbRelocInput[] = [],
  ) {
    const builder = _getActiveModuleBuilder();
    if (!builder || typeof builder.allocBlob !== "function") {
      throw new EudslError("EUDSL-SCOPE", "Db() must be called inside buildModule()");
    }
    const blob = builder.allocBlob(payload, relocs);
    super(blob.id, 0);
    this.size = blob.size;
  }
}

/** Build-time helper form of `new Db(payload)` (py parity: `Db(...)`). */
export function db(
  payload: number | Uint8Array,
  relocs: readonly DbRelocInput[] = [],
): Db {
  return new Db(payload, relocs);
}

/**
 * Return a `Var` holding the EPD of `ref`'s payload dword.
 *
 * One preserved zero-condition trigger: a mode-1 blob relocation writes the
 * EPD into the fresh `Var`'s value field. `ref` must already be a `Db`/`DbRef`
 * (4-byte aligned); a `Db` means offset 0 (ADR 0029).
 */
export function epdOfDb(ref: Db | DbRef): VarImpl {
  if (!(ref instanceof DbRef)) {
    throw new EudslError(
      "EUDSL-TYPE",
      `epd_of_db(): expected Db or DbRef, got ${describeValue(ref)}`,
    );
  }
  const out = new VarImpl(0);
  emitBlobEpdInto(out, ref);
  return out;
}

/**
 * Write `ref`'s assembly-time blob EPD into the existing `dst` var.
 *
 * One preserved zero-condition trigger whose mode-1 relocation writes the
 * payload EPD into `dst`'s value field. Shared by {@link epdOfDb} (fresh Var)
 * and block-memory seeders (caller-owned destination cursor).
 */
export function emitBlobEpdInto(dst: VarImpl, ref: DbRef): void {
  const base = dst.act_set(0);
  const act: HirAct = {
    ...base,
    relocs: [
      ...(base.relocs ?? []),
      {
        field_offset: ACT_FIELD_OFFSETS.value!,
        target_blob: ref.blobId,
        target_offset: ref.byteOffset,
        mode: 1,
      },
    ],
  };
  // `act_set` queued `base` as a standalone top-level act. The relocated copy
  // is a different object, so consume the original before `Trigger` owns the
  // copy.
  _getActiveModuleBuilder()?.consumeQueuedActs([base]);
  new Trigger({ conds: [], acts: [act], preserved: true });
}

/** Py-spelling alias of {@link epdOfDb} (parity with `epd_of_db`). */
export const epd_of_db = epdOfDb;

/** Standard base64 (matches `eudkit_lir::b64_encode`, with `=` padding). */
function bytesToBase64(bytes: Uint8Array): string {
  const alphabet =
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
  let out = "";
  for (let i = 0; i < bytes.length; i += 3) {
    const b0 = bytes[i]!;
    const has1 = i + 1 < bytes.length;
    const has2 = i + 2 < bytes.length;
    const b1 = has1 ? bytes[i + 1]! : 0;
    const b2 = has2 ? bytes[i + 2]! : 0;
    const n = (b0 << 16) | (b1 << 8) | b2;
    out += alphabet[(n >> 18) & 63];
    out += alphabet[(n >> 12) & 63];
    out += has1 ? alphabet[(n >> 6) & 63] : "=";
    out += has2 ? alphabet[n & 63] : "=";
  }
  return out;
}

/**
 * Validate author-supplied blob-internal relocations into HIR records.
 *
 * Each entry names a 4-byte `offset` inside the blob and exactly one `target`
 * (a `DbRef`/`Db`, a `Trigger`, or explicit `target_blob`/`target_trigger`),
 * plus `target_offset` and `mode`. `target_offset` is always emitted (0 when
 * absent), matching the Rust record serializer.
 */
function normalizeBlobRelocs(
  relocs: readonly DbRelocInput[],
): HirBlobRelocNode[] {
  const out: HirBlobRelocNode[] = [];
  for (const reloc of relocs as unknown as readonly Record<string, unknown>[]) {
    if (typeof reloc !== "object" || reloc === null) {
      throw new EudslError("EUDSL-TYPE", `Db reloc must be an object, got ${describeValue(reloc)}`);
    }
    const offset = reloc.offset;
    if (!Number.isInteger(offset) || (offset as number) < 0 || (offset as number) % 4 !== 0) {
      throw new EudslError(
        "EUDSL-BOUNDS",
        `Db reloc offset must be a non-negative multiple of 4, got ${String(offset)}`,
      );
    }
    let targetOffset = reloc.target_offset ?? 0;
    if (!Number.isInteger(targetOffset) || (targetOffset as number) < 0) {
      throw new EudslError(
        "EUDSL-BOUNDS",
        `Db reloc target_offset must be a non-negative integer, got ${String(targetOffset)}`,
      );
    }
    let mode = reloc.mode ?? 4;
    if (!Number.isInteger(mode)) {
      throw new EudslError("EUDSL-BOUNDS", `Db reloc mode must be an integer, got ${String(mode)}`);
    }
    let targetBlob = reloc.target_blob;
    let targetTrigger = reloc.target_trigger;
    const target = reloc.target;
    if (target !== undefined) {
      if (target instanceof DbRef) {
        targetBlob = target.blobId;
        targetOffset = (targetOffset as number) + target.byteOffset;
      } else if (isTriggerInstance(target)) {
        targetTrigger = target.id;
      } else {
        throw new EudslError(
          "EUDSL-TYPE",
          `Db reloc target must be a Db or Trigger, got ${describeValue(target)}`,
        );
      }
    }
    if ((targetBlob === undefined) === (targetTrigger === undefined)) {
      throw new EudslError(
        "EUDSL-BOUNDS",
        "Db reloc needs exactly one of target_blob/target_trigger",
      );
    }
    const entry: Record<string, number> = {
      offset: offset as number,
      target_offset: targetOffset as number,
      mode: mode as number,
    };
    if (targetBlob !== undefined) {
      if (!Number.isInteger(targetBlob) || (targetBlob as number) < 0) {
        throw new EudslError(
          "EUDSL-BOUNDS",
          `Db reloc target_blob must be a non-negative integer, got ${String(targetBlob)}`,
        );
      }
      entry.target_blob = targetBlob as number;
    } else {
      if (!Number.isInteger(targetTrigger) || (targetTrigger as number) < 0) {
        throw new EudslError(
          "EUDSL-BOUNDS",
          `Db reloc target_trigger must be a non-negative integer, got ${String(targetTrigger)}`,
        );
      }
      entry.target_trigger = targetTrigger as number;
    }
    out.push(entry as unknown as HirBlobRelocNode);
  }
  return out;
}

function isTriggerInstance(v: unknown): v is Trigger {
  return (
    typeof v === "object" &&
    v !== null &&
    "id" in v &&
    "conds" in v &&
    "acts" in v &&
    "next" in v
  );
}

function unwrapFieldRef(v: unknown): TriggerFieldRef | null {
  if (v instanceof TriggerFieldRef) {
    return v;
  }
  if (
    typeof v === "object" &&
    v !== null &&
    "value" in v &&
    (v as { value: unknown }).value instanceof TriggerFieldRef
  ) {
    return (v as { value: TriggerFieldRef }).value;
  }
  return null;
}

function resolveEpdDest(
  target: PlayerOrFieldTarget,
  fieldOffset: number,
  relocs: HirReloc[],
): number {
  const ref = unwrapFieldRef(target);
  if (ref !== null) {
    if (ref.varId !== undefined) {
      relocs.push({
        field_offset: fieldOffset,
        target_var: ref.varId,
        target_offset: ref.alignedByteOffset,
        mode: 1,
      });
    } else {
      relocs.push({
        field_offset: fieldOffset,
        target_trigger: ref.triggerId,
        target_offset: ref.alignedByteOffset,
        mode: 1,
      });
    }
    return 0;
  }
  if (target instanceof DbRef) {
    relocs.push({
      field_offset: fieldOffset,
      target_blob: target.blobId,
      target_offset: target.byteOffset,
      mode: 1,
    });
    return 0;
  }
  // A relocation form is consumed above; what is left is a plain integer
  // address or nothing (ADR 0019 §1, cycle 16). `>>> 0` here silently
  // truncated floats, numeric strings, and bools (`"abc"` became 0).
  return requirePlainInt(target, "memory destination") >>> 0;
}

function resolveValueOrPtr(
  val: ValueOrPtrTarget,
  fieldOffset: number,
  relocs: HirReloc[],
): number {
  const ref = unwrapFieldRef(val);
  if (ref !== null) {
    if (ref.varId !== undefined) {
      relocs.push({
        field_offset: fieldOffset,
        target_var: ref.varId,
        target_offset: ref.byteOffset,
        mode: 4,
      });
    } else {
      relocs.push({
        field_offset: fieldOffset,
        target_trigger: ref.triggerId,
        target_offset: ref.byteOffset,
        mode: 4,
      });
    }
    return 0;
  }
  if (isTriggerInstance(val)) {
    relocs.push({
      field_offset: fieldOffset,
      target_trigger: val.id,
      target_offset: 0,
      mode: 4,
    });
    return 0;
  }
  if (val instanceof DbRef) {
    relocs.push({
      field_offset: fieldOffset,
      target_blob: val.blobId,
      target_offset: val.byteOffset,
      mode: 4,
    });
    return 0;
  }
  return requirePlainInt(val, "memory value") >>> 0;
}

/**
 * The integer field tuple for each HIR record — one explicit `[name, mask]`
 * per field, so "which fields are in the operand domain" is a list a reader
 * can count. `text`/`wav`/`uprp`/`str_index`/`relocs` are not integer
 * operands and stay out (ADR 0019 §6).
 */
const COND_INT_FIELDS = [
  ["locid", 0xffffffff],
  ["player", 0xffffffff],
  ["amount", 0xffffffff],
  ["unit", 0xffff],
  ["comparison", 0xff],
  ["condtype", 0xff],
  ["restype", 0xff],
  ["flags", 0xff],
  ["eudx", 0xffff],
] as const;

const ACT_INT_FIELDS = [
  ["mask", 0xffffffff],
  ["strid", 0xffffffff],
  ["wavid", 0xffffffff],
  ["time", 0xffffffff],
  ["dest", 0xffffffff],
  ["value", 0xffffffff],
  ["unit", 0xffff],
  ["acttype", 0xff],
  ["modifier", 0xff],
  ["flags", 0xff],
  ["padding", 0xff],
  ["eudx", 0xffff],
] as const;

/** Record type tags: no default, so an omitted one is an error, not a `0`. */
const COND_REQUIRED = ["condtype"] as const;
const ACT_REQUIRED = ["acttype"] as const;

/**
 * Validate every integer field, then mask it.
 *
 * The record builders are the single chokepoint every death-table and
 * raw-memory constructor converges on (hand-written rows and generated stock
 * rows alike), so this is the one place a mask can be applied only to a
 * validated integer. Validate first: `1.5 & 0xff` would write `1`. Keys are
 * emitted in the tuple order, so the HIR field order is unchanged.
 */
function maskedIntFields(
  record: string,
  fields: readonly (readonly [string, number])[],
  init: Readonly<Record<string, unknown>>,
  required: readonly string[] = [],
): Record<string, number> {
  const masked: Record<string, number> = {};
  for (const [key, mask] of fields) {
    const raw = init[key];
    if (raw === undefined && !required.includes(key)) {
      masked[key] = 0;
      continue;
    }
    const value = requirePlainInt(raw, `${record} field ${key}`);
    // A full-width field normalises through `>>> 0` (unsigned), the narrow
    // ones through `& mask`; `& 0xffffffff` would keep the sign.
    masked[key] = mask === 0xffffffff ? value >>> 0 : value & mask;
  }
  return masked;
}

function buildCond(init: {
  locid?: number;
  player?: number;
  amount?: number;
  unit?: number;
  comparison?: number;
  condtype: number;
  restype?: number;
  flags?: number;
  eudx?: number;
  relocs?: HirReloc[];
}): HirCond {
  const cond: Record<string, unknown> = maskedIntFields(
    "condition",
    COND_INT_FIELDS,
    init,
    COND_REQUIRED,
  );
  if (init.relocs && init.relocs.length > 0) {
    cond.relocs = init.relocs;
  }
  return Object.freeze(cond) as unknown as HirCond;
}

/**
 * Legacy VarStore target object: a fixed byte address or a trigger field.
 * Kept for un-migrated call sites; new code uses a relocation descriptor.
 */
export type VarStoreTarget =
  | { readonly addr: number }
  | { readonly triggerId: number; readonly offset: number };

/** A VarStore destination named by an absolute, 4-byte-aligned address. */
export interface AddrReloc {
  readonly kind: "addr";
  readonly addr: number;
}

/** A VarStore destination named by a dword field inside a trigger record. */
export interface TriggerFieldReloc {
  readonly kind: "trigger";
  readonly triggerId: number;
  readonly offset?: number;
}

/** The two relocation shapes accepted by `emitVarStore`. */
export type VarStoreReloc = AddrReloc | TriggerFieldReloc;

/** Descriptor or legacy object; the boundary normalises both to the wire form. */
export type VarStoreTargetInput = VarStoreTarget | VarStoreReloc;

// The descriptors are class instances, not structural `{ kind }` objects.
// Python's descriptors are dataclasses and its boundary uses `isinstance`, so
// a plain legacy mapping keeps its legacy meaning even when it carries an
// unrelated `kind` key. Runtime branding gives TypeScript the same rule: a
// caller-built object is never reinterpreted as a descriptor just because it
// happens to have `kind: "addr"` / `"trigger"`.
class AddrRelocDescriptor implements AddrReloc {
  readonly kind = "addr" as const;
  readonly addr: number;

  constructor(addr: number) {
    this.addr = addr;
  }
}

class TriggerFieldRelocDescriptor implements TriggerFieldReloc {
  readonly kind = "trigger" as const;
  readonly triggerId: number;
  readonly offset?: number;

  constructor(triggerId: number, offset?: number) {
    this.triggerId = triggerId;
    this.offset = offset;
  }
}

/** Build an address relocation descriptor. */
export function AddrReloc(addr: number): AddrReloc {
  return new AddrRelocDescriptor(addr);
}

/** Build a trigger-field relocation descriptor; `offset` defaults to `0`. */
export function TriggerFieldReloc(
  triggerId: number,
  offset?: number,
): TriggerFieldReloc {
  return new TriggerFieldRelocDescriptor(triggerId, offset);
}

/**
 * Lower a relocation descriptor to the legacy HIR target object.
 *
 * Only the factory-built descriptors are normalised; a plain object keeps its
 * legacy meaning and is never reinterpreted, even with an unrelated `kind`
 * key. This mirrors `py/hir.py`, where `AddrReloc` / `TriggerFieldReloc` are
 * dataclasses and the boundary uses `isinstance`.
 */
function normalizeVarStoreTarget(target: VarStoreTargetInput): VarStoreTarget {
  if (typeof target !== "object" || target === null) {
    throw new EudslError(
      "EUDSL-TYPE",
      `VarStore target must be a relocation descriptor or an object, got ${describeValue(target)}`,
    );
  }
  if (target instanceof AddrRelocDescriptor) {
    return { addr: target.addr };
  }
  if (target instanceof TriggerFieldRelocDescriptor) {
    return target.offset === undefined
      ? ({ triggerId: target.triggerId } as VarStoreTarget)
      : { triggerId: target.triggerId, offset: target.offset };
  }
  // A plain mapping is already the legacy wire shape; keep its fields as-is.
  return target as VarStoreTarget;
}

interface InternalModuleBuilder extends ModuleBuilderHook {
  allocVar(init: number, mask?: number, storage?: "page"): number;
  allocStringVar(index: number, offset: number, mask?: number): number;
  allocString(text: string): number;
  getNextStringIndex(): number;
  getScopeSerial(): number;
  getNextPageSlot(): number;
  queueAct(act: HirAct): void;
  queueVarTransfer(dstVar: number, srcVar: number, modifier: number): void;
  queueVarStore(
    srcVar: number,
    target: VarStoreTargetInput,
    modifier: number,
  ): void;
  queueCall(
    funcId: number,
    args: readonly HirCallArgNode[],
    dstVar: number | null,
  ): void;
  allocFuncId(): number;
  queueFunc(func: HirFuncNode): void;
  captureEntries(thunk: () => void): HirStmtNode[];
  embedFile(name: string, path: string): void;
}

/**
 * Store a variable's value into an arbitrary 4-byte memory location.
 *
 * `target.addr` is a fixed byte address (e.g. the `CurrentPlayer` slot);
 * `target.triggerId` + `target.offset` names a field inside a trigger
 * (e.g. a condition's `player` field). This is the runtime primitive behind
 * dynamic-EPD indirection; scalar variables are copied via their
 * `TriggerPage` slot.
 *
 * Both numeric forms are validated eagerly: `addr`, `triggerId` and `offset`
 * must be plain integers, an `addr` must be 4-byte aligned, and a
 * trigger-field `offset` (default `0`) must be a multiple of 4 because the
 * write is a whole dword.
 */
export function emitVarStore(
  srcVar: number,
  target: VarStoreTargetInput,
  modifier = SetTo,
): void {
  const builder = _getActiveModuleBuilder() as InternalModuleBuilder | null;
  if (!builder || typeof builder.queueVarStore !== "function") {
    throw new EudslError(
      "EUDSL-SCOPE",
      "emitVarStore() must be called inside buildModule()",
    );
  }
  builder.queueVarStore(srcVar, target, modifier & 0xff);
}

/**
 * Queue a build-time file to be added to the output MPQ under `name`.
 *
 * `name` is the archived file name (e.g. `"bgm"`); `path` is authored as
 * written, and the build resolves relative paths against the directory of the
 * compiling source. The same `(name, path)` pair may be requested more than
 * once (it is queued once); a different path for an existing `name` is an
 * error. This is the general MPQ embed surface shared by helpers such as
 * `playBgm` and the dataDumper.
 */
export function embedFile(name: string, path: string): void {
  const builder = _getActiveModuleBuilder() as InternalModuleBuilder | null;
  if (!builder || typeof builder.embedFile !== "function") {
    throw new EudslError(
      "EUDSL-SCOPE",
      "embedFile() must be called inside buildModule()",
    );
  }
  builder.embedFile(name, path);
}

export const embed_file = embedFile;

function buildAct(init: {
  mask?: number;
  strid?: number;
  wavid?: number;
  time?: number;
  dest?: number;
  value?: number;
  unit?: number;
  acttype: number;
  modifier?: number;
  flags?: number;
  padding?: number;
  eudx?: number;
  text?: string;
  wav?: string;
  uprp?: readonly number[];
  strIndex?: number;
  relocs?: HirReloc[];
}): HirAct {
  const act: Record<string, unknown> = maskedIntFields(
    "action",
    ACT_INT_FIELDS,
    init,
    ACT_REQUIRED,
  );
  if (init.text !== undefined) {
    act.text = init.text;
  }
  if (init.wav !== undefined) {
    act.wav = init.wav;
  }
  if (init.uprp !== undefined) {
    act.uprp = init.uprp;
  }
  if (init.strIndex !== undefined) {
    act.str_index = init.strIndex;
  }
  if (init.relocs && init.relocs.length > 0) {
    act.relocs = init.relocs;
  }
  const frozen = Object.freeze(act) as unknown as HirAct;
  const builder = _getActiveModuleBuilder() as InternalModuleBuilder | null;
  if (builder && typeof builder.queueAct === "function") {
    builder.queueAct(frozen);
  }
  return frozen;
}

// ---------------------------------------------------------------------------
// Generated stock row emitter
// ---------------------------------------------------------------------------

function validateStockValue(validator: StockValidator, value: unknown): unknown {
  switch (validator.kind) {
    case "none":
      return value;
    case "range":
      return stockInt(validator.label, value as number, validator.min, validator.max as number);
    case "enum":
      return stockEnum(validator.label, value as number, validator.values);
    case "count": {
      const count = value as number;
      return count === validator.all
        ? validator.all
        : stockInt(validator.label, count, validator.min, validator.max);
    }
  }
}

function stockFieldOffset(row: StockRowSpec, field: string): number {
  const offsets = row.kind === "condition" ? COND_FIELD_OFFSETS : ACT_FIELD_OFFSETS;
  const offset = offsets[field];
  if (offset === undefined) {
    throw new EudslError(
      "EUDSL-TYPE",
      `${row.id}: HIR field ${field} has no record offset`,
    );
  }
  return offset;
}

function resolveStockValue(
  row: StockRowSpec,
  param: StockParamSpec,
  raw: unknown,
  relocs: HirReloc[],
): unknown {
  if (param.encoding === "string-index") {
    const label = param.validator.kind === "range" ? param.validator.label : "string index";
    if (!Number.isInteger(raw) || (raw as number) < 0) {
      throw new EudslError(
        "EUDSL-BOUNDS",
        `${row.id}: ${label} must be a non-negative integer, got ${String(raw)}`,
      );
    }
    return raw;
  }
  const value = validateStockValue(param.validator, raw);
  switch (param.encoding) {
    case "epd":
      return resolveEpdDest(
        value as PlayerOrFieldTarget,
        stockFieldOffset(row, param.field),
        relocs,
      );
    case "epd-from-address": {
      // Guard *before* `EPD()` folds the address: `EPD` masks any operand
      // into a plausible lane, so the guard downstream never saw the float
      // (cycle 17, ADR 0019 §1). Subject stays `memory address` so the test
      // can prove the guard sits at this site. A `Db`/`DbRef` is already a
      // relocation target (HIR #99), so it bypasses the address fold — its
      // byte offset is already a non-negative multiple of 4 and `lir2payload`
      // starts every payload item at `align4(previous end)`, so the blob
      // payload base is dword-aligned by layout (cycle 26, #120); no address
      // guard is needed there (#111).
      if (value instanceof DbRef) {
        return resolveEpdDest(value, stockFieldOffset(row, param.field), relocs);
      }
      const address = requirePlainInt(value, "memory address");
      requireAlignedDwordAddress(address, "memory address");
      return resolveEpdDest(EPD(address), stockFieldOffset(row, param.field), relocs);
    }
    case "value-or-ptr":
      return resolveValueOrPtr(
        value as ValueOrPtrTarget,
        stockFieldOffset(row, param.field),
        relocs,
      );
    case "ptr": {
      // Only a `Trigger` (its `.next`) or a handle that unwraps to a
      // `TriggerFieldRef` is legal. Never read `.next` before the type
      // check: `src.next` on a float/string/null/undefined threw a raw
      // `TypeError` (cycle 17).
      const ref = unwrapFieldRef(value) ?? (isTriggerInstance(value) ? value.next : null);
      if (ref === null) {
        throw new EudslError(
          "EUDSL-TYPE",
          `src must be a plain integer, got ${describeValue(value)}`,
        );
      }
      return resolveEpdDest(ref, ACT_FIELD_OFFSETS.dest!, relocs);
    }
    case "mask":
    case "u32-wrap":
      // Not truncated here: `buildCond` / `buildAct` validate-then-mask every
      // integer field, and this resolver masking first is what let
      // `SetDeathsX(1002, Add, 1, 0, 1.5)` write `mask: 1` (ADR 0019 §6,
      // cycle 16). One masking site, after the guard.
      return value;
    default:
      return value;
  }
}

/**
 * Validate every parameter, in the canonical field order, before the row is
 * built. `buildAct` queues as it constructs, so a validator that threw after
 * the call would leave a half-emitted action behind.
 */
function collectStockValues(
  row: StockRowSpec,
  args: readonly unknown[],
): { values: Map<string, unknown>; relocs: HirReloc[] } {
  const order: readonly string[] =
    row.kind === "condition" ? COND_FIELD_ORDER : ACT_FIELD_ORDER;
  // A `ptr` parameter writes the action destination field even though the
  // spec stores it under `relocs`; resolve it where `dest` is validated so
  // relocations keep their frozen push order.
  const fieldOf = (param: StockParamSpec): string =>
    param.encoding === "ptr" ? "dest" : param.field;
  const byField = new Map<string, { param: StockParamSpec; arg: unknown }>();
  for (const [index, param] of row.params.entries()) {
    const field = fieldOf(param);
    if (byField.has(field)) {
      throw new EudslError(
        "EUDSL-TYPE",
        `${row.id}: duplicate HIR field ${field}`,
      );
    }
    byField.set(field, { param, arg: args[index] });
  }
  for (const field of byField.keys()) {
    if (!order.includes(field)) {
      throw new EudslError(
        "EUDSL-TYPE",
        `${row.id}: HIR field ${field} is not an emit field`,
      );
    }
  }
  const values = new Map<string, unknown>();
  const relocs: HirReloc[] = [];
  for (const field of order) {
    const entry = byField.get(field);
    if (entry) {
      values.set(field, resolveStockValue(row, entry.param, entry.arg, relocs));
    }
  }
  return { values, relocs };
}

/** Emit one generated condition row after validating all of its parameters. */
export function _emitStockCond(row: StockRowSpec, args: readonly unknown[]): HirCond {
  const { values, relocs } = collectStockValues(row, args);
  return buildCond({
    locid: values.get("locid") as number | undefined,
    player: values.get("player") as number | undefined,
    amount: values.get("amount") as number | undefined,
    unit: values.get("unit") as number | undefined,
    comparison: values.get("comparison") as number | undefined,
    condtype: row.chkType,
    restype: values.get("restype") as number | undefined,
    flags: (values.get("flags") ?? row.flags) as number,
    eudx: (values.get("eudx") ?? row.eudx ?? 0) as number,
    relocs,
  });
}

/** Emit one generated action row after validating all of its parameters. */
export function _emitStockAct(row: StockRowSpec, args: readonly unknown[]): HirAct {
  const { values, relocs } = collectStockValues(row, args);
  return buildAct({
    mask: values.get("mask") as number | undefined,
    strid: values.get("strid") as number | undefined,
    wavid: values.get("wavid") as number | undefined,
    time: values.get("time") as number | undefined,
    dest: values.get("dest") as number | undefined,
    value: values.get("value") as number | undefined,
    unit: values.get("unit") as number | undefined,
    acttype: row.chkType,
    modifier: (values.get("modifier") ?? row.modifier) as number | undefined,
    flags: (values.get("flags") ?? row.flags) as number,
    eudx: (values.get("eudx") ?? row.eudx ?? 0) as number,
    text: values.get("text") as string | undefined,
    wav: values.get("wav") as string | undefined,
    strIndex: values.get("str_index") as number | undefined,
    relocs,
  });
}

// ---------------------------------------------------------------------------
// Stock Conditions
// ---------------------------------------------------------------------------

export function Deaths(
  player: PlayerOrFieldTarget,
  comparison: number,
  amount: ValueOrPtrTarget,
  unit = 0,
): HirCond {
  const relocs: HirReloc[] = [];
  const p = resolveEpdDest(player, 4, relocs);
  const a = resolveValueOrPtr(amount, 8, relocs);
  return buildCond({
    player: p,
    amount: a,
    unit,
    comparison,
    condtype: 15,
    relocs,
  });
}

// ---------------------------------------------------------------------------
// Stock Actions
// ---------------------------------------------------------------------------

export function SetDeaths(
  player: PlayerOrFieldTarget,
  modifier: number,
  value: ValueOrPtrTarget,
  unit = 0,
): HirAct {
  const relocs: HirReloc[] = [];
  const dest = resolveEpdDest(player, 16, relocs);
  const val = resolveValueOrPtr(value, 20, relocs);
  return buildAct({
    dest,
    value: val,
    unit,
    acttype: 45,
    modifier,
    flags: 20,
    relocs,
  });
}

/**
 * Write one 32-bit word of a module string buffer through its late-bound
 * `StrAddr`. `byteOffset` must be 4-byte aligned (the address is EPD folded).
 * Partial writes use `SetDeathsX`; full-dword writes use plain `SetDeaths`.
 */
export function SetStringDword(
  index: number,
  byteOffset: number,
  value: number,
  mask = 0xffffffff,
  modifier = SetTo,
): HirAct {
  if (!Number.isInteger(index) || index < 0) {
    throw new EudslError(
      "EUDSL-BOUNDS",
      `SetStringDword: string index must be a non-negative integer, got ${index}`,
    );
  }
  if (!Number.isInteger(byteOffset) || byteOffset < 0 || (byteOffset & 3) !== 0) {
    throw new EudslError(
      "EUDSL-BOUNDS",
      `SetStringDword: byte offset must be a non-negative multiple of 4, got ${byteOffset}`,
    );
  }
  // Validate then mask at the one site (ADR 0019 §6, cycle 17): masking
  // first below turned `value=1.5` into `value: 1`, `mask=1.5` into
  // `mask: 1, eudx: 17235` (a different act classification), and
  // `modifier=true` into `modifier: 1`.
  value = requirePlainInt(value, "SetStringDword value");
  mask = requirePlainInt(mask, "SetStringDword mask");
  modifier = requirePlainInt(modifier, "SetStringDword modifier");
  const m = mask >>> 0;
  return buildAct({
    mask: m === 0xffffffff ? 0 : m,
    value: value >>> 0,
    acttype: 45,
    modifier: modifier & 0xff,
    flags: 20,
    eudx: m === 0xffffffff ? 0 : EUDX_MAGIC,
    relocs: [
      {
        field_offset: 16,
        target_string: index,
        target_offset: byteOffset,
        mode: 1,
      },
    ],
  });
}

/** Input fields for [`UnitProperty`]. Omitted fields mean "don't care". */
export interface UnitPropertyOptions {
  hitPoints?: number;
  shields?: number;
  energy?: number;
  resource?: number;
  hangar?: number;
  cloaked?: boolean;
  burrowed?: boolean;
  inTransit?: boolean;
  hallucinated?: boolean;
  invincible?: boolean;
}

const UNIT_PROPERTY_KEYS: ReadonlySet<string> = new Set([
  "hitPoints",
  "shields",
  "energy",
  "resource",
  "hangar",
  "cloaked",
  "burrowed",
  "inTransit",
  "hallucinated",
  "invincible",
]);

function optionalUnitPropertyInt(
  name: string,
  value: number | undefined,
  min: number,
  max: number,
): number | undefined {
  return value === undefined ? undefined : stockInt(name, value, min, max);
}

function optionalUnitPropertyBool(
  name: string,
  value: boolean | undefined,
): boolean | undefined {
  if (value === undefined) {
    return undefined;
  }
  if (typeof value !== "boolean") {
    throw new EudslError(
      "EUDSL-TYPE",
      `${name} must be true, false, or undefined, got ${String(value)}`,
    );
  }
  return value;
}

/**
 * Build the normalized 20-byte UPRP record used by
 * [`CreateUnitWithProperties`].
 *
 * `hitPoints`, `shields`, and `energy` are percentages (`0..100`).
 * `resource` is `0..65535`, `hangar` is `0..255`, and the five special
 * flags accept `true`, `false`, or `undefined`. Normalization follows
 * eudplib's `property_key`: `100%` HP/shields and a `0` hangar mean
 * "unset", and an explicit `false` flag is equivalent to unset.
 */
export function UnitProperty(
  options: UnitPropertyOptions = {},
): readonly number[] {
  for (const key of Object.keys(options)) {
    if (!UNIT_PROPERTY_KEYS.has(key)) {
      throw new EudslError(
        "EUDSL-TYPE",
        `UnitProperty has no field ${key}`,
      );
    }
  }

  let hitPoints = optionalUnitPropertyInt("hit points", options.hitPoints, 0, 100);
  let shields = optionalUnitPropertyInt("shields", options.shields, 0, 100);
  let energy = optionalUnitPropertyInt("energy", options.energy, 0, 100);
  let resource = optionalUnitPropertyInt("resource", options.resource, 0, 65535);
  let hangar = optionalUnitPropertyInt("hangar", options.hangar, 0, 255);
  const cloaked = optionalUnitPropertyBool("cloaked", options.cloaked);
  const burrowed = optionalUnitPropertyBool("burrowed", options.burrowed);
  const inTransit = optionalUnitPropertyBool("in transit", options.inTransit);
  const hallucinated = optionalUnitPropertyBool(
    "hallucinated",
    options.hallucinated,
  );
  const invincible = optionalUnitPropertyBool("invincible", options.invincible);

  let prpvalid = 0;
  if (hitPoints !== undefined) prpvalid |= 1 << 1;
  if (shields !== undefined) prpvalid |= 1 << 2;
  if (energy !== undefined) prpvalid |= 1 << 3;
  if (resource !== undefined) prpvalid |= 1 << 4;
  if (hangar !== undefined) prpvalid |= 1 << 5;

  let sprpvalid = 0;
  if (cloaked !== undefined) sprpvalid |= 1 << 0;
  if (burrowed !== undefined) sprpvalid |= 1 << 1;
  if (inTransit !== undefined) sprpvalid |= 1 << 2;
  if (hallucinated !== undefined) sprpvalid |= 1 << 3;
  if (invincible !== undefined) sprpvalid |= 1 << 4;

  let sprpflag = 0;
  if (cloaked) sprpflag |= 1 << 0;
  if (burrowed) sprpflag |= 1 << 1;
  if (inTransit) sprpflag |= 1 << 2;
  if (hallucinated) sprpflag |= 1 << 3;
  if (invincible) sprpflag |= 1 << 4;

  // eudplib `property_key` normalization.
  if (hitPoints === 100) {
    prpvalid &= ~(1 << 1);
    hitPoints = 0;
  }
  if (shields === 100) {
    prpvalid &= ~(1 << 2);
    shields = 0;
  }
  if (hangar === 0) {
    prpvalid &= ~(1 << 5);
  }
  if ((prpvalid & (1 << 1)) === 0) hitPoints = 0;
  if ((prpvalid & (1 << 2)) === 0) shields = 0;
  if ((prpvalid & (1 << 3)) === 0) energy = 0;
  if ((prpvalid & (1 << 4)) === 0) resource = 0;
  if ((prpvalid & (1 << 5)) === 0) hangar = 0;
  sprpvalid &= sprpflag;
  sprpflag &= sprpvalid;

  const record = [
    sprpvalid & 0xff,
    (sprpvalid >>> 8) & 0xff,
    prpvalid & 0xff,
    (prpvalid >>> 8) & 0xff,
    0, // player: always NULL
    hitPoints ?? 0,
    shields ?? 0,
    energy ?? 0,
    (resource ?? 0) & 0xff,
    ((resource ?? 0) >>> 8) & 0xff,
    ((resource ?? 0) >>> 16) & 0xff,
    ((resource ?? 0) >>> 24) & 0xff,
    (hangar ?? 0) & 0xff,
    ((hangar ?? 0) >>> 8) & 0xff,
    sprpflag & 0xff,
    (sprpflag >>> 8) & 0xff,
    0,
    0,
    0,
    0,
  ];
  return Object.freeze(record);
}

/**
 * Create `count` units of `unit` at `location` for `player` with the
 * properties from a [`UnitProperty`] record (action 11).
 *
 * The action's 1-based property index is assigned when the map is
 * assembled, after UPRP slot allocation.
 */
export function CreateUnitWithProperties(
  count: number,
  unit: number,
  location: number,
  player: number,
  properties: readonly number[],
): HirAct {
  if (!Array.isArray(properties) || properties.length !== 20) {
    throw new EudslError(
      "EUDSL-TYPE",
      "properties must be a 20-byte UnitProperty record",
    );
  }
  for (const byte of properties) {
    if (!Number.isInteger(byte) || byte < 0 || byte > 255) {
      throw new EudslError(
        "EUDSL-TYPE",
        `properties bytes must be integers in 0..255, got ${String(byte)}`,
      );
    }
  }
  return buildAct({
    mask: stockLocation(location),
    dest: stockPlayer(player),
    value: 0,
    unit: stockUnit(unit),
    acttype: 11,
    modifier: stockInt("count", count, 1, 255),
    flags: 28,
    uprp: properties,
  });
}

// ---------------------------------------------------------------------------
// buildModule & HIR 3.0 Serialization
// ---------------------------------------------------------------------------

export interface HirVarDecl {
  readonly id: number;
  readonly init: number;
  readonly mask?: number;
  /** `"page"` pins the variable to a TriggerPage action slot (container backing). */
  readonly storage?: "page";
  /** Late-bound module-string address initializer (page-pinned variables only). */
  readonly init_string?: { readonly index: number; readonly offset: number };
}

export interface HirTriggerNode {
  readonly kind: "trigger";
  readonly id: number;
  readonly conds: readonly HirCond[];
  readonly acts: readonly HirAct[];
  readonly preserved: boolean;
  readonly flags: number;
  readonly prev?: HirTriggerPtr;
  readonly next?: HirTriggerPtr;
}

export interface HirVarTransferNode {
  readonly kind: "vartransfer";
  readonly dst_var: number;
  readonly src_var: number;
  readonly modifier: number;
}

export interface HirVarStoreNode {
  readonly kind: "varstore";
  readonly src_var: number;
  readonly dst_addr?: number;
  readonly dst_trigger?: number;
  readonly dst_offset?: number;
  readonly modifier: number;
}

export interface HirActNode extends HirAct {
  readonly kind: "act";
}

/** One argument of a subroutine call: a variable or a constant integer. */
export type HirCallArgNode =
  | { readonly kind: "var"; readonly id: number }
  | { readonly kind: "const"; readonly value: number };

/**
 * Call of a callable subroutine (see `HirFuncNode`).
 *
 * `dst` is absent when the result is unused (or the subroutine has none).
 */
export interface HirCallNode {
  readonly kind: "call";
  readonly func: number;
  readonly args: readonly HirCallArgNode[];
  readonly dst?: number;
}

/**
 * A callable subroutine: a statement body emitted once per module.
 *
 * Call sites bind `params` positionally, jump into the one shared body, and
 * copy `result` back into their own fresh variable. Subroutines cannot
 * recurse: the return path is a single pointer patched per call site.
 */
export interface HirFuncNode {
  readonly id: number;
  readonly params: readonly number[];
  readonly result?: number;
  readonly body: readonly HirStmtNode[];
}

export interface HirWhenNode {
  readonly kind: "when";
  readonly conds: readonly HirCond[];
  readonly body: readonly HirStmtNode[];
  readonly else_body?: readonly HirStmtNode[];
}

export interface HirOnceNode {
  readonly kind: "once";
  readonly conds: readonly HirCond[];
  readonly body: readonly HirStmtNode[];
}

export interface HirWhileNode {
  readonly kind: "while";
  readonly conds: readonly HirCond[];
  readonly body: readonly HirStmtNode[];
}

/**
 * Until node (C11-02, corrected mechanism). `latch_var` is the latch
 * state variable reserved by the DSL at emission time, in exactly the
 * old desugar's allocation position; the opt desugar uses it verbatim
 * and never mints ids.
 */
export interface HirUntilNode {
  readonly kind: "until";
  readonly conds: readonly HirCond[];
  readonly body: readonly HirStmtNode[];
  readonly latch_var: number;
}

/**
 * Edge node (C11-03). `curr_var` / `prev_var` are the two state
 * variables reserved by the DSL at emission time (see `onRising` /
 * `onFalling`); the opt desugar uses them verbatim.
 */
export interface HirEdgeNode {
  readonly kind: "edge";
  readonly direction: "rising" | "falling";
  readonly conds: readonly HirCond[];
  readonly body: readonly HirStmtNode[];
  readonly curr_var: number;
  readonly prev_var: number;
}

/**
 * Periodic execution block (`every(n)`): runs `body` once every `n`
 * trigger loops. C11-04: `counter_var` is the counter variable
 * allocated by the DSL at emission time, in the old allocation order;
 * opt desugars using that id verbatim.
 */
export interface HirEveryNode {
  readonly kind: "every";
  readonly n: number;
  readonly counter_var: number;
  readonly body: readonly HirStmtNode[];
}

/**
 * Delayed one-shot execution block (`after(n)`): runs `body` once,
 * after `n` trigger loops. C11-04: same node/desugar split as `every`.
 */
export interface HirAfterNode {
  readonly kind: "after";
  readonly n: number;
  readonly counter_var: number;
  readonly body: readonly HirStmtNode[];
}

export type HirStmtNode =
  | HirTriggerNode
  | HirActNode
  | HirVarTransferNode
  | HirVarStoreNode
  | HirCallNode
  | HirWhenNode
  | HirOnceNode
  | HirWhileNode
  | HirUntilNode
  | HirEdgeNode
  | HirEveryNode
  | HirAfterNode;

export interface HirEmbeddedFile {
  readonly name: string;
  readonly path: string;
}

/** A relocatable raw data blob (`Db`), lowered to an `LirItem::Blob`. */
export interface HirBlobNode {
  readonly size: number;
  /** Base64 payload bytes (may be shorter than `size`; zero-padded). */
  readonly data?: string;
  readonly relocs?: readonly HirBlobRelocNode[];
}

export interface HirDocument {
  readonly version: string;
  readonly module: {
    readonly vars?: readonly HirVarDecl[];
    readonly stmts: readonly HirStmtNode[];
    readonly funcs?: readonly HirFuncNode[];
    readonly strings?: readonly string[];
    readonly files?: readonly HirEmbeddedFile[];
    readonly blobs?: readonly HirBlobNode[];
  };
}

export class CompiledModule {
  readonly triggers: readonly Trigger[];
  readonly vars: readonly HirVarDecl[];
  readonly files: readonly HirEmbeddedFile[];
  readonly funcs: readonly HirFuncNode[];
  readonly blobs: readonly HirBlobNode[];
  private readonly doc: HirDocument;

  constructor(
    triggers: readonly Trigger[],
    vars: readonly HirVarDecl[] = [],
    stmts?: readonly HirStmtNode[],
    strings: readonly string[] = [],
    files: readonly HirEmbeddedFile[] = [],
    funcs: readonly HirFuncNode[] = [],
    blobs: readonly HirBlobNode[] = [],
  ) {
    this.triggers = Object.freeze([...triggers]);
    this.vars = Object.freeze([...vars]);
    this.files = Object.freeze(files.map((f) => Object.freeze({ ...f })));
    this.funcs = Object.freeze(funcs.map((f) => Object.freeze({ ...f })));
    this.blobs = Object.freeze(blobs.map((b) => Object.freeze({ ...b })));
    const finalStmts: HirStmtNode[] = stmts
      ? [...stmts]
      : this.triggers.map((t) => triggerToHirNode(t));

    const modObj: Record<string, unknown> = {};
    if (this.vars.length > 0) {
      modObj.vars = this.vars;
    }
    modObj.stmts = finalStmts;
    if (strings.length > 0) {
      modObj.strings = [...strings];
    }
    if (this.files.length > 0) {
      modObj.files = this.files;
    }
    if (this.funcs.length > 0) {
      modObj.funcs = this.funcs;
    }
    if (this.blobs.length > 0) {
      modObj.blobs = this.blobs;
    }

    this.doc = {
      version: HIR_VERSION,
      module: modObj as HirDocument["module"],
    };
  }

  toHir(): HirDocument {
    return this.doc;
  }

  toDict(): HirDocument {
    return this.doc;
  }

  to_dict(): HirDocument {
    return this.doc;
  }

  toJson(): string {
    return JSON.stringify(this.doc);
  }

  to_json(): string {
    return this.toJson();
  }
}

function triggerToHirNode(t: Trigger): HirTriggerNode {
  const node: Record<string, unknown> = {
    kind: "trigger",
    id: t.id,
    conds: t.conds,
    acts: t.acts,
    preserved: t.preserved,
    flags: t.flags,
  };
  if (t.prevPtr !== undefined) {
    node.prev = t.prevPtr;
  }
  if (t.nextPtr !== undefined) {
    node.next = t.nextPtr;
  }
  return node as unknown as HirTriggerNode;
}

export type BuilderEntry =
  | { kind: "act"; act: HirAct }
  | { kind: "trigger"; trig: Trigger }
  | {
      kind: "vartransfer";
      dst_var: number;
      src_var: number;
      modifier: number;
    }
  | {
      kind: "varstore";
      src_var: number;
      target: VarStoreTarget;
      modifier: number;
    }
  | { kind: "stmt"; stmt: HirStmtNode };

function varStoreToHir(entry: {
  src_var: number;
  target: VarStoreTarget;
  modifier: number;
}): HirVarStoreNode {
  const node: Record<string, unknown> = {
    kind: "varstore",
    src_var: entry.src_var,
    modifier: entry.modifier,
  };
  if ("addr" in entry.target) {
    node.dst_addr = entry.target.addr >>> 0;
  } else {
    node.dst_trigger = entry.target.triggerId;
    node.dst_offset = entry.target.offset;
  }
  return node as unknown as HirVarStoreNode;
}

export function flushEntriesToStmts(entries: readonly BuilderEntry[]): HirStmtNode[] {
  const out: HirStmtNode[] = [];
  for (const entry of entries) {
    if (entry.kind === "act") {
      out.push({ kind: "act", ...entry.act });
    } else if (entry.kind === "trigger") {
      out.push(triggerToHirNode(entry.trig));
    } else if (entry.kind === "vartransfer") {
      out.push({
        kind: "vartransfer",
        dst_var: entry.dst_var,
        src_var: entry.src_var,
        modifier: entry.modifier,
      });
    } else if (entry.kind === "varstore") {
      out.push(varStoreToHir(entry));
    } else {
      out.push(entry.stmt);
    }
  }
  return out;
}

export interface ScopeBuilderHook extends ModuleBuilderHook {
  pushScope(): void;
  popScope(): BuilderEntry[];
  queueStmt(stmt: HirStmtNode): void;
  queueAct(act: HirAct): void;
  /** Subroutine seams (see `HirFuncNode`); absent on partial test doubles. */
  allocFuncId?(): number;
  queueFunc?(func: HirFuncNode): void;
  queueCall?(
    funcId: number,
    args: readonly HirCallArgNode[],
    dstVar: number | null,
  ): void;
  captureEntries?(thunk: () => void): HirStmtNode[];
  queueVarStore?(
    srcVar: number,
    target: VarStoreTargetInput,
    modifier: number,
  ): void;
  embedFile?(name: string, path: string): void;
}

/**
 * Flatten a root entry list into triggers + HIR statements.
 *
 * Adjacent top-level actions are bin-packed into `Trigger (16C, 64A)` nodes
 * with ids from `allocId`. Shared by `buildModule` (module root) and
 * subroutine body capture, so both surfaces produce the same shape.
 */
function finalizeRootEntries(
  rootEntries: readonly BuilderEntry[],
  allocId: () => number,
): { triggers: Trigger[]; stmts: HirStmtNode[] } {
  const finalTriggers: Trigger[] = [];
  const finalStmts: HirStmtNode[] = [];
  const pendingActs: HirAct[] = [];

  const flushPending = (): void => {
    for (let i = 0; i < pendingActs.length; i += ACT_COUNT) {
      const chunk = pendingActs.slice(i, i + ACT_COUNT);
      const prev = _setActiveModuleBuilder(null);
      try {
        const trig = new Trigger({
          conds: [],
          acts: chunk,
          preserved: true,
        });
        (trig as { id: number }).id = allocId();
        finalTriggers.push(trig);
        finalStmts.push(triggerToHirNode(trig));
      } finally {
        _setActiveModuleBuilder(prev);
      }
    }
    pendingActs.length = 0;
  };

  for (const entry of rootEntries) {
    if (entry.kind === "act") {
      pendingActs.push(entry.act);
    } else if (entry.kind === "trigger") {
      if (pendingActs.length > 0) flushPending();
      finalTriggers.push(entry.trig);
      finalStmts.push(triggerToHirNode(entry.trig));
    } else if (entry.kind === "vartransfer") {
      if (pendingActs.length > 0) flushPending();
      finalStmts.push({
        kind: "vartransfer",
        dst_var: entry.dst_var,
        src_var: entry.src_var,
        modifier: entry.modifier,
      });
    } else if (entry.kind === "varstore") {
      if (pendingActs.length > 0) flushPending();
      finalStmts.push(varStoreToHir(entry));
    } else {
      if (pendingActs.length > 0) flushPending();
      finalStmts.push(entry.stmt);
    }
  }
  if (pendingActs.length > 0) flushPending();
  return { triggers: finalTriggers, stmts: finalStmts };
}

/**
 * Compile a top-level EUD module body into a canonical HIR 3.0 [`CompiledModule`].
 * Adjacent unconditional top-level actions are automatically bin-packed up to
 * 64 actions per `Trigger (16C, 64A)` before opening a new `Trigger`.
 */
export function buildModule(fn: () => void): CompiledModule {
  // Single entry point (user rule, 2026-11-04): one buildModule per map,
  // like Rust main. A nested call throws instead of saving/restoring a
  // previous builder; there is no inner-builder case anymore.
  if (_getActiveModuleBuilder() !== null) {
    throw new EudslError(
      "EUDSL-SCOPE",
      "buildModule() must not be called inside buildModule() (single entry point, one per map)",
    );
  }
  // Builder seam implementation (candidate 8, shape C): this closure OWNS
  // every piece of builder state — trigger/var/page counters, the scope
  // serial, the scope stack, and the flusher set — and exposes it only
  // through the `hook` object installed as the active builder. `trigger.ts`
  // holds just the `ModuleBuilderHook` interface and the active-builder
  // slot; see its contract comment for the ordering guarantees.
  // - Flushers run before every scope change (pushScope AND popScope) and
  //   once more at module end; `scopeSerial` bumps at every scope change.
  //   Scope-boundary flushes call each flusher with align=true; the
  //   module-end flush calls it with align=false.
  // - `flushers` is a `Set`: registering the same function twice runs it
  //   once per flush. The Python mirror uses a list (runs twice); the seam
  //   tests pin that divergence as-is instead of unifying it.
  // - `popScope` pops unconditionally, including the root scope; the
  //   Python mirror guards the root. Balanced temporal scopes never pop
  //   the root, so the divergence is unreachable in practice (pinned
  //   as-is by the seam tests).
  let nextId = 0;
  let nextVarId = 0;
  let nextPageSlot = 0;
  let scopeSerial = 0;
  let nextFuncId = 0;
  const vars: HirVarDecl[] = [];
  const strings: string[] = [];
  const files: HirEmbeddedFile[] = [];
  const funcs: HirFuncNode[] = [];
  const blobs: HirBlobNode[] = [];
  let scopeStack: BuilderEntry[][] = [[]];
  let flushers = new Set<(align: boolean) => void>();

  const currentEntries = (): BuilderEntry[] => scopeStack[scopeStack.length - 1];

  const hook: ScopeBuilderHook = {
    allocTriggerId(): number {
      return nextId++;
    },
    allocVar(init: number, mask = 0xffffffff, storage?: "page"): number {
      const id = nextVarId++;
      const decl: Record<string, number | string> = {
        id,
        init: init >>> 0,
      };
      if ((mask >>> 0) !== 0xffffffff) {
        decl.mask = mask >>> 0;
      }
      if (storage !== undefined) {
        decl.storage = storage;
      }
      if (storage === "page") {
        nextPageSlot++;
      }
      vars.push(Object.freeze(decl) as unknown as HirVarDecl);
      return id;
    },
    allocString(text: string): number {
      const index = strings.length;
      strings.push(text);
      return index;
    },
    embedFile(name: string, path: string): void {
      if (typeof name !== "string" || name.length === 0) {
        throw new EudslError(
          "EUDSL-BOUNDS",
          "embedFile: embedded file name must be a non-empty string",
        );
      }
      if (typeof path !== "string" || path.length === 0) {
        throw new EudslError(
          "EUDSL-BOUNDS",
          `embedFile: embedded file ${JSON.stringify(name)} must have a non-empty path`,
        );
      }
      for (const existing of files) {
        if (existing.name !== name) continue;
        if (existing.path === path) return;
        throw new EudslError(
          "EUDSL-BOUNDS",
          `duplicate embedded file name ${JSON.stringify(name)}`,
        );
      }
      files.push(Object.freeze({ name, path }));
    },
    allocBlob(
      payload: number | Uint8Array,
      relocs: readonly DbRelocInput[] = [],
    ): { readonly id: number; readonly size: number } {
      let size: number;
      let data = "";
      if (typeof payload === "number") {
        if (!Number.isInteger(payload) || payload < 0) {
          throw new EudslError(
            "EUDSL-BOUNDS",
            `Db size must be a non-negative integer, got ${String(payload)}`,
          );
        }
        size = payload;
      } else if (payload instanceof Uint8Array) {
        size = payload.length;
        data = bytesToBase64(payload);
      } else {
        throw new EudslError(
          "EUDSL-TYPE",
          `Db payload must be a byte size or Uint8Array, got ${describeValue(payload)}`,
        );
      }
      const decl: Record<string, unknown> = { size };
      if (data.length > 0) {
        decl.data = data;
      }
      const normalized = normalizeBlobRelocs(relocs);
      if (normalized.length > 0) {
        decl.relocs = normalized;
      }
      const id = blobs.length;
      blobs.push(Object.freeze(decl) as unknown as HirBlobNode);
      return { id, size };
    },
    getNextStringIndex(): number {
      return strings.length;
    },
    allocStringVar(index: number, offset = 0, mask = 0xffffffff): number {
      if (!Number.isInteger(index) || index < 0) {
        throw new EudslError(
          "EUDSL-BOUNDS",
          `allocStringVar: string index must be a non-negative integer, got ${index}`,
        );
      }
      const id = nextVarId++;
      const decl: Record<string, unknown> = {
        id,
        init: 0,
        storage: "page",
        init_string: { index, offset: offset >>> 0 },
      };
      if ((mask >>> 0) !== 0xffffffff) {
        decl.mask = mask >>> 0;
      }
      nextPageSlot++;
      vars.push(Object.freeze(decl) as unknown as HirVarDecl);
      return id;
    },
    getNextVarId(): number {
      return nextVarId;
    },
    getNextPageSlot(): number {
      return nextPageSlot;
    },
    getScopeSerial(): number {
      return scopeSerial;
    },
    registerFlusher(fn: (align: boolean) => void): void {
      flushers.add(fn);
    },
    pushScope(): void {
      // Align before a branch so its skipped path and its taken path share a
      // lane; the module-end flush above stays unaligned because no run
      // follows it.
      for (const flush of flushers) flush(true);
      scopeSerial++;
      scopeStack.push([]);
    },
    popScope(): BuilderEntry[] {
      for (const flush of flushers) flush(true);
      scopeSerial++;
      return scopeStack.pop() ?? [];
    },
    queueStmt(stmt: HirStmtNode): void {
      currentEntries().push({ kind: "stmt", stmt });
    },
    queueAct(act: HirAct): void {
      currentEntries().push({ kind: "act", act });
    },
    queueVarTransfer(dstVar: number, srcVar: number, modifier: number): void {
      currentEntries().push({
        kind: "vartransfer",
        dst_var: dstVar,
        src_var: srcVar,
        modifier: modifier & 0xff,
      });
    },
    queueVarStore(
      srcVar: number,
      target: VarStoreTargetInput,
      modifier: number,
    ): void {
      const normalized = normalizeVarStoreTarget(target);
      if ("addr" in normalized) {
        const addr = requirePlainInt(normalized.addr, "VarStore address");
        // `Db`/`DbRef` never reaches here (it is a `dst_trigger` field
        // relocation), and a `triggerId` target is a field inside a trigger,
        // not an absolute address — so this is the only place a VarStore
        // names a dword by byte address (#111). Guard here so the error is
        // eager, before the `>>> 0` in `varStoreToHir` (ADR 0019).
        requireAlignedDwordAddress(addr, "VarStore address");
        currentEntries().push({
          kind: "varstore",
          src_var: srcVar,
          target: { addr } as VarStoreTarget,
          modifier: modifier & 0xff,
        });
        return;
      }
      const triggerId = requirePlainInt(
        normalized.triggerId,
        "VarStore trigger_id",
      );
      const offset =
        "offset" in normalized
          ? requirePlainInt(normalized.offset, "VarStore offset")
          : 0;
      if (offset % 4 !== 0) {
        throw new EudslError(
          "EUDSL-BOUNDS",
          `VarStore offset ${offset} is not a multiple of 4`,
        );
      }
      currentEntries().push({
        kind: "varstore",
        src_var: srcVar,
        target: { triggerId, offset } as VarStoreTarget,
        modifier: modifier & 0xff,
      });
    },
    consumeQueuedActs(acts: readonly HirAct[]): void {
      if (acts.length === 0) return;
      const toConsume = new Set(acts);
      const entries = currentEntries();
      for (let i = entries.length - 1; i >= 0 && toConsume.size > 0; i--) {
        const e = entries[i];
        if (e.kind === "act" && toConsume.has(e.act)) {
          toConsume.delete(e.act);
          entries.splice(i, 1);
        }
      }
    },
    registerTrigger(trig: Trigger): void {
      currentEntries().push({ kind: "trigger", trig });
    },
    allocFuncId(): number {
      return nextFuncId++;
    },
    queueFunc(func: HirFuncNode): void {
      funcs.push(Object.freeze({ ...func }));
    },
    queueCall(
      funcId: number,
      args: readonly HirCallArgNode[],
      dstVar: number | null,
    ): void {
      const node: Record<string, unknown> = {
        kind: "call",
        func: funcId,
        args: args.map((a) => ({ ...a })),
      };
      if (dstVar !== null) node.dst = dstVar;
      currentEntries().push({ kind: "stmt", stmt: node as unknown as HirStmtNode });
    },
    /**
     * Run `thunk` in an isolated root scope and return its statements.
     *
     * Subroutine bodies are ordinary statement lists -- they may open
     * `when`/`while` scopes and register flushers -- so the capture swaps in
     * a fresh scope stack and flusher set, runs the body, then restores both.
     * The collected entries go through the same act bin-packing as a module
     * root, but are returned instead of installed.
     */
    captureEntries(thunk: () => void): HirStmtNode[] {
      const savedScope = scopeStack;
      const savedFlushers = flushers;
      scopeStack = [[]];
      flushers = new Set<(align: boolean) => void>();
      let entries: BuilderEntry[] = [];
      try {
        thunk();
      } finally {
        entries = scopeStack[0];
        scopeStack = savedScope;
        flushers = savedFlushers;
      }
      return finalizeRootEntries(entries, () => hook.allocTriggerId()).stmts;
    },
  };

  const prevBuilder = _setActiveModuleBuilder(hook);
  try {
    fn();
    for (const flush of flushers) flush(false);
  } finally {
    _setActiveModuleBuilder(prevBuilder);
  }

  const rootEntries = scopeStack[0];
  const { triggers: finalTriggers, stmts: finalStmts } = finalizeRootEntries(
    rootEntries,
    () => nextId++,
  );

  const compiled = new CompiledModule(
    finalTriggers,
    vars,
    finalStmts,
    strings,
    files,
    funcs,
    blobs,
  );
  return compiled;
}

export const build_module = buildModule;
