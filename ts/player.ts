/**
 * Player-name table helpers and the layout-A nickname matcher (ADR 0032).
 *
 * StarCraft keeps twelve 36-byte player-name records starting at `0x57EEE8`
 * (nine dwords per player). The table sits below `EPD_BASE`, so `EPD()`
 * already returns a wrapped 32-bit index that the engine reads and writes like
 * any other Deaths-table target.
 *
 * `matchNames` is a build-time unroll, not a runtime loop: for every player it
 * emits one ordered `when` chain over the candidate names. Each arm compares
 * the player's name field dword by dword with `DeathsX` and calls
 * `onMatch(player, name)` for the first candidate that matches. Nicknames are
 * encoded by `encodeUtf8` (the same byte source `FixedString` uses), and the
 * final dword's mask always includes the NUL byte, so a prefix such as
 * `"Armo"` can never match a record containing `"Armoha"`.
 *
 * Layout C (a runtime byte reader over a NUL-separated name DB) is
 * deliberately not shipped: it needs `SetNextPtr`-style trigger rewiring,
 * which eudkit rejects. See ADR 0032 §5 for the deferral and the cost model.
 */

import { FixedArray, arrayDword, type FixedArrayImpl } from "./array.ts";
import { GAME_TICK_ADDR, USER_PLAYER_ADDR } from "./camera.ts";
import { CP_ADDR, readCurrentPlayerRegister, withEpd } from "./cp.ts";
import {
  Add,
  AddrReloc,
  AtLeast,
  AtMost,
  CenterView,
  Deaths,
  DeathsX,
  DisplayText,
  Exactly,
  Memory,
  MinimapPing,
  Never,
  PlayWAV,
  SetDeaths,
  SetMemory,
  SetMissionObjectives,
  SetTo,
  Subtract,
  TalkingPortrait,
  emitVarStore,
  type HirCond,
} from "./hir.ts";
import { encodeUtf8 } from "./string.ts";
import { when, whileLoop } from "./temporal.ts";
import { _getActiveModuleBuilder, type CondLike } from "./trigger.ts";
import {
  CURRENT_PLAYER,
  EPD,
  EPD_CURRENT_PLAYER,
  EudslError,
  MemFieldHandle,
  describeValue,
  defineMemView,
  readEpd,
  requirePlainInt,
  type FieldSpec,
} from "./view.ts";
import { Expr, Var, VarImpl, toVar, type ReadonlyVar } from "./var.ts";

export { GAME_TICK_ADDR, USER_PLAYER_ADDR } from "./camera.ts";

/** First byte of player 0's name record. */
export const PLAYER_NAME_TABLE = 0x57eee8;
/** Dwords per player record (36 bytes / 4). */
export const PLAYER_NAME_EPDS = 9;
/** The game's record size in bytes. */
export const PLAYER_NAME_BYTES = 36;
/** Longest name that still fits its trailing NUL in the record. */
export const PLAYER_NAME_MAX_BYTES = 35;

const PLAYER_MAX = 11;

function requirePlayer(player: unknown, what: string): number {
  const value = requirePlainInt(player, `${what}: player`);
  if (value < 0 || value > PLAYER_MAX) {
    throw new EudslError(
      "EUDSL-RANGE",
      `${what}: player must be in 0..${PLAYER_MAX}, got ${value}`,
    );
  }
  return value;
}

/**
 * EPD of `player`'s 36-byte name record.
 *
 * Returns the unsigned 32-bit value the engine reads as an EPD:
 * `(EPD(0x57EEE8) + 9 * player) & 0xFFFFFFFF`.
 */
export function playerNameEpd(player: number): number {
  const value = requirePlayer(player, "playerNameEpd()");
  return (EPD(PLAYER_NAME_TABLE) + PLAYER_NAME_EPDS * value) >>> 0;
}

export const player_name_epd = playerNameEpd;

/** Byte address of `player`'s name record: `0x57EEE8 + 36 * player`. */
export function playerNameAddr(player: number): number {
  const value = requirePlayer(player, "playerNameAddr()");
  return (PLAYER_NAME_TABLE + PLAYER_NAME_BYTES * value) >>> 0;
}

export const player_name_addr = playerNameAddr;

/** Little-endian dword `data[4*index:4*index+4]`, zero-padded. */
function word(data: readonly number[], index: number): number {
  let value = 0;
  for (let lane = 0; lane < 4; lane += 1) {
    const pos = 4 * index + lane;
    if (pos < data.length) {
      value |= (data[pos] & 0xff) << (8 * lane);
    }
  }
  return value >>> 0;
}

/** One trigger's conditions comparing `player`'s record to `data`. */
function condsFor(player: number, data: readonly number[]): HirCond[] {
  const base = playerNameEpd(player);
  const conds: HirCond[] = [];
  const whole = Math.floor(data.length / 4);
  for (let index = 0; index < whole; index += 1) {
    conds.push(
      DeathsX(base + index, Exactly, word(data, index), 0, 0xffffffff),
    );
  }
  const index = whole;
  const lane = data.length % 4;
  // The mask spans the bytes before the NUL plus the NUL itself. It is the
  // correctness property: without it, "Armo" would match "Armoha".
  const mask = (2 ** (8 * (lane + 1)) - 1) >>> 0;
  conds.push(
    DeathsX(
      base + index,
      Exactly,
      (word(data, index) & mask) >>> 0,
      0,
      mask,
    ),
  );
  return conds;
}

function validateNames(names: readonly string[]): readonly string[] {
  if (!Array.isArray(names)) {
    throw new EudslError(
      "EUDSL-TYPE",
      "matchNames(): names must be an array of strings",
    );
  }
  if (names.length === 0) {
    throw new EudslError(
      "EUDSL-BOUNDS",
      "matchNames(): names must contain at least one name",
    );
  }
  const seen = new Set<string>();
  for (const name of names) {
    if (typeof name !== "string") {
      throw new EudslError(
        "EUDSL-TYPE",
        `matchNames(): every name must be a string, got ${describeValue(name)}`,
      );
    }
    if (seen.has(name)) {
      throw new EudslError(
        "EUDSL-BOUNDS",
        `matchNames(): duplicate name ${JSON.stringify(name)}`,
      );
    }
    seen.add(name);
  }
  return names;
}

function validatePlayers(players: Iterable<number>): readonly number[] {
  if (players instanceof Set) {
    throw new EudslError(
      "EUDSL-TYPE",
      "matchNames(): players must be an ordered iterable; a Set is not allowed",
    );
  }
  if (
    players == null ||
    typeof (players as Iterable<number>)[Symbol.iterator] !== "function"
  ) {
    throw new EudslError(
      "EUDSL-TYPE",
      "matchNames(): players must be an iterable of player indices",
    );
  }
  const rawPlayers = Array.from(players);
  if (rawPlayers.length === 0) {
    throw new EudslError(
      "EUDSL-BOUNDS",
      "matchNames(): players must contain at least one player",
    );
  }
  const out: number[] = [];
  const seen = new Set<number>();
  for (const player of rawPlayers) {
    if (typeof player !== "number" || !Number.isInteger(player)) {
      throw new EudslError(
        "EUDSL-TYPE",
        `matchNames(): every player must be a plain integer, got ${describeValue(player)}`,
      );
    }
    if (player < 0 || player > PLAYER_MAX) {
      throw new EudslError(
        "EUDSL-RANGE",
        `matchNames(): player must be in 0..${PLAYER_MAX}, got ${player}`,
      );
    }
    if (seen.has(player)) {
      throw new EudslError(
        "EUDSL-BOUNDS",
        `matchNames(): duplicate player ${player}`,
      );
    }
    seen.add(player);
    out.push(player);
  }
  return out;
}

/**
 * Emit an unrolled nickname matcher; call `onMatch(player, name)` on a hit.
 *
 * `players` defaults to a fresh `[0..7]` array per call (the eight playable
 * slots). Matching is exact and case-sensitive; the first matching name in
 * caller order wins for each player. Every `(player, name)` arm is exactly one
 * trigger with at most nine conditions.
 */
export function matchNames(
  names: readonly string[],
  onMatch: (player: number, name: string) => void,
  players: Iterable<number> = [0, 1, 2, 3, 4, 5, 6, 7],
): void {
  if (_getActiveModuleBuilder() === null) {
    throw new EudslError(
      "EUDSL-SCOPE",
      "matchNames() must be called inside buildModule()",
    );
  }
  const validatedNames = validateNames(names);
  const encoded = validatedNames.map((name) => encodeUtf8(name, "nickname"));
  validatedNames.forEach((name, index) => {
    if (encoded[index].length > PLAYER_NAME_MAX_BYTES) {
      throw new EudslError(
        "EUDSL-BOUNDS",
        `matchNames(): name ${JSON.stringify(name)} encodes to ${
          encoded[index].length
        } bytes; the record holds at most ${PLAYER_NAME_MAX_BYTES} plus its NUL`,
      );
    }
  });
  const validatedPlayers = validatePlayers(players);
  if (typeof onMatch !== "function") {
    throw new EudslError(
      "EUDSL-TYPE",
      "matchNames(): onMatch must be callable",
    );
  }

  // Every validation above has run before the first emission, so a rejected
  // call leaves no partial chain behind.
  for (const player of validatedPlayers) {
    const arm = (index: number): void => {
      if (index === validatedNames.length) {
        return;
      }
      const name = validatedNames[index];
      const conds = condsFor(player, encoded[index]);
      if (index + 1 === validatedNames.length) {
        when(conds, () => onMatch(player, name));
      } else {
        when(
          conds,
          () => onMatch(player, name),
          () => arm(index + 1),
        );
      }
    };
    arm(0);
  }
}

export const match_names = matchNames;

// ---------------------------------------------------------------------------
// Player state, existence, and all-player broadcasts
// ---------------------------------------------------------------------------

/** First byte of the eight-slot player existence table (`pexist.py:38`). */
export const PLAYER_TABLE_ADDR = 0x51a280;
/** Bytes per existence-table record (two dwords plus the "has left" dword). */
export const PLAYER_SLOT_BYTES = 12;
/** Live melee slots addressed by `playerExists`/`forEachPlayer`. */
export const PLAYER_SLOT_COUNT = 8;
/** Highest id accepted by `Player` (mirrors `TrgPlayer.range`). */
export const PLAYER_MAX_ID = 11;
/** `dwTurbo`; `turbo()` writes zero here in the tail position. */
export const TURBO_ADDR = 0x6509a0;

/** Disable the game's speed governor (`eudTurbo.py:9`). */
export function turbo(): void {
  SetMemory(TURBO_ADDR, SetTo, 0);
}

/** Read `dwUserPlayerId` (the local human player index). */
export function userPlayer(): VarImpl {
  return readEpd(EPD(USER_PLAYER_ADDR), 32, 0);
}

export const user_player = userPlayer;

/** Read the game tick dword. */
export function gameTick(): VarImpl {
  return readEpd(EPD(GAME_TICK_ADDR), 32, 0);
}

export const game_tick = gameTick;

function isRuntimeOperand(value: unknown): value is VarImpl | Expr {
  return value instanceof VarImpl || value instanceof Expr;
}

/** Return a condition comparing `dwUserPlayerId` with `player`. */
export function isUser(player: number | VarImpl | ReadonlyVar): HirCond {
  const current = userPlayer();
  if (typeof player === "number") {
    return current.eq(requirePlainInt(player, "isUser() player"));
  }
  if (isRuntimeOperand(player)) {
    // A runtime operand materializes at its use site; the deferred condition
    // is still consumed by `when`/`Trigger` exactly like a `HirCond`.
    return current.eq(toVar(player)) as unknown as HirCond;
  }
  throw new EudslError(
    "EUDSL-TYPE",
    `isUser(): player must be a plain integer or Var; got ${describeValue(player)}`,
  );
}

export const is_user = isUser;

function requireExistsPlayer(player: unknown, what: string): number {
  if (typeof player !== "number" || !Number.isInteger(player)) {
    throw new EudslError(
      "EUDSL-TYPE",
      `${what}: player must be a plain integer, got ${describeValue(player)}`,
    );
  }
  if (player < 0 || player >= PLAYER_SLOT_COUNT) {
    throw new EudslError(
      "EUDSL-RANGE",
      `${what}: player must be in 0..${PLAYER_SLOT_COUNT - 1}, got ${player}`,
    );
  }
  return player;
}

/** The constant `pexist.py` predicate as one 1-condition gate. */
function existsCondition(player: number): HirCond {
  const base = PLAYER_TABLE_ADDR + PLAYER_SLOT_BYTES * player;
  return Memory(base + 8, Exactly, ~(base + 4) >>> 0);
}

/** Return a fresh `Var` that is 1 while `player` is still in the game. */
export function playerExists(
  player: number | VarImpl | ReadonlyVar | Expr,
): VarImpl {
  if (typeof player === "number") {
    const value = requireExistsPlayer(player, "playerExists()");
    const out = Var(0);
    out.set(1);
    when(existsCondition(value), () => out.set(0));
    return out;
  }

  if (!isRuntimeOperand(player)) {
    throw new EudslError(
      "EUDSL-TYPE",
      `playerExists(): player must be a plain integer or Var; got ${describeValue(player)}`,
    );
  }

  const source = toVar(player);
  // np = 3 * player (three table dwords), built with value copies.
  const np = Var(0);
  np.set(source);
  np.iadd(source);
  np.iadd(source);
  // a = EPD(table) + 2 + np: the "has left" dword of the record.
  const target = Var(0);
  target.set((EPD(PLAYER_TABLE_ADDR) + 2) >>> 0);
  target.iadd(np);
  // amt = ~(table + 4) - 4 * np, the expected value in 32-bit byte space.
  const expected = Var(0);
  expected.set(~(PLAYER_TABLE_ADDR + 4) >>> 0);
  expected.isub(np);
  expected.isub(np);
  expected.isub(np);
  expected.isub(np);
  const value = withEpd(target, (_cp) => readEpd(CURRENT_PLAYER, 32, 0));
  const out = Var(0);
  out.set(0);
  when(value.eq(expected), () => out.set(1));
  return out;
}

export const player_exists = playerExists;

function validatePlayerSubset(players: unknown, what: string): readonly number[] {
  if (players instanceof Set) {
    throw new EudslError(
      "EUDSL-TYPE",
      `${what}: players must be an ordered iterable; a Set is not allowed`,
    );
  }
  if (!Array.isArray(players)) {
    throw new EudslError(
      "EUDSL-TYPE",
      `${what}: players must be an array of player indices`,
    );
  }
  if (players.length === 0) {
    throw new EudslError(
      "EUDSL-BOUNDS",
      `${what}: players must contain at least one player`,
    );
  }
  const out: number[] = [];
  const seen = new Set<number>();
  for (const player of players as readonly unknown[]) {
    if (typeof player !== "number" || !Number.isInteger(player)) {
      throw new EudslError(
        "EUDSL-TYPE",
        `${what}: every player must be a plain integer, got ${describeValue(player)}`,
      );
    }
    if (player < 0 || player > PLAYER_MAX_ID) {
      throw new EudslError(
        "EUDSL-RANGE",
        `${what}: player must be in 0..${PLAYER_MAX_ID}, got ${player}`,
      );
    }
    if (seen.has(player)) {
      throw new EudslError(
        "EUDSL-BOUNDS",
        `${what}: duplicate player ${player}`,
      );
    }
    seen.add(player);
    out.push(player);
  }
  return out;
}

/**
 * Run `body(playerVar)` once per selected player with CP bound to it.
 *
 * With `players` omitted the selection is the runtime loop `0..7`; when a
 * subset is given the loop is unrolled at build time. The ambient
 * `CurrentPlayer` register is saved once and restored unconditionally.
 */
export function forEachPlayer(
  body: (player: VarImpl) => unknown,
  opts: { activeOnly?: boolean; players?: readonly number[] } = {},
): void {
  if (typeof body !== "function") {
    throw new EudslError(
      "EUDSL-TYPE",
      "forEachPlayer(): body must be callable",
    );
  }

  const activeOnly = opts.activeOnly ?? true;
  const selected =
    opts.players === undefined
      ? null
      : validatePlayerSubset(opts.players, "forEachPlayer()");

  const saved = readCurrentPlayerRegister();

  if (selected === null) {
    const current = Var(0);

    whileLoop(current.le(PLAYER_SLOT_COUNT - 1), () => {
      const active = (): void => {
        emitVarStore(current.id, AddrReloc(CP_ADDR), SetTo);
        body(current);
      };
      if (activeOnly) {
        when(playerExists(current).eq(1), active);
      } else {
        active();
      }
      current.iadd(1);
    });
  } else {
    for (const index of selected) {
      const playerVar = Var(index);
      const run = (): void => {
        SetDeaths(EPD_CURRENT_PLAYER, SetTo, index, 0);
        body(playerVar);
      };
      if (activeOnly) {
        when([existsCondition(index)], run);
      } else {
        run();
      }
    }
  }

  emitVarStore(saved.id, AddrReloc(CP_ADDR));
}

export const for_each_player = forEachPlayer;

const PLAYER_FIELD_VIEW = defineMemView("PlayerField", 4, {
  value: { offset: 0, size: 4 },
});
const PLAYER_FIELD_SPEC: FieldSpec = { offset: 0, size: 4 };

/** The `scdata/player.py` dword arrays, one element per player. */
const PLAYER_FIELD_ADDRS: readonly (readonly [string, number])[] = [
  ["minerals", 0x57f0f0],
  ["gas", 0x57f120],
  ["cumulative_gas", 0x57f150],
  ["cumulative_minerals", 0x57f180],
  ["zerg_control_available", 0x582144],
  ["zerg_control_used", 0x582174],
  ["zerg_control_max", 0x5821a4],
  ["terran_supply_available", 0x5821d4],
  ["terran_supply_used", 0x582204],
  ["terran_supply_max", 0x582234],
  ["protoss_psi_available", 0x582264],
  ["protoss_psi_used", 0x582294],
  ["protoss_psi_max", 0x5822c4],
  ["unit_score", 0x581e44],
  ["building_score", 0x582024],
  ["kill_score", 0x581f04],
  ["razing_score", 0x582054],
  ["custom_score", 0x5822f4],
] as const;

/** One dword of one player's `scdata/player.py` state array. */
export class PlayerField {
  readonly player: number;
  readonly baseAddr: number;
  readonly addr: number;
  readonly epd: number;

  constructor(player: number, addr: number) {
    this.player = requirePlayer(player, "PlayerField()");
    this.baseAddr = addr >>> 0;
    this.addr = (addr + 4 * this.player) >>> 0;
    this.epd = (EPD(addr) + this.player) >>> 0;
  }

  private handle(): MemFieldHandle {
    return new MemFieldHandle(
      PLAYER_FIELD_VIEW,
      PLAYER_FIELD_SPEC,
      this.epd,
      "value",
    );
  }

  read(): VarImpl {
    return readEpd(this.epd, 32, 0);
  }

  set(value: number): void {
    SetDeaths(
      this.epd,
      SetTo,
      requirePlainInt(value, "PlayerField.set value"),
      0,
    );
  }

  iadd(value: number): void {
    SetDeaths(
      this.epd,
      Add,
      requirePlainInt(value, "PlayerField.iadd value"),
      0,
    );
  }

  isub(value: number): void {
    SetDeaths(
      this.epd,
      Subtract,
      requirePlainInt(value, "PlayerField.isub value"),
      0,
    );
  }

  private compare(op: string, rhs: number): HirCond | CondLike {
    const value = requirePlainInt(rhs, `PlayerField.${op} value`) >>> 0;
    if (op === "eq") return Deaths(this.epd, Exactly, value, 0);
    if (op === "ge") return Deaths(this.epd, AtLeast, value, 0);
    if (op === "le") return Deaths(this.epd, AtMost, value, 0);
    if (op === "gt") {
      if (value === 0xffffffff) return Never();
      return Deaths(this.epd, AtLeast, (value + 1) >>> 0, 0);
    }
    if (op === "lt") {
      if (value === 0) return Never();
      return Deaths(this.epd, AtMost, (value - 1) >>> 0, 0);
    }
    if (op === "ne") {
      return this.handle().ne(value);
    }
    throw new Error(`unknown PlayerField comparison ${op}`);
  }

  eq(rhs: number): HirCond {
    return this.compare("eq", rhs) as HirCond;
  }

  ne(rhs: number): CondLike {
    return this.compare("ne", rhs);
  }

  ge(rhs: number): HirCond {
    return this.compare("ge", rhs) as HirCond;
  }

  le(rhs: number): HirCond {
    return this.compare("le", rhs) as HirCond;
  }

  gt(rhs: number): HirCond {
    return this.compare("gt", rhs) as HirCond;
  }

  lt(rhs: number): HirCond {
    return this.compare("lt", rhs) as HirCond;
  }
}

/** Player state arrays and existence for one player id (`0..11`). */
export class Player {
  readonly index: number;

  readonly minerals: PlayerField;
  readonly gas: PlayerField;
  readonly cumulative_gas: PlayerField;
  readonly cumulative_minerals: PlayerField;
  readonly zerg_control_available: PlayerField;
  readonly zerg_control_used: PlayerField;
  readonly zerg_control_max: PlayerField;
  readonly terran_supply_available: PlayerField;
  readonly terran_supply_used: PlayerField;
  readonly terran_supply_max: PlayerField;
  readonly protoss_psi_available: PlayerField;
  readonly protoss_psi_used: PlayerField;
  readonly protoss_psi_max: PlayerField;
  readonly unit_score: PlayerField;
  readonly building_score: PlayerField;
  readonly kill_score: PlayerField;
  readonly razing_score: PlayerField;
  readonly custom_score: PlayerField;

  readonly cumulativeMinerals: PlayerField;
  readonly cumulativeGas: PlayerField;
  readonly zergControlAvailable: PlayerField;
  readonly zergControlUsed: PlayerField;
  readonly zergControlMax: PlayerField;
  readonly terranSupplyAvailable: PlayerField;
  readonly terranSupplyUsed: PlayerField;
  readonly terranSupplyMax: PlayerField;
  readonly protossPsiAvailable: PlayerField;
  readonly protossPsiUsed: PlayerField;
  readonly protossPsiMax: PlayerField;
  readonly unitScore: PlayerField;
  readonly buildingScore: PlayerField;
  readonly killScore: PlayerField;
  readonly razingScore: PlayerField;
  readonly customScore: PlayerField;
  readonly mineral: PlayerField;
  readonly cumulativeMineral: PlayerField;

  constructor(player: number) {
    this.index = requirePlayer(player, "Player()");
    const fields = new Map<string, PlayerField>();
    for (const [name, addr] of PLAYER_FIELD_ADDRS) {
      fields.set(name, new PlayerField(this.index, addr));
    }
    const get = (name: string): PlayerField => fields.get(name)!;

    this.minerals = get("minerals");
    this.gas = get("gas");
    this.cumulative_gas = get("cumulative_gas");
    this.cumulative_minerals = get("cumulative_minerals");
    this.zerg_control_available = get("zerg_control_available");
    this.zerg_control_used = get("zerg_control_used");
    this.zerg_control_max = get("zerg_control_max");
    this.terran_supply_available = get("terran_supply_available");
    this.terran_supply_used = get("terran_supply_used");
    this.terran_supply_max = get("terran_supply_max");
    this.protoss_psi_available = get("protoss_psi_available");
    this.protoss_psi_used = get("protoss_psi_used");
    this.protoss_psi_max = get("protoss_psi_max");
    this.unit_score = get("unit_score");
    this.building_score = get("building_score");
    this.kill_score = get("kill_score");
    this.razing_score = get("razing_score");
    this.custom_score = get("custom_score");

    this.cumulativeMinerals = this.cumulative_minerals;
    this.cumulativeGas = this.cumulative_gas;
    this.zergControlAvailable = this.zerg_control_available;
    this.zergControlUsed = this.zerg_control_used;
    this.zergControlMax = this.zerg_control_max;
    this.terranSupplyAvailable = this.terran_supply_available;
    this.terranSupplyUsed = this.terran_supply_used;
    this.terranSupplyMax = this.terran_supply_max;
    this.protossPsiAvailable = this.protoss_psi_available;
    this.protossPsiUsed = this.protoss_psi_used;
    this.protossPsiMax = this.protoss_psi_max;
    this.unitScore = this.unit_score;
    this.buildingScore = this.building_score;
    this.killScore = this.kill_score;
    this.razingScore = this.razing_score;
    this.customScore = this.custom_score;
    this.mineral = this.minerals;
    this.cumulativeMineral = this.cumulative_minerals;
  }

  exists(): VarImpl {
    return playerExists(this.index);
  }
}

/** An eight-slot per-player variable backed by `FixedArray(dword, 8)`. */
export class PVariable {
  private readonly array: FixedArrayImpl<VarImpl>;

  constructor(init = 0) {
    const value = requirePlainInt(init, "PVariable() init");
    this.array = FixedArray(arrayDword, PLAYER_SLOT_COUNT);
    if (value !== 0) {
      this.array.fill(value >>> 0);
    }
  }

  get storage(): FixedArrayImpl<VarImpl> {
    return this.array;
  }

  private index(player: number | VarImpl | ReadonlyVar | Expr): number | VarImpl {
    if (typeof player === "number") {
      const value = requirePlainInt(player, "PVariable index");
      if (value < 0 || value >= PLAYER_SLOT_COUNT) {
        throw new EudslError(
          "EUDSL-RANGE",
          `PVariable index must be in 0..${PLAYER_SLOT_COUNT - 1}, got ${value}`,
        );
      }
      return value;
    }
    if (isRuntimeOperand(player)) {
      return toVar(player);
    }
    throw new EudslError(
      "EUDSL-TYPE",
      `PVariable index must be a plain integer or Var; got ${describeValue(player)}`,
    );
  }

  get(player: number | VarImpl | ReadonlyVar | Expr): VarImpl {
    const index = this.index(player);
    return typeof index === "number"
      ? this.array.get(index)
      : this.array.get(index);
  }

  at(player: number | VarImpl | ReadonlyVar | Expr): VarImpl {
    return this.get(player);
  }

  set(player: number | VarImpl | ReadonlyVar | Expr, value: number | VarImpl): void {
    const index = this.index(player);
    if (typeof value === "number") {
      const number = requirePlainInt(value, "PVariable.set value");
      if (typeof index === "number") {
        this.array.set(index, number);
      } else {
        this.array.set(index, number);
      }
      return;
    }
    if (value instanceof VarImpl) {
      if (typeof index === "number") {
        this.array.set(index, value);
      } else {
        this.array.set(index, value);
      }
      return;
    }
    throw new EudslError(
      "EUDSL-TYPE",
      `PVariable.set value must be a plain integer or Var; got ${describeValue(value)}`,
    );
  }
}

function requireText(text: unknown, what: string): string {
  if (typeof text !== "string") {
    throw new EudslError(
      "EUDSL-TYPE",
      `${what}: text must be a string, got ${describeValue(text)}`,
    );
  }
  return text;
}

/** Show `text` to every live player. */
export function displayTextAll(text: string): void {
  const message = requireText(text, "displayTextAll()");
  forEachPlayer((_p) => {
    DisplayText(message, 4);
  });
}

export const display_text_all = displayTextAll;

/** Play `wav` for every live player. */
export function playWavAll(wav: string): void {
  const sound = requireText(wav, "playWavAll()");
  forEachPlayer((_p) => {
    PlayWAV(sound);
  });
}

export const play_wav_all = playWavAll;

/** Ping a raw location id (`0..255`) on every live player's minimap. */
export function minimapPingAll(location: number): void {
  const loc = requirePlainInt(location, "minimapPingAll() location");
  if (loc < 0 || loc > 255) {
    throw new EudslError(
      "EUDSL-RANGE",
      `minimapPingAll(): location must be in 0..255, got ${loc}`,
    );
  }
  forEachPlayer((_p) => {
    MinimapPing(loc);
  });
}

export const minimap_ping_all = minimapPingAll;

/** Center every live player's view on a raw location id (`0..255`). */
export function centerViewAll(location: number): void {
  const loc = requirePlainInt(location, "centerViewAll() location");
  if (loc < 0 || loc > 255) {
    throw new EudslError(
      "EUDSL-RANGE",
      `centerViewAll(): location must be in 0..255, got ${loc}`,
    );
  }
  forEachPlayer((_p) => {
    CenterView(loc);
  });
}

export const center_view_all = centerViewAll;

/** Replace the mission objectives text for every live player. */
export function setMissionObjectivesAll(text: string): void {
  const message = requireText(text, "setMissionObjectivesAll()");
  forEachPlayer((_p) => {
    SetMissionObjectives(message);
  });
}

export const set_mission_objectives_all = setMissionObjectivesAll;

/** Show `unit`'s portrait for `time` ms to every live player. */
export function talkingPortraitAll(unit: number, time: number): void {
  const portrait = requirePlainInt(unit, "talkingPortraitAll() unit");
  if (portrait < 0 || portrait > 227) {
    throw new EudslError(
      "EUDSL-RANGE",
      `talkingPortraitAll(): unit must be in 0..227, got ${portrait}`,
    );
  }
  const delay = requirePlainInt(time, "talkingPortraitAll() time");
  if (delay < 0 || delay > 0xffffffff) {
    throw new EudslError(
      "EUDSL-RANGE",
      `talkingPortraitAll(): time must be in 0..4294967295, got ${delay}`,
    );
  }
  forEachPlayer((_p) => {
    TalkingPortrait(portrait, delay);
  });
}

export const talking_portrait_all = talkingPortraitAll;
