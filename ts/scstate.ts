/**
 * Tech and upgrade state over the SC/BW per-player byte tables.
 *
 * An entry lives at byte `addr & 3` of the dword at `EPD(addr)`, where
 * `addr = BASE + indexOffset + STRIDE * player`. The table is split at id 24
 * (tech) and 46 (upgrade): SC and BW use different bases and player strides,
 * so {@link scBwSlot} is the one place that picks the pair.
 *
 * A constant `player` costs one masked `DeathsX` (read) or one `SetDeathsX`
 * (write). A runtime `player` binds the computed EPD with `withEpd` and
 * reads/writes through `CurrentPlayer`. Tech strides are multiples of four, so
 * the byte lane stays constant; upgrade strides are not, so its runtime form
 * dispatches over the four lanes.
 */

import { withEpd } from "./cp.ts";
import { AtLeast, DeathsX, SetDeathsX, SetTo, type HirCond } from "./hir.ts";
import { when } from "./temporal.ts";
import { Expr, Var, VarImpl, type ReadonlyVar } from "./var.ts";
import {
  CURRENT_PLAYER,
  EPD,
  EPD_BASE,
  EudslError,
  describeValue,
  readEpd,
  requirePlainInt,
} from "./view.ts";

export type PlayerOperand = number | VarImpl | ReadonlyVar | Expr;

export const TECH_COUNT = 44;
export const TECH_SC_COUNT = 24;
export const TECH_STATE_SC_BASE = 0x58cf44;
export const TECH_STATE_BW_BASE = 0x58f140;
export const TECH_STATE_SC_STRIDE = 24;
export const TECH_STATE_BW_STRIDE = 20;

export const UPGRADE_COUNT = 61;
export const UPGRADE_SC_COUNT = 46;
export const UPGRADE_STATE_SC_BASE = 0x58d2b0;
export const UPGRADE_STATE_BW_BASE = 0x58f32c;
export const UPGRADE_STATE_SC_STRIDE = 46;
export const UPGRADE_STATE_BW_STRIDE = 15;

export const MAX_PLAYER_ID = 11;

/** The SC/BW bases, counts, and strides for one id domain. */
interface Split {
  readonly scCount: number;
  readonly scBase: number;
  readonly bwBase: number;
  readonly scStride: number;
  readonly bwStride: number;
}

const TECH_SPLIT: Split = {
  scCount: TECH_SC_COUNT,
  scBase: TECH_STATE_SC_BASE,
  bwBase: TECH_STATE_BW_BASE,
  scStride: TECH_STATE_SC_STRIDE,
  bwStride: TECH_STATE_BW_STRIDE,
};

const UPGRADE_SPLIT: Split = {
  scCount: UPGRADE_SC_COUNT,
  scBase: UPGRADE_STATE_SC_BASE,
  bwBase: UPGRADE_STATE_BW_BASE,
  scStride: UPGRADE_STATE_SC_STRIDE,
  bwStride: UPGRADE_STATE_BW_STRIDE,
};

/** `[base + indexOffset, playerStride]` for one id in a split table. */
function scBwSlot(index: number, split: Split): [number, number] {
  if (index < split.scCount) return [split.scBase + index, split.scStride];
  return [
    split.bwBase + (index - split.scCount),
    split.bwStride,
  ];
}

function requireCount(value: unknown, what: string, maximum: number): number {
  const index = requirePlainInt(value, what);
  if (index < 0 || index > maximum) {
    throw new EudslError(
      "EUDSL-RANGE",
      `${what} must be in 0..${maximum}, got ${index}`,
    );
  }
  return index;
}

/** Validate a build-time player id (`0..11`). */
function requirePlayerId(player: unknown, what: string): number {
  return requireCount(player, what, MAX_PLAYER_ID);
}

/** Validate a player operand: a plain id or a runtime value handle. */
function requirePlayer(player: unknown, what: string): PlayerOperand {
  if (typeof player === "number") {
    return requirePlayerId(player, what);
  }
  if (player instanceof VarImpl || player instanceof Expr) {
    return player;
  }
  throw new EudslError(
    "EUDSL-TYPE",
    `${what} must be a plain integer in 0..${MAX_PLAYER_ID}, a Var, ` +
      `or an Expr; got ${describeValue(player)}`,
  );
}

/** `[epd, shift]` for a constant `(id, player)` pair. */
function slotted(
  index: number,
  player: number,
  split: Split,
): [number, number] {
  const [anchor, stride] = scBwSlot(index, split);
  const addr = (anchor + stride * player) >>> 0;
  return [EPD(addr), 8 * (addr & 3)];
}

/** Materialize `(base + indexOffset) + stride * player` as a value. */
function runtimeAddr(
  index: number,
  player: PlayerOperand,
  split: Split,
): VarImpl {
  const [anchor, stride] = scBwSlot(index, split);
  const addr = new Var(0);
  addr.set(player); // value copy, never a pointer store
  addr.imul(stride);
  addr.iadd(anchor);
  return addr;
}

/** Fold a byte address into its dword EPD. */
function addrEpd(addr: VarImpl): VarImpl {
  const epd = new Var(0);
  epd.set(addr.shr(2));
  epd.isub(EPD_BASE >>> 2);
  return epd;
}

/** One condition testing the byte lane `shift` of `epd`. */
function maskedBit(epd: number, shift: number): HirCond {
  return DeathsX(epd, AtLeast, 1, 0, (0xff << shift) >>> 0);
}

/** Read one byte lane into a fresh `Var` (`epd` may be runtime). */
function maskedRead(epd: number | VarImpl, shift: number): VarImpl {
  if (typeof epd === "number") {
    return readEpd(epd, 8, shift);
  }
  return withEpd(epd, () => readEpd(CURRENT_PLAYER, 8, shift));
}

/** Write `value` into one byte lane, leaving the other lanes untouched. */
function maskedWrite(epd: number | VarImpl, value: number, shift: number): void {
  const mask = (0xff << shift) >>> 0;
  const payload = (value << shift) >>> 0;
  if (typeof epd === "number") {
    SetDeathsX(epd, SetTo, payload, 0, mask);
    return;
  }
  withEpd(epd, () => {
    SetDeathsX(CURRENT_PLAYER, SetTo, payload, 0, mask);
  });
}

/** Bind the dword EPD of `addr` and dispatch to its byte lane. */
function withLane(addr: VarImpl, body: (lane: number) => void): void {
  const epd = addrEpd(addr);
  const lane = new Var(0);
  lane.set(addr.bitAnd(3));
  withEpd(epd, () => {
    for (let k = 0; k < 4; k += 1) {
      when(lane.eq(k), () => body(k));
    }
  });
}

/** Read the byte at `addr` through a four-arm lane dispatch. */
function laneRead(addr: VarImpl): VarImpl {
  const out = new Var(0);
  withLane(addr, (k) => {
    out.set(maskedRead(CURRENT_PLAYER, 8 * k));
  });
  return out;
}

/** Write `level` to the byte at `addr` through four lane arms. */
function laneWrite(addr: VarImpl, level: number): void {
  withLane(addr, (k) => {
    maskedWrite(CURRENT_PLAYER, level, 8 * k);
  });
}

/** `[epd, shift]` of one tech entry for a build-time player id. */
export function techStateSlot(tech: number, player: number): [number, number] {
  const t = requireCount(tech, "techStateSlot() tech", TECH_COUNT - 1);
  const p = requirePlayerId(player, "techStateSlot() player");
  return slotted(t, p, TECH_SPLIT);
}

export const tech_state_slot = techStateSlot;

/** `[epd, shift]` of one upgrade entry for a build-time player id. */
export function upgradeStateSlot(
  upgrade: number,
  player: number,
): [number, number] {
  const u = requireCount(
    upgrade,
    "upgradeStateSlot() upgrade",
    UPGRADE_COUNT - 1,
  );
  const p = requirePlayerId(player, "upgradeStateSlot() player");
  return slotted(u, p, UPGRADE_SPLIT);
}

export const upgrade_state_slot = upgradeStateSlot;

/**
 * One condition that holds when `player` has researched `tech`.
 *
 * Constant-player only: a condition cannot name a runtime EPD. Use
 * {@link techResearchedVar} for a runtime player.
 */
export function techResearched(tech: number, player: number): HirCond {
  const t = requireCount(tech, "techResearched() tech", TECH_COUNT - 1);
  const p = requirePlayer(player, "techResearched() player");
  if (typeof p !== "number") {
    throw new EudslError(
      "EUDSL-TYPE",
      "techResearched() player must be a build-time plain integer; " +
        "use techResearchedVar() for a runtime player",
    );
  }
  const [epd, shift] = slotted(t, p, TECH_SPLIT);
  return maskedBit(epd, shift);
}

export const tech_researched = techResearched;

/** Researched state as a `0`/`1` value (runtime player allowed). */
export function techResearchedVar(
  tech: number,
  player: PlayerOperand,
): VarImpl {
  const t = requireCount(tech, "techResearchedVar() tech", TECH_COUNT - 1);
  const p = requirePlayer(player, "techResearchedVar() player");
  let res: VarImpl;
  if (typeof p === "number") {
    const [epd, shift] = slotted(t, p, TECH_SPLIT);
    res = maskedRead(epd, shift);
  } else {
    const [anchor] = scBwSlot(t, TECH_SPLIT);
    res = maskedRead(
      addrEpd(runtimeAddr(t, p, TECH_SPLIT)),
      8 * (anchor & 3),
    );
  }
  const out = new Var(0);
  when(res.ne(0), () => out.set(1));
  return out;
}

export const tech_researched_var = techResearchedVar;

/** Current level of `upgrade` for `player` as a byte value. */
export function upgradeLevel(
  upgrade: number,
  player: PlayerOperand,
): VarImpl {
  const u = requireCount(upgrade, "upgradeLevel() upgrade", UPGRADE_COUNT - 1);
  const p = requirePlayer(player, "upgradeLevel() player");
  if (typeof p === "number") {
    const [epd, shift] = slotted(u, p, UPGRADE_SPLIT);
    return maskedRead(epd, shift);
  }
  return laneRead(runtimeAddr(u, p, UPGRADE_SPLIT));
}

export const upgrade_level = upgradeLevel;

/** Write `on` to one tech entry, leaving the other byte lanes intact. */
export function setTechResearched(
  tech: number,
  player: PlayerOperand,
  on: boolean,
): void {
  const t = requireCount(tech, "setTechResearched() tech", TECH_COUNT - 1);
  const p = requirePlayer(player, "setTechResearched() player");
  if (typeof on !== "boolean") {
    throw new EudslError(
      "EUDSL-TYPE",
      `setTechResearched() on must be a bool, got ${describeValue(on)}`,
    );
  }
  const value = on ? 1 : 0;
  if (typeof p === "number") {
    const [epd, shift] = slotted(t, p, TECH_SPLIT);
    maskedWrite(epd, value, shift);
    return;
  }
  const [anchor] = scBwSlot(t, TECH_SPLIT);
  maskedWrite(
    addrEpd(runtimeAddr(t, p, TECH_SPLIT)),
    value,
    8 * (anchor & 3),
  );
}

export const set_tech_researched = setTechResearched;

/** Write `level` (a byte) to one upgrade entry. */
export function setUpgradeLevel(
  upgrade: number,
  player: PlayerOperand,
  level: number,
): void {
  const u = requireCount(
    upgrade,
    "setUpgradeLevel() upgrade",
    UPGRADE_COUNT - 1,
  );
  const p = requirePlayer(player, "setUpgradeLevel() player");
  const value = requireCount(level, "setUpgradeLevel() level", 0xff);
  if (typeof p === "number") {
    const [epd, shift] = slotted(u, p, UPGRADE_SPLIT);
    maskedWrite(epd, value, shift);
    return;
  }
  laneWrite(runtimeAddr(u, p, UPGRADE_SPLIT), value);
}

export const set_upgrade_level = setUpgradeLevel;
