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

import { DeathsX, Exactly, type HirCond } from "./hir.ts";
import { encodeUtf8 } from "./string.ts";
import { when } from "./temporal.ts";
import { _getActiveModuleBuilder } from "./trigger.ts";
import {
  EPD,
  EudslError,
  describeValue,
  requirePlainInt,
} from "./view.ts";

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
  let rawPlayers: number[];
  try {
    rawPlayers = Array.from(players);
  } catch {
    throw new EudslError(
      "EUDSL-TYPE",
      "matchNames(): players must be an iterable of player indices",
    );
  }
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
