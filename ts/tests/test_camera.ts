/**
 * Camera-follow helper (issue #22): a per-loop port of euddraft's cammove.py.
 *
 * Two things are pinned here:
 *
 * - the integer smoothing, re-implemented in `model` straight from the
 *   plugin's `afterTriggerExec` arithmetic and compared value-for-value;
 * - the run gates (switch set/cleared, multiplayer mode) and the widescreen
 *   `screenX` correction, asserted directly against simulated memory writes.
 *
 * What the simulator does not model, and how the cases work around it:
 * `MoveLocation` is not simulated, so the scratch box is seeded directly at
 * the location-table address the helper reads; `CenterView` is not simulated,
 * so the post-center camera read returns the seeded camera X. The helper
 * derives the visible width from how far the camera travelled, which is what
 * the widescreen cases assert against.
 *
 * A build-time accumulator that is not cleared every pass silently drifts from
 * the second loop on, so the multi-loop cases are its regression tests. The
 * per-axis sign fix is only visible when the two axes point in opposite
 * directions, so the mixed-sign case below (not the same-sign diagonal case)
 * is its regression test.
 *
 * The Python suite (`py/tests/test_camera_follow.py`) also pins this module's
 * emitted HIR byte-for-byte against `py/camera.py`, so the behavior here is a
 * mirror rather than a second source of truth.
 */

import assert from "node:assert/strict";
import test from "node:test";

import { runSim, runStatic, flattenStmts, type HirStmtNode, type SimReport } from "./helpers.ts";

import {
  CP_ADDR,
  EPD,
  EPD_CURRENT_PLAYER,
  EudslError,
  SetMemory,
  SetSwitch,
  SetTo,
  SwitchActionClear,
  SwitchActionSet,
  Var,
  buildModule,
  emitVarStore,
  when,
  type CameraFollowState,
} from "../index.ts";
import {
  CAMERA_TILE_X_ADDR,
  CAMERA_TILE_Y_ADDR,
  CAMERA_X_ADDR,
  CAMERA_Y_ADDR,
  FP_Q,
  GAME_TICK_ADDR,
  LOCATION_STRIDE_DWORDS,
  LOCATION_TABLE_ADDR,
  MAP_REVEALER_OWNER,
  MAP_REVEALER_UNIT,
  MULTIPLAYER_MODE_ADDR,
  SCREEN_H,
  SCREEN_W,
  USER_PLAYER_ADDR,
  cameraFollow,
  camera_follow,
} from "../index.ts";

const EPD_BASE = 0x58a364;
const MASK = 0xffffffff;

const SWITCH_ID = 3;
const CAMERA_LOC = 10;
const TARGET_LOC = 11;
const BOX_ADDR = LOCATION_TABLE_ADDR + LOCATION_STRIDE_DWORDS * 4 * CAMERA_LOC;

/** The 1 ms tick unit the source plugin uses: 24 game ticks == 256 fp units. */
const TICK_STEP = 24;

/** Simulator key for the EPD index of `addr` (negative EPDs are u32). */
function epdKey(addr: number): string {
  return String(Math.floor((addr - EPD_BASE) / 4) >>> 0);
}

function read(report: SimReport, addr: number): number {
  return report.external_epds[epdKey(addr)] ?? 0;
}

function written(report: SimReport, addr: number): boolean {
  return epdKey(addr) in report.external_epds;
}

interface Step {
  multiplayer: number;
  switchOn: boolean;
  tick: number;
  camera: [number, number];
  box: [number, number];
}

function step(overrides: Partial<Step> = {}): Step {
  return {
    multiplayer: 1,
    switchOn: true,
    tick: 0,
    camera: [960, 800],
    box: [1000, 800],
    ...overrides,
  };
}

interface ModelRow {
  box: [number, number];
  right: number;
  bottom: number;
  tile: [number, number];
  screenX: number;
}

function isqrt(n: number): number {
  let x = Math.floor(Math.sqrt(n));
  while (x * x > n) x -= 1;
  while ((x + 1) * (x + 1) <= n) x += 1;
  return x;
}

/**
 * Reference implementation of the plugin's fixed-point easing: `fpQ = 256`,
 * `dt = ticks * fpQ // 24`, `d = fpQ * sqrt(dx^2 + dy^2)` (capped past 100 px
 * on either axis), `newd = d * fpQ^2 // (d * dt // inertia + fpQ^2)` clipped
 * to `maxspeed * fpQ`, and the per-axis `loc - sign * newd` move.
 *
 * The switch is assumed set on every pass; the gate cases assert the
 * simulator state directly instead of calling the model.
 */
function model(steps: Step[], inertia = 5, maxSpeed = 48): ModelRow[] {
  const fpQ = FP_Q;
  const fpQ2 = fpQ * fpQ;
  let lastTick = MASK;
  let screenX = SCREEN_W;
  const out: ModelRow[] = [];
  for (const s of steps) {
    if (lastTick === MASK) lastTick = s.tick;
    const [locx, locy] = s.box;
    const [camx, camy] = s.camera;
    let dstx = locx;
    let dsty = locy;
    if (inertia !== 0) {
      const dx = (locx - camx) >>> 0;
      const dy = (locy - camy) >>> 0;
      const sx = dx >= 0x80000000 ? -1 : 1;
      const sy = dy >= 0x80000000 ? -1 : 1;
      const dxabs = dx >= 0x80000000 ? (-dx) >>> 0 : dx;
      const dyabs = dy >= 0x80000000 ? (-dy) >>> 0 : dy;
      const delta = (s.tick - lastTick) >>> 0;
      const dt = Math.floor(((delta * fpQ) >>> 0) / 24);
      let d: number;
      if (dxabs <= 100 && dyabs <= 100) {
        d = isqrt(fpQ2 * (dxabs * dxabs + dyabs * dyabs));
      } else {
        d = fpQ * 141;
      }
      if (d >= 1) {
        // The source multiplies at 32 bits (`d * dt`); the port's shift-add
        // mirrors that width, so do not truncate `dt` here.
        const den = (Math.floor(((d * dt) >>> 0) / inertia) + fpQ2) >>> 0;
        let newd = Math.floor((d * 65536) / den);
        if (d - newd >= maxSpeed * fpQ) newd = d - maxSpeed * fpQ;
        const qx = Math.floor((dxabs * 65536) / d);
        const qy = Math.floor((dyabs * 65536) / d);
        const newdx = Math.floor((newd * qx) / 65536);
        const newdy = Math.floor((newd * qy) / 65536);
        dstx = (locx - sx * newdx) >>> 0;
        dsty = (locy - sy * newdy) >>> 0;
      }
    }
    out.push({
      box: [dstx, dsty],
      right: (dstx + screenX) >>> 0,
      bottom: (dsty + SCREEN_H) >>> 0,
      tile: [dstx >>> 5, dsty >>> 5],
      screenX,
    });
    screenX = (2 * dstx + screenX - 2 * camx) >>> 0;
    lastTick = s.tick;
  }
  return out;
}

/** The width the helper carries into the pass after the last row. */
function finalScreenX(rows: ModelRow[], steps: Step[]): number {
  let screenX = SCREEN_W;
  rows.forEach((row, index) => {
    screenX = (2 * row.box[0] + row.screenX - 2 * steps[index].camera[0]) >>> 0;
  });
  return screenX;
}

interface RunResult {
  box: [number, number];
  right: number;
  bottom: number;
  tile: [number, number];
  tileWritten: boolean;
  screenX: number;
  lastTick: number;
  report: SimReport;
}

/**
 * Build, simulate, and snapshot one camera-follow program. Each element of
 * `steps` is applied on its own loop: the switch, the multiplayer flag, the
 * game tick, the camera position the helper reads, and the scratch box
 * `MoveLocation` would have produced.
 */
function run(steps: Step[], inertia = 5, maxSpeed = 48): RunResult {
  let state: CameraFollowState | null = null;
  const mod = buildModule(() => {
    const loop = Var(0);
    const tick = Var(0);

    const apply = (s: Step): void => {
      SetMemory(MULTIPLAYER_MODE_ADDR, SetTo, s.multiplayer);
      tick.set(s.tick);
      SetMemory(CAMERA_X_ADDR, SetTo, s.camera[0]);
      SetMemory(CAMERA_Y_ADDR, SetTo, s.camera[1]);
      SetMemory(BOX_ADDR, SetTo, s.box[0]);
      SetMemory(BOX_ADDR + 4, SetTo, s.box[1]);
      SetSwitch(SWITCH_ID, s.switchOn ? SwitchActionSet : SwitchActionClear);
    };

    steps.forEach((s, index) =>
      when(loop.eq(index), () => apply(s)),
    );
    emitVarStore(tick.id, { addr: GAME_TICK_ADDR }, SetTo);
    state = cameraFollow(TARGET_LOC, CAMERA_LOC, inertia, maxSpeed, SWITCH_ID);
    loop.iadd(1);
  });
  const report = runSim(mod.toJson(), steps.length);
  const follow = state as CameraFollowState | null;
  assert.ok(follow, "cameraFollow() must return state");
  return {
    box: [read(report, BOX_ADDR), read(report, BOX_ADDR + 4)] as [number, number],
    right: read(report, BOX_ADDR + 8),
    bottom: read(report, BOX_ADDR + 12),
    tile: [
      read(report, CAMERA_TILE_X_ADDR),
      read(report, CAMERA_TILE_Y_ADDR),
    ] as [number, number],
    tileWritten: written(report, CAMERA_TILE_X_ADDR),
    screenX: report.var_values[follow.screenX.id],
    lastTick: report.var_values[follow.lastTick.id],
    report,
  };
}

test("first pass snaps the box onto the camera", () => {
  // The box follows the camera: the first gated pass has no elapsed tick, so
  // it moves the box the whole remaining distance from the unit onto the view.
  const got = run([step()]);
  assert.deepEqual(got.box, [960, 800]);
  assert.deepEqual(got.tile, [30, 25]);
  assert.equal(got.lastTick, 0);
});

test("second pass eases a fraction of the remaining distance", () => {
  const steps = [step(), step({ tick: TICK_STEP })];
  const got = run(steps);
  const expected = model(steps).at(-1)!;
  assert.deepEqual(got.box, expected.box);
  assert.deepEqual(got.tile, expected.tile);
  assert.equal(got.right, expected.right);
  assert.equal(got.bottom, expected.bottom);
  assert.ok(got.box[0] > 960 && got.box[0] < 1000);
  assert.equal(got.box[0], 996);
});

test("third pass keeps easing without drifting", () => {
  const steps = [step(), step({ tick: TICK_STEP }), step({ tick: 2 * TICK_STEP })];
  const got = run(steps);
  const expected = model(steps).at(-1)!;
  assert.deepEqual(got.box, expected.box);
  // A stale accumulator makes later passes diverge wildly; pin the value.
  assert.deepEqual(got.box, [996, 800]);
  assert.deepEqual(got.tile, [31, 25]);
  assert.equal(got.right, 996 + 712);
});

test("maxspeed caps one pass", () => {
  // 160 px away on X is past the 100 px accuracy window, so the source falls
  // back to its overflow-safe norm and clips to maxspeed * fpQ.
  const steps = [
    step({ camera: [960, 800] }),
    step({ camera: [840, 800], tick: TICK_STEP }),
  ];
  const got = run(steps, 1);
  const expected = model(steps, 1).at(-1)!;
  assert.deepEqual(got.box, expected.box);
  assert.deepEqual(got.box, [895, 800]);
  assert.deepEqual(got.tile, expected.tile);
});

test("diagonal motion eases both axes", () => {
  // Same-sign case: dx and dy are both positive, so it cannot tell the
  // per-axis sign fix from the bug (see the mixed-sign case below).
  const steps = [
    step({ box: [500, 500], camera: [400, 420] }),
    step({ box: [500, 500], camera: [400, 420], tick: TICK_STEP }),
  ];
  const got = run(steps);
  const expected = model(steps).at(-1)!;
  assert.deepEqual(got.box, expected.box);
  assert.deepEqual(got.box, [438, 451]);
});

test("mixed-sign axes each keep their own sign", () => {
  // Regression test for the third fixed bug: X points right while Y points
  // down (and the mirror), so a single shared sign moves Y the wrong way by
  // twice the step (~98 px here). Reverting the fix fails this case.
  const downRight = [
    step({ box: [500, 420], camera: [400, 500] }),
    step({ box: [500, 420], camera: [400, 500], tick: TICK_STEP }),
  ];
  const upLeft = [
    step({ box: [420, 500], camera: [500, 400] }),
    step({ box: [420, 500], camera: [500, 400], tick: TICK_STEP }),
  ];
  for (const [steps, pinned] of [
    [downRight, [438, 469]],
    [upLeft, [469, 438]],
  ] as const) {
    const got = run(steps);
    assert.deepEqual(got.box, model(steps).at(-1)!.box);
    assert.deepEqual(got.box, pinned);
  }
});

test("long tick gap uses the full dword time step", () => {
  // `dt = 6144 * 256 // 24 == 65536` is the first value whose high half a
  // 16-bit multiply drops. The source then keeps the 10 px distance (970);
  // with the truncation the denominator collapses to `fpQ^2` and the camera
  // snaps to 960 instead of easing.
  const steps = [
    step({ box: [970, 800], camera: [960, 800] }),
    step({ box: [970, 800], camera: [960, 800], tick: 6144 }),
  ];
  const got = run(steps);
  assert.deepEqual(got.box, model(steps).at(-1)!.box);
  assert.deepEqual(got.box, [970, 800]);
});

test("target behind the camera moves backwards", () => {
  const steps = [
    step({ box: [960, 800], camera: [1000, 900] }),
    step({ box: [960, 800], camera: [1000, 900], tick: TICK_STEP }),
  ];
  const got = run(steps);
  const expected = model(steps).at(-1)!;
  assert.deepEqual(got.box, expected.box);
  assert.deepEqual(got.box, [982, 855]);
});

test("inertia zero jumps straight to the target", () => {
  const got = run([step()], 0);
  assert.deepEqual(got.box, [1000, 800]);
  assert.deepEqual(got.tile, [31, 25]);
  assert.equal(got.right, 1640);
  assert.equal(got.bottom, 800 + SCREEN_H);
});

test("inertia zero ignores the elapsed tick", () => {
  const got = run([step(), step({ tick: TICK_STEP })], 0);
  assert.deepEqual(got.box, [1000, 800]);
});

test("screen width is rebalanced from the camera travel", () => {
  // 2 * 1000 + 640 - 2 * 960 = 720.
  const got = run([step()], 0);
  assert.equal(got.right, 1000 + SCREEN_W);
  assert.equal(got.screenX, 720);
});

test("box right uses the carried width, not the correction", () => {
  const steps = [step(), step({ tick: TICK_STEP })];
  const got = run(steps);
  const rows = model(steps);
  assert.equal(got.screenX, finalScreenX(rows, steps));
  assert.equal(got.screenX, 712);
  assert.equal(got.right, 996 + SCREEN_W);
});

test("corrected width is used by the next pass", () => {
  const steps = [step(), step({ tick: TICK_STEP }), step({ tick: 2 * TICK_STEP })];
  const got = run(steps);
  const expected = model(steps).at(-1)!;
  assert.equal(got.right, expected.right);
  assert.equal(got.right, 996 + 712);
  assert.equal(got.screenX, 784);
});

test("width is untouched when the camera does not move", () => {
  const got = run([step({ box: [960, 800], camera: [960, 800] })]);
  assert.deepEqual(got.box, [960, 800]);
  assert.equal(got.screenX, SCREEN_W);
});

// Gated-off passes leave the seeded box corner where the test put it; the
// helper would otherwise snap it onto the camera. `tileWritten` is
// unambiguous because only the helper writes the camera tile dwords.
const OFF: Partial<Step> = { box: [2000, 1600], camera: [960, 800] };

test("cleared switch leaves the camera alone", () => {
  const got = run([step({ switchOn: false, ...OFF })]);
  assert.deepEqual(got.box, [2000, 1600]);
  assert.equal(got.tileWritten, false);
  assert.equal(got.screenX, SCREEN_W);
  assert.equal(got.lastTick, MASK);
});

test("single player gate blocks the camera", () => {
  const got = run([step({ multiplayer: 0, ...OFF })]);
  assert.deepEqual(got.box, [2000, 1600]);
  assert.equal(got.tileWritten, false);
  assert.equal(got.screenX, SCREEN_W);
  assert.equal(got.lastTick, MASK);
});

test("any non-zero multiplayer mode value runs the body", () => {
  const got = run([step({ multiplayer: 2 })]);
  assert.equal(got.tileWritten, true);
  assert.deepEqual(got.box, [960, 800]);
});

test("clearing the switch resets the follow state", () => {
  const steps = [
    step(),
    step({ switchOn: false, box: [2000, 1600], camera: [960, 800], tick: TICK_STEP }),
  ];
  const got = run(steps);
  assert.deepEqual(got.box, [2000, 1600]);
  assert.deepEqual(got.tile, [30, 25]);
  assert.equal(got.lastTick, MASK);
});

test("reenabling after a clear snaps to the camera", () => {
  const steps = [
    step(),
    step({ switchOn: false, box: [2000, 1600], camera: [960, 800], tick: TICK_STEP }),
    step({ box: [2000, 1000], camera: [1900, 1000], tick: 2 * TICK_STEP }),
  ];
  const got = run(steps);
  assert.equal(got.lastTick, 2 * TICK_STEP);
  assert.deepEqual(got.box, [1900, 1000]);
  assert.deepEqual(got.tile, [59, 31]);
});

function errorCode(fn: () => void): string {
  try {
    buildModule(fn);
  } catch (error) {
    assert.ok(error instanceof EudslError, `unexpected error: ${String(error)}`);
    return error.code;
  }
  assert.fail("expected an EudslError");
}

test("location ids are switch ids and unit ids are range checked", () => {
  assert.equal(errorCode(() => cameraFollow(256, CAMERA_LOC)), "EUDSL-RANGE");
  assert.equal(errorCode(() => cameraFollow(TARGET_LOC, -1)), "EUDSL-RANGE");
  assert.equal(
    errorCode(() => cameraFollow(TARGET_LOC, CAMERA_LOC, 5, 48, 256)),
    "EUDSL-RANGE",
  );
  assert.equal(
    errorCode(() => cameraFollow(TARGET_LOC, CAMERA_LOC, 5, 48, 0, { onUnit: 228 })),
    "EUDSL-RANGE",
  );
  assert.equal(
    errorCode(() => cameraFollow(TARGET_LOC, CAMERA_LOC, 5, 48, 0, { owner: 12 })),
    "EUDSL-RANGE",
  );
});

test("inertia and maxspeed are 32 bit", () => {
  assert.equal(
    errorCode(() => cameraFollow(TARGET_LOC, CAMERA_LOC, -1)),
    "EUDSL-RANGE",
  );
  assert.equal(
    errorCode(() => cameraFollow(TARGET_LOC, CAMERA_LOC, 5, 2 ** 32)),
    "EUDSL-RANGE",
  );
});

test("non-integers are type errors", () => {
  assert.equal(
    errorCode(() => cameraFollow(TARGET_LOC, CAMERA_LOC, 1.5)),
    "EUDSL-TYPE",
  );
  assert.equal(
    errorCode(() => cameraFollow(true as unknown as number, CAMERA_LOC)),
    "EUDSL-TYPE",
  );
  assert.equal(
    errorCode(() => cameraFollow(TARGET_LOC, CAMERA_LOC, 5, "48" as unknown as number)),
    "EUDSL-TYPE",
  );
});

test("defaults match the source plugin and the snake_case alias exists", () => {
  assert.equal(MAP_REVEALER_UNIT, 101);
  assert.equal(MAP_REVEALER_OWNER, 8);
  assert.equal(FP_Q, 256);
  assert.equal(camera_follow, cameraFollow);
});

// ---------------------------------------------------------------------------
// Static shape: the two working actions and the CurrentPlayer sandwich.
//
// The simulator models neither `MoveLocation` nor `CenterView`, so deleting
// either one leaves every behavioural case green. The emitted HIR is the
// contract here.
// ---------------------------------------------------------------------------

interface OpNode extends HirStmtNode {
  acttype?: number;
  dst_addr?: number;
  dest?: number;
  value?: number;
  mask?: number;
  unit?: number;
  modifier?: number;
  relocs?: Array<{ target_offset?: number; target_var?: number }>;
}

function staticModule(inertia = 5): string {
  return buildModule(() => {
    const loop = Var(0);
    const tick = Var(0);
    emitVarStore(tick.id, { addr: GAME_TICK_ADDR }, SetTo);
    cameraFollow(TARGET_LOC, CAMERA_LOC, inertia, 48, SWITCH_ID);
    loop.iadd(1);
  }).toJson();
}

function hirOps(hirJson: string): OpNode[] {
  const doc = JSON.parse(hirJson) as { module: { stmts: HirStmtNode[] } };
  return flattenStmts(doc.module.stmts).filter(
    (stmt) => stmt.kind === "act" || stmt.kind === "varstore",
  ) as OpNode[];
}

const STATIC_HIR = staticModule();

function acts(ops: OpNode[], acttype: number): OpNode[] {
  return ops.filter((op) => op.kind === "act" && op.acttype === acttype);
}

test("MoveLocation targets the map revealer in the search location", () => {
  const moves = acts(hirOps(STATIC_HIR), 38);
  assert.equal(moves.length, 1);
  const [move] = moves;
  assert.equal(move.dest, MAP_REVEALER_OWNER);
  assert.equal(move.value, CAMERA_LOC);
  assert.equal(move.mask, TARGET_LOC);
  assert.equal(move.unit, MAP_REVEALER_UNIT);
});

test("CenterView targets the scratch box location", () => {
  const centers = acts(hirOps(STATIC_HIR), 10);
  assert.equal(centers.length, 1);
  assert.equal(centers[0].mask, CAMERA_LOC);
});

test("CurrentPlayer is written before the view and restored after", () => {
  const ops = hirOps(STATIC_HIR);
  const cpIndex = ops.findIndex(
    (op) => op.kind === "varstore" && op.dst_addr === CP_ADDR,
  );
  const centerIndex = ops.findIndex(
    (op) => op.kind === "act" && op.acttype === 10,
  );
  const restores = ops
    .map((op, index) => ({ op, index }))
    .filter(
      ({ op }) =>
        op.kind === "act" &&
        op.acttype === 45 &&
        op.dest === EPD_CURRENT_PLAYER,
    );
  assert.equal(restores.length, 1);
  const [{ op: restore, index: restoreIndex }] = restores;
  assert.ok(cpIndex >= 0 && centerIndex >= 0, "CP store and CenterView exist");
  assert.ok(cpIndex < centerIndex, "CP store comes before CenterView");
  assert.ok(centerIndex < restoreIndex, "restore comes after CenterView");
  assert.equal(restore.value, 0);
  assert.equal(restore.modifier, SetTo);
  // The stored value is the cached read of `dwUserPlayerId`, not a literal.
  // The gate-scoped `once` fills the cache from the shared reader body (one
  // body per distinct EPD, whose 32 bit-test conditions carry that EPD as the
  // literal `player` field); the CP store copies the cache var, so the reader
  // call is not on the per-pass path.
  const userEpd = EPD(USER_PLAYER_ADDR) >>> 0;
  const cpSrc = (ops[cpIndex] as unknown as { src_var?: number }).src_var;
  const doc = JSON.parse(STATIC_HIR) as {
    module: {
      stmts: HirStmtNode[];
      funcs?: Array<{ id: number; body: HirStmtNode[] }>;
    };
  };
  const byId = new Map((doc.module.funcs ?? []).map((f) => [f.id, f]));
  const stmts = flattenStmts(doc.module.stmts);
  const writers = stmts.filter(
    (stmt) =>
      stmt.kind === "vartransfer" &&
      (stmt as { dst_var?: number }).dst_var === cpSrc,
  );
  assert.equal(writers.length, 1, "the CP store copies the cached player id");
  const onces = stmts.filter((stmt) => stmt.kind === "once");
  assert.equal(onces.length, 1, "one gate-scoped once cache");
  const onceBody = (onces[0] as unknown as { body: HirStmtNode[] }).body;
  assert.ok(onceBody.includes(writers[0]), "the cache is filled by the once");
  const callSrc = (writers[0] as unknown as { src_var?: number }).src_var;
  const calls = stmts.filter(
    (stmt) =>
      stmt.kind === "call" && (stmt as { dst?: number }).dst === callSrc,
  );
  assert.equal(calls.length, 1, "the cache copies one call result");
  const conds = (
    byId.get((calls[0] as unknown as { func: number }).func)?.body ?? []
  ).flatMap(
    (stmt) => (stmt as { conds?: Array<{ player?: number }> }).conds ?? [],
  );
  assert.equal(conds.length, 32);
  assert.deepEqual(new Set(conds.map((cond) => cond.player)), new Set([userEpd]));
});

// ---------------------------------------------------------------------------
// Cost: per-pass deltas over identical scaffolding (S4).
// ---------------------------------------------------------------------------

function costModule(inertia: number | null, multiplayer = 1): string {
  return buildModule(() => {
    const loop = Var(0);
    const tick = Var(0);
    SetMemory(MULTIPLAYER_MODE_ADDR, SetTo, multiplayer);
    tick.set(0);
    SetMemory(CAMERA_X_ADDR, SetTo, 960);
    SetMemory(CAMERA_Y_ADDR, SetTo, 800);
    SetMemory(BOX_ADDR, SetTo, 1000);
    SetMemory(BOX_ADDR + 4, SetTo, 800);
    SetSwitch(SWITCH_ID, SwitchActionSet);
    emitVarStore(tick.id, { addr: GAME_TICK_ADDR }, SetTo);
    if (inertia !== null) {
      cameraFollow(TARGET_LOC, CAMERA_LOC, inertia, 48, SWITCH_ID);
    }
    loop.iadd(1);
  }).toJson();
}

const BASE_STATIC = runStatic(costModule(null)).static_cost;
const BASE_EXEC = runSim(costModule(null), 1).executed_cost;
// The cache only pays off from the second loop, so the seeding baseline is
// kept for both pass counts the tests observe.
const BASE_EXEC_2 = runSim(costModule(null), 2).executed_cost;

/** Static and executed cost deltas over the seeding-only module. */
function costDelta(hirJson: string, loops = 1): [number[], number[]] {
  const staticCost = runStatic(hirJson).static_cost;
  const baseExec = loops === 1 ? BASE_EXEC : BASE_EXEC_2;
  const executed = runSim(hirJson, loops).executed_cost;
  return [
    [
      staticCost.triggers - BASE_STATIC.triggers,
      staticCost.conditions - BASE_STATIC.conditions,
      staticCost.actions - BASE_STATIC.actions,
      staticCost.total_bytes - BASE_STATIC.total_bytes,
    ],
    [
      executed.triggers - baseExec.triggers,
      executed.conditions - baseExec.conditions,
      executed.actions - baseExec.actions,
    ],
  ];
}

test("gate-open per-pass cost", () => {
  // Measured deltas; the scaffolding is identical on both sides, so these are
  // attributable to the helper rather than to the seeding.
  //
  // #97 re-baselined both rows: full-dword reads emit one reader body per
  // distinct EPD (a literal `player` field cannot be patched without changing
  // the body for every site). The previous pins came from a cross-address body
  // whose per-site patches clobbered each other, so a module reading N
  // addresses returned the last-patched one for every read. Paying one 33T
  // body per address costs +137T static here but is correct; it is still
  // 29T/32C/21A/69,828B below the inlined master
  // (1328T / 650C / 3134A / 3,197,872B).
  // Cycle 26: 1-trigger const shifts and in-place unary ops cut the
  // `inertia=5` pin from 1299T/618C static and 1381T/611C executed to
  // 1213T/532C static and 1299T/525C executed. Triggers and conditions
  // decreased; actions rose slightly (3,113 -> 3,133 static and
  // 21,003 -> 21,356 executed), so this is not a pure shrink.
  // Cycle 27 (#02 mul chunk-16 + #04 isqrt u-recurrence): #02 adds the
  // 32-bit chunk-16 gate (+2T/+1C/+2A static, +4,816 B) while cutting exec;
  // #04 replaces isqrt's trial rebuild with u=2*root (-15T/+79A/-36,120 B
  // static and -58T/-1,931A exec).
  // Cycle 6 delegates `mulConst` to `Var.mul`, so `delta * FP_Q` is one masked
  // const-shift instead of a 24-trigger ladder: -24T/-24C static and
  // -22T/-24C executed, +30A/+182A (the surviving trigger's action lanes
  // absorb the work), and -57,796 B. Landing at 1176T/509C/3244A static
  // (2,831,848 B) and 1159T/486C/18,602A executed.
  // RT1 then moves the `dwUserPlayerId` read into a gate-scoped `once` cache.
  // The first pass still performs that read, so its trigger and condition
  // counts are unchanged at 1159T/486C; only the action lanes merge (-67A).
  // Static is the deliberate cost-pin update of one `once` gate: +1T, no
  // condition change, -3A, +2,408 B.
  assert.deepEqual(costDelta(costModule(5)), [
    [1177, 509, 3241, 2_834_256],
    [1159, 486, 18_535],
  ]);
  assert.deepEqual(costDelta(costModule(0)), [
    [223, 198, 376, 537_000],
    [271, 229, 1_164],
  ]);
});

test("steady-state second pass skips the cached reader", () => {
  // The parent commit's second pass over the same scaffolding was
  // 1,156T/486C/18,533A; the once cache removes its 32 bit-test triggers plus
  // the call overhead, landing at 1,121T/454C/18,463A -- the measured
  // -35T/-32C/-70A. The no-smoothing row drops 268T -> 233T.
  assert.deepEqual(costDelta(costModule(5), 2), [
    [1177, 509, 3241, 2_834_256],
    [1_121, 454, 18_463],
  ]);
  assert.deepEqual(costDelta(costModule(0), 2), [
    [223, 198, 376, 537_000],
    [233, 197, 1_092],
  ]);
});

test("closed gate only pays for the two gate conditions", () => {
  const [staticDelta, executedDelta] = costDelta(costModule(5, 0));
  // The body (including the `once`) is emitted either way; the chain jump
  // skips it at runtime, so the gate-scoped cache never fires here.
  assert.deepEqual(staticDelta, [1177, 509, 3241, 2_834_256]);
  assert.deepEqual(executedDelta, [3, 2, 0]);
});
