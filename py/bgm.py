"""Periodic background-music player (port of euddraft ``plugins/bgmplayer.py``).

``play_bgm(path, length_seconds)`` embeds an audio file into the output MPQ as
``bgm`` and arms a preserved trigger that replays it every ``length_seconds``
seconds for the local player. This is a regular core helper: there is no plugin
system, no settings manifest, and no lifecycle hook — one call inside
``buildModule`` arms the player for the compiled module.

Timing
------
The game clock is the dword at ``0x51CE8C``, which counts *down*; elapsed
milliseconds are ``0xFFFFFFFF - dword`` (eudplib ``f_time``). eudplib's firing
test is ``target <= now`` with ``now = 0xFFFFFFFF - raw``. Rearranged, that is
``raw <= threshold`` with ``threshold = 0xFFFFFFFF - target``. The helper keeps
the threshold in a variable and, on every play, patches it into the probe's
``Memory(0x51CE8C, AtMost, ...)`` condition. The idle loop therefore never
reads the clock and never does arithmetic.

``f_starttimer(duration)`` sets ``target = now + duration - 42``; in threshold
units that is ``raw - duration + 42``. The 42 ms is eudplib's start-latency
compensation and is kept on every schedule.

``onPluginStart`` calls ``f_starttimer(0)`` so the first play is immediate. A
build-time initial value cannot read the runtime clock, so the probe starts
with an all-ones threshold (already due): the first trigger loop always plays.
That is the documented equivalent of the ``-42`` start path — the millisecond
compensation only exists to make a zero-length start already due.

Per play the helper switches CurrentPlayer to the local player id
(``0x512684``) for ``PlayWAV("bgm")`` and restores the previous value, exactly
like ``f_getcurpl`` / ``f_getuserplayerid`` / ``SetMemory(0x6509B0)`` in the
source. The local player id is read once at map start (a ``once`` block, the
counterpart of eudplib's ``_f_inituserplayerid``), so only the saved
CurrentPlayer and the clock are read per play.

Executed cost
-------------
Armed and idle: 2T (the probe and the ``when`` gate, one condition each), no
actions, no clock read. A play loop adds the body: two 32-test dword reads (the
saved CurrentPlayer and the raw clock), the ``PlayWAV``, the threshold
arithmetic and the probe patch.

The helper returns the threshold variable so tests and advanced callers can
inspect or override the schedule.
"""

from __future__ import annotations

import math
from typing import Any

from .camera import USER_PLAYER_ADDR
from .cp import CP_ADDR, read_current_player_register
from .hir import AddrReloc, AtMost, TriggerFieldReloc, emitVarStore, embedFile
from .stock_rows_generated import Memory, PlayWAV
from .temporal import once, when
from .trigger import Trigger, _register_trigger
from .var import Var
from .view import EPD, EudslError, read_epd

#: Absolute address of the countdown clock read by euddraft's ``f_time``.
BGM_CLOCK_ADDR = 0x51CE8C
#: Archived MPQ name the embedded track is played from.
BGM_ARCHIVE_NAME = "bgm"
#: eudplib's start-latency compensation, in milliseconds.
START_LATENCY_MS = 42


def _period_ms(length_seconds: Any) -> int:
    """Truncate ``length_seconds`` to whole milliseconds (eudplib ``int()``)."""
    if isinstance(length_seconds, bool) or not isinstance(
        length_seconds, (int, float)
    ):
        raise EudslError(
            "EUDSL-TYPE",
            f"play_bgm: length_seconds must be a number, got {length_seconds!r}",
        )
    value = float(length_seconds)
    if not math.isfinite(value):
        raise EudslError(
            "EUDSL-TYPE",
            f"play_bgm: length_seconds must be finite, got {length_seconds!r}",
        )
    period = int(value * 1000)
    if period <= 0:
        raise EudslError(
            "EUDSL-RANGE",
            f"play_bgm: length_seconds must be positive, got {length_seconds!r}",
        )
    return period


def _read_user_player() -> Var:
    """Materialise the dword at ``USER_PLAYER_ADDR`` through the shared reader."""
    return read_epd(EPD(USER_PLAYER_ADDR), 32, 0)


def _read_clock() -> Var:
    """Materialise the raw countdown dword through the shared reader."""
    return read_epd(EPD(BGM_CLOCK_ADDR), 32, 0)


def play_bgm(path: str, length_seconds: float) -> Var:
    """Play ``path`` from the MPQ as ``bgm``, repeating every ``length_seconds``.

    ``path`` is resolved by the build against the compiling source's directory.
    Returns the next-fire threshold: the play happens on the trigger loop where
    the raw clock dword is at most this value.
    """
    period = _period_ms(length_seconds)
    embedFile(BGM_ARCHIVE_NAME, path)

    # Already due, so the first evaluated loop plays (see module docstring).
    threshold = Var(0xFFFFFFFF)
    hit = Var(0)
    # eudplib reads the local player id once at map start; do the same so the
    # 32-test read stays off the per-play path.
    local_player = Var(0)
    once(lambda: local_player.set(_read_user_player()))

    probe = Trigger(
        conds=[Memory(BGM_CLOCK_ADDR, AtMost, 0xFFFFFFFF)],
        acts=[hit.act_set(1)],
        register=False,
    )
    _register_trigger(probe)

    def replay() -> None:
        old_cp = read_current_player_register()
        # `emitVarStore` copies the variable's value; `SetMemory(..., Var)`
        # would write the variable's address (value-or-ptr mode 4).
        emitVarStore(local_player.id, AddrReloc(CP_ADDR))
        PlayWAV(BGM_ARCHIVE_NAME)
        emitVarStore(old_cp.id, AddrReloc(CP_ADDR))

        raw = _read_clock()
        threshold.set(raw)
        adjustment = period - START_LATENCY_MS
        if adjustment != 0:
            threshold.isub(adjustment)
        # Patch the next loop's probe condition from the fresh threshold. This
        # runs only on a play, so idle loops stay at one compare per trigger.
        emitVarStore(
            threshold.id,
            TriggerFieldReloc(probe.id, probe.cond(0).amount.byteOffset),
        )
        hit.set(0)

    when(hit.eq(1), replay)
    return threshold


playBgm = play_bgm
