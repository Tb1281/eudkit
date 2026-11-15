"""Loop-sound player: a port of euddraft's ``lib/soundlooper.py``.

euddraft's soundlooper splits a song into ``000``-``999`` numbered ``.ogg``
segments, embeds them in the MPQ, and plays them as an intro / bar / bridge
loop. This module ports the build-time segment scanner, the ``_TinyTag`` Ogg
length reader (as :mod:`eudkit.ogg`, shared with the TypeScript frontend) and
the ``SoundLooper`` runtime as a regular core helper. There is no plugin
system, no settings manifest and no lifecycle hook.

Relationship to the BGM helper
------------------------------
:func:`eudkit.bgm.play_bgm` is the *simpler* background-music player: one
track, repeated every fixed number of seconds. Soundlooper is the *loop*
system: a segmented song with per-segment lengths, an intro that plays once,
a bridge on the last segment, and a configurable ``goto`` sound that the loop
returns to. Both schedule on the same countdown clock at ``0x51CE8C``; the BGM
helper uses one fixed period, soundlooper recomputes the next firing point on
every segment.

Build-time registration
-----------------------
:class:`SoundLoops` owns the per-module loop registry and the shared progress
array (the source's ``_Loop.loop_dict`` + class-level ``SoundLooper._bars``):

* :meth:`SoundLoops.add_loop` scans ``<path>/<title>/<title>NNN.ogg`` (the
  source's ``AddLoop``), reads each segment's duration, embeds the segment
  under ``<5-char-id><NNN>`` and registers the loop. The scan tries the
  shortest zero-padding first (``theme0.ogg`` before ``theme000.ogg``),
  exactly like the source.
* :meth:`SoundLoops.manual_add_loop` registers a loop whose five-byte
  segments are already in the MPQ (the source's ``ManualAddLoop``).

The progress array must exist before the runtime can use it, so registering a
loop after the first player call is an error — the source's
``ep_assert(SoundLooper._bars is None, ...)``.

Runtime playback
----------------
:class:`SoundLooper` keeps the source's state: ``current_loop``,
``previous_loop``, ``current_bar`` and the packed three-digit bar register.
``player()`` emits the per-loop body once; it fires when the raw clock dword at
``0x51CE8C`` has fallen to the segment's next-due threshold, and then

1. advances ``current_bar`` and the packed digit register (with the source's
   decimal carries: ones -> tens -> hundreds),
2. mirrors the bar into the loop's progress-array slot,
3. plays ``<id><bar-1:03d>`` for the current loop (constant names per case),
4. recomputes the next threshold from the raw clock, the loop's
   ``ceil(-bar * 1000)`` base and the speed-error correction, adding the
   intro correction on bar 1 and the bridge correction on the last bar,
5. returns to ``goto`` after the last bar (the source's "if last bar was
   played, go to start").

``play`` / ``pause`` / ``resume`` / ``toggle`` / ``setbar`` / ``sendbar`` /
``sendcurrentbar`` / ``setloopbar`` / ``cond`` / ``loopis`` keep the source's
names. ``cond`` and ``loopis`` return conditions for ``when``; the rest emit
actions at the call site, so the caller decides when they run.

Deviations from the source
--------------------------
* **Deterministic archive ids.** The source's ``_id_generator`` samples five
  random characters, so two builds of the same map differ. The port derives
  the five-character prefix from a SHA-256 digest of the title and the
  registration ordinal (and rejects duplicate titles), so builds are
  reproducible. ``manual_add_loop`` takes the title as the prefix, as the
  source does.
* **Identifiers are five ASCII characters.** The source only checks that a
  manual title is five *cp949 bytes*; the TS frontend has no cp949 codec, so
  the shared contract is five single-byte characters (``EUDSL-RANGE``
  otherwise). ``add_loop`` titles are only directory names and are not
  restricted.
* **No string-buffer playback path.** In this source version the
  ``StringBuffer`` write and per-pass digit increment survive from an older
  implementation, but the ``PlayWAV(_sb.StringIndex)`` line is commented out
  and playback goes through ``PlaySoundWorkaround``'s constant names per bar.
  The port keeps the packed digit register (``bar_digits``) with the exact
  packing and carries, and plays constant names per case; it does not allocate
  the unused eight-byte string buffer.
* **A `when` chain replaces `EUDSwitch`.** eudplib generates a jump table for
  ``EUDMethod`` dispatches; the port emits one ``when`` per loop and per bar.
  For a loop with ``N`` segments the play dispatch costs ``N + 1`` conditions
  per loop, the same order as the source's case table, and is only evaluated
  on a due pass.
* **`play(title=None)` with `bar=None` reads the current loop's slot.** The
  source raises ``UnboundLocalError`` here because ``index`` is only bound in
  the ``else`` branch; the port reads ``bars[current_loop]`` (guarded, so a
  paused player is a no-op).
* **Unknown loop titles are rejected.** The source returns the title unchanged
  from ``_T2i`` and its switch silently matches nothing; the port raises
  ``EUDSL-RANGE``.
* **`setbar`/`play` reject out-of-range constant bars.** A packed three-digit
  register holds 0-999; a constant above that is ``EUDSL-RANGE`` (the source
  would pack it and print nonsense).
* **CurrentPlayer is saved and restored.** The source leaves ``0x6509B0`` at
  the local player id after each pass; the port reads the previous value,
  switches for ``PlayWAV`` and restores it (the BGM helper's shape, and
  ADR 0004's per-loop reset). The local human id is read once in a ``once``
  block, the counterpart of the source's ``EUDOnStart``.
* **Segments are embedded with `embedFile`, not `Db`.** ``PlayWAV`` resolves
  an archived *name* from the MPQ, so the merged #99 ``Db`` blob (which lives
  in the trigger payload, not the archive) cannot serve this helper; the
  file surface is ADR 0021's general MPQ embed.

Cost
----
The idle loop pays one probe trigger (two conditions) plus the ``when`` gate
and never reads the clock. A due pass pays the 32-test clock read, the bar and
digit arithmetic, the per-bar play dispatch, the progress-array write (dynamic
index) and the threshold patch. The per-loop constants are written by
``play``/``resume``, not per pass. Segment bytes dominate map size.
"""

from __future__ import annotations

import hashlib
import math
import os
import string
from dataclasses import dataclass, replace
from typing import Any, Iterator, Optional

from .camera import USER_PLAYER_ADDR
from .cp import CP_ADDR, read_current_player_register
from .array import FixedArray
from .hir import (
    AddrReloc,
    AtLeast,
    AtMost,
    Exactly,
    Memory,
    TriggerFieldReloc,
    embedFile,
    emitVarStore,
)
from .ogg import ogg_duration, round_duration
from .stock_rows_generated import DeathsX, PlayWAV
from .temporal import once, when
from .trigger import Trigger, _register_trigger
from .var import Var
from .view import (
    EPD,
    EudslError,
    FieldSpec,
    MemFieldHandle,
    _is_plain_int,
    _require_plain_int,
    defineMemView,
)

#: The countdown clock eudplib's ``f_time`` reads (``0xFFFFFFFF - raw`` is the
#: elapsed milliseconds).
SOUND_CLOCK_ADDR = 0x51CE8C
#: ``dwGameSpeed``-adjacent dword: 42 selects the speed-table path.
GAME_SPEED_ADDR = 0x5124F0
#: The two speed multipliers the source's ``_calculate_error`` compares.
SPEED_84_ADDR = 0x51CE84
SPEED_88_ADDR = 0x51CE88
#: ``dwUserPlayerId`` (``0x512684``) is imported from :mod:`eudkit.camera`,
#: its single home; the comment above ``SPEED_88_ADDR`` documents its use.

#: The source's ``speed_map`` in insertion order: ``(0x51CE84, 0x51CE88)``
#: value pair -> the ``x`` the correction formula uses.
SPEED_MAP: tuple[tuple[tuple[int, int], int], ...] = (
    ((2047, 2047), 29),
    ((4095, 4095), 18),
    ((65535, 65535), 1),
    ((1157, 1157), 36),
    ((1437, 1437), 29),
    ((1984, 1984), 29),
    ((3472, 3472), 18),
    ((41667, 41667), 1),
    ((1736, 1736), 36),
    ((2155, 2155), 29),
    ((2976, 2976), 21),
    ((5208, 5208), 18),
    ((62500, 62500), 1),
    ((2463, 2463), 29),
    ((3401, 3401), 21),
    ((5952, 5952), 18),
    ((71429, 71429), 1),
)

#: The source's default ``x`` when ``0x5124F0`` is not 42 (``-41``).
DEFAULT_SPEED_X = (-41) & 0xFFFFFFFF
#: The source's speed-table sentinel at ``0x5124F0``.
SPEED_SENTINEL = 42

_ALPHABET = string.ascii_letters + string.digits

_DWORD_VIEW = defineMemView("SoundLooperDword", 4, {"value": FieldSpec(0, 4)})
_DWORD_FIELD = FieldSpec(0, 4)


def loop_identifier(title: str, ordinal: int) -> str:
    """A deterministic five-character archive prefix (see the docstring)."""
    digest = hashlib.sha256(f"{title}\x00{ordinal}".encode("utf-8")).digest()
    return "".join(_ALPHABET[byte % len(_ALPHABET)] for byte in digest[:5])


def _require_seconds(value: Any, what: str) -> float:
    """Validate one loop length in seconds (the source's ``intro``/``bar``)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise EudslError(
            "EUDSL-TYPE",
            f"soundlooper: {what} must be a finite number, got {value!r}",
        )
    number = float(value)
    if not math.isfinite(number):
        raise EudslError(
            "EUDSL-TYPE",
            f"soundlooper: {what} must be finite, got {value!r}",
        )
    if number < 0:
        raise EudslError(
            "EUDSL-RANGE",
            f"soundlooper: {what} must not be negative, got {value!r}",
        )
    return number


def pack_bar_digits(bar: int) -> int:
    """Pack ``bar`` as the source's three little-endian decimal digits.

    ``get_three_digits`` returns ``(hundreds, tens, ones)`` and the source packs
    them as ``hundreds | (tens << 8) | (ones << 16)`` so the three bytes read
    as an ASCII ``%03d`` string. Bars above 999 do not fit three digits and are
    rejected by the constant paths.
    """
    bar = _require_plain_int(bar, "soundlooper: bar")
    if bar < 0 or bar > 999:
        raise EudslError(
            "EUDSL-RANGE", f"soundlooper: bar must be in [0, 999], got {bar}"
        )
    hundreds, rest = divmod(bar, 10)
    hundreds, tens = divmod(hundreds, 10)
    return hundreds | (tens << 8) | (rest << 16)


def _read_dword(addr: int) -> Var:
    """Materialise the dword at ``addr`` (32 bit-test triggers)."""
    return MemFieldHandle(_DWORD_VIEW, _DWORD_FIELD, EPD(addr), "value").read()


def calculate_speed_error() -> Var:
    """Emit the source's ``_calculate_error`` and return its result.

    ``x`` starts at ``-41``; when ``0x5124F0`` equals 42 it becomes that dword,
    and a game-speed pair at ``0x51CE84``/``0x51CE88`` overrides it. The
    correction is ``(493 - 7 * x) // 41`` on the 32-bit wire values (the
    wrapping subtraction turns ``-41`` into the source's ``780 // 41 = 19``).
    """
    x = Var(DEFAULT_SPEED_X)

    def read_sentinel() -> None:
        x.set(_read_dword(GAME_SPEED_ADDR))

    when(Memory(GAME_SPEED_ADDR, Exactly, SPEED_SENTINEL), read_sentinel)
    for (v84, v88), speed in SPEED_MAP:
        when(
            [
                Memory(SPEED_84_ADDR, Exactly, v84),
                Memory(SPEED_88_ADDR, Exactly, v88),
            ],
            lambda speed=speed: x.set(speed),
        )
    result = Var(0)
    result.set(x.mul(7).neg().add(493).div(41))
    return result


@dataclass(frozen=True)
class SoundLoop:
    """One registered loop, the source's ``_Loop`` instance."""

    title: str
    identifier: str
    count: int
    intro: float
    bar: float
    bridge: float
    goto: int
    index: int

    @property
    def bar_count(self) -> int:
        """The last bar number (``count + 1``; bars start at 1)."""
        return self.count + 1

    @property
    def base_correction_ms(self) -> int:
        """The source's ``ceil(-bar * 1000)`` action value (negative)."""
        return math.ceil(-self.bar * 1000)

    @property
    def intro_correction_ms(self) -> int:
        """The source's ``ceil((bar - intro) * 1000)`` intro addend."""
        return math.ceil((self.bar - self.intro) * 1000)

    @property
    def bridge_correction_ms(self) -> int:
        """The source's ``ceil((bar - bridge) * 1000)`` bridge addend."""
        return math.ceil((self.bar - self.bridge) * 1000)


class SoundLoops:
    """Build-time loop registry plus the shared per-loop progress array.

    ``path`` is the source's ``SetPath`` root: :meth:`add_loop` looks in
    ``<path>/<title>/``. Relative paths are used as authored (the source's
    ``SetPath`` is verbatim too).
    """

    def __init__(self, path: str = "") -> None:
        self.path = path
        self._loops: list[SoundLoop] = []
        self._by_title: dict[str, SoundLoop] = {}
        self._bars: Any = None

    # -- registration -------------------------------------------------

    def set_path(self, new_path: str) -> None:
        """Set the root directory used by :meth:`add_loop`."""
        if not isinstance(new_path, str):
            raise EudslError(
                "EUDSL-TYPE",
                f"soundlooper: path must be a string, got {new_path!r}",
            )
        self.path = new_path

    def _segment_path(self, title: str, number: int) -> Optional[str]:
        """The source's ``get_filepath``: shortest zero-padding first."""
        prefix = f"{self.path}/{title}/{title}" if self.path else f"{title}/{title}"
        digits = str(number)
        while len(digits) <= 3:
            candidate = f"{prefix}{digits}.ogg"
            if os.path.isfile(candidate):
                return candidate
            digits = "0" + digits  # the source prepends a "0"
        return None

    def _register(self, loop: SoundLoop) -> SoundLoop:
        if loop.title in self._by_title:
            raise EudslError(
                "EUDSL-BOUNDS",
                f"soundlooper: a loop titled {loop.title!r} is already registered",
            )
        registered = replace(loop, index=len(self._loops))
        self._loops.append(registered)
        self._by_title[registered.title] = registered
        return registered

    def add_loop(self, title: str, goto: int = 1) -> Optional[SoundLoop]:
        """Scan and embed ``<title>000.ogg`` .. ``<title>999.ogg``.

        The first segment is the intro, the second sets the bar length, and the
        last found segment sets the bridge length; ``count`` is the number of
        segments after the intro. Returns the registered loop, or ``None`` when
        all 1000 numbered segments exist (the source's loop also falls through
        without registering).
        """
        if not isinstance(title, str) or not title:
            raise EudslError(
                "EUDSL-TYPE", f"soundlooper: title must be a non-empty string, got {title!r}"
            )
        goto = _require_plain_int(goto, "soundlooper: goto")
        if not 0 <= goto <= 999:
            raise EudslError(
                "EUDSL-RANGE", f"soundlooper: goto must be in [0, 999], got {goto}"
            )
        if self._bars is not None:
            raise EudslError(
                "EUDSL-SCOPE",
                f"soundlooper: cannot add {title!r} after the player is initialized; "
                "register every loop before the first player call",
            )

        identifier = loop_identifier(title, len(self._loops))
        intro = bar = bridge = 0.0
        for number in range(1000):
            path = self._segment_path(title, number)
            if path is not None:
                duration = ogg_duration(path)
                if duration is None:
                    raise EudslError(
                        "EUDSL-FORMAT",
                        f"soundlooper: {path!r} has no audio page to measure",
                    )
                length = round_duration(duration)
                if number == 0:
                    intro = length
                elif bar == 0:
                    bar = length
                bridge = length
                embedFile(f"{identifier}{number:03d}", path)
            elif number == 0:
                continue
            elif intro or bar or bridge:
                return self._register(
                    SoundLoop(
                        title=title,
                        identifier=identifier,
                        count=number - 1,
                        intro=intro,
                        bar=bar,
                        bridge=bridge,
                        goto=goto,
                        index=-1,
                    )
                )
            else:
                raise EudslError(
                    "EUDSL-RANGE",
                    f"soundlooper: no segments found for {title!r} under {self.path!r}",
                )
        return None

    def manual_add_loop(
        self,
        title: str,
        count: int,
        intro: float,
        bar: float,
        bridge: float,
        goto: int = 1,
    ) -> SoundLoop:
        """Register a loop whose segments are already in the MPQ.

        ``title`` is the five-byte archive prefix (the source checks five
        cp949 bytes; the shared py/ts contract is five single-byte ASCII
        characters). ``count`` is the number of segments after the intro.
        """
        if not isinstance(title, str) or len(title) != 5:
            raise EudslError(
                "EUDSL-RANGE",
                f"soundlooper: manual_add_loop title must be 5 characters, got {title!r}",
            )
        if any(ord(char) > 0x7F for char in title):
            raise EudslError(
                "EUDSL-RANGE",
                "soundlooper: manual_add_loop title must be five ASCII characters "
                f"(one byte each), got {title!r}",
            )
        count = _require_plain_int(count, "soundlooper: count")
        if not 0 <= count <= 999:
            raise EudslError(
                "EUDSL-RANGE", f"soundlooper: count must be in [0, 999], got {count}"
            )
        goto = _require_plain_int(goto, "soundlooper: goto")
        if not 0 <= goto <= 999:
            raise EudslError(
                "EUDSL-RANGE", f"soundlooper: goto must be in [0, 999], got {goto}"
            )
        if self._bars is not None:
            raise EudslError(
                "EUDSL-SCOPE",
                f"soundlooper: cannot add {title!r} after the player is initialized; "
                "register every loop before the first player call",
            )
        intro_seconds = _require_seconds(intro, "intro")
        bar_seconds = _require_seconds(bar, "bar")
        bridge_seconds = _require_seconds(bridge, "bridge")
        return self._register(
            SoundLoop(
                title=title,
                identifier=title,
                count=count,
                intro=intro_seconds,
                bar=bar_seconds,
                bridge=bridge_seconds,
                goto=goto,
                index=-1,
            )
        )

    # -- lookup / progress array --------------------------------------

    def __len__(self) -> int:
        return len(self._loops)

    def __iter__(self) -> Iterator[SoundLoop]:
        return iter(self._loops)

    def get(self, title: str) -> Optional[SoundLoop]:
        return self._by_title.get(title)

    def index_of(self, title: str) -> int:
        loop = self._by_title.get(title)
        if loop is None:
            raise EudslError(
                "EUDSL-RANGE", f"soundlooper: no loop titled {title!r}"
            )
        return loop.index

    def bars(self) -> Any:
        """The shared progress array, allocated on first use (the source lazy
        ``SoundLooper._bars``). Registration is frozen afterwards."""
        if self._bars is None:
            if not self._loops:
                raise EudslError(
                    "EUDSL-RANGE",
                    "soundlooper: no loops registered before the player was used",
                )
            self._bars = FixedArray("dword", len(self._loops))
        return self._bars


_CMP_METHOD = {Exactly: "eq", AtLeast: "ge", AtMost: "le"}
_CMP_NAMES = {Exactly: "Exactly", AtLeast: "AtLeast", AtMost: "AtMost"}


class SoundLooper:
    """Loop-sound player (the source's ``SoundLooper``)."""

    def __init__(self, loops: Optional[SoundLoops] = None) -> None:
        self.loops = loops if loops is not None else SoundLoops()
        self.current_loop = Var(0xFFFFFFFF)
        self.previous_loop = Var(0xFFFFFFFF)
        self.current_bar = Var(0)
        self.bar_digits = Var(0)
        #: Largest raw clock value at which the next pass is due.
        self.threshold = Var(0xFFFFFFFE)

        self._segment = Var(0)
        self._intro_add = Var(0)
        self._bridge_add = Var(0)
        self._last_bar = Var(0)
        self._goto_bar = Var(1)
        self._goto_digits = Var(0x10000)
        self._hit = Var(0)
        self._local_player = Var(0)
        self._probe: Optional[Trigger] = None
        self._player_emitted = False

    # -- helpers ------------------------------------------------------

    def _bars(self) -> Any:
        return self.loops.bars()

    def _resolve(self, title: Any) -> Any:
        """Resolve a title / index / ``None`` to a build-time index or `Var`."""
        if title is None:
            return self.current_loop
        if isinstance(title, bool) or not isinstance(title, (str, int, Var)):
            raise EudslError(
                "EUDSL-TYPE",
                "soundlooper: loop must be a registered title, an index, or a Var, "
                f"got {title!r}",
            )
        if isinstance(title, str):
            return self.loops.index_of(title)
        if isinstance(title, int):
            if not 0 <= title < len(self.loops):
                raise EudslError(
                    "EUDSL-RANGE",
                    f"soundlooper: loop index {title} out of range "
                    f"(0..{len(self.loops) - 1})",
                )
            return title
        return title

    def _apply_loop_constants(self, loop: SoundLoop) -> None:
        self._segment.set(loop.base_correction_ms)
        self._intro_add.set(loop.intro_correction_ms)
        self._bridge_add.set(loop.bridge_correction_ms)
        self._last_bar.set(loop.bar_count)
        self._goto_bar.set(loop.goto)
        self._goto_digits.set(pack_bar_digits(loop.goto))

    def _setloop(self, index: Any) -> None:
        """Set the playing loop (the source's ``_setloop``)."""
        # VProc semantics: every argument is read before any assignment, so
        # `resume()` (index == previous_loop) survives the previous_loop write.
        if isinstance(index, Var):
            incoming = Var(0)
            incoming.set(index)
        else:
            incoming = index
        self.previous_loop.set(self.current_loop)
        self.current_loop.set(incoming)

        err = calculate_speed_error()
        if isinstance(incoming, int):
            self._apply_loop_constants(self.loops._loops[incoming])
        else:
            for loop in self.loops:
                when(
                    incoming.eq(loop.index),
                    lambda loop=loop: self._apply_loop_constants(loop),
                )
        self._segment.iadd(err)

    def _digits_from(self, bar: Any) -> None:
        """Write ``bar``'s packed digits into :attr:`bar_digits`."""
        if _is_plain_int(bar):
            self.bar_digits.set(pack_bar_digits(int(bar)))
            return
        if not isinstance(bar, Var):
            raise EudslError(
                "EUDSL-TYPE",
                f"soundlooper: bar must be an integer or a Var, got {bar!r}",
            )
        high, ones = bar.divmod(10)
        hundreds, tens = high.divmod(10)
        self.bar_digits.set(hundreds.add(tens.shl(8)).add(ones.shl(16)))

    # -- build-time emitted player ------------------------------------

    def _ensure_probe(self) -> Trigger:
        if self._probe is None:
            probe = Trigger(
                conds=[
                    Memory(SOUND_CLOCK_ADDR, AtMost, 0xFFFFFFFE),
                    self.current_loop.ne(0xFFFFFFFF),
                ],
                acts=[self._hit.act_set(1)],
                register=False,
            )
            _register_trigger(probe)
            self._probe = probe
        return self._probe

    def _play_current_bar(self) -> None:
        """The source's ``PlaySoundWorkaround``: constant names per loop/bar."""
        for loop in self.loops:
            def per_loop(loop: SoundLoop = loop) -> None:
                for number in range(loop.count + 1):
                    when(
                        self.current_bar.eq(number + 1),
                        lambda name=f"{loop.identifier}{number:03d}": PlayWAV(name),
                    )

            when(self.current_loop.eq(loop.index), per_loop)

    def _tick(self) -> None:
        self._hit.set(0)
        bars = self._bars()
        slot_count = len(self.loops)

        # 1. Advance the bar and the packed digit register (source order).
        self.current_bar.iadd(1)
        self.bar_digits.iadd(1 << 16)
        when(
            DeathsX(self.bar_digits.value, AtLeast, 10 << 16, 0, 0xFF << 16),
            lambda: self.bar_digits.iadd((1 << 8) - (10 << 16)),
        )
        when(
            DeathsX(self.bar_digits.value, AtLeast, 10 << 8, 0, 0xFF << 8),
            lambda: self.bar_digits.iadd(1 - (10 << 8)),
        )
        when(
            self.current_loop.lt(slot_count),
            lambda: bars.set(self.current_loop, self.current_bar),
        )

        # 2. Play the current segment for the local player. The source stores
        #    the local player id into CurrentPlayer for the whole pass; the
        #    port saves and restores it like the BGM helper (ADR 0004 resets
        #    curpl once per trigger loop).
        old_cp = read_current_player_register()
        emitVarStore(self._local_player.id, AddrReloc(CP_ADDR))
        self._play_current_bar()
        emitVarStore(old_cp.id, AddrReloc(CP_ADDR))

        # 3. Schedule the next firing from the raw clock and this segment's
        #    correction (the source patches the probe condition's amount).
        raw = _read_dword(SOUND_CLOCK_ADDR)
        self.threshold.set(raw)
        self.threshold.iadd(self._segment)
        self.threshold.isub(1)  # probe tests `raw <= threshold`
        when(
            self.current_bar.eq(1),
            lambda: self.threshold.iadd(self._intro_add),
        )
        when(
            self.current_bar.ge(self._last_bar),
            lambda: self.threshold.iadd(self._bridge_add),
        )
        probe = self._ensure_probe()
        emitVarStore(
            self.threshold.id,
            TriggerFieldReloc(probe.id, probe.cond(0).amount.byteOffset),
        )

        # 4. After the last bar, return to `goto` (source: "if last bar was
        #    played, go to start").
        def reset() -> None:
            self.current_bar.set(self._goto_bar)
            self.bar_digits.set(self._goto_digits)
            when(
                self.current_loop.lt(slot_count),
                lambda: bars.set(self.current_loop, self._goto_bar),
            )

        when(self.current_bar.ge(self._last_bar), reset)

    def player(self) -> None:
        """Emit the per-loop playback body (the source's ``player()``).

        Call once, after registering loops and arming ``play`` at the call
        site of your choice. The body fires on the trigger loops where the
        countdown clock has reached the next segment boundary and a loop is
        playing.
        """
        if self._player_emitted:
            raise EudslError(
                "EUDSL-SCOPE", "soundlooper: player() may only be emitted once"
            )
        self._player_emitted = True
        self._ensure_probe()
        # eudplib reads the local player id once at map start (EUDOnStart);
        # do the same so the 32-test read stays off the per-segment path.
        once(lambda: self._local_player.set(_read_dword(USER_PLAYER_ADDR)))
        when(self._hit.eq(1), self._tick)

    # -- player controls ----------------------------------------------

    def play(self, title: Any, bar: Any = None) -> None:
        """Play ``title`` (a registered name, an index, a `Var`, or ``None``).

        ``bar`` selects the progress position; omitted, the loop resumes from
        its progress-array slot (``bars[index]``). The next due pass plays
        ``bar + 1``.
        """
        if _is_plain_int(bar) and not isinstance(bar, bool):
            if not 0 <= int(bar) <= 999:
                raise EudslError(
                    "EUDSL-RANGE",
                    f"soundlooper: bar must be in [0, 999], got {bar}",
                )
        elif bar is not None and not isinstance(bar, Var):
            raise EudslError(
                "EUDSL-TYPE",
                f"soundlooper: bar must be an integer, a Var, or None, got {bar!r}",
            )
        index = self._resolve(title)
        self._setloop(index)
        if bar is None:
            self.setbar(self._bars().get(index))
        else:
            self.setbar(bar)

    def pause(self) -> None:
        """Pause the current loop (``previous_loop`` remembers it)."""
        self.previous_loop.set(self.current_loop)
        self.current_loop.set(0xFFFFFFFF)

    def resume(self) -> None:
        """Resume the paused loop."""
        self._setloop(self.previous_loop)

    def toggle(self) -> None:
        """Toggle between playing and paused."""
        when(self.current_loop.eq(0xFFFFFFFF), self.resume)
        when(self.current_loop.ne(0xFFFFFFFF), self.pause)

    def setbar(self, bar: Any) -> None:
        """Set the current loop's progress bar (and its progress slot).

        ``bar`` is the *last played* position: the next due pass advances to
        ``bar + 1`` (the source's progress-array convention).
        """
        self.current_bar.set(bar)
        self._digits_from(bar)
        bars = self._bars()
        when(
            self.current_loop.lt(len(self.loops)),
            lambda: bars.set(self.current_loop, self.current_bar),
        )

    def sendcurrentbar(self, loop: Any) -> None:
        """Copy the current bar into ``loop``'s progress slot."""
        index = self._resolve(loop)
        if not isinstance(index, int):
            raise EudslError(
                "EUDSL-BOUNDS",
                "soundlooper: sendcurrentbar() needs a build-time loop index",
            )
        self._bars().set(index, self.current_bar)

    def sendbar(self, dst: Any, src: Any) -> None:
        """Copy ``src``'s progress into ``dst``'s slot."""
        destination = self._resolve(dst)
        source = self._resolve(src)
        if not isinstance(destination, int) or not isinstance(source, int):
            raise EudslError(
                "EUDSL-BOUNDS",
                "soundlooper: sendbar() needs build-time loop indices",
            )
        self._bars().set(destination, self._bars().get(source))

    def setloopbar(self, loop: Any, bar: Any) -> None:
        """Set a non-playing loop's progress slot."""
        index = self._resolve(loop)
        if not isinstance(index, int):
            raise EudslError(
                "EUDSL-BOUNDS",
                "soundlooper: setloopbar() needs a build-time loop index",
            )
        self._bars().set(index, bar)

    def cond(self, loop: Any, cmptype: int, value: Any) -> Any:
        """A condition on ``loop``'s progress (``Exactly``/``AtLeast``/``AtMost``)."""
        method = _CMP_METHOD.get(cmptype)
        if method is None:
            raise EudslError(
                "EUDSL-RANGE",
                "soundlooper: cond() needs Exactly, AtLeast, or AtMost, got "
                f"{cmptype!r}",
            )
        index = self._resolve(loop)
        if not isinstance(index, int):
            raise EudslError(
                "EUDSL-BOUNDS",
                "soundlooper: cond() needs a build-time loop index",
            )
        element = self._bars().get(index)
        return getattr(element, method)(value)

    def loopis(self, loop: Any) -> Any:
        """A condition matching the currently playing loop."""
        return self.current_loop.eq(self._resolve(loop))


def sound_looper(loops: Optional[SoundLoops] = None) -> SoundLooper:
    """Create a :class:`SoundLooper` (canonical snake_case constructor)."""
    return SoundLooper(loops)


#: The canonical camelCase name (ADR 0002 §3), mirroring ``ts/soundlooper.ts``.
soundLooper = sound_looper

__all__ = [
    "DEFAULT_SPEED_X",
    "GAME_SPEED_ADDR",
    "SOUND_CLOCK_ADDR",
    "SPEED_84_ADDR",
    "SPEED_88_ADDR",
    "SPEED_MAP",
    "SPEED_SENTINEL",
    "SoundLoop",
    "SoundLooper",
    "SoundLoops",
    "calculate_speed_error",
    "loop_identifier",
    "pack_bar_digits",
    "soundLooper",
    "sound_looper",
]
