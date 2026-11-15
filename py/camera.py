"""Camera-follow helper: a port of euddraft's ``plugins/cammove.py``.

The source plugin is a per-loop ``afterTriggerExec`` body with two pieces of
persistent state (the last game tick it ran and the current screen width in
pixels). This module emits the same body behind the same run gates, keeping
the source arithmetic and its fixed-point units (``fpQ = 256``) intact, and
keeps the local player id in a third piece of persistent state (a gate-scoped
``once`` cache, since the id is constant for the map).

Deviations from the plugin, forced by eudkit having no location, switch, or
plugin settings allocator, plus ADR 0004's per-loop ``curpl`` reset:

* the caller owns both locations: ``camera_location`` is used as the
  scratch box the source plugin called ``cammoveLoc``, and
  ``target_location`` is the search location;
* the unit/owner pair the source hard-codes (``"Map Revealer"`` / ``P9``)
  are keyword options with the same defaults;
* ``switch_id`` (default ``0``) replaces the source's named ``"cammove"``
  switch;
* the per-loop state is returned as :class:`CameraFollowState` so a caller
  can inspect or share it instead of relying on plugin globals;
* the source's ``onPluginStart`` ``f_setloc`` of the scratch box is omitted:
  the first gated pass overwrites the box through ``MoveLocation`` and then
  through the emitted ``varstore`` writes, so the initial 640x384 box is
  never read;
* ``CenterView`` targets the *CurrentPlayer* (``0x6509B0``), which the
  payload runner leaves at ``0`` (P1). The source stores
  ``f_getuserplayerid()`` there inside its ``SeqCompute`` and then leans on
  eudplib's ``f_setcurpl2cpcache``/finalizer; the port writes the register
  directly before ``CenterView`` and restores ``0`` right after, because
  ADR 0004 resets ``curpl`` once per trigger loop rather than once per call.

``maxspeed`` keeps the source setting's spelling (``settings["maxspeed"]``)
instead of the mechanical ``max_speed``.

Cost, measured on one instance with the native simulator (``dump-ir --json
--no-exec`` for the static side, ``sim`` for the executed side, pre-assembly,
so the map-build tick gate is excluded), as a delta over identical seeding
scaffolding: ``inertia=5`` is 1,177 T / 509 C / 3,241 A static (2,834,256 B)
and 1,159 T / 486 C / 18,535 A executed on the first gated pass, easing to
1,121 T / 454 C / 18,463 A on the steady-state second pass; ``inertia=0`` is
223 T / 198 C / 376 A static (537,000 B) and 271 T / 229 C / 1,164 A executed
on the first pass, 233 T / 197 C / 1,092 A on the second; a pass where the
gate is closed is 3 T / 2 C / 0 A executed because the chain jump skips the
body. The gate-scoped ``once`` for ``USER_PLAYER_ADDR`` keeps the first pass
at the parent's 1,159 T and saves the 33 T reader plus its call overhead on
later open passes (a measured -35 T / -32 C / -70 A steady state); it adds
1 T / 2,408 B static. The constant multiply inside the smoothing kernel
delegates to `Var.mul`, so ``(currentTick - lastTick) * 256`` is one masked
const-shift instead of a 24-trigger ladder. The ``run`` body's shared path
holds four coalesced transfer pairs (``dstx``/``dsty``, ``right``/``bottom``,
the two additive ``updated`` additions, and ``screen_x``/``last_tick``), so
body-wide transfer coalescing moves ``inertia=0`` by 4 static triggers, 8
executed triggers, and 9,632 B as well. Most of the open cost is the six
steady-state 32-bit dword reads (seven on the first pass, 33 T each),
``isqrt(16, 8)`` and the three 16-step divides; see ``CameraFollowCostTest``
for the pinned numbers.

The source reads/writes fixed game addresses (camera position, game tick,
location table, CurrentPlayer), so those are module constants here. The
source's ``MoveLocation`` writes the box before the helper reads it; the
simulator is free to model that action, but the emitted order is the
contract.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .cp import CP_ADDR
from .hir import (
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
)
from .mathf import isqrt
from .temporal import once, when
from .trigger import Trigger
from .var import Var, _saturating_sub_into, mul_shift_add as _mul_shift_add
from .view import (
    EPD,
    EPD_CURRENT_PLAYER,
    EudslError,
    FieldSpec,
    MemFieldHandle,
    _require_plain_int,
    defineMemView,
)

CAMERA_X_ADDR = 0x62848C
CAMERA_Y_ADDR = 0x6284A8
CAMERA_TILE_X_ADDR = 0x628498
CAMERA_TILE_Y_ADDR = 0x6284AC
MULTIPLAYER_MODE_ADDR = 0x57F0B4
GAME_TICK_ADDR = 0x57F23C
LOCATION_TABLE_ADDR = 0x58DC4C
LOCATION_STRIDE_DWORDS = 5
#: ``dwUserPlayerId``: the local human's player index, the value the source
#: plugin puts in ``CurrentPlayer`` before ``CenterView``.
USER_PLAYER_ADDR = 0x512684

FP_Q = 256
FP_Q2 = FP_Q * FP_Q
SCREEN_W = 640
SCREEN_H = 384
MAP_REVEALER_UNIT = 101  # "Map Revealer" trigger-unit id
MAP_REVEALER_OWNER = 8  # P9

PLAYER_VALUES = (0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 13, 14, 15, 16, 17, 18, 19, 20, 21, 26)

#: A dword field used by the raw memory reads below. `MemFieldHandle.read`
#: already owns the accumulator reset + one-bit-test-per-bit shape.
_DWORD_VIEW = defineMemView("CameraDword", 4, {"value": FieldSpec(0, 4)})
_DWORD_FIELD = FieldSpec(0, 4)


@dataclass
class CameraFollowState:
    """Persistent per-loop state of one :func:`camera_follow` instance."""

    last_tick: Var
    screen_x: Var
    user_player: Var


def _require_int(name: str, value: Any, minimum: int, maximum: int) -> int:
    """Validate one build-time integer option: operand domain, then range."""
    value = _require_plain_int(value, f"camera_follow(): {name}")
    if value < minimum or value > maximum:
        raise EudslError(
            "EUDSL-RANGE",
            f"camera_follow(): {name} must be in [{minimum}, {maximum}], got {value}",
        )
    return value


def _read_epd(epd: int) -> Var:
    """Read the dword at ``epd`` into a fresh `Var` (one bit test per bit)."""
    return MemFieldHandle(_DWORD_VIEW, _DWORD_FIELD, epd, "value").read()


def _read_dword(addr: int) -> Var:
    return _read_epd(EPD(addr))


def _square(x: Var, bits: int) -> Var:
    """``out = x * x`` for an ``x`` whose bits above ``bits`` are zero.

    Each set-bit pair ``b <= k`` contributes ``2**(b+k)`` (diagonal) or
    ``2**(b+k+1)`` (off-diagonal): one trigger per pair instead of the
    528-trigger ``Var * Var`` kernel.

    The accumulator is cleared on every call: the kernel is emitted inside a
    per-loop body, so a build-time zero init would let pass 2 accumulate on
    pass 1.
    """
    out = Var(0)
    out.set(0)
    for b in range(bits):
        cond_b = DeathsX(x.value, AtLeast, 1 << b, 0, 1 << b)
        Trigger(
            conds=[cond_b],
            acts=[SetDeathsX(out.value, Add, (1 << (2 * b)) & 0xFFFFFFFF, 0, 0xFFFFFFFF)],
        )
        for k in range(b + 1, bits):
            Trigger(
                conds=[cond_b, DeathsX(x.value, AtLeast, 1 << k, 0, 1 << k)],
                acts=[
                    SetDeathsX(
                        out.value, Add, (1 << (b + k + 1)) & 0xFFFFFFFF, 0, 0xFFFFFFFF
                    )
                ],
            )
    return out


def _mul_const(x: Var, c: int) -> Var:
    """``out = x * c`` for a build-time constant, via ``Var.mul``.

    Delegates to the Cycle 4 constant-multiply kernel: ``0 -> 0``, ``1`` ->
    a copy, ``0xFFFFFFFF`` -> a negate, a power of two -> the single masked
    const-shift, and any other value -> the measured k-term shift-add chain
    when its cost gate fits (otherwise the 32-bit window scan).

    Every branch assigns ``out`` directly on every call for the same per-loop
    safety property as :func:`_square`.
    """
    out = Var(0)
    out.set(x.mul(c))
    return out


def _div_const(x: Var, c: int) -> Var:
    """``out = x // c`` for a build-time constant, via `Var.div`."""
    out = Var(0)
    out.set(x.div(c))
    return out


def _shr_const(x: Var, k: int) -> Var:
    out = Var(0)
    out.set(x.shr(k))
    return out


def _div_shift16(hi: Var, den: Var, bits: int = 16) -> Var:
    """``out = (hi << 16) // den`` with ``hi < den`` and quotient < ``2**bits``.

    Two long-division facts keep this short: bringing down the 16 high bits
    of ``hi << 16`` leaves ``hi mod den == hi`` as the remainder, and only the
    16 zero bits below contribute quotient bits. Each iteration doubles the
    remainder and quotient (``iadd(x)`` is a 3-trigger copy, not a 31-trigger
    shift) and subtracts ``den`` when it fits. ``bits`` caps the iterations
    when the caller knows the quotient is narrower.

    The quotient accumulator is cleared on every call for the same per-loop
    reason as :func:`_square`; ``rem`` and ``diff`` are seeded explicitly.
    """
    rem = Var(0)
    rem.set(hi)
    quot = Var(0)
    quot.set(0)
    diff = Var(0)
    for _ in range(bits):
        rem.iadd(rem)
        quot.iadd(quot)
        diff.set(den)
        _saturating_sub_into(diff, rem)  # 0 exactly when rem >= den

        def take(rem: Var = rem, quot: Var = quot, den: Var = den) -> None:
            _saturating_sub_into(rem, den)
            quot.iadd(1)

        when(diff.eq(0), take)
    return quot


def _negate_into(dst: Var, src: Var) -> None:
    dst.set(0xFFFFFFFF)
    _saturating_sub_into(dst, src)
    dst.iadd(1)


def camera_follow(
    target_location: int,
    camera_location: int,
    inertia: int = 5,
    maxspeed: int = 48,
    switch_id: int = 0,
    *,
    on_unit: int = MAP_REVEALER_UNIT,
    owner: int = MAP_REVEALER_OWNER,
) -> CameraFollowState:
    """Emit a per-loop camera-follow body (euddraft ``cammove.py`` port).

    Every trigger loop where the game is in multiplayer (``Memory(0x57F0B4,
    AtLeast, 1)``) and ``switch_id`` is set:

    1. remembers ``currentTick`` the first time the switch is seen set;
    2. moves ``camera_location`` onto ``on_unit`` owned by ``owner`` inside
       ``target_location`` and reads its top-left corner;
    3. eases the view toward that corner with the source plugin's
       fixed-point smoothing and ``maxspeed`` cap;
    4. writes the scratch box, points *CurrentPlayer* at the local human,
       centers the view on it, restores ``CurrentPlayer = 0``, updates the
       camera tile coordinates and the widescreen ``screenX`` correction, and
       stores ``currentTick`` as the last tick.

    When the switch is cleared (still in multiplayer) and the state is not already
    reset, ``lastTick`` is set back to ``-1`` so re-enabling snaps instead of
    lerping from a stale tick.

    ``inertia == 0`` is a build-time branch: the emitted body jumps straight
    to the target box and omits the smoothing kernel. The source's
    ``dx == 0 and dy == 0`` shortcut is subsumed by the emitted
    ``d >= 1`` guard (zero distance leaves ``dstx/dsty`` at the target).

    Returns the three persistent state variables.
    """
    target_location = _require_int("target_location", target_location, 0, 255)
    camera_location = _require_int("camera_location", camera_location, 0, 255)
    inertia = _require_int("inertia", inertia, 0, 0xFFFFFFFF)
    maxspeed = _require_int("maxspeed", maxspeed, 0, 0xFFFFFFFF)
    switch_id = _require_int("switch_id", switch_id, 0, 255)
    on_unit = _require_int("on_unit", on_unit, 0, 227)
    owner = _require_plain_int(owner, "camera_follow(): owner")
    if owner not in PLAYER_VALUES:
        raise EudslError(
            "EUDSL-RANGE",
            f"camera_follow(): owner must be one of {PLAYER_VALUES}, got {owner!r}",
        )

    last_tick = Var(0xFFFFFFFF)
    screen_x = Var(SCREEN_W)
    user_player = Var(0)
    state = CameraFollowState(
        last_tick=last_tick, screen_x=screen_x, user_player=user_player
    )

    box_epd = EPD(LOCATION_TABLE_ADDR) + LOCATION_STRIDE_DWORDS * camera_location
    box_addr = LOCATION_TABLE_ADDR + LOCATION_STRIDE_DWORDS * 4 * camera_location

    def run() -> None:
        current_tick = _read_dword(GAME_TICK_ADDR)
        # `dwUserPlayerId` is constant for the map. Reading it once here (still
        # inside the run gate, so a closed gate skips it entirely) keeps the
        # 33 T reader off every later open pass.
        once(lambda: user_player.set(_read_dword(USER_PLAYER_ADDR)))
        when(last_tick.eq(0xFFFFFFFF), lambda: last_tick.set(current_tick))

        prev_cam_x = _read_dword(CAMERA_X_ADDR)
        prev_cam_y = _read_dword(CAMERA_Y_ADDR)

        MoveLocation(camera_location, on_unit, owner, target_location)
        locx = _read_epd(box_epd)
        locy = _read_epd(box_epd + 1)

        dstx = Var(0)
        dstx.set(locx)
        dsty = Var(0)
        dsty.set(locy)

        if inertia == 0:
            # No smoothing: the view jumps straight to the target box.
            pass
        else:
            dx = Var(0)
            dx.set(locx)
            dx.isub(prev_cam_x)
            dy = Var(0)
            dy.set(locy)
            dy.isub(prev_cam_y)

            dxabs = Var(0)
            dxabs.set(dx)
            dyabs = Var(0)
            dyabs.set(dy)
            when(dx.ge(0x80000000), lambda: _negate_into(dxabs, dx))
            when(dy.ge(0x80000000), lambda: _negate_into(dyabs, dy))

            d = Var(0)
            # Assign the cap `fpQ * f_sqrt(20000)` (the overflow-safe norm) on
            # every pass: the accurate branch below is conditional, so an
            # initializer alone would leave a stale accurate value behind for
            # the next loop's overflow path.
            d.set(FP_Q * 141)

            def accurate() -> None:
                sumsq = _square(dxabs, 7)
                sumsq.iadd(_square(dyabs, 7))
                # Q8 square root: floor(256 * sqrt(sumsq)), with sumsq < 2**16.
                d.set(isqrt(sumsq, 16, fixed_bits=8))

            when([dxabs.le(100), dyabs.le(100)], accurate)

            delta = Var(0)
            delta.set(current_tick)
            delta.isub(last_tick)
            dt = _div_const(_mul_const(delta, FP_Q), 24)

            newd = Var(0)
            newdx = Var(0)
            newdy = Var(0)

            def ease() -> None:
                # `dt` is a full dword (`(currentTick - lastTick) * 256 // 24`),
                # so the multiply uses all 32 bits like the source; at 16 bits
                # a gap of >= 6144 ticks dropped the high half and the view
                # snapped instead of easing.
                den = _mul_shift_add(d, dt, 32)
                den = _div_const(den, inertia)
                den.iadd(FP_Q2)
                newd.set(_div_shift16(d, den))  # d < 65536 <= den

                gap = Var(0)
                gap.set(d)
                _saturating_sub_into(gap, newd)
                cap = maxspeed * FP_Q
                when(gap.ge(cap), lambda: (newd.set(d), newd.isub(cap)))

                # `qx = (dxabs << 16) // d` must stay below 2**16 for the
                # 16-step divide. The accurate branch has `d >= 256 * dxabs`,
                # and the capped branch has `d = 36096`, so any axis below
                # 36096 px fits (a 256-tile map is 8192 px). The source's full
                # 32-bit divide has no such bound; a larger map would need
                # `bits=32` here.
                qx = _div_shift16(dxabs, d)
                qy = _div_shift16(dyabs, d)
                newdx.set(_shr_const(_mul_shift_add(newd, qx, 16), 16))
                newdy.set(_shr_const(_mul_shift_add(newd, qy, 16), 16))

                # Each axis keeps its own sign (the source's dx_sgn / dy_sgn):
                # `dstx << locx - dx_sgn * newdx` is per-axis, so a target up
                # and to the left must move X one way and Y the other.
                when(dx.ge(0x80000000), lambda: dstx.iadd(newdx))
                when(dx.lt(0x80000000), lambda: dstx.isub(newdx))
                when(dy.ge(0x80000000), lambda: dsty.iadd(newdy))
                when(dy.lt(0x80000000), lambda: dsty.isub(newdy))
            when(d.ge(1), ease)

        tile_x = _shr_const(dstx, 5)
        tile_y = _shr_const(dsty, 5)
        right = Var(0)
        right.set(dstx)
        right.iadd(screen_x)
        bottom = Var(0)
        bottom.set(dsty)
        bottom.iadd(SCREEN_H)

        emitVarStore(dstx.id, AddrReloc(box_addr), SetTo)
        emitVarStore(dsty.id, AddrReloc(box_addr + 4), SetTo)
        emitVarStore(right.id, AddrReloc(box_addr + 8), SetTo)
        emitVarStore(bottom.id, AddrReloc(box_addr + 12), SetTo)
        # `CenterView` centers the *CurrentPlayer*'s view, and the payload
        # runner leaves 0x6509B0 at 0 (P1). The source stores the local human's
        # player id there in the same `SeqCompute` as the box writes; the port
        # writes the register directly (the absolute box stores replace
        # eudplib's cache indirection) and restores 0 immediately after, so a
        # later `Deaths(CurrentPlayer, ...)` in the same loop is not poisoned
        # (ADR 0004 resets curpl once per loop).
        emitVarStore(user_player.id, AddrReloc(CP_ADDR), SetTo)
        CenterView(camera_location)
        SetDeaths(EPD_CURRENT_PLAYER, SetTo, 0, 0)
        emitVarStore(tile_x.id, AddrReloc(CAMERA_TILE_X_ADDR), SetTo)
        emitVarStore(tile_y.id, AddrReloc(CAMERA_TILE_Y_ADDR), SetTo)

        post_cam_x = _read_dword(CAMERA_X_ADDR)
        updated = Var(0)
        updated.set(dstx)
        updated.iadd(dstx)
        updated.iadd(screen_x)
        updated.isub(post_cam_x)
        updated.isub(post_cam_x)
        screen_x.set(updated)

        last_tick.set(current_tick)

    when([Memory(MULTIPLAYER_MODE_ADDR, AtLeast, 1), Switch(switch_id, SwitchSet)], run)

    when(
        [
            Memory(MULTIPLAYER_MODE_ADDR, AtLeast, 1),
            Switch(switch_id, SwitchCleared),
            last_tick.ne(0xFFFFFFFF),
        ],
        lambda: last_tick.set(0xFFFFFFFF),
    )

    return state


#: The canonical camelCase name (ADR 0002 §3); `camera_follow` is the Python
#: snake_case alias, mirroring `ts/camera.ts`.
cameraFollow = camera_follow

__all__ = [
    "CAMERA_TILE_X_ADDR",
    "CAMERA_TILE_Y_ADDR",
    "CAMERA_X_ADDR",
    "CAMERA_Y_ADDR",
    "CameraFollowState",
    "FP_Q",
    "FP_Q2",
    "GAME_TICK_ADDR",
    "LOCATION_STRIDE_DWORDS",
    "LOCATION_TABLE_ADDR",
    "MAP_REVEALER_OWNER",
    "MAP_REVEALER_UNIT",
    "MULTIPLAYER_MODE_ADDR",
    "PLAYER_VALUES",
    "SCREEN_H",
    "SCREEN_W",
    "USER_PLAYER_ADDR",
    "cameraFollow",
    "camera_follow",
]
