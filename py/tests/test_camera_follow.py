"""Camera-follow helper (issue #22): a per-loop port of euddraft's cammove.py.

Two oracles pin the emitted body:

* the static shape and the run gates (switch set/cleared, multiplayer mode),
  asserted directly against the simulated memory writes;
* the integer smoothing, re-implemented in `_model` from the original plugin's
  `afterTriggerExec` arithmetic and compared value-for-value.

What the simulator does not model, and how the cases work around it:

* `MoveLocation` is not simulated, so the scratch box's top-left corner is
  seeded directly at the location-table address the helper reads (`box_addr`);
  the helper therefore eases toward a box the test placed, exactly as a real
  `MoveLocation` would leave it.
* `CenterView` is not simulated, so the post-center camera read returns the
  seeded camera X. That is what the widescreen `screenX` correction is
  asserted against: the helper derives the visible width from how far the
  camera actually travelled.

A build-time accumulator that is not cleared every pass silently drifts from
the second loop on (the kernels are emitted inside the per-loop body, so a
zero *initializer* only holds for pass 1); the multi-loop cases below are the
regression tests for that. The per-axis sign fix is only visible when the two
axes point in *opposite* directions, so the mixed-sign cases below (not the
same-sign diagonal cases) are its regression tests.
"""

from __future__ import annotations

import json
import math
import subprocess
import textwrap
import unittest
from typing import Any

from py import (
    SetMemory,
    SetSwitch,
    SetTo,
    SwitchActionClear,
    SwitchActionSet,
    Var,
    build_module,
    emitVarStore,
    when,
)
from py.camera import (
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
)
from py.cp import CP_ADDR
from py.tests.helpers import REPO_ROOT, flatten_stmts, run_sim, run_static
from py.view import EPD, EPD_CURRENT_PLAYER, EudslError

EPD_BASE = 0x58A364

SWITCH_ID = 3
CAMERA_LOC = 10
TARGET_LOC = 11
BOX_ADDR = LOCATION_TABLE_ADDR + LOCATION_STRIDE_DWORDS * 4 * CAMERA_LOC

# The 1 ms tick unit the source plugin uses: ticks * fpQ // 24, i.e. 24 game
# ticks == 256 fixed-point units of a "one second" time base.
TICK_STEP = 24


def _epd_key(addr: int) -> str:
    """Simulator key for the EPD index of `addr` (negative EPDs are u32)."""
    return str(((addr - EPD_BASE) // 4) & 0xFFFFFFFF)


def _read(report: dict[str, Any], addr: int) -> int:
    return report["external_epds"].get(_epd_key(addr), 0)


def _written(report: dict[str, Any], addr: int) -> bool:
    return _epd_key(addr) in report["external_epds"]


def _step(**overrides: Any) -> dict[str, Any]:
    step = {
        "multiplayer": 1,
        "switch": True,
        "tick": 0,
        "camera": (960, 800),
        "box": (1000, 800),
    }
    step.update(overrides)
    return step


def _model(
    steps: list[dict[str, Any]], inertia: int = 5, maxspeed: int = 48
) -> list[dict[str, int]]:
    """Reference implementation of the plugin's fixed-point easing.

    Mirrors `afterTriggerExec` step for step: `fpQ = 256`, `dt = ticks * fpQ //
    24`, `d = fpQ * sqrt(dx^2 + dy^2)` (capped at `fpQ * 141` past 100 px on
    either axis), `newd = d * fpQ^2 // (d * dt // inertia + fpQ^2)` clipped to
    `maxspeed * fpQ`, and the per-axis `loc - sign * newd` move.

    The switch is assumed set on every pass; the gate cases assert the
    simulator state directly instead of calling the model.
    """
    mask = 0xFFFFFFFF
    fp_q = FP_Q
    fp_q2 = fp_q * fp_q
    last_tick = mask
    screen_x = SCREEN_W
    out: list[dict[str, int]] = []
    for step in steps:
        tick = step["tick"]
        locx, locy = step["box"]
        camx, camy = step["camera"]
        if last_tick == mask:
            last_tick = tick
        dstx, dsty = locx, locy
        if inertia != 0:
            dx = (locx - camx) & mask
            dy = (locy - camy) & mask
            sx = -1 if dx >= 0x80000000 else 1
            sy = -1 if dy >= 0x80000000 else 1
            dxabs = (-dx) & mask if dx >= 0x80000000 else dx
            dyabs = (-dy) & mask if dy >= 0x80000000 else dy
            delta = (tick - last_tick) & mask
            dt = ((delta * fp_q) & mask) // 24
            if dxabs <= 100 and dyabs <= 100:
                d = math.isqrt(fp_q2 * (dxabs * dxabs + dyabs * dyabs))
            else:
                d = fp_q * 141
            if d >= 1:
                # The source multiplies at 32 bits (`d * dt`); the port's
                # shift-add mirrors that width, so do not truncate `dt` here.
                den = (((d * dt) & mask) // inertia + fp_q2) & mask
                newd = (d << 16) // den
                if d - newd >= maxspeed * fp_q:
                    newd = d - maxspeed * fp_q
                qx = (dxabs << 16) // d
                qy = (dyabs << 16) // d
                newdx = (newd * qx) >> 16
                newdy = (newd * qy) >> 16
                dstx = (locx - sx * newdx) & mask
                dsty = (locy - sy * newdy) & mask
        out.append(
            {
                "box": (dstx, dsty),
                "right": (dstx + screen_x) & mask,
                "bottom": (dsty + SCREEN_H) & mask,
                "tile": (dstx >> 5, dsty >> 5),
                "screen_x": screen_x,
            }
        )
        screen_x = (2 * dstx + screen_x - 2 * camx) & mask
        last_tick = tick
    return out


def _model_final(
    steps: list[dict[str, Any]], inertia: int = 5, maxspeed: int = 48
) -> dict[str, Any]:
    """Model result of the last step, plus the state it leaves behind."""
    trail = _model(steps, inertia, maxspeed)
    last = trail[-1]
    return {
        "box": last["box"],
        "right": last["right"],
        "bottom": last["bottom"],
        "tile": last["tile"],
        "screen_x": _screen_x_after(trail, steps),
        "last_tick": steps[-1]["tick"],
    }


def _screen_x_after(
    trail: list[dict[str, int]], steps: list[dict[str, Any]]
) -> int:
    """The width the helper carries into the next pass."""
    screen_x = SCREEN_W
    for step, row in zip(steps, trail):
        dstx = row["box"][0]
        screen_x = (2 * dstx + screen_x - 2 * step["camera"][0]) & 0xFFFFFFFF
    return screen_x


def _run(
    steps: list[dict[str, Any]], *, inertia: int = 5, maxspeed: int = 48
) -> dict[str, Any]:
    """Build, simulate, and snapshot one camera-follow program.

    Each element of `steps` is applied on its own loop: the switch, the
    multiplayer flag, the game tick, the camera position the helper reads, and
    the scratch box `MoveLocation` would have produced.
    """
    slots: dict[str, Any] = {}

    def prog() -> None:
        loop = Var(0)
        tick = Var(0)

        def apply(step: dict[str, Any]) -> None:
            SetMemory(MULTIPLAYER_MODE_ADDR, SetTo, step["multiplayer"])
            tick.set(step["tick"])
            SetMemory(CAMERA_X_ADDR, SetTo, step["camera"][0])
            SetMemory(CAMERA_Y_ADDR, SetTo, step["camera"][1])
            SetMemory(BOX_ADDR, SetTo, step["box"][0])
            SetMemory(BOX_ADDR + 4, SetTo, step["box"][1])
            SetSwitch(
                SWITCH_ID,
                SwitchActionSet if step["switch"] else SwitchActionClear,
            )

        for index, step in enumerate(steps):
            when(loop.eq(index), lambda step=step: apply(step))
        emitVarStore(tick.id, {"addr": GAME_TICK_ADDR}, SetTo)
        slots["state"] = camera_follow(
            TARGET_LOC,
            CAMERA_LOC,
            inertia=inertia,
            maxspeed=maxspeed,
            switch_id=SWITCH_ID,
        )
        loop.iadd(1)

    report = run_sim(build_module(prog).to_json(), len(steps))
    state = slots["state"]
    return {
        "box": (_read(report, BOX_ADDR), _read(report, BOX_ADDR + 4)),
        "right": _read(report, BOX_ADDR + 8),
        "bottom": _read(report, BOX_ADDR + 12),
        "tile": (_read(report, CAMERA_TILE_X_ADDR), _read(report, CAMERA_TILE_Y_ADDR)),
        "tile_written": _written(report, CAMERA_TILE_X_ADDR),
        "screen_x": report["var_values"][state.screen_x.id],
        "last_tick": report["var_values"][state.last_tick.id],
        "report": report,
    }


class CameraFollowEasingTest(unittest.TestCase):
    def test_first_pass_snaps_the_box_onto_the_camera(self) -> None:
        # The box follows the camera, not the unit: the first gated pass has
        # no elapsed tick, so it moves the box the whole remaining distance
        # from the unit's corner onto the current view.
        got = _run([_step()])
        self.assertEqual(got["box"], (960, 800))
        self.assertEqual(got["tile"], (30, 25))
        self.assertEqual(got["last_tick"], 0)

    def test_second_pass_eases_a_fraction_of_the_remaining_distance(self) -> None:
        steps = [_step(), _step(tick=TICK_STEP)]
        got = _run(steps)
        expected = _model_final(steps)
        self.assertEqual(got["box"], expected["box"])
        self.assertEqual(got["tile"], expected["tile"])
        self.assertEqual(got["right"], expected["right"])
        self.assertEqual(got["bottom"], expected["bottom"])
        # 40 px to go: the eased pass keeps a fraction of it instead of
        # snapping, and never overshoots the target.
        self.assertGreater(got["box"][0], 960)
        self.assertLess(got["box"][0], 1000)
        self.assertEqual(got["box"][0], 996)

    def test_third_pass_keeps_easing_without_drifting(self) -> None:
        steps = [
            _step(),
            _step(tick=TICK_STEP),
            _step(tick=2 * TICK_STEP),
        ]
        got = _run(steps)
        expected = _model_final(steps)
        self.assertEqual(got["box"], expected["box"])
        # A stale accumulator makes later passes diverge wildly; pin the
        # value the model predicts instead of only "no crash".
        self.assertEqual(got["box"], (996, 800))
        self.assertEqual(got["tile"], (31, 25))
        # The pass-3 box right edge uses the 712 corrected at the end of pass
        # 2, so the widescreen fix-up reaches the next pass's box edges.
        self.assertEqual(got["right"], 996 + 712)

    def test_maxspeed_caps_one_pass(self) -> None:
        # 160 px away on X is past the 100 px accuracy window, so the source
        # falls back to its overflow-safe norm and then clips the step to
        # maxspeed * fpQ before scaling it back onto the axis.
        steps = [
            _step(camera=(960, 800)),
            _step(camera=(840, 800), tick=TICK_STEP),
        ]
        got = _run(steps, inertia=1)
        expected = _model_final(steps, inertia=1)
        self.assertEqual(got["box"], expected["box"])
        self.assertEqual(got["box"], (895, 800))
        self.assertEqual(got["tile"], expected["tile"])

    def test_diagonal_motion_eases_both_axes(self) -> None:
        # Same-sign case: dx and dy are both positive, so it cannot tell the
        # per-axis sign fix from the bug (see the mixed-sign cases below).
        steps = [
            _step(box=(500, 500), camera=(400, 420)),
            _step(box=(500, 500), camera=(400, 420), tick=TICK_STEP),
        ]
        got = _run(steps)
        expected = _model_final(steps)
        self.assertEqual(got["box"], expected["box"])
        self.assertEqual(got["box"], (438, 451))

    def test_mixed_sign_axes_each_keep_their_own_sign(self) -> None:
        # Regression test for the third fixed bug: X points right while Y
        # points down, so a single shared sign moves Y the wrong way by twice
        # the step (~98 px here). Reverting the fix fails this case.
        down_right = [
            _step(box=(500, 420), camera=(400, 500)),
            _step(box=(500, 420), camera=(400, 500), tick=TICK_STEP),
        ]
        up_left = [
            _step(box=(420, 500), camera=(500, 400)),
            _step(box=(420, 500), camera=(500, 400), tick=TICK_STEP),
        ]
        for steps, pinned in ((down_right, (438, 469)), (up_left, (469, 438))):
            got = _run(steps)
            self.assertEqual(got["box"], _model_final(steps)["box"])
            self.assertEqual(got["box"], pinned)

    def test_long_tick_gap_uses_the_full_dword_time_step(self) -> None:
        # `dt = 6144 * 256 // 24 == 65536` is the first value whose high half
        # a 16-bit multiply drops. The source then keeps the 10 px distance
        # (970); with the truncation the denominator collapses to `fpQ^2` and
        # the camera snaps to 960 instead of easing.
        steps = [
            _step(box=(970, 800), camera=(960, 800)),
            _step(box=(970, 800), camera=(960, 800), tick=6144),
        ]
        got = _run(steps)
        self.assertEqual(got["box"], _model_final(steps)["box"])
        self.assertEqual(got["box"], (970, 800))

    def test_target_behind_the_camera_moves_backwards(self) -> None:
        steps = [
            _step(box=(960, 800), camera=(1000, 900)),
            _step(box=(960, 800), camera=(1000, 900), tick=TICK_STEP),
        ]
        got = _run(steps)
        expected = _model_final(steps)
        self.assertEqual(got["box"], expected["box"])
        self.assertEqual(got["box"], (982, 855))

    def test_inertia_zero_jumps_straight_to_the_target(self) -> None:
        got = _run([_step()], inertia=0)
        self.assertEqual(got["box"], (1000, 800))
        self.assertEqual(got["tile"], (31, 25))
        self.assertEqual(got["right"], 1640)
        self.assertEqual(got["bottom"], 800 + SCREEN_H)

    def test_inertia_zero_ignores_the_elapsed_tick(self) -> None:
        steps = [_step(), _step(tick=TICK_STEP)]
        got = _run(steps, inertia=0)
        self.assertEqual(got["box"], (1000, 800))


class CameraFollowWidescreenTest(unittest.TestCase):
    def test_screen_width_is_rebalanced_from_the_camera_travel(self) -> None:
        # The box right edge uses the width carried into the pass (640), then
        # the correction re-derives it from dstx and the post-center camera:
        # 2 * 1000 + 640 - 2 * 960 = 720.
        got = _run([_step()], inertia=0)
        self.assertEqual(got["right"], 1000 + SCREEN_W)
        self.assertEqual(got["screen_x"], 720)

    def test_box_right_uses_the_carried_width_not_the_correction(self) -> None:
        # Pass 1 snapped onto the camera (960), so its correction keeps 640.
        # Pass 2 writes `dstx + 640` and only then narrows the width to
        # `2 * 996 + 640 - 2 * 960 = 712` for the pass after it.
        steps = [_step(), _step(tick=TICK_STEP)]
        got = _run(steps)
        expected = _model_final(steps)
        self.assertEqual(got["screen_x"], expected["screen_x"])
        self.assertEqual(got["screen_x"], 712)
        self.assertEqual(got["right"], 996 + SCREEN_W)

    def test_corrected_width_is_used_by_the_next_pass(self) -> None:
        steps = [_step(), _step(tick=TICK_STEP), _step(tick=2 * TICK_STEP)]
        got = _run(steps)
        expected = _model_final(steps)
        self.assertEqual(got["right"], expected["right"])
        self.assertEqual(got["right"], 996 + 712)
        # 2 * 996 + 712 - 2 * 960 = 784 carried out of the third pass.
        self.assertEqual(got["screen_x"], 784)

    def test_width_is_untouched_when_the_camera_does_not_move(self) -> None:
        got = _run([_step(box=(960, 800), camera=(960, 800))])
        self.assertEqual(got["box"], (960, 800))
        self.assertEqual(got["screen_x"], SCREEN_W)


class CameraFollowGateTest(unittest.TestCase):
    # A gated-off pass leaves the seeded box corner where the test put it; the
    # helper would otherwise snap that corner onto the camera. `tile_written`
    # is unambiguous because only the helper writes the camera tile dwords.
    OFF_STEP = {"box": (2000, 1600), "camera": (960, 800)}

    def test_cleared_switch_leaves_the_camera_alone(self) -> None:
        got = _run([_step(switch=False, **self.OFF_STEP)])
        self.assertEqual(got["box"], (2000, 1600))
        self.assertFalse(got["tile_written"])
        self.assertEqual(got["screen_x"], SCREEN_W)
        self.assertEqual(got["last_tick"], 0xFFFFFFFF)

    def test_single_player_gate_blocks_the_camera(self) -> None:
        got = _run([_step(multiplayer=0, **self.OFF_STEP)])
        self.assertEqual(got["box"], (2000, 1600))
        self.assertFalse(got["tile_written"])
        self.assertEqual(got["screen_x"], SCREEN_W)
        self.assertEqual(got["last_tick"], 0xFFFFFFFF)

    def test_any_non_zero_multiplayer_mode_value_runs_the_body(self) -> None:
        # The gate is `Memory(0x57F0B4, AtLeast, 1)`, like the source's
        # `Exactly 0` early return: any non-zero multiplayer flag follows.
        got = _run([_step(multiplayer=2)])
        self.assertTrue(got["tile_written"])
        self.assertEqual(got["box"], (960, 800))

    def test_clearing_the_switch_resets_the_follow_state(self) -> None:
        steps = [
            _step(),
            _step(switch=False, box=(2000, 1600), camera=(960, 800), tick=TICK_STEP),
        ]
        got = _run(steps)
        # The cleared pass must not move the camera ...
        self.assertEqual(got["box"], (2000, 1600))
        self.assertEqual(got["tile"], (30, 25))
        # ... and must drop the remembered tick so a later enable snaps.
        self.assertEqual(got["last_tick"], 0xFFFFFFFF)

    def test_reenabling_after_a_clear_snaps_to_the_camera(self) -> None:
        # The new corner is within the 100 px accuracy window so the eased
        # (dt == 0) step lands exactly on the camera without the overflow
        # approximation's one-pixel loss.
        steps = [
            _step(),
            _step(switch=False, box=(2000, 1600), camera=(960, 800), tick=TICK_STEP),
            _step(box=(2000, 1000), camera=(1900, 1000), tick=2 * TICK_STEP),
        ]
        got = _run(steps)
        # lastTick was reset, so pass 3 starts from dt == 0 and lands on the
        # camera instead of easing from a stale tick.
        self.assertEqual(got["last_tick"], 2 * TICK_STEP)
        self.assertEqual(got["box"], (1900, 1000))
        self.assertEqual(got["tile"], (59, 31))


class CameraFollowValidationTest(unittest.TestCase):
    def _error(self, **kwargs: Any) -> EudslError:
        with self.assertRaises(EudslError) as caught:

            def prog() -> None:
                camera_follow(
                    kwargs.pop("target_location", TARGET_LOC),
                    kwargs.pop("camera_location", CAMERA_LOC),
                    **kwargs,
                )

            build_module(prog)
        return caught.exception

    def test_location_ids_are_switch_ids_and_unit_ids_are_range_checked(self) -> None:
        self.assertEqual(self._error(target_location=256).code, "EUDSL-RANGE")
        self.assertEqual(self._error(camera_location=-1).code, "EUDSL-RANGE")
        self.assertEqual(self._error(switch_id=256).code, "EUDSL-RANGE")
        self.assertEqual(self._error(on_unit=228).code, "EUDSL-RANGE")
        self.assertEqual(self._error(owner=12).code, "EUDSL-RANGE")

    def test_inertia_and_maxspeed_are_32_bit(self) -> None:
        self.assertEqual(self._error(inertia=-1).code, "EUDSL-RANGE")
        self.assertEqual(self._error(maxspeed=2**32).code, "EUDSL-RANGE")

    def test_non_integers_are_type_errors(self) -> None:
        self.assertEqual(self._error(inertia=1.5).code, "EUDSL-TYPE")
        self.assertEqual(self._error(target_location=True).code, "EUDSL-TYPE")
        self.assertEqual(self._error(maxspeed="48").code, "EUDSL-TYPE")

    def test_defaults_match_the_source_plugin(self) -> None:
        self.assertEqual(MAP_REVEALER_UNIT, 101)
        self.assertEqual(MAP_REVEALER_OWNER, 8)
        self.assertEqual(FP_Q, 256)

    def test_canonical_camel_case_alias_exists(self) -> None:
        self.assertIs(cameraFollow, camera_follow)


# ---------------------------------------------------------------------------
# Static shape: the two working actions and the CurrentPlayer sandwich.
#
# The simulator models neither `MoveLocation` nor `CenterView`, so deleting
# either one leaves every behavioural case green. The emitted HIR is the
# contract here: act 38 must move the scratch box onto the map revealer inside
# the search location, act 10 must center that box, and CurrentPlayer must be
# written before the center and restored to 0 after it.
# ---------------------------------------------------------------------------


def _static_module(*, inertia: int = 5) -> str:
    def prog() -> None:
        loop = Var(0)
        tick = Var(0)
        emitVarStore(tick.id, {"addr": GAME_TICK_ADDR}, SetTo)
        camera_follow(TARGET_LOC, CAMERA_LOC, inertia, 48, SWITCH_ID)
        loop.iadd(1)

    return build_module(prog).to_json()


def _hir_ops(hir_json: str) -> list[dict[str, Any]]:
    """Emitted actions and var stores, flattened in execution order."""
    return [
        stmt
        for stmt in flatten_stmts(json.loads(hir_json)["module"]["stmts"])
        if stmt["kind"] in ("act", "varstore")
    ]


class CameraFollowStaticHirTest(unittest.TestCase):
    def setUp(self) -> None:
        self.hir = _static_module()
        self.ops = _hir_ops(self.hir)

    def _acts(self, acttype: int) -> list[dict[str, Any]]:
        return [
            op
            for op in self.ops
            if op["kind"] == "act" and op["acttype"] == acttype
        ]

    def test_move_location_targets_the_map_revealer_in_the_search_location(self) -> None:
        (move,) = self._acts(38)
        self.assertEqual(move["dest"], MAP_REVEALER_OWNER)
        self.assertEqual(move["value"], CAMERA_LOC)
        self.assertEqual(move["mask"], TARGET_LOC)
        self.assertEqual(move["unit"], MAP_REVEALER_UNIT)

    def test_center_view_targets_the_scratch_box_location(self) -> None:
        (center,) = self._acts(10)
        self.assertEqual(center["mask"], CAMERA_LOC)

    def test_current_player_is_written_before_the_view_and_restored_after(self) -> None:
        cp_index = next(
            index
            for index, op in enumerate(self.ops)
            if op["kind"] == "varstore" and op["dst_addr"] == CP_ADDR
        )
        center_index = next(
            index
            for index, op in enumerate(self.ops)
            if op["kind"] == "act" and op["acttype"] == 10
        )
        restores = [
            (index, op)
            for index, op in enumerate(self.ops)
            if op["kind"] == "act"
            and op["acttype"] == 45
            and op["dest"] == EPD_CURRENT_PLAYER
        ]
        self.assertEqual(len(restores), 1)
        restore_index, restore = restores[0]
        self.assertLess(cp_index, center_index)
        self.assertLess(center_index, restore_index)
        self.assertEqual(restore["value"], 0)
        self.assertEqual(restore["modifier"], SetTo)
        # The stored value is the cached read of `dwUserPlayerId`, not a
        # literal. The gate-scoped `once` fills the cache from the shared reader
        # body (one body per distinct EPD, whose 32 bit-test conditions carry
        # that EPD as the literal `player` field); the CP store copies the cache
        # var, so the reader call is not on the per-pass path.
        user_epd = EPD(USER_PLAYER_ADDR) & 0xFFFFFFFF
        cp_src = self.ops[cp_index]["src_var"]
        doc = json.loads(self.hir)
        stmts = flatten_stmts(doc["module"]["stmts"])
        funcs = {f["id"]: f for f in doc["module"].get("funcs", [])}
        writers = [
            stmt
            for stmt in stmts
            if stmt["kind"] == "vartransfer" and stmt.get("dst_var") == cp_src
        ]
        self.assertEqual(len(writers), 1, "the CP store copies the cached player id")
        onces = [stmt for stmt in stmts if stmt["kind"] == "once"]
        self.assertEqual(len(onces), 1, "one gate-scoped once cache")
        self.assertIn(writers[0], onces[0]["body"], "the cache is filled by the once")
        call_src = writers[0]["src_var"]
        calls = [
            stmt for stmt in stmts if stmt["kind"] == "call" and stmt.get("dst") == call_src
        ]
        self.assertEqual(len(calls), 1, "the cache copies one call result")
        conds = [
            cond
            for stmt in funcs[calls[0]["func"]]["body"]
            for cond in (stmt.get("conds") or [])
        ]
        self.assertEqual(len(conds), 32)
        self.assertEqual({cond["player"] for cond in conds}, {user_epd})


# ---------------------------------------------------------------------------
# Cost: per-pass deltas over identical scaffolding (S4).
# ---------------------------------------------------------------------------

_COST_KEYS_STATIC = ("triggers", "conditions", "actions", "total_bytes")
_COST_KEYS_EXEC = ("triggers", "conditions", "actions")


def _cost_module(*, inertia: int | None, multiplayer: int = 1) -> str:
    """One camera-follow instance plus fixed seeding, or the seeding alone."""

    def prog() -> None:
        loop = Var(0)
        tick = Var(0)
        SetMemory(MULTIPLAYER_MODE_ADDR, SetTo, multiplayer)
        tick.set(0)
        SetMemory(CAMERA_X_ADDR, SetTo, 960)
        SetMemory(CAMERA_Y_ADDR, SetTo, 800)
        SetMemory(BOX_ADDR, SetTo, 1000)
        SetMemory(BOX_ADDR + 4, SetTo, 800)
        SetSwitch(SWITCH_ID, SwitchActionSet)
        emitVarStore(tick.id, {"addr": GAME_TICK_ADDR}, SetTo)
        if inertia is not None:
            camera_follow(TARGET_LOC, CAMERA_LOC, inertia, 48, SWITCH_ID)
        loop.iadd(1)

    return build_module(prog).to_json()


class CameraFollowCostTest(unittest.TestCase):
    """Pin the per-pass cost as a delta over identical scaffolding.

    ``static`` is one emitted instance (``dump-ir --json --no-exec``);
    ``executed`` is the last simulated pass. The scaffolding is byte-identical
    on both sides, so the difference is attributable to the helper rather than
    to the seeding the tests happen to use. Keep the four static and three
    executed counters in sync with the docstring.
    """

    def setUp(self) -> None:
        base = _cost_module(inertia=None)
        self.base_static = run_static(base)["static_cost"]
        # The cache only pays off from the second loop, so keep the seeding
        # baseline for both pass counts the tests observe.
        self.base_exec = {
            loops: run_sim(base, loops)["executed_cost"] for loops in (1, 2)
        }

    def _delta(
        self, *, inertia: int | None, multiplayer: int = 1, loops: int = 1
    ) -> tuple[tuple[int, ...], tuple[int, ...]]:
        hir = _cost_module(inertia=inertia, multiplayer=multiplayer)
        static = run_static(hir)["static_cost"]
        executed = run_sim(hir, loops)["executed_cost"]
        return (
            tuple(static[key] - self.base_static[key] for key in _COST_KEYS_STATIC),
            tuple(executed[key] - self.base_exec[loops][key] for key in _COST_KEYS_EXEC),
        )

    def test_gate_open_cost(self) -> None:
        # #96 folds the helper's camera stores into the block's transfer run:
        # the four stores now share the run's page firing instead of opening
        # store flights of their own, so both passes get cheaper. The delta is
        # measured against the no-helper scaffolding, which also compacted
        # (5T -> 3T), so it shrinks by less than the absolute cost does.
        #
        # #97 re-baselined both rows: full-dword reads now emit one reader body
        # per distinct EPD (a literal `player` field cannot be patched without
        # changing the body for every site). The previous pins came from a
        # cross-address body whose per-site patches clobbered each other, so a
        # module reading N addresses returned the last-patched one for every
        # read. Paying one 33T body per address costs +137T static here but is
        # correct; it is still 29T/32C/21A/69,828B below the inlined master
        # (1328T / 650C / 3134A / 3,197,872B).
        # Cycle 26: 1-trigger const shifts and in-place unary ops cut the
        # `inertia=5` pin from 1299T/618C static and 1381T/611C executed to
        # 1213T/532C static and 1299T/525C executed. Triggers and conditions
        # decreased; actions rose slightly (3,113 -> 3,133 static and
        # 21,003 -> 21,356 executed), so this is not a pure shrink.
        # Cycle 27 (#02 mul chunk-16 + #04 isqrt u-recurrence): #02 adds the
        # 32-bit chunk-16 gate (+2T/+1C/+2A static, +4,816 B) while cutting exec;
        # #04 replaces isqrt's trial rebuild with u=2*root (-15T/+79A/-36,120 B
        # static and -58T/-1,931A exec).
        # Cycle 6 delegates `_mul_const` to `Var.mul`, so `delta * FP_Q` is one
        # masked const-shift instead of a 24-trigger ladder: -24T/-24C static
        # and -22T/-24C executed, +30A/+182A (the surviving trigger's action
        # lanes absorb the work), and -57,796 B. Landing at 1176T/509C/3244A
        # static (2,831,848 B) and 1159T/486C/18,602A executed.
        # RT1 then moves the `dwUserPlayerId` read into a gate-scoped `once`
        # cache. The first pass still performs that read, so its trigger and
        # condition counts are unchanged at 1159T/486C; only the action lanes
        # merge (-67A). Static is the deliberate cost-pin update of one `once`
        # gate: +1T, no condition change, -3A, +2,408 B.
        self.assertEqual(
            self._delta(inertia=5),
            ((1177, 509, 3241, 2_834_256), (1159, 486, 18_535)),
        )
        self.assertEqual(
            self._delta(inertia=0),
            ((223, 198, 376, 537_000), (271, 229, 1_164)),
        )

    def test_steady_state_second_pass_skips_the_cached_reader(self) -> None:
        # The parent commit's second pass over the same scaffolding was
        # 1,156T/486C/18,533A; the once cache removes its 32 bit-test triggers
        # plus the call overhead, landing at 1,121T/454C/18,463A -- the
        # measured -35T/-32C/-70A. The no-smoothing row drops 268T -> 233T.
        self.assertEqual(
            self._delta(inertia=5, loops=2),
            ((1177, 509, 3241, 2_834_256), (1_121, 454, 18_463)),
        )
        self.assertEqual(
            self._delta(inertia=0, loops=2),
            ((223, 198, 376, 537_000), (233, 197, 1_092)),
        )

    def test_closed_gate_only_pays_for_the_two_gate_conditions(self) -> None:
        static_delta, executed_delta = self._delta(inertia=5, multiplayer=0)
        # The body (including the `once`) is emitted either way; the chain jump
        # skips it at runtime, so the gate-scoped cache never fires here.
        self.assertEqual(static_delta, (1177, 509, 3241, 2_834_256))
        self.assertEqual(executed_delta, (3, 2, 0))


# ---------------------------------------------------------------------------
# py/ts parity: the emitted HIR for every new case is byte-identical.
# ---------------------------------------------------------------------------

_PARITY_TS_SCRIPT = textwrap.dedent(
    """
    import {
      SetMemory,
      SetSwitch,
      SetTo,
      SwitchActionClear,
      SwitchActionSet,
      Var,
      buildModule,
      cameraFollow,
      emitVarStore,
      when,
      CAMERA_X_ADDR,
      CAMERA_Y_ADDR,
      GAME_TICK_ADDR,
      LOCATION_STRIDE_DWORDS,
      LOCATION_TABLE_ADDR,
      MULTIPLAYER_MODE_ADDR,
    } from "./ts/index.ts";

    const SWITCH_ID = 3;
    const CAMERA_LOC = 10;
    const TARGET_LOC = 11;
    const BOX_ADDR =
      LOCATION_TABLE_ADDR + LOCATION_STRIDE_DWORDS * 4 * CAMERA_LOC;

    const mod = buildModule(() => {
      const loop = Var(0);
      const tick = Var(0);

      const apply = (
        multiplayer,
        switchOn,
        tickValue,
        camera,
        box,
      ) => {
        SetMemory(MULTIPLAYER_MODE_ADDR, SetTo, multiplayer);
        tick.set(tickValue);
        SetMemory(CAMERA_X_ADDR, SetTo, camera[0]);
        SetMemory(CAMERA_Y_ADDR, SetTo, camera[1]);
        SetMemory(BOX_ADDR, SetTo, box[0]);
        SetMemory(BOX_ADDR + 4, SetTo, box[1]);
        SetSwitch(
          SWITCH_ID,
          switchOn ? SwitchActionSet : SwitchActionClear,
        );
      };

      when(loop.eq(0), () =>
        apply(1, true, 0, [960, 800], [1000, 800]),
      );
      when(loop.eq(1), () =>
        apply(1, true, 24, [960, 800], [1000, 800]),
      );
      emitVarStore(tick.id, { addr: GAME_TICK_ADDR }, SetTo);
      cameraFollow(TARGET_LOC, CAMERA_LOC, 5, 48, SWITCH_ID);

      // Second instance: no smoothing, non-default unit/owner/switch.
      cameraFollow(13, 12, 0, 24, 4, { onUnit: 0, owner: 0 });

      // Third: a cleared switch keeps the reset branch in the same module.
      cameraFollow(15, 14, 2, 48, 5);

      loop.iadd(1);
    });

    process.stdout.write(mod.toJson());
    """
)


def _build_parity_module():
    def prog() -> None:
        loop = Var(0)
        tick = Var(0)

        def apply(step: dict[str, Any]) -> None:
            SetMemory(MULTIPLAYER_MODE_ADDR, SetTo, step["multiplayer"])
            tick.set(step["tick"])
            SetMemory(CAMERA_X_ADDR, SetTo, step["camera"][0])
            SetMemory(CAMERA_Y_ADDR, SetTo, step["camera"][1])
            SetMemory(BOX_ADDR, SetTo, step["box"][0])
            SetMemory(BOX_ADDR + 4, SetTo, step["box"][1])
            SetSwitch(
                SWITCH_ID,
                SwitchActionSet if step["switch"] else SwitchActionClear,
            )

        when(loop.eq(0), lambda: apply(_step()))
        when(loop.eq(1), lambda: apply(_step(tick=TICK_STEP)))
        emitVarStore(tick.id, {"addr": GAME_TICK_ADDR}, SetTo)
        camera_follow(TARGET_LOC, CAMERA_LOC, 5, 48, SWITCH_ID)

        # Second instance: no smoothing, non-default unit/owner/switch.
        camera_follow(13, 12, 0, 24, 4, on_unit=0, owner=0)

        # Third: a cleared switch keeps the reset branch in the same module.
        camera_follow(15, 14, 2, 48, 5)

        loop.iadd(1)

    return build_module(prog)


class CameraFollowParityTest(unittest.TestCase):
    def test_py_ts_hir_is_byte_identical(self) -> None:
        py_hir = _build_parity_module().to_json()
        result = subprocess.run(
            ["node", "--input-type=module", "-e", _PARITY_TS_SCRIPT],
            cwd=str(REPO_ROOT),
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertEqual(json.loads(py_hir), json.loads(result.stdout))
        self.assertEqual(py_hir, result.stdout)


if __name__ == "__main__":
    unittest.main()
