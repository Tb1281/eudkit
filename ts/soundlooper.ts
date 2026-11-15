/**
 * Loop-sound player: a port of euddraft's `lib/soundlooper.py`.
 *
 * euddraft's soundlooper splits a song into `000`-`999` numbered `.ogg`
 * segments, embeds them in the MPQ, and plays them as an intro / bar / bridge
 * loop. This module ports the build-time segment scanner, the `_TinyTag` Ogg
 * length reader (as `ogg.ts`, shared with the Python frontend) and the
 * `SoundLooper` runtime as a regular core helper. There is no plugin system,
 * no settings manifest and no lifecycle hook.
 *
 * Relationship to the BGM helper
 * ------------------------------
 * `playBgm` is the *simpler* background-music player: one track, repeated
 * every fixed number of seconds. Soundlooper is the *loop* system: a segmented
 * song with per-segment lengths, an intro that plays once, a bridge on the
 * last segment, and a configurable `goto` sound that the loop returns to. Both
 * schedule on the same countdown clock at `0x51CE8C`; the BGM helper uses one
 * fixed period, soundlooper recomputes the next firing point on every segment.
 *
 * Build-time registration
 * -----------------------
 * `SoundLoops` owns the per-module loop registry and the shared progress array
 * (the source's `_Loop.loop_dict` + class-level `SoundLooper._bars`):
 *
 * - `addLoop` scans `<path>/<title>/<title>NNN.ogg` (the source's `AddLoop`),
 *   reads each segment's duration, embeds the segment under `<5-char-id><NNN>`
 *   and registers the loop. The scan tries the shortest zero-padding first
 *   (`theme0.ogg` before `theme000.ogg`), exactly like the source.
 * - `manualAddLoop` registers a loop whose five-byte segments are already in
 *   the MPQ (the source's `ManualAddLoop`).
 *
 * The progress array must exist before the runtime can use it, so registering
 * a loop after the first player call is an error — the source's
 * `ep_assert(SoundLooper._bars is None, ...)`.
 *
 * Runtime playback
 * ----------------
 * `SoundLooper` keeps the source's state: `currentLoop`, `previousLoop`,
 * `currentBar` and the packed three-digit bar register. `player()` emits the
 * per-loop body once; it fires when the raw clock dword at `0x51CE8C` has
 * fallen to the segment's next-due threshold, and then
 *
 * 1. advances `currentBar` and the packed digit register (with the source's
 *    decimal carries: ones -> tens -> hundreds),
 * 2. mirrors the bar into the loop's progress-array slot,
 * 3. plays `<id><bar-1:03d>` for the current loop (constant names per case),
 * 4. recomputes the next threshold from the raw clock, the loop's
 *    `ceil(-bar * 1000)` base and the speed-error correction, adding the
 *    intro correction on bar 1 and the bridge correction on the last bar,
 * 5. returns to `goto` after the last bar (the source's "if last bar was
 *    played, go to start").
 *
 * `play` / `pause` / `resume` / `toggle` / `setbar` / `sendbar` /
 * `sendcurrentbar` / `setloopbar` / `cond` / `loopis` keep the source's names
 * in camelCase. `cond` and `loopis` return conditions for `when`; the rest
 * emit actions at the call site, so the caller decides when they run.
 *
 * Deviations from the source
 * --------------------------
 * - **Deterministic archive ids.** The source's `_id_generator` samples five
 *   random characters, so two builds of the same map differ. The port derives
 *   the five-character prefix from a SHA-256 digest of the title and the
 *   registration ordinal (and rejects duplicate titles), so builds are
 *   reproducible. `manualAddLoop` takes the title as the prefix, as the
 *   source does.
 * - **Identifiers are five ASCII characters.** The source only checks that a
 *   manual title is five *cp949 bytes*; the TS frontend has no cp949 codec, so
 *   the shared contract is five single-byte characters (`EUDSL-RANGE`
 *   otherwise). `addLoop` titles are only directory names and are not
 *   restricted.
 * - **No string-buffer playback path.** In this source version the
 *   `StringBuffer` write and per-pass digit increment survive from an older
 *   implementation, but the `PlayWAV(_sb.StringIndex)` line is commented out
 *   and playback goes through `PlaySoundWorkaround`'s constant names per bar.
 *   The port keeps the packed digit register (`barDigits`) with the exact
 *   packing and carries, and plays constant names per case; it does not
 *   allocate the unused eight-byte string buffer.
 * - **A `when` chain replaces `EUDSwitch`.** eudplib generates a jump table for
 *   `EUDMethod` dispatches; the port emits one `when` per loop and per bar.
 *   For a loop with `N` segments the play dispatch costs `N + 1` conditions
 *   per loop, the same order as the source's case table, and is only evaluated
 *   on a due pass.
 * - **`play(null)` with `bar` omitted reads the current loop's slot.** The
 *   source raises `UnboundLocalError` here because `index` is only bound in
 *   the `else` branch; the port reads `bars[currentLoop]` (guarded, so a
 *   paused player is a no-op).
 * - **Unknown loop titles are rejected.** The source returns the title
 *   unchanged from `_T2i` and its switch silently matches nothing; the port
 *   raises `EUDSL-RANGE`.
 * - **`setbar`/`play` reject out-of-range constant bars.** A packed
 *   three-digit register holds 0-999; a constant above that is `EUDSL-RANGE`
 *   (the source would pack it and print nonsense).
 * - **CurrentPlayer is saved and restored.** The source leaves `0x6509B0` at
 *   the local player id after each pass; the port reads the previous value,
 *   switches for `PlayWAV` and restores it (the BGM helper's shape, and
 *   ADR 0004's per-loop reset). The local human id is read once in a `once`
 *   block, the counterpart of the source's `EUDOnStart`.
 * - **Segments are embedded with `embedFile`, not `Db`.** `PlayWAV` resolves
 *   an archived *name* from the MPQ, so the merged #99 `Db` blob (which lives
 *   in the trigger payload, not the archive) cannot serve this helper; the
 *   file surface is ADR 0021's general MPQ embed.
 *
 * Cost
 * ----
 * The idle loop pays one probe trigger (two conditions) plus the `when` gate
 * and never reads the clock. A due pass pays the 32-test clock read, the bar
 * and digit arithmetic, the per-bar play dispatch, the progress-array write
 * (dynamic index) and the threshold patch. The per-loop constants are written
 * by `play`/`resume`, not per pass. Segment bytes dominate map size.
 */

import { createHash } from "node:crypto";
import fs from "node:fs";

import { FixedArray, type FixedArrayImpl } from "./array.ts";
import { USER_PLAYER_ADDR } from "./camera.ts";
import { CP_ADDR, readCurrentPlayerRegister } from "./cp.ts";
import {
  AddrReloc,
  AtLeast,
  AtMost,
  Exactly,
  TriggerFieldReloc,
  embedFile,
  emitVarStore,
} from "./hir.ts";
import { oggDuration, roundDuration } from "./ogg.ts";
import { DeathsX, Memory, PlayWAV } from "./stock.generated.ts";
import { once, when } from "./temporal.ts";
import { Trigger, _registerTrigger, type CondLike } from "./trigger.ts";
import { Var, VarImpl } from "./var.ts";
import {
  EPD,
  EudslError,
  type FieldSpec,
  MemFieldHandle,
  defineMemView,
} from "./view.ts";

/** The countdown clock eudplib's `f_time` reads (`0xFFFFFFFF - raw`). */
export const SOUND_CLOCK_ADDR = 0x51ce8c;
/** `dwGameSpeed`-adjacent dword: 42 selects the speed-table path. */
export const GAME_SPEED_ADDR = 0x5124f0;
/** The two speed multipliers the source's `_calculate_error` compares. */
export const SPEED_84_ADDR = 0x51ce84;
export const SPEED_88_ADDR = 0x51ce88;

/**
 * The source's `speed_map` in insertion order:
 * `(0x51CE84, 0x51CE88)` value pair -> the `x` the correction formula uses.
 */
export const SPEED_MAP: ReadonlyArray<readonly [readonly [number, number], number]> = [
  [[2047, 2047], 29],
  [[4095, 4095], 18],
  [[65535, 65535], 1],
  [[1157, 1157], 36],
  [[1437, 1437], 29],
  [[1984, 1984], 29],
  [[3472, 3472], 18],
  [[41667, 41667], 1],
  [[1736, 1736], 36],
  [[2155, 2155], 29],
  [[2976, 2976], 21],
  [[5208, 5208], 18],
  [[62500, 62500], 1],
  [[2463, 2463], 29],
  [[3401, 3401], 21],
  [[5952, 5952], 18],
  [[71429, 71429], 1],
];

/** The source's default `x` when `0x5124F0` is not 42 (`-41`). */
export const DEFAULT_SPEED_X = (-41) >>> 0;
/** The source's speed-table sentinel at `0x5124F0`. */
export const SPEED_SENTINEL = 42;

const ALPHABET = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789";

const DWORD_VIEW = defineMemView("SoundLooperDword", 4, {
  value: { offset: 0, size: 4 },
});
const DWORD_FIELD: FieldSpec = { offset: 0, size: 4 };

/** A deterministic five-character archive prefix (see the module docstring). */
export function loopIdentifier(title: string, ordinal: number): string {
  const digest = createHash("sha256")
    .update(`${title}\u0000${ordinal}`, "utf8")
    .digest();
  let out = "";
  for (let i = 0; i < 5; i++) {
    out += ALPHABET[digest[i] % ALPHABET.length];
  }
  return out;
}

function requirePlainInt(value: unknown, what: string): number {
  if (typeof value !== "number" || !Number.isInteger(value)) {
    throw new EudslError(
      "EUDSL-TYPE",
      `soundlooper: ${what} must be a plain integer, got ${String(value)}`,
    );
  }
  return value;
}

function requireSeconds(value: unknown, what: string): number {
  if (typeof value !== "number" || !Number.isFinite(value)) {
    throw new EudslError(
      "EUDSL-TYPE",
      `soundlooper: ${what} must be a finite number, got ${String(value)}`,
    );
  }
  if (value < 0) {
    throw new EudslError(
      "EUDSL-RANGE",
      `soundlooper: ${what} must not be negative, got ${String(value)}`,
    );
  }
  return value;
}

/**
 * Pack `bar` as the source's three little-endian decimal digits.
 *
 * `get_three_digits` returns `(hundreds, tens, ones)` and the source packs them
 * as `hundreds | (tens << 8) | (ones << 16)` so the three bytes read as an
 * ASCII `%03d` string. Bars above 999 do not fit three digits and are rejected
 * by the constant paths.
 */
export function packBarDigits(bar: number): number {
  requirePlainInt(bar, "bar");
  if (bar < 0 || bar > 999) {
    throw new EudslError(
      "EUDSL-RANGE",
      `soundlooper: bar must be in [0, 999], got ${bar}`,
    );
  }
  const hundreds = Math.floor(bar / 100);
  const tens = Math.floor(bar / 10) % 10;
  const ones = bar % 10;
  return (hundreds | (tens << 8) | (ones << 16)) >>> 0;
}

/** Materialise the dword at `addr` (32 bit-test triggers). */
function readDword(addr: number): VarImpl {
  return new MemFieldHandle(DWORD_VIEW, DWORD_FIELD, EPD(addr), "value").read();
}

/**
 * Emit the source's `_calculate_error` and return its result.
 *
 * `x` starts at `-41`; when `0x5124F0` equals 42 it becomes that dword, and a
 * game-speed pair at `0x51CE84`/`0x51CE88` overrides it. The correction is
 * `(493 - 7 * x) / 41` on the 32-bit wire values (the wrapping negate+add
 * turns `-41` into the source's `780 / 41 = 19`).
 */
export function calculateSpeedError(): VarImpl {
  const x = Var(DEFAULT_SPEED_X);
  when(Memory(GAME_SPEED_ADDR, Exactly, SPEED_SENTINEL), () => {
    x.set(readDword(GAME_SPEED_ADDR));
  });
  for (const [pair, speed] of SPEED_MAP) {
    when(
      [
        Memory(SPEED_84_ADDR, Exactly, pair[0]),
        Memory(SPEED_88_ADDR, Exactly, pair[1]),
      ],
      () => {
        x.set(speed);
      },
    );
  }
  const result = Var(0);
  result.set(x.mul(7).neg().add(493).div(41));
  return result;
}

/** One registered loop, the source's `_Loop` instance. */
export class SoundLoop {
  readonly title: string;
  readonly identifier: string;
  readonly count: number;
  readonly intro: number;
  readonly bar: number;
  readonly bridge: number;
  readonly goto: number;
  readonly index: number;

  constructor(
    title: string,
    identifier: string,
    count: number,
    intro: number,
    bar: number,
    bridge: number,
    goto: number,
    index: number,
  ) {
    this.title = title;
    this.identifier = identifier;
    this.count = count;
    this.intro = intro;
    this.bar = bar;
    this.bridge = bridge;
    this.goto = goto;
    this.index = index;
  }

  /** The last bar number (`count + 1`; bars start at 1). */
  get barCount(): number {
    return this.count + 1;
  }

  /** The source's `ceil(-bar * 1000)` action value (negative). */
  get baseCorrectionMs(): number {
    return Math.ceil(-this.bar * 1000);
  }

  /** The source's `ceil((bar - intro) * 1000)` intro addend. */
  get introCorrectionMs(): number {
    return Math.ceil((this.bar - this.intro) * 1000);
  }

  /** The source's `ceil((bar - bridge) * 1000)` bridge addend. */
  get bridgeCorrectionMs(): number {
    return Math.ceil((this.bar - this.bridge) * 1000);
  }
}

/**
 * Build-time loop registry plus the shared per-loop progress array.
 *
 * `path` is the source's `SetPath` root: `addLoop` looks in
 * `<path>/<title>/`. Relative paths are used as authored (the source's
 * `SetPath` is verbatim too).
 */
export class SoundLoops {
  path: string;
  private readonly loops: SoundLoop[] = [];
  private readonly byTitle = new Map<string, SoundLoop>();
  private barsArray: FixedArrayImpl<VarImpl> | null = null;

  constructor(path = "") {
    this.path = path;
  }

  // -- registration ---------------------------------------------------

  /** Set the root directory used by `addLoop`. */
  setPath(newPath: unknown): void {
    if (typeof newPath !== "string") {
      throw new EudslError(
        "EUDSL-TYPE",
        `soundlooper: path must be a string, got ${String(newPath)}`,
      );
    }
    this.path = newPath;
  }

  /** The source's `get_filepath`: shortest zero-padding first. */
  private segmentPath(title: string, number: number): string | null {
    const prefix = this.path ? `${this.path}/${title}/${title}` : `${title}/${title}`;
    let digits = String(number);
    while (digits.length <= 3) {
      const candidate = `${prefix}${digits}.ogg`;
      if (fs.existsSync(candidate) && fs.statSync(candidate).isFile()) {
        return candidate;
      }
      digits = `0${digits}`; // the source prepends a "0"
    }
    return null;
  }

  private register(loop: SoundLoop): SoundLoop {
    if (this.byTitle.has(loop.title)) {
      throw new EudslError(
        "EUDSL-BOUNDS",
        `soundlooper: a loop titled ${JSON.stringify(loop.title)} is already registered`,
      );
    }
    const registered = new SoundLoop(
      loop.title,
      loop.identifier,
      loop.count,
      loop.intro,
      loop.bar,
      loop.bridge,
      loop.goto,
      this.loops.length,
    );
    this.loops.push(registered);
    this.byTitle.set(registered.title, registered);
    return registered;
  }

  /**
   * Scan and embed `<title>000.ogg` .. `<title>999.ogg`.
   *
   * The first segment is the intro, the second sets the bar length, and the
   * last found segment sets the bridge length; `count` is the number of
   * segments after the intro. Returns the registered loop, or `null` when all
   * 1000 numbered segments exist (the source's loop also falls through without
   * registering).
   */
  addLoop(title: string, goto = 1): SoundLoop | null {
    if (typeof title !== "string" || title.length === 0) {
      throw new EudslError(
        "EUDSL-TYPE",
        `soundlooper: title must be a non-empty string, got ${String(title)}`,
      );
    }
    requirePlainInt(goto, "goto");
    if (goto < 0 || goto > 999) {
      throw new EudslError(
        "EUDSL-RANGE",
        `soundlooper: goto must be in [0, 999], got ${goto}`,
      );
    }
    if (this.barsArray !== null) {
      throw new EudslError(
        "EUDSL-SCOPE",
        `soundlooper: cannot add ${JSON.stringify(title)} after the player is initialized; ` +
          "register every loop before the first player call",
      );
    }

    const identifier = loopIdentifier(title, this.loops.length);
    let intro = 0;
    let bar = 0;
    let bridge = 0;
    for (let number = 0; number < 1000; number++) {
      const path = this.segmentPath(title, number);
      if (path !== null) {
        const duration = oggDuration(path);
        if (duration === null) {
          throw new EudslError(
            "EUDSL-FORMAT",
            `soundlooper: ${JSON.stringify(path)} has no audio page to measure`,
          );
        }
        const length = roundDuration(duration);
        if (number === 0) {
          intro = length;
        } else if (bar === 0) {
          bar = length;
        }
        bridge = length;
        embedFile(`${identifier}${String(number).padStart(3, "0")}`, path);
      } else if (number === 0) {
        continue;
      } else if (intro !== 0 || bar !== 0 || bridge !== 0) {
        return this.register(
          new SoundLoop(
            title,
            identifier,
            number - 1,
            intro,
            bar,
            bridge,
            goto,
            -1,
          ),
        );
      } else {
        throw new EudslError(
          "EUDSL-RANGE",
          `soundlooper: no segments found for ${JSON.stringify(title)} under ` +
            `${JSON.stringify(this.path)}`,
        );
      }
    }
    return null;
  }

  /**
   * Register a loop whose segments are already in the MPQ.
   *
   * `title` is the five-byte archive prefix (the source checks five cp949
   * bytes; the shared py/ts contract is five single-byte ASCII characters).
   * `count` is the number of segments after the intro.
   */
  manualAddLoop(
    title: string,
    count: number,
    intro: number,
    bar: number,
    bridge: number,
    goto = 1,
  ): SoundLoop {
    if (typeof title !== "string" || title.length !== 5) {
      throw new EudslError(
        "EUDSL-RANGE",
        `soundlooper: manualAddLoop title must be 5 characters, got ${String(title)}`,
      );
    }
    for (const char of title) {
      if (char.codePointAt(0)! > 0x7f) {
        throw new EudslError(
          "EUDSL-RANGE",
          "soundlooper: manualAddLoop title must be five ASCII characters " +
            `(one byte each), got ${JSON.stringify(title)}`,
        );
      }
    }
    requirePlainInt(count, "count");
    if (count < 0 || count > 999) {
      throw new EudslError(
        "EUDSL-RANGE",
        `soundlooper: count must be in [0, 999], got ${count}`,
      );
    }
    requirePlainInt(goto, "goto");
    if (goto < 0 || goto > 999) {
      throw new EudslError(
        "EUDSL-RANGE",
        `soundlooper: goto must be in [0, 999], got ${goto}`,
      );
    }
    const introSeconds = requireSeconds(intro, "intro");
    const barSeconds = requireSeconds(bar, "bar");
    const bridgeSeconds = requireSeconds(bridge, "bridge");
    if (this.barsArray !== null) {
      throw new EudslError(
        "EUDSL-SCOPE",
        `soundlooper: cannot add ${JSON.stringify(title)} after the player is initialized; ` +
          "register every loop before the first player call",
      );
    }
    return this.register(
      new SoundLoop(
        title,
        title,
        count,
        introSeconds,
        barSeconds,
        bridgeSeconds,
        goto,
        -1,
      ),
    );
  }

  // -- lookup / progress array ----------------------------------------

  get length(): number {
    return this.loops.length;
  }

  /** Registered loops in registration order. */
  get all(): readonly SoundLoop[] {
    return this.loops;
  }

  /** The loop registered at `index` (`SoundLoop.index` sorted). */
  at(index: number): SoundLoop {
    return this.loops[index];
  }

  get(title: string): SoundLoop | undefined {
    return this.byTitle.get(title);
  }

  indexOf(title: string): number {
    const loop = this.byTitle.get(title);
    if (loop === undefined) {
      throw new EudslError(
        "EUDSL-RANGE",
        `soundlooper: no loop titled ${JSON.stringify(title)}`,
      );
    }
    return loop.index;
  }

  /**
   * The shared progress array, allocated on first use (the source's lazy
   * `SoundLooper._bars`). Registration is frozen afterwards.
   */
  bars(): FixedArrayImpl<VarImpl> {
    if (this.barsArray === null) {
      if (this.loops.length === 0) {
        throw new EudslError(
          "EUDSL-RANGE",
          "soundlooper: no loops registered before the player was used",
        );
      }
      this.barsArray = FixedArray<VarImpl>("dword", this.loops.length);
    }
    return this.barsArray;
  }
}

const CMP_METHOD = new Map<number, "eq" | "ge" | "le">([
  [Exactly, "eq"],
  [AtLeast, "ge"],
  [AtMost, "le"],
]);

/** Loop-sound player (the source's `SoundLooper`). */
export class SoundLooper {
  readonly loops: SoundLoops;
  readonly currentLoop = Var(0xffffffff);
  readonly previousLoop = Var(0xffffffff);
  readonly currentBar = Var(0);
  readonly barDigits = Var(0);
  /** Largest raw clock value at which the next pass is due. */
  readonly threshold = Var(0xfffffffe);

  // Schedule constants written by `play`/`resume` (the source's per-loop
  // `_set_bar_length` / `_set_intro_length` action values). Public so tests and
  // advanced callers can inspect them; each is written, never replaced.
  readonly segmentVar = Var(0);
  readonly introAdd = Var(0);
  readonly bridgeAdd = Var(0);
  readonly lastBar = Var(0);
  readonly gotoBar = Var(1);
  readonly gotoDigits = Var(0x10000);
  private readonly hit = Var(0);
  private readonly localPlayer = Var(0);
  private probe: Trigger | null = null;
  private playerEmitted = false;

  constructor(loops?: SoundLoops) {
    this.loops = loops ?? new SoundLoops();
  }

  // -- helpers -------------------------------------------------------

  private bars(): FixedArrayImpl<VarImpl> {
    return this.loops.bars();
  }

  /** Resolve a title / index / `null` to a build-time index or `Var`. */
  private resolve(title: string | number | VarImpl | null): number | VarImpl {
    if (title === null || title === undefined) {
      return this.currentLoop;
    }
    if (typeof title === "string") {
      return this.loops.indexOf(title);
    }
    if (typeof title === "number") {
      if (!Number.isInteger(title)) {
        throw new EudslError(
          "EUDSL-TYPE",
          "soundlooper: loop must be a registered title, an index, or a Var, " +
            `got ${String(title)}`,
        );
      }
      if (title < 0 || title >= this.loops.length) {
        throw new EudslError(
          "EUDSL-RANGE",
          `soundlooper: loop index ${title} out of range ` +
            `(0..${this.loops.length - 1})`,
        );
      }
      return title;
    }
    if (title instanceof VarImpl) {
      return title;
    }
    throw new EudslError(
      "EUDSL-TYPE",
      "soundlooper: loop must be a registered title, an index, or a Var, " +
        `got ${String(title)}`,
    );
  }

  private applyLoopConstants(loop: SoundLoop): void {
    this.segmentVar.set(loop.baseCorrectionMs);
    this.introAdd.set(loop.introCorrectionMs);
    this.bridgeAdd.set(loop.bridgeCorrectionMs);
    this.lastBar.set(loop.barCount);
    this.gotoBar.set(loop.goto);
    this.gotoDigits.set(packBarDigits(loop.goto));
  }

  private setloop(index: number | VarImpl): void {
    // VProc semantics: every argument is read before any assignment, so
    // `resume()` (index === previousLoop) survives the previousLoop write.
    let incoming: number | VarImpl;
    if (index instanceof VarImpl) {
      const snapshot = Var(0);
      snapshot.set(index);
      incoming = snapshot;
    } else {
      incoming = index;
    }
    this.previousLoop.set(this.currentLoop);
    this.currentLoop.set(incoming);

    const err = calculateSpeedError();
    if (typeof incoming === "number") {
      this.applyLoopConstants(this.loops.at(incoming));
    } else {
      const incomingVar = incoming;
      for (const loop of this.loops.all) {
        when(incomingVar.eq(loop.index), () => this.applyLoopConstants(loop));
      }
    }
    this.segmentVar.iadd(err);
  }

  /** Write `bar`'s packed digits into `barDigits`. */
  private digitsFrom(bar: number | VarImpl): void {
    if (typeof bar === "number") {
      this.barDigits.set(packBarDigits(bar));
      return;
    }
    const [high, ones] = bar.divmod(10);
    const [hundreds, tens] = high.divmod(10);
    this.barDigits.set(hundreds.add(tens.shl(8)).add(ones.shl(16)));
  }

  // -- build-time emitted player -------------------------------------

  private ensureProbe(): Trigger {
    if (this.probe === null) {
      const probe = new Trigger({
        conds: [
          Memory(SOUND_CLOCK_ADDR, AtMost, 0xfffffffe),
          this.currentLoop.ne(0xffffffff),
        ],
        acts: [this.hit.act_set(1)],
        register: false,
      });
      _registerTrigger(probe);
      this.probe = probe;
    }
    return this.probe;
  }

  /** The source's `PlaySoundWorkaround`: constant names per loop/bar. */
  private playCurrentBar(): void {
    for (const loop of this.loops.all) {
      when(this.currentLoop.eq(loop.index), () => {
        for (let number = 0; number <= loop.count; number++) {
          const name = `${loop.identifier}${String(number).padStart(3, "0")}`;
          when(this.currentBar.eq(number + 1), () => PlayWAV(name));
        }
      });
    }
  }

  private tick(): void {
    this.hit.set(0);
    const bars = this.bars();
    const slotCount = this.loops.length;

    // 1. Advance the bar and the packed digit register (source order).
    this.currentBar.iadd(1);
    this.barDigits.iadd(1 << 16);
    when(
      DeathsX(this.barDigits.value, AtLeast, 10 << 16, 0, 0xff << 16),
      () => this.barDigits.iadd((1 << 8) - (10 << 16)),
    );
    when(
      DeathsX(this.barDigits.value, AtLeast, 10 << 8, 0, 0xff << 8),
      () => this.barDigits.iadd(1 - (10 << 8)),
    );
    when(this.currentLoop.lt(slotCount), () => {
      bars.set(this.currentLoop, this.currentBar);
    });

    // 2. Play the current segment for the local player. The source stores the
    //    local player id into CurrentPlayer for the whole pass; the port saves
    //    and restores it like the BGM helper (ADR 0004 resets curpl once per
    //    trigger loop).
    const oldCp = readCurrentPlayerRegister();
    emitVarStore(this.localPlayer.id, AddrReloc(CP_ADDR));
    this.playCurrentBar();
    emitVarStore(oldCp.id, AddrReloc(CP_ADDR));

    // 3. Schedule the next firing from the raw clock and this segment's
    //    correction (the source patches the probe condition's amount).
    const raw = readDword(SOUND_CLOCK_ADDR);
    this.threshold.set(raw);
    this.threshold.iadd(this.segmentVar);
    this.threshold.isub(1); // probe tests `raw <= threshold`
    when(this.currentBar.eq(1), () => this.threshold.iadd(this.introAdd));
    when(this.currentBar.ge(this.lastBar), () => this.threshold.iadd(this.bridgeAdd));
    const probe = this.ensureProbe();
    emitVarStore(
      this.threshold.id,
      TriggerFieldReloc(probe.id, probe.cond(0).amount.byteOffset),
    );

    // 4. After the last bar, return to `goto` (source: "if last bar was
    //    played, go to start").
    const reset = (): void => {
      this.currentBar.set(this.gotoBar);
      this.barDigits.set(this.gotoDigits);
      when(this.currentLoop.lt(slotCount), () => {
        bars.set(this.currentLoop, this.gotoBar);
      });
    };
    when(this.currentBar.ge(this.lastBar), reset);
  }

  /**
   * Emit the per-loop playback body (the source's `player()`).
   *
   * Call once, after registering loops and arming `play` at the call site of
   * your choice. The body fires on the trigger loops where the countdown clock
   * has reached the next segment boundary and a loop is playing.
   */
  player(): void {
    if (this.playerEmitted) {
      throw new EudslError(
        "EUDSL-SCOPE",
        "soundlooper: player() may only be emitted once",
      );
    }
    this.playerEmitted = true;
    this.ensureProbe();
    // eudplib reads the local player id once at map start (EUDOnStart); do
    // the same so the 32-test read stays off the per-segment path.
    once(() => this.localPlayer.set(readDword(USER_PLAYER_ADDR)));
    when(this.hit.eq(1), () => this.tick());
  }

  // -- player controls -----------------------------------------------

  /**
   * Play `title` (a registered name, an index, a `Var`, or `null`).
   *
   * `bar` selects the progress position; omitted, the loop resumes from its
   * progress-array slot (`bars[index]`). The next due pass plays `bar + 1`.
   */
  play(
    title: string | number | VarImpl | null = null,
    bar: number | VarImpl | null = null,
  ): void {
    if (typeof bar === "number") {
      if (!Number.isInteger(bar) || bar < 0 || bar > 999) {
        throw new EudslError(
          "EUDSL-RANGE",
          `soundlooper: bar must be in [0, 999], got ${String(bar)}`,
        );
      }
    } else if (bar !== null && !(bar instanceof VarImpl)) {
      throw new EudslError(
        "EUDSL-TYPE",
        `soundlooper: bar must be an integer, a Var, or null, got ${String(bar)}`,
      );
    }
    const index = this.resolve(title);
    this.setloop(index);
    if (bar === null) {
      const bars = this.bars();
      this.setbar(typeof index === "number" ? bars.get(index) : bars.get(index));
    } else {
      this.setbar(bar);
    }
  }

  /** Pause the current loop (`previousLoop` remembers it). */
  pause(): void {
    this.previousLoop.set(this.currentLoop);
    this.currentLoop.set(0xffffffff);
  }

  /** Resume the paused loop. */
  resume(): void {
    this.setloop(this.previousLoop);
  }

  /** Toggle between playing and paused. */
  toggle(): void {
    when(this.currentLoop.eq(0xffffffff), () => this.resume());
    when(this.currentLoop.ne(0xffffffff), () => this.pause());
  }

  /**
   * Set the current loop's progress bar (and its progress slot).
   *
   * `bar` is the *last played* position: the next due pass advances to
   * `bar + 1` (the source's progress-array convention).
   */
  setbar(bar: number | VarImpl): void {
    this.currentBar.set(bar);
    this.digitsFrom(bar);
    const bars = this.bars();
    when(this.currentLoop.lt(this.loops.length), () => {
      bars.set(this.currentLoop, this.currentBar);
    });
  }

  /** Copy the current bar into `loop`'s progress slot. */
  sendcurrentbar(loop: string | number | VarImpl | null): void {
    const index = this.resolve(loop);
    if (typeof index !== "number") {
      throw new EudslError(
        "EUDSL-BOUNDS",
        "soundlooper: sendcurrentbar() needs a build-time loop index",
      );
    }
    this.bars().set(index, this.currentBar);
  }

  /** Copy `src`'s progress into `dst`'s slot. */
  sendbar(
    dst: string | number | VarImpl | null,
    src: string | number | VarImpl | null,
  ): void {
    const destination = this.resolve(dst);
    const source = this.resolve(src);
    if (typeof destination !== "number" || typeof source !== "number") {
      throw new EudslError(
        "EUDSL-BOUNDS",
        "soundlooper: sendbar() needs build-time loop indices",
      );
    }
    this.bars().set(destination, this.bars().get(source));
  }

  /** Set a non-playing loop's progress slot. */
  setloopbar(loop: string | number | VarImpl | null, bar: number | VarImpl): void {
    const index = this.resolve(loop);
    if (typeof index !== "number") {
      throw new EudslError(
        "EUDSL-BOUNDS",
        "soundlooper: setloopbar() needs a build-time loop index",
      );
    }
    this.bars().set(index, bar);
  }

  /** A condition on `loop`'s progress (`Exactly`/`AtLeast`/`AtMost`). */
  cond(
    loop: string | number | VarImpl | null,
    cmptype: number,
    value: number | VarImpl,
  ): CondLike {
    const method = CMP_METHOD.get(cmptype);
    if (method === undefined) {
      throw new EudslError(
        "EUDSL-RANGE",
        "soundlooper: cond() needs Exactly, AtLeast, or AtMost, got " +
          `${String(cmptype)}`,
      );
    }
    const index = this.resolve(loop);
    if (typeof index !== "number") {
      throw new EudslError(
        "EUDSL-BOUNDS",
        "soundlooper: cond() needs a build-time loop index",
      );
    }
    const element = this.bars().get(index);
    switch (method) {
      case "eq":
        return element.eq(value);
      case "ge":
        return element.ge(value);
      case "le":
        return element.le(value);
    }
  }

  /** A condition matching the currently playing loop. */
  loopis(loop: string | number | VarImpl | null): CondLike {
    return this.currentLoop.eq(this.resolve(loop));
  }
}

/** Create a `SoundLooper` (canonical camelCase constructor). */
export function soundLooper(loops?: SoundLoops): SoundLooper {
  return new SoundLooper(loops);
}

// The canonical snake_case alias (ADR 0002 §3), mirroring `py/soundlooper.py`.
export const sound_looper = soundLooper;
export const loop_identifier = loopIdentifier;
export const pack_bar_digits = packBarDigits;
export const calculate_speed_error = calculateSpeedError;

