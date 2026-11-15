/**
 * Periodic background-music player (port of euddraft `plugins/bgmplayer.py`).
 *
 * `playBgm(path, lengthSeconds)` embeds an audio file into the output MPQ as
 * `bgm` and arms a preserved trigger that replays it every `lengthSeconds`
 * seconds for the local player. This is a regular core helper: there is no
 * plugin system, no settings manifest, and no lifecycle hook — one call inside
 * `buildModule` arms the player for the compiled module.
 *
 * Timing
 * ------
 * The game clock is the dword at `0x51CE8C`, which counts *down*; elapsed
 * milliseconds are `0xFFFFFFFF - dword` (eudplib `f_time`). eudplib's firing
 * test is `target <= now` with `now = 0xFFFFFFFF - raw`. Rearranged, that is
 * `raw <= threshold` with `threshold = 0xFFFFFFFF - target`. The helper keeps
 * the threshold in a variable and, on every play, patches it into the probe's
 * `Memory(0x51CE8C, AtMost, ...)` condition. The idle loop therefore never
 * reads the clock and never does arithmetic.
 *
 * `f_starttimer(duration)` sets `target = now + duration - 42`; in threshold
 * units that is `raw - duration + 42`. The 42 ms is eudplib's start-latency
 * compensation and is kept on every schedule.
 *
 * `onPluginStart` calls `f_starttimer(0)` so the first play is immediate. A
 * build-time initial value cannot read the runtime clock, so the probe starts
 * with an all-ones threshold (already due): the first trigger loop always
 * plays. That is the documented equivalent of the `-42` start path — the
 * millisecond compensation only exists to make a zero-length start already due.
 *
 * Per play the helper switches CurrentPlayer to the local player id
 * (`0x512684`) for `PlayWAV("bgm")` and restores the previous value, exactly
 * like `f_getcurpl` / `f_getuserplayerid` / `SetMemory(0x6509B0)` in the
 * source. The local player id is read once at map start (a `once` block, the
 * counterpart of eudplib's `_f_inituserplayerid`), so only the saved
 * CurrentPlayer and the clock are read per play.
 *
 * Executed cost
 * -------------
 * Armed and idle: 2T (the probe and the `when` gate, one condition each), no
 * actions, no clock read. A play loop adds the body: two 32-test dword reads
 * (the saved CurrentPlayer and the raw clock), the `PlayWAV`, the threshold
 * arithmetic and the probe patch.
 *
 * The helper returns the threshold variable so tests and advanced callers can
 * inspect or override the schedule.
 */

import { USER_PLAYER_ADDR } from "./camera.ts";
import { CP_ADDR, readCurrentPlayerRegister } from "./cp.ts";
import { AddrReloc, AtMost, TriggerFieldReloc, emitVarStore, embedFile } from "./hir.ts";
import { Memory, PlayWAV } from "./stock.generated.ts";
import { once, when } from "./temporal.ts";
import { Trigger, _registerTrigger } from "./trigger.ts";
import { Var, type VarImpl } from "./var.ts";
import { EPD, EudslError, readEpd } from "./view.ts";

/** Absolute address of the countdown clock read by euddraft's `f_time`. */
export const BGM_CLOCK_ADDR = 0x51ce8c;
/** Archived MPQ name the embedded track is played from. */
export const BGM_ARCHIVE_NAME = "bgm";
/** eudplib's start-latency compensation, in milliseconds. */
export const START_LATENCY_MS = 42;

/** Truncate `lengthSeconds` to whole milliseconds (eudplib `int()`). */
function periodMs(lengthSeconds: unknown): number {
  if (typeof lengthSeconds !== "number") {
    throw new EudslError(
      "EUDSL-TYPE",
      `playBgm: lengthSeconds must be a number, got ${String(lengthSeconds)}`,
    );
  }
  if (!Number.isFinite(lengthSeconds)) {
    throw new EudslError(
      "EUDSL-TYPE",
      `playBgm: lengthSeconds must be finite, got ${String(lengthSeconds)}`,
    );
  }
  const period = Math.trunc(lengthSeconds * 1000);
  if (period <= 0) {
    throw new EudslError(
      "EUDSL-RANGE",
      `playBgm: lengthSeconds must be positive, got ${String(lengthSeconds)}`,
    );
  }
  return period;
}

/** Materialise the dword at `USER_PLAYER_ADDR` through the shared reader. */
function readUserPlayer(): VarImpl {
  return readEpd(EPD(USER_PLAYER_ADDR), 32, 0);
}

/** Materialise the raw countdown dword through the shared reader. */
function readClock(): VarImpl {
  return readEpd(EPD(BGM_CLOCK_ADDR), 32, 0);
}

/**
 * Play `path` from the MPQ as `bgm`, repeating every `lengthSeconds`.
 *
 * `path` is resolved by the build against the compiling source's directory.
 * Returns the next-fire threshold: the play happens on the trigger loop where
 * the raw clock dword is at most this value.
 */
export function playBgm(path: string, lengthSeconds: number): VarImpl {
  const period = periodMs(lengthSeconds);
  embedFile(BGM_ARCHIVE_NAME, path);

  // Already due, so the first evaluated loop plays (see module docstring).
  const threshold = Var(0xffffffff);
  const hit = Var(0);
  // eudplib reads the local player id once at map start; do the same so the
  // 32-test read stays off the per-play path.
  const localPlayer = Var(0);
  once(() => {
    localPlayer.set(readUserPlayer());
  });

  const probe = new Trigger({
    conds: [Memory(BGM_CLOCK_ADDR, AtMost, 0xffffffff)],
    acts: [hit.act_set(1)],
    register: false,
  });
  _registerTrigger(probe);

  when(hit.eq(1), () => {
    const oldCp = readCurrentPlayerRegister();
    // `emitVarStore` copies the variable's value; `SetMemory(..., Var)` would
    // write the variable's address (value-or-ptr mode 4).
    emitVarStore(localPlayer.id, AddrReloc(CP_ADDR));
    PlayWAV(BGM_ARCHIVE_NAME);
    emitVarStore(oldCp.id, AddrReloc(CP_ADDR));

    const raw = readClock();
    threshold.set(raw);
    const adjustment = period - START_LATENCY_MS;
    if (adjustment !== 0) {
      threshold.isub(adjustment);
    }
    // Patch the next loop's probe condition from the fresh threshold. This
    // runs only on a play, so idle loops stay at one compare per trigger.
    emitVarStore(
      threshold.id,
      TriggerFieldReloc(probe.id, probe.cond(0).amount.byteOffset),
    );
    hit.set(0);
  });
  return threshold;
}

export const play_bgm = playBgm;
