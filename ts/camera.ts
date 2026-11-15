/**
 * Camera-follow helper: a port of euddraft's `plugins/cammove.py`.
 *
 * The source plugin is a per-loop `afterTriggerExec` body with two pieces of
 * persistent state (the last game tick it ran and the current screen width in
 * pixels). This module emits the same body behind the same run gates, keeping
 * the source arithmetic and its fixed-point units (`fpQ = 256`) intact, and
 * keeps the local player id in a third piece of persistent state (a gate-scoped
 * `once` cache, since the id is constant for the map).
 *
 * Deviations from the plugin, forced by eudkit having no location, switch, or
 * plugin settings allocator, plus ADR 0004's per-loop `curpl` reset:
 *
 * - the caller owns both locations: `cameraLocation` is used as the
 *   scratch box the source plugin called `cammoveLoc`, and `targetLocation`
 *   is the search location;
 * - the unit/owner pair the source hard-codes (`"Map Revealer"` / `P9`)
 *   are keyword options with the same defaults;
 * - `switchId` (default `0`) replaces the source's named `"cammove"`
 *   switch;
 * - the per-loop state is returned as `CameraFollowState` so a caller
 *   can inspect or share it instead of relying on plugin globals;
 * - the source's `onPluginStart` `f_setloc` of the scratch box is omitted:
 *   the first gated pass overwrites the box through `MoveLocation` and then
 *   through the emitted `varstore` writes, so the initial 640x384 box is
 *   never read;
 * - `CenterView` targets the *CurrentPlayer* (`0x6509B0`), which the payload
 *   runner leaves at `0` (P1). The source stores `f_getuserplayerid()` there
 *   inside its `SeqCompute` and then leans on eudplib's
 *   `f_setcurpl2cpcache`/finalizer; the port writes the register directly
 *   before `CenterView` and restores `0` right after, because ADR 0004
 *   resets `curpl` once per trigger loop rather than once per call.
 *
 * `maxSpeed` keeps the source setting's spelling (`settings["maxspeed"]`)
 * rather than the mechanical `max_speed` (the TS camelCase form is
 * `maxSpeed`).
 *
 * Cost, measured on one instance with the native simulator (`dump-ir --json
 * --no-exec` for the static side, `sim` for the executed side, pre-assembly,
 * so the map-build tick gate is excluded), as a delta over identical seeding
 * scaffolding: `inertia: 5` is 1,177 T / 509 C / 3,241 A static (2,834,256 B)
 * and 1,159 T / 486 C / 18,535 A executed on the first gated pass, easing to
 * 1,121 T / 454 C / 18,463 A on the steady-state second pass; `inertia: 0` is
 * 223 T / 198 C / 376 A static (537,000 B) and 271 T / 229 C / 1,164 A
 * executed on the first pass, 233 T / 197 C / 1,092 A on the second; a pass
 * where the gate is closed is 3 T / 2 C / 0 A executed because the chain jump
 * skips the body. The gate-scoped `once` for `USER_PLAYER_ADDR` keeps the
 * first pass at the parent's 1,159 T and saves the 33 T reader plus its call
 * overhead on later open passes (a measured -35 T / -32 C / -70 A steady
 * state); it adds 1 T / 2,408 B static. The constant multiply inside the
 * smoothing kernel delegates to `Var.mul`, so `(currentTick - lastTick) * 256`
 * is one masked const-shift instead of a 24-trigger ladder. The `run` body's
 * shared path holds four coalesced transfer pairs (`dstx`/`dsty`,
 * `right`/`bottom`, the two additive `updated` additions, and
 * `screenX`/`lastTick`), so body-wide transfer coalescing moves `inertia: 0`
 * by 4 static triggers, 8 executed triggers, and 9,632 B as well. Most of the
 * open cost is the six steady-state 32-bit dword reads (seven on the first
 * pass, 33 T each), `isqrt(16, 8)` and the three 16-step divides; see the
 * camera test's cost pin for the numbers.
 *
 * The source reads/writes fixed game addresses (camera position, game tick,
 * location table, CurrentPlayer), so those are module constants here. The
 * source's `MoveLocation` writes the box before the helper reads it; the
 * simulator is free to model that action, but the emitted order is the
 * contract.
 */

import { CP_ADDR } from "./cp.ts";
import {
  Add,
  AddrReloc,
  AtLeast,
  CenterView,
  DeathsX,
  Memory,
  MoveLocation,
  SetDeaths,
  SetDeathsX,
  SetTo,
  Switch,
  SwitchCleared,
  SwitchSet,
  emitVarStore,
} from "./hir.ts";
import { isqrt } from "./mathf.ts";
import { once, when } from "./temporal.ts";
import { Trigger } from "./trigger.ts";
import {
  Var,
  VarImpl,
  mulShiftAdd,
  saturatingSubInto,
} from "./var.ts";
import {
  EPD,
  EPD_CURRENT_PLAYER,
  EudslError,
  MemFieldHandle,
  defineMemView,
  requirePlainInt,
  type FieldSpec,
} from "./view.ts";

export const CAMERA_X_ADDR = 0x62848c;
export const CAMERA_Y_ADDR = 0x6284a8;
export const CAMERA_TILE_X_ADDR = 0x628498;
export const CAMERA_TILE_Y_ADDR = 0x6284ac;
/** `gbMultiPlayerMode`: 0 = single player, 1 = multiplayer (SC:R read-only). */
export const MULTIPLAYER_MODE_ADDR = 0x57f0b4;
export const GAME_TICK_ADDR = 0x57f23c;
export const LOCATION_TABLE_ADDR = 0x58dc4c;
export const LOCATION_STRIDE_DWORDS = 5;
/**
 * `dwUserPlayerId`: the local human's player index, the value the source
 * plugin puts in `CurrentPlayer` before `CenterView`.
 */
export const USER_PLAYER_ADDR = 0x512684;

export const FP_Q = 256;
export const FP_Q2 = FP_Q * FP_Q;
export const SCREEN_W = 640;
export const SCREEN_H = 384;
export const MAP_REVEALER_UNIT = 101; // "Map Revealer" trigger-unit id
export const MAP_REVEALER_OWNER = 8; // P9

export const PLAYER_VALUES = [
  0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 13, 14, 15, 16, 17, 18, 19, 20, 21, 26,
] as const;

/** Persistent per-loop state of one `cameraFollow` instance. */
export interface CameraFollowState {
  readonly lastTick: VarImpl;
  readonly screenX: VarImpl;
  readonly userPlayer: VarImpl;
}

const DWORD_VIEW = defineMemView("CameraDword", 4, {
  value: { offset: 0, size: 4 },
});
const DWORD_FIELD: FieldSpec = { offset: 0, size: 4 };

function requireInt(name: string, value: unknown, minimum: number, maximum: number): number {
  const checked = requirePlainInt(value, `cameraFollow(): ${name}`);
  if (checked < minimum || checked > maximum) {
    throw new EudslError(
      "EUDSL-RANGE",
      `cameraFollow(): ${name} must be in [${minimum}, ${maximum}], got ${checked}`,
    );
  }
  return checked;
}

/** Read the dword at `epd` into a fresh `Var` (one bit test per bit). */
function readEpd(epd: number): VarImpl {
  return new MemFieldHandle(DWORD_VIEW, DWORD_FIELD, epd, "value").read();
}

function readDword(addr: number): VarImpl {
  return readEpd(EPD(addr));
}

/**
 * `out = x * x` for an `x` whose bits above `bits` are zero.
 *
 * Each set-bit pair `b <= k` contributes `2 ** (b + k)` (diagonal) or
 * `2 ** (b + k + 1)` (off-diagonal): one trigger per pair instead of the
 * 528-trigger `Var * Var` kernel.
 *
 * The accumulator is cleared on every call: the kernel is emitted inside a
 * per-loop body, so a build-time zero init would let pass 2 accumulate on
 * pass 1.
 */
function square(x: VarImpl, bits: number): VarImpl {
  const out = new Var(0);
  out.set(0);
  for (let b = 0; b < bits; b++) {
    const condB = DeathsX(x.value, AtLeast, 1 << b, 0, 1 << b);
    new Trigger({
      conds: [condB],
      acts: [SetDeathsX(out.value, Add, (1 << (2 * b)) & 0xffffffff, 0, 0xffffffff)],
    });
    for (let k = b + 1; k < bits; k++) {
      new Trigger({
        conds: [condB, DeathsX(x.value, AtLeast, 1 << k, 0, 1 << k)],
        acts: [
          SetDeathsX(
            out.value,
            Add,
            (1 << (b + k + 1)) & 0xffffffff,
            0,
            0xffffffff,
          ),
        ],
      });
    }
  }
  return out;
}

/**
 * `out = x * c` for a build-time constant, via `Var.mul`.
 *
 * Delegates to the Cycle 4 constant-multiply kernel: `0 -> 0`, `1` -> a copy,
 * `0xFFFFFFFF` -> a negate, a power of two -> the single masked const-shift,
 * and any other value -> the measured k-term shift-add chain when its cost
 * gate fits (otherwise the 32-bit window scan).
 *
 * Every branch assigns `out` directly on every call for the same per-loop
 * safety property as `square`.
 */
function mulConst(x: VarImpl, c: number): VarImpl {
  const out = new Var(0);
  out.set(x.mul(c));
  return out;
}

/** `out = x / c` for a build-time constant, via `Var.div`. */
function divConst(x: VarImpl, c: number): VarImpl {
  const out = new Var(0);
  out.set(x.div(c));
  return out;
}

function shrConst(x: VarImpl, k: number): VarImpl {
  const out = new Var(0);
  out.set(x.shr(k));
  return out;
}

/**
 * `out = (hi << 16) / den` with `hi < den` and quotient < `2 ** bits`.
 *
 * Two long-division facts keep this short: bringing down the 16 high bits
 * of `hi << 16` leaves `hi mod den == hi` as the remainder, and only the
 * 16 zero bits below contribute quotient bits. Each iteration doubles the
 * remainder and quotient (`iadd(x)` is a 3-trigger copy, not a 31-trigger
 * shift) and subtracts `den` when it fits. `bits` caps the iterations
 * when the caller knows the quotient is narrower.
 *
 * The quotient accumulator is cleared on every call for the same per-loop
 * reason as `square`; `rem` and `diff` are seeded explicitly.
 */
function divShift16(hi: VarImpl, den: VarImpl, bits = 16): VarImpl {
  const rem = new Var(0);
  rem.set(hi);
  const quot = new Var(0);
  quot.set(0);
  const diff = new Var(0);
  for (let i = 0; i < bits; i++) {
    rem.iadd(rem);
    quot.iadd(quot);
    diff.set(den);
    saturatingSubInto(diff, rem); // 0 exactly when rem >= den
    when(diff.eq(0), () => {
      saturatingSubInto(rem, den);
      quot.iadd(1);
    });
  }
  return quot;
}

function negateInto(dst: VarImpl, src: VarImpl): void {
  dst.set(0xffffffff);
  saturatingSubInto(dst, src);
  dst.iadd(1);
}

/**
 * Emit a per-loop camera-follow body (euddraft `cammove.py` port).
 *
 * Every trigger loop where the game is in multiplayer (`Memory(0x57F0B4,
 * AtLeast, 1)`) and `switchId` is set:
 *
 * 1. remembers `currentTick` the first time the switch is seen set;
 * 2. moves `cameraLocation` onto `onUnit` owned by `owner` inside
 *    `targetLocation` and reads its top-left corner;
 * 3. eases the view toward that corner with the source plugin's
 *    fixed-point smoothing and `maxSpeed` cap;
 * 4. writes the scratch box, centers the view, updates the camera tile
 *    coordinates and the widescreen `screenX` correction, and stores
 *    `currentTick` as the last tick.
 *
 * When the switch is cleared (still in multiplayer) and the state is not
 * already reset, `lastTick` is set back to `-1` so re-enabling snaps instead
 * of lerping from a stale tick.
 *
 * `inertia === 0` is a build-time branch: the emitted body jumps straight
 * to the target box and omits the smoothing kernel. The source's
 * `dx == 0 and dy == 0` shortcut is subsumed by the emitted
 * `d >= 1` guard (zero distance leaves `dstx`/`dsty` at the target).
 *
 * Returns the three persistent state variables.
 */
export function cameraFollow(
  targetLocation: number,
  cameraLocation: number,
  inertia = 5,
  maxSpeed = 48,
  switchId = 0,
  options: { onUnit?: number; owner?: number } = {},
): CameraFollowState {
  targetLocation = requireInt("targetLocation", targetLocation, 0, 255);
  cameraLocation = requireInt("cameraLocation", cameraLocation, 0, 255);
  inertia = requireInt("inertia", inertia, 0, 0xffffffff);
  maxSpeed = requireInt("maxSpeed", maxSpeed, 0, 0xffffffff);
  switchId = requireInt("switchId", switchId, 0, 255);
  const onUnit = requireInt("onUnit", options.onUnit ?? MAP_REVEALER_UNIT, 0, 227);
  const owner = requirePlainInt(options.owner ?? MAP_REVEALER_OWNER, "cameraFollow(): owner");
  if (!(PLAYER_VALUES as readonly number[]).includes(owner)) {
    throw new EudslError(
      "EUDSL-RANGE",
      `cameraFollow(): owner must be one of [${PLAYER_VALUES.join(", ")}], got ${String(owner)}`,
    );
  }

  const lastTick = new Var(0xffffffff);
  const screenX = new Var(SCREEN_W);
  const userPlayer = new Var(0);
  const state: CameraFollowState = { lastTick, screenX, userPlayer };

  const boxEpd = EPD(LOCATION_TABLE_ADDR) + LOCATION_STRIDE_DWORDS * cameraLocation;
  const boxAddr = LOCATION_TABLE_ADDR + LOCATION_STRIDE_DWORDS * 4 * cameraLocation;

  const run = (): void => {
    const currentTick = readDword(GAME_TICK_ADDR);
    // `dwUserPlayerId` is constant for the map. Read it once inside the gated
    // body so later open passes skip the 33 T reader; a closed gate never
    // reaches the `once`, so that path stays 3 T / 2 C / 0 A.
    once(() => userPlayer.set(readDword(USER_PLAYER_ADDR)));
    when(lastTick.eq(0xffffffff), () => lastTick.set(currentTick));

    const prevCamX = readDword(CAMERA_X_ADDR);
    const prevCamY = readDword(CAMERA_Y_ADDR);

    MoveLocation(cameraLocation, onUnit, owner, targetLocation);
    const locx = readEpd(boxEpd);
    const locy = readEpd(boxEpd + 1);

    const dstx = new Var(0);
    dstx.set(locx);
    const dsty = new Var(0);
    dsty.set(locy);

    if (inertia === 0) {
      // No smoothing: the view jumps straight to the target box.
    } else {
      const dx = new Var(0);
      dx.set(locx);
      dx.isub(prevCamX);
      const dy = new Var(0);
      dy.set(locy);
      dy.isub(prevCamY);

      const dxabs = new Var(0);
      dxabs.set(dx);
      const dyabs = new Var(0);
      dyabs.set(dy);
      when(dx.ge(0x80000000), () => negateInto(dxabs, dx));
      when(dy.ge(0x80000000), () => negateInto(dyabs, dy));

      const d = new Var(0);
      // Assign the cap `fpQ * f_sqrt(20000)` (the overflow-safe norm) on
      // every pass: the accurate branch below is conditional, so an
      // initializer alone would leave a stale accurate value behind for the
      // next loop's overflow path.
      d.set(FP_Q * 141);

      const accurate = (): void => {
        const sumsq = square(dxabs, 7);
        sumsq.iadd(square(dyabs, 7));
        // Q8 square root: floor(256 * sqrt(sumsq)), with sumsq < 2 ** 16.
        d.set(isqrt(sumsq, 16, 8));
      };

      when([dxabs.le(100), dyabs.le(100)], accurate);

      const delta = new Var(0);
      delta.set(currentTick);
      delta.isub(lastTick);
      const dt = divConst(mulConst(delta, FP_Q), 24);

      const newd = new Var(0);
      const newdx = new Var(0);
      const newdy = new Var(0);

      const ease = (): void => {
        // `dt` is a full dword (`(currentTick - lastTick) * 256 // 24`), so
        // the multiply uses all 32 bits like the source; at 16 bits a gap of
        // >= 6144 ticks dropped the high half and the view snapped instead of
        // easing.
        let den = mulShiftAdd(d, dt, 32);
        den = divConst(den, inertia);
        den.iadd(FP_Q2);
        newd.set(divShift16(d, den)); // d < 65536 <= den

        const gap = new Var(0);
        gap.set(d);
        saturatingSubInto(gap, newd);
        const cap = maxSpeed * FP_Q;
        when(gap.ge(cap), () => {
          newd.set(d);
          newd.isub(cap);
        });

        // `qx = (dxabs << 16) / d` must stay below 2 ** 16 for the 16-step
        // divide. The accurate branch has `d >= 256 * dxabs`, and the capped
        // branch has `d = 36096`, so any axis below 36096 px fits (a 256-tile
        // map is 8192 px). The source's full 32-bit divide has no such bound;
        // a larger map would need `bits = 32` here.
        const qx = divShift16(dxabs, d);
        const qy = divShift16(dyabs, d);
        newdx.set(shrConst(mulShiftAdd(newd, qx, 16), 16));
        newdy.set(shrConst(mulShiftAdd(newd, qy, 16), 16));

        // Each axis keeps its own sign (the source's dx_sgn / dy_sgn):
        // `dstx << locx - dx_sgn * newdx` is per-axis, so a target up
        // and to the left must move X one way and Y the other.
        when(dx.ge(0x80000000), () => dstx.iadd(newdx));
        when(dx.lt(0x80000000), () => dstx.isub(newdx));
        when(dy.ge(0x80000000), () => dsty.iadd(newdy));
        when(dy.lt(0x80000000), () => dsty.isub(newdy));
      };

      when(d.ge(1), ease);
    }

    const tileX = shrConst(dstx, 5);
    const tileY = shrConst(dsty, 5);
    const right = new Var(0);
    right.set(dstx);
    right.iadd(screenX);
    const bottom = new Var(0);
    bottom.set(dsty);
    bottom.iadd(SCREEN_H);

    emitVarStore(dstx.id, AddrReloc(boxAddr), SetTo);
    emitVarStore(dsty.id, AddrReloc(boxAddr + 4), SetTo);
    emitVarStore(right.id, AddrReloc(boxAddr + 8), SetTo);
    emitVarStore(bottom.id, AddrReloc(boxAddr + 12), SetTo);
    // `CenterView` centers the *CurrentPlayer*'s view, and the payload runner
    // leaves 0x6509B0 at 0 (P1). The source stores the local human's player
    // id there in the same `SeqCompute` as the box writes; the port writes the
    // register directly (the absolute box stores replace eudplib's cache
    // indirection) and restores 0 immediately after, so a later
    // `Deaths(CurrentPlayer, ...)` in the same loop is not poisoned (ADR 0004
    // resets curpl once per loop).
    emitVarStore(userPlayer.id, AddrReloc(CP_ADDR), SetTo);
    CenterView(cameraLocation);
    SetDeaths(EPD_CURRENT_PLAYER, SetTo, 0, 0);
    emitVarStore(tileX.id, AddrReloc(CAMERA_TILE_X_ADDR), SetTo);
    emitVarStore(tileY.id, AddrReloc(CAMERA_TILE_Y_ADDR), SetTo);

    const postCamX = readDword(CAMERA_X_ADDR);
    const updated = new Var(0);
    updated.set(dstx);
    updated.iadd(dstx);
    updated.iadd(screenX);
    updated.isub(postCamX);
    updated.isub(postCamX);
    screenX.set(updated);

    lastTick.set(currentTick);
  };

  when([Memory(MULTIPLAYER_MODE_ADDR, AtLeast, 1), Switch(switchId, SwitchSet)], run);

  when(
    [
      Memory(MULTIPLAYER_MODE_ADDR, AtLeast, 1),
      Switch(switchId, SwitchCleared),
      lastTick.ne(0xffffffff),
    ],
    () => lastTick.set(0xffffffff),
  );

  return state;
}

export const camera_follow = cameraFollow;
