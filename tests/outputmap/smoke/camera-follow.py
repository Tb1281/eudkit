"""Smoke map 4: `camera_follow` (Cycle 6 `_mul_const` -> `Var.mul`) + live camera HUD.

Spawns a P1 Terran Marine at map center (`Anywhere`) after 1 second, snaps the
initial camera onto it, enables Switch 0, keeps location 63 following the Marine,
and attaches `camera_follow(63, 62, inertia=5, on_unit=TerranMarine, owner=P1)`.

Note: `camera_follow` gates on Multiplayer (`0x57F0B4 >= 1`), matching euddraft's
`cammove.py` contract; single-player keeps the easing gate closed cleanly while
still updating the live Camera X/Y and ScreenW HUD readout.
"""

from py import (
    Anywhere,
    AtLeast,
    CAMERA_X_ADDR,
    CAMERA_Y_ADDR,
    CenterView,
    CreateUnit,
    EPD,
    ElapsedTime,
    FieldSpec,
    FixedString,
    MULTIPLAYER_MODE_ADDR,
    MemFieldHandle,
    MoveLocation,
    P1,
    SetSwitch,
    SwitchActionSet,
    UnitType,
    build_module,
    camera_follow,
    defineMemView,
    every,
    once,
    when,
)

TARGET_LOC = 63
CAMERA_LOC = 62


def prog() -> None:
    u32_field = FieldSpec(0, 4)
    u32_schema = defineMemView("U32", 4, {"val": u32_field})
    cam_x_field = MemFieldHandle(u32_schema, u32_field, EPD(CAMERA_X_ADDR), "val")
    cam_y_field = MemFieldHandle(u32_schema, u32_field, EPD(CAMERA_Y_ADDR), "val")
    mp_field = MemFieldHandle(u32_schema, u32_field, EPD(MULTIPLAYER_MODE_ADDR), "val")

    def setup() -> None:
        CreateUnit(1, UnitType.TerranMarine, Anywhere, P1)
        MoveLocation(TARGET_LOC, UnitType.TerranMarine, P1, Anywhere)
        CenterView(TARGET_LOC)
        SetSwitch(0, SwitchActionSet)

    once(ElapsedTime(AtLeast, 1), setup)

    # Track the Marine continuously with TARGET_LOC so camera_follow chases it.
    MoveLocation(TARGET_LOC, UnitType.TerranMarine, P1, Anywhere)
    state = camera_follow(
        TARGET_LOC,
        CAMERA_LOC,
        5,
        48,
        0,
        on_unit=UnitType.TerranMarine,
        owner=P1,
    )

    line1 = FixedString(64)
    line2 = FixedString(64)
    line3 = FixedString(64)

    def render_hud() -> None:
        cam_x = cam_x_field.read()
        cam_y = cam_y_field.read()
        mp_mode = mp_field.read()

        line1.clear()
        line1.write("CAMERA X={:\x1a>4d} Y={:\x1a>4d} SCREENW={:\x1a>4d}", cam_x, cam_y, state.screen_x)
        line1.displayAt(1)

        line2.clear()
        when(
            [mp_mode.ge(1)],
            lambda: line2.write("MODE=MP ({:\x1a>1d}) CAMMOVE ACTIVE - move Marine!", mp_mode),
            lambda: line2.write("MODE=SP (0x57F0B4=0) cammove idle; use LAN/MP to ease"),
        )
        line2.displayAt(2)

        line3.clear()
        line3.write("LAST_TICK={:x}", state.last_tick)
        line3.displayAt(3)

    every(6, render_hud)


module = build_module(prog)
