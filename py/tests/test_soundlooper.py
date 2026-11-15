"""Loop-sound player (``py/soundlooper.py``), a port of euddraft's
``lib/soundlooper.py``. Mirror of ``ts/tests/test_soundlooper.ts``.

Covers the build-time segment scanner (``SoundLoops.add_loop`` /
``manual_add_loop``), the emitted loop structure (intro -> bar -> bridge ->
``goto``), the bar/digit machinery, and the runtime schedule driven by the
``0x51CE8C`` countdown clock.

The fixtures under ``tests/fixtures/sounds/theme`` are the tiny hand-built Ogg
streams from the length-reader tests: ``theme000`` is 1.0 s, ``theme001``
1.5 s and ``theme002`` 2.0 s, so the loop is intro=1.0, bar=1.5,
bridge=2.0, count=2.
"""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from py import (
    Add,
    AtLeast,
    AtMost,
    EudslError,
    Exactly,
    SetMemory,
    SetTo,
    SoundLoop,
    SoundLooper,
    SoundLoops,
    Trigger,
    Var,
    build_module,
    calculate_speed_error,
    loop_identifier,
    once,
    pack_bar_digits,
    sound_looper,
)
from py.cp import CP_ADDR
from py.hir import emitVarStore
from py.soundlooper import (
    DEFAULT_SPEED_X,
    GAME_SPEED_ADDR,
    SOUND_CLOCK_ADDR,
    SPEED_84_ADDR,
    SPEED_88_ADDR,
    SPEED_MAP,
    SPEED_SENTINEL,
    USER_PLAYER_ADDR,
)
from py.tests.helpers import EPD_BASE, read_dword, run_sim, run_static

FIXTURES = Path(__file__).resolve().parent.parent.parent / "tests" / "fixtures"
SOUNDS = FIXTURES / "sounds"
THEME = SOUNDS / "theme"

SENTINEL = 0x600000
CLOCK_START = 1_000_000
LOCAL_PLAYER = 3
SAVED_CP = 7
TICK_MS = 100

ACT_TYPE_SET_MEMORY = 45
ACT_TYPE_PLAY_WAV = 8
ACT_DEST_OFFSET = 16
ACT_VALUE_OFFSET = 20


def _epd(addr: int) -> int:
    return ((addr - EPD_BASE) // 4) & 0xFFFFFFFF


def _module(body) -> dict:
    return json.loads(build_module(body).to_json())["module"]


def _walk(stmts):
    """Yield every statement and action, descending into block bodies.

    Raw action slots (a ``trigger``'s ``acts``) carry no ``kind`` tag, so
    normalize them to the same shape as the ``act`` statements inside a
    ``when`` body.
    """
    for stmt in stmts:
        yield stmt
        if stmt.get("kind") in ("when", "once", "until", "func"):
            yield from _walk(stmt.get("body") or [])
        if stmt.get("kind") == "trigger":
            for act in stmt.get("acts") or []:
                yield {"kind": "act", **act}


def _read_addr(stmts, addr: int, funcs=None) -> bool:
    """True when a bit-test read of ``addr`` appears under ``stmts``.

    Full-dword reads emit one reader body per distinct EPD and the caller
    reaches it through a ``call``; ``funcs`` lets the walk follow those edges.
    """
    epd = _epd(addr)
    by_id = {func["id"]: func for func in (funcs or [])}
    for stmt in _walk(stmts):
        if any(
            cond.get("condtype") == 15 and cond.get("player") == epd
            for cond in (stmt.get("conds") or [])
        ):
            return True
        if stmt.get("kind") == "call":
            body = by_id.get(stmt.get("func"), {}).get("body") or []
            if _read_addr(body, addr):
                return True
    return False


def _plays(stmts) -> list[str]:
    return [
        stmt["wav"]
        for stmt in _walk(stmts)
        if stmt.get("kind") == "act" and stmt.get("acttype") == ACT_TYPE_PLAY_WAV
    ]


def _probe(module: dict) -> dict:
    for stmt in module["stmts"]:
        if stmt["kind"] != "trigger":
            continue
        for cond in stmt["conds"]:
            if cond.get("player") == _epd(SOUND_CLOCK_ADDR):
                return stmt
    raise AssertionError("no clock probe trigger found")


def _tick(module: dict) -> dict:
    """The top-level ``when`` whose body plays a segment."""
    for stmt in module["stmts"]:
        if stmt["kind"] == "when" and _plays(stmt.get("body") or []):
            return stmt
    raise AssertionError("no player tick body found")


def _varstores(stmts, **match) -> list[dict]:
    return [
        stmt
        for stmt in _walk(stmts)
        if stmt.get("kind") == "varstore"
        and all(stmt.get(key) == value for key, value in match.items())
    ]


def _assigned_act(stmts, var_id: int, modifier: int | None = None):
    found = []
    for stmt in _walk(stmts):
        if stmt.get("kind") != "act" or stmt.get("acttype") != ACT_TYPE_SET_MEMORY:
            continue
        if modifier is not None and stmt.get("modifier") != modifier:
            continue
        if any(
            reloc.get("target_var") == var_id
            and reloc.get("target_offset") == ACT_VALUE_OFFSET
            for reloc in stmt.get("relocs", [])
        ):
            found.append(stmt)
    return found


def _theme_module(goto: int = 1, bar=None):
    """A module that registers the theme loop, plays it once, and emits the
    player; the registration object is returned too."""
    holder = {}

    def prog() -> None:
        loops = SoundLoops(str(SOUNDS))
        holder["loops"] = loops
        loops.add_loop("theme", goto=goto)
        looper = SoundLooper(loops)
        holder["looper"] = looper

        def start() -> None:
            looper.play("theme", bar)

        once(start)
        looper.player()

    module = _module(prog)
    return module, holder["loops"], holder["looper"]


class TestLoopIdentifier(unittest.TestCase):
    def test_identifier_is_five_alnum_characters_and_deterministic(self):
        first = loop_identifier("theme", 0)
        self.assertEqual(len(first), 5)
        self.assertTrue(first.isalnum())
        self.assertEqual(first, loop_identifier("theme", 0))
        self.assertNotEqual(first, loop_identifier("theme", 1))
        self.assertNotEqual(first, loop_identifier("other", 0))


class TestPackBarDigits(unittest.TestCase):
    def test_three_decimal_digits_are_little_endian_ascii(self):
        self.assertEqual(pack_bar_digits(0), 0)
        self.assertEqual(pack_bar_digits(1), 0x10000)  # "001"
        self.assertEqual(pack_bar_digits(10), 0x100)  # "010"
        self.assertEqual(pack_bar_digits(123), 0x030201)  # "123"
        self.assertEqual(pack_bar_digits(999), 0x090909)  # "999"

    def test_out_of_range_bars_are_rejected(self):
        for value in (-1, 1000, 70000):
            with self.subTest(value=value):
                with self.assertRaises(EudslError) as caught:
                    pack_bar_digits(value)
                self.assertEqual(caught.exception.code, "EUDSL-RANGE")


class TestAddLoop(unittest.TestCase):
    def test_scans_the_numbered_segments(self):
        holder = {}

        def prog() -> None:
            holder["loop"] = SoundLoops(str(SOUNDS)).add_loop("theme")

        _module(prog)
        loop = holder["loop"]
        self.assertIsInstance(loop, SoundLoop)
        self.assertEqual(loop.title, "theme")
        self.assertEqual(loop.index, 0)
        self.assertEqual(loop.count, 2)
        self.assertEqual(loop.bar_count, 3)
        self.assertEqual((loop.intro, loop.bar, loop.bridge), (1.0, 1.5, 2.0))
        self.assertEqual(loop.goto, 1)
        self.assertEqual(len(loop.identifier), 5)

    def test_embeds_each_segment_under_id_plus_three_digits(self):
        module, loops, _looper = _theme_module()
        identifier = loops.get("theme").identifier
        self.assertEqual(
            module["files"],
            [
                {
                    "name": f"{identifier}{number:03d}",
                    "path": str(THEME / f"theme{number:03d}.ogg"),
                }
                for number in range(3)
            ],
        )

    def test_last_segment_sets_the_bridge_length(self):
        module, loops, _looper = _theme_module()
        loop = loops.get("theme")
        self.assertEqual(loop.bridge, 2.0)
        self.assertEqual(loop.base_correction_ms, -1500)
        self.assertEqual(loop.intro_correction_ms, 500)
        self.assertEqual(loop.bridge_correction_ms, -500)

    def test_shortest_zero_padding_is_tried_first(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            folder = Path(tmpdir) / "song"
            folder.mkdir()
            shutil.copy(THEME / "theme000.ogg", folder / "song0.ogg")

            def prog() -> None:
                loops = SoundLoops(tmpdir)
                loop = loops.add_loop("song")
                self.assertEqual(loop.count, 0)
                self.assertEqual(loop.intro, 1.0)
                self.assertEqual(loop.bar, 0.0)
                self.assertEqual(loop.bridge, 1.0)

            _module(prog)

    def test_a_gap_ends_the_scan(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            folder = Path(tmpdir) / "gap"
            folder.mkdir()
            shutil.copy(THEME / "theme000.ogg", folder / "gap000.ogg")
            shutil.copy(THEME / "theme002.ogg", folder / "gap002.ogg")

            def prog() -> None:
                loops = SoundLoops(tmpdir)
                loop = loops.add_loop("gap")
                # Segment 001 is missing: the scan stops after 000, so the
                # intro is the only measured segment and there is no bar.
                self.assertEqual(loop.count, 0)
                self.assertEqual(loop.intro, 1.0)
                self.assertEqual(loop.bar, 0.0)
                self.assertEqual(loop.bridge, 1.0)

            _module(prog)

    def test_no_segments_is_an_error(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            def prog() -> None:
                SoundLoops(tmpdir).add_loop("absent")

            with self.assertRaises(EudslError) as caught:
                build_module(prog)
            self.assertEqual(caught.exception.code, "EUDSL-RANGE")

    def test_unknown_loop_title_is_rejected_by_play(self):
        module, loops, looper = _theme_module()

        def prog() -> None:
            looper.play("absent")

        with self.assertRaises(EudslError) as caught:
            build_module(prog)
        self.assertEqual(caught.exception.code, "EUDSL-RANGE")

    def test_duplicate_title_is_rejected(self):
        def prog() -> None:
            loops = SoundLoops(str(SOUNDS))
            loops.add_loop("theme")
            loops.add_loop("theme")

        with self.assertRaises(EudslError) as caught:
            build_module(prog)
        self.assertEqual(caught.exception.code, "EUDSL-BOUNDS")

    def test_registration_is_frozen_after_the_player_is_used(self):
        def prog() -> None:
            loops = SoundLoops(str(SOUNDS))
            loops.add_loop("theme")
            looper = SoundLooper(loops)
            looper.play("theme")
            loops.add_loop("theme")

        with self.assertRaises(EudslError) as caught:
            build_module(prog)
        self.assertEqual(caught.exception.code, "EUDSL-SCOPE")


class TestManualAddLoop(unittest.TestCase):
    def test_registers_a_pre_embedded_loop(self):
        loops = SoundLoops()
        loop = loops.manual_add_loop("ABCDE", 4, 1.0, 2.5, 3.0, goto=2)
        self.assertEqual(loop.identifier, "ABCDE")
        self.assertEqual(loop.count, 4)
        self.assertEqual(loop.bar_count, 5)
        self.assertEqual((loop.intro, loop.bar, loop.bridge), (1.0, 2.5, 3.0))
        self.assertEqual(loop.goto, 2)

    def test_title_must_be_five_single_byte_characters(self):
        for title in ("ABCD", "ABCDEF", "가나다라마"):
            with self.subTest(title=title):
                with self.assertRaises(EudslError) as caught:
                    SoundLoops().manual_add_loop(title, 1, 1.0, 1.0, 1.0)
                self.assertEqual(caught.exception.code, "EUDSL-RANGE")

    def test_five_ascii_characters_are_accepted(self):
        # The source checks five cp949 *bytes* only, so a space is a legal
        # archive-name character; the port keeps that contract (five ASCII
        # bytes, no whitespace filtering).
        loop = SoundLoops().manual_add_loop("A BCD", 1, 1.0, 1.0, 1.0)
        self.assertEqual(loop.identifier, "A BCD")

    def test_count_and_goto_ranges(self):
        for kwargs in ({"count": -1}, {"count": 1000}, {"goto": -1}, {"goto": 1000}):
            with self.subTest(kwargs=kwargs):
                args = dict(count=1, intro=1.0, bar=1.0, bridge=1.0, goto=1)
                args.update(kwargs)
                with self.assertRaises(EudslError) as caught:
                    SoundLoops().manual_add_loop("ABCDE", **args)
                self.assertEqual(caught.exception.code, "EUDSL-RANGE")

    def test_lengths_must_be_finite_and_non_negative(self):
        cases = (
            ({"intro": "1.0"}, "EUDSL-TYPE"),
            ({"bar": float("nan")}, "EUDSL-TYPE"),
            ({"bridge": -1.0}, "EUDSL-RANGE"),
        )
        for kwargs, code in cases:
            with self.subTest(kwargs=kwargs):
                args = dict(count=1, intro=1.0, bar=1.0, bridge=1.0)
                args.update(kwargs)
                with self.assertRaises(EudslError) as caught:
                    SoundLoops().manual_add_loop("ABCDE", **args)
                self.assertEqual(caught.exception.code, code)


class TestBarsArray(unittest.TestCase):
    def test_bars_are_allocated_once_and_shared(self):
        def prog() -> None:
            loops = SoundLoops()
            loops.manual_add_loop("ABCDE", 2, 1.0, 2.0, 3.0)
            loops.manual_add_loop("FGHIJ", 1, 1.0, 2.0, 2.0)
            bars = loops.bars()
            self.assertEqual(bars.length, 2)
            self.assertIs(bars, loops.bars())

        _module(prog)

    def test_no_loops_before_bars_is_an_error(self):
        with self.assertRaises(EudslError) as caught:
            SoundLoops().bars()
        self.assertEqual(caught.exception.code, "EUDSL-RANGE")


class TestSoundLooperSurface(unittest.TestCase):
    def test_camel_case_alias_constructs_the_same_class(self):
        holder = {}

        def prog() -> None:
            holder["looper"] = sound_looper(SoundLoops())

        _module(prog)
        self.assertIsInstance(holder["looper"], SoundLooper)

    def test_play_tracks_current_and_previous_loop(self):
        module, loops, looper = _theme_module()
        # Inside `once(start)`: previous_loop := current_loop, then
        # current_loop := 0.
        once_body = next(s for s in module["stmts"] if s["kind"] == "once")["body"]
        transfers = [s for s in once_body if s["kind"] == "vartransfer"]
        self.assertTrue(
            any(
                s["dst_var"] == looper.previous_loop.id
                and s["src_var"] == looper.current_loop.id
                for s in transfers
            )
        )
        sets = _assigned_act(once_body, looper.current_loop.id, SetTo)
        self.assertTrue(
            any(a["value"] == 0 for a in sets),
            "play(const index) sets current_loop to that index",
        )

    def test_pause_moves_current_to_previous_and_clears_it(self):
        holder = {}

        def prog() -> None:
            loops = SoundLoops()
            loops.manual_add_loop("ABCDE", 2, 1.0, 2.0, 3.0)
            looper = SoundLooper(loops)
            holder["looper"] = looper
            once(lambda: (looper.play("ABCDE"), looper.pause()))

        module = _module(prog)
        looper = holder["looper"]
        once_body = next(s for s in module["stmts"] if s["kind"] == "once")["body"]
        transfers = [s for s in once_body if s["kind"] == "vartransfer"]
        self.assertTrue(
            any(
                s["dst_var"] == looper.previous_loop.id
                and s["src_var"] == looper.current_loop.id
                for s in transfers
            )
        )
        self.assertTrue(
            any(
                s["dst_var"] == looper.current_loop.id and s["src_var"] == looper.current_loop.id
                for s in transfers
            ) is False
        )
        # The -1 write is a `SetDeaths` act or a vartransfer from a constant.
        acts = _assigned_act(once_body, looper.current_loop.id)
        self.assertTrue(acts, "current_loop must be cleared on pause")

    def test_toggle_emits_both_branches(self):
        holder = {}

        def prog() -> None:
            loops = SoundLoops()
            loops.manual_add_loop("ABCDE", 2, 1.0, 2.0, 3.0)
            looper = SoundLooper(loops)
            holder["looper"] = looper
            once(lambda: (looper.play("ABCDE"), looper.toggle()))

        module = _module(prog)
        once_body = next(s for s in module["stmts"] if s["kind"] == "once")["body"]
        whens = [s for s in once_body if s["kind"] == "when"]
        self.assertGreaterEqual(len(whens), 2)
        pairs = [
            (c["comparison"], c["amount"])
            for s in whens
            for c in s["conds"]
        ]
        self.assertIn((Exactly, 0xFFFFFFFF), pairs)

    def test_sendbar_copies_between_progress_slots(self):
        holder = {}

        def prog() -> None:
            loops = SoundLoops()
            loops.manual_add_loop("ABCDE", 2, 1.0, 2.0, 3.0)
            loops.manual_add_loop("FGHIJ", 2, 1.0, 2.0, 3.0)
            looper = SoundLooper(loops)
            holder["loops"] = loops
            looper.sendbar("FGHIJ", "ABCDE")

        module = _module(prog)
        bars = holder["loops"].bars()
        transfers = [
            s for s in module["stmts"] if s["kind"] == "vartransfer"
        ]
        self.assertTrue(
            any(
                s["dst_var"] == bars.get(1).id and s["src_var"] == bars.get(0).id
                for s in transfers
            )
        )

    def test_setloopbar_and_sendcurrentbar_write_the_slot(self):
        holder = {}

        def prog() -> None:
            loops = SoundLoops()
            loops.manual_add_loop("ABCDE", 2, 1.0, 2.0, 3.0)
            looper = SoundLooper(loops)
            holder["loops"] = loops
            holder["looper"] = looper
            looper.setloopbar("ABCDE", 2)
            looper.sendcurrentbar("ABCDE")

        module = _module(prog)
        looper = holder["looper"]
        bars = holder["loops"].bars()
        acts = _assigned_act(module["stmts"], bars.get(0).id)
        self.assertTrue(acts, "setloopbar must write the progress slot")
        self.assertTrue(
            any(
                s["dst_var"] == bars.get(0).id
                and s["src_var"] == looper.current_bar.id
                for s in module["stmts"]
                if s["kind"] == "vartransfer"
            ),
            "sendcurrentbar must copy current_bar into the slot",
        )

    def test_cond_and_loopis_are_conditions(self):
        holder = {}

        def prog() -> None:
            loops = SoundLoops()
            loops.manual_add_loop("ABCDE", 2, 1.0, 2.0, 3.0)
            looper = SoundLooper(loops)
            holder["looper"] = looper
            holder["loops"] = loops

            def use():
                when = __import__("py").when
                when([looper.cond("ABCDE", AtLeast, 2), looper.loopis("ABCDE")], lambda: None)

            once(use)

        module = _module(prog)
        looper = holder["looper"]
        bars = holder["loops"].bars()
        once_body = next(s for s in module["stmts"] if s["kind"] == "once")["body"]
        conds = [c for s in once_body for c in (s.get("conds") or [])]
        self.assertTrue(
            any(
                any(
                    reloc.get("target_var") == looper.current_loop.id
                    and reloc.get("target_offset") == 20
                    for reloc in c.get("relocs", [])
                )
                for c in conds
            ),
            "loopis reads the current_loop slot",
        )
        # cond(..., AtLeast, 2) materializes as a masked or plain AtLeast read
        self.assertTrue(
            any(
                c["comparison"] == AtLeast
                and c["amount"] == 2
                and any(
                    reloc.get("target_var") == bars.get(0).id
                    for reloc in c.get("relocs", [])
                )
                for c in conds
            )
        )

    def test_play_bar_argument_is_packed_into_digits(self):
        module, loops, looper = _theme_module(bar=2)
        once_body = next(s for s in module["stmts"] if s["kind"] == "once")["body"]
        acts = _assigned_act(once_body, looper.bar_digits.id, SetTo)
        self.assertTrue(
            any(a["value"] == pack_bar_digits(2) for a in acts),
            "setbar(2) must pack the three digits",
        )

    def test_dynamic_index_methods_need_constants(self):
        holder = {}

        def prog() -> None:
            loops = SoundLoops()
            loops.manual_add_loop("ABCDE", 2, 1.0, 2.0, 3.0)
            looper = SoundLooper(loops)
            index = Var(0)
            holder["calls"] = [
                lambda: looper.sendcurrentbar(index),
                lambda: looper.sendbar("ABCDE", index),
                lambda: looper.setloopbar(index, 1),
                lambda: looper.cond(index, Exactly, 1),
            ]

        _module(prog)
        for call in holder["calls"]:
            with self.assertRaises(EudslError) as caught:
                call()
            self.assertEqual(caught.exception.code, "EUDSL-BOUNDS")

    def test_play_setbar_and_cond_validate_their_arguments(self):
        def prog() -> None:
            loops = SoundLoops()
            loops.manual_add_loop("ABCDE", 2, 1.0, 2.0, 3.0)
            looper = SoundLooper(loops)
            cases = [
                (lambda: looper.play(5), "EUDSL-RANGE"),
                (lambda: looper.play("ABCDE", 1000), "EUDSL-RANGE"),
                (lambda: looper.setbar(1000), "EUDSL-RANGE"),
                (lambda: looper.cond("ABCDE", 12345, 1), "EUDSL-RANGE"),
                (lambda: looper.play(1.5), "EUDSL-TYPE"),
            ]
            for call, code in cases:
                with self.assertRaises(EudslError) as caught:
                    call()
                self.assertEqual(caught.exception.code, code)

        build_module(prog)


class TestLoopStructure(unittest.TestCase):
    def test_probe_is_a_clock_read_gated_on_a_playing_loop(self):
        module, _loops, looper = _theme_module()
        probe = _probe(module)
        self.assertEqual(len(probe["conds"]), 2)
        clock, playing = probe["conds"]
        self.assertEqual(clock["condtype"], 15)
        self.assertEqual(clock["comparison"], AtMost)
        self.assertEqual(clock["amount"], 0xFFFFFFFE)
        self.assertEqual(playing["condtype"], 15)
        self.assertEqual(playing["comparison"], AtMost)
        self.assertEqual(playing["amount"], 0xFFFFFFFE)
        self.assertEqual(looper.current_loop.value is not None, True)

    def test_tick_plays_every_segment_of_the_loop(self):
        module, loops, _looper = _theme_module()
        identifier = loops.get("theme").identifier
        self.assertEqual(
            _plays(_tick(module)["body"]),
            [f"{identifier}{number:03d}" for number in range(3)],
        )

    def test_tick_patches_the_probe_threshold(self):
        module, _loops, looper = _theme_module()
        probe = _probe(module)
        # The amount field offset is 16 within the condition slot.
        patches = [
            s
            for s in _varstores(_tick(module)["body"], dst_trigger=probe["id"])
            if s.get("dst_offset") == 16
        ]
        self.assertEqual(len(patches), 1)
        self.assertEqual(patches[0]["src_var"], looper.threshold.id)

    def test_schedule_constants_match_the_source(self):
        module, loops, looper = _theme_module()
        once_body = next(s for s in module["stmts"] if s["kind"] == "once")["body"]
        loop = loops.get("theme")
        expected = {
            looper._intro_add.id: loop.intro_correction_ms,
            looper._bridge_add.id: loop.bridge_correction_ms,
            looper._last_bar.id: loop.bar_count,
            looper._goto_bar.id: loop.goto,
            looper._goto_digits.id: pack_bar_digits(loop.goto),
        }
        for var_id, value in expected.items():
            acts = _assigned_act(once_body, var_id, SetTo)
            self.assertTrue(
                any(a["value"] == (value & 0xFFFFFFFF) for a in acts),
                f"var {var_id} must be set to {value}",
            )
        # `_segment` holds the negative base correction before `err` is added.
        bases = _assigned_act(once_body, looper._segment.id, SetTo)
        self.assertTrue(
            any(
                a["value"] == (loop.base_correction_ms & 0xFFFFFFFF) for a in bases
            )
        )

    def test_tick_applies_intro_and_bridge_corrections(self):
        module, _loops, looper = _theme_module()
        body = _tick(module)["body"]
        adds = [
            s
            for s in _walk(body)
            if s.get("kind") == "vartransfer"
            and s.get("dst_var") == looper.threshold.id
            and s.get("modifier") == Add
        ]
        sources = {s.get("src_var") for s in adds}
        self.assertIn(looper._intro_add.id, sources)
        self.assertIn(looper._bridge_add.id, sources)
        self.assertIn(looper._segment.id, sources)

    def test_tick_switches_cp_around_the_play(self):
        module, _loops, _looper = _theme_module()
        body = _tick(module)["body"]
        cp_stores = [
            (i, s)
            for i, s in enumerate(body)
            if s.get("kind") == "varstore" and s.get("dst_addr") == CP_ADDR
        ]
        play_at = next(
            i
            for i, s in enumerate(body)
            if s.get("kind") == "when" and _plays(s.get("body") or [])
        )
        self.assertEqual(len(cp_stores), 2)
        self.assertLess(cp_stores[0][0], play_at)
        self.assertLess(play_at, cp_stores[1][0])

    def test_local_player_is_read_once(self):
        module, _loops, _looper = _theme_module()
        funcs = module.get("funcs", [])
        self.assertTrue(
            _read_addr(
                [s for s in module["stmts"] if s["kind"] == "once"],
                USER_PLAYER_ADDR,
                funcs,
            )
        )
        self.assertFalse(_read_addr(_tick(module)["body"], USER_PLAYER_ADDR, funcs))

    def test_digit_carries_follow_the_source(self):
        module, _loops, looper = _theme_module()
        body = _tick(module)["body"]
        # The mask lives in the condition's `locid` field (DeathsX row).
        carries = [
            s
            for s in body
            if s.get("kind") == "when"
            and any(c.get("locid") == 0xFF0000 for c in s["conds"])
        ]
        self.assertEqual(len(carries), 1, "one ones->tens carry")
        carries = [
            s
            for s in body
            if s.get("kind") == "when"
            and any(c.get("locid") == 0xFF00 for c in s["conds"])
        ]
        self.assertEqual(len(carries), 1, "one tens->hundreds carry")

    def test_last_bar_resets_to_goto(self):
        module, loops, looper = _theme_module(goto=1)
        body = _tick(module)["body"]
        # The reset block is the when whose body assigns current_bar = goto.
        resets = [
            s
            for s in body
            if s.get("kind") == "when"
            and any(
                b.get("kind") == "vartransfer"
                and b.get("dst_var") == looper.current_bar.id
                and b.get("src_var") == looper._goto_bar.id
                for b in s.get("body") or []
            )
        ]
        self.assertEqual(len(resets), 1)
        self.assertTrue(_plays(resets[0]["body"]) == [])


class TestSpeedError(unittest.TestCase):
    def test_constants_match_the_source(self):
        self.assertEqual(DEFAULT_SPEED_X, (-41) & 0xFFFFFFFF)
        self.assertEqual(SPEED_SENTINEL, 42)
        self.assertEqual(SPEED_MAP[0], ((2047, 2047), 29))
        self.assertEqual(SPEED_MAP[-1], ((71429, 71429), 1))

    def _error(self, speed: int = 0, sentinel: int = 0, pair=(0, 0)) -> int:
        def prog() -> None:
            SetMemory(GAME_SPEED_ADDR, SetTo, sentinel)
            SetMemory(SPEED_84_ADDR, SetTo, pair[0])
            SetMemory(SPEED_88_ADDR, SetTo, pair[1])
            err = calculate_speed_error()
            emitVarStore(err.id, {"addr": SENTINEL})

        report = run_sim(build_module(prog).to_json(), 1)
        return read_dword(report, _epd(SENTINEL))

    def test_default_correction_is_19(self):
        self.assertEqual(self._error(), 19)

    def test_sentinel_42_uses_the_dword_value(self):
        # x = 42 -> (493 - 294) // 41 = 4
        self.assertEqual(self._error(sentinel=42), 4)

    def test_speed_pair_overrides_the_reading(self):
        # x = 29 -> (493 - 203) // 41 = 7
        self.assertEqual(self._error(pair=(2047, 2047)), 7)
        # x = 36 -> (493 - 252) // 41 = 5
        self.assertEqual(self._error(pair=(1157, 1157)), 5)


def _runtime_module(loops_factory, play, extra=None):
    def prog() -> None:
        loops = loops_factory()
        looper = SoundLooper(loops)

        def seed() -> None:
            SetMemory(SOUND_CLOCK_ADDR, SetTo, CLOCK_START)
            SetMemory(USER_PLAYER_ADDR, SetTo, LOCAL_PLAYER)
            SetMemory(CP_ADDR, SetTo, SAVED_CP)
            play(loops, looper)

        once(seed)
        looper.player()

        def dump() -> None:
            emitVarStore(looper.current_bar.id, {"addr": SENTINEL + 0})
            emitVarStore(looper.current_loop.id, {"addr": SENTINEL + 4})
            emitVarStore(looper.bar_digits.id, {"addr": SENTINEL + 8})
            emitVarStore(looper.threshold.id, {"addr": SENTINEL + 12})
            emitVarStore(looper.previous_loop.id, {"addr": SENTINEL + 16})
            for index in range(len(loops)):
                emitVarStore(loops.bars().get(index).id, {"addr": SENTINEL + 20 + 4 * index})
            if extra is not None:
                extra(loops, looper)

        dump()
        Trigger(
            conds=[],
            acts=[SetMemory(SOUND_CLOCK_ADDR, Add, -TICK_MS)],
            preserved=True,
        )

    return build_module(prog)


def _theme_runtime(goto: int = 1, bar=None):
    return _runtime_module(
        lambda: _theme_loops(goto),
        lambda loops, looper: looper.play("theme", bar),
    )


def _theme_loops(goto: int = 1) -> SoundLoops:
    loops = SoundLoops(str(SOUNDS))
    loops.add_loop("theme", goto=goto)
    return loops


def _sentinel(report, offset: int) -> int:
    return read_dword(report, _epd(SENTINEL + offset))


class TestRuntimeSchedule(unittest.TestCase):
    def test_first_pass_plays_the_intro_bar(self):
        module = _theme_runtime()
        report = run_sim(module.to_json(), 1)
        self.assertEqual(_sentinel(report, 0), 1)  # bar 1
        self.assertEqual(_sentinel(report, 4), 0)  # loop index 0
        self.assertEqual(_sentinel(report, 8), pack_bar_digits(1))
        self.assertEqual(_sentinel(report, 20), 1)  # bars[0]
        # raw + err(19) + ceil(-1.5s) + intro add(500) - 1
        self.assertEqual(_sentinel(report, 12), CLOCK_START + 19 - 1500 + 500 - 1)

    def test_second_bar_comes_after_the_intro_length(self):
        module = _theme_runtime()
        # 1.0 s intro at 100 ms/loop: due on loop 11.
        for loops in range(2, 11):
            report = run_sim(module.to_json(), loops)
            self.assertEqual(_sentinel(report, 0), 1, f"loop {loops}")
        report = run_sim(module.to_json(), 11)
        self.assertEqual(_sentinel(report, 0), 2)
        self.assertEqual(_sentinel(report, 8), pack_bar_digits(2))
        self.assertEqual(_sentinel(report, 20), 2)

    def test_bridge_bar_resets_to_goto(self):
        module = _theme_runtime()
        # bar 2 is due at loop 11; bar 3 (bridge) 1.5 s later, loop 26.
        report = run_sim(module.to_json(), 26)
        # The last-bar pass plays the bridge and resets in the same pass, so
        # the observable state is the loop's `goto` bar.
        self.assertEqual(_sentinel(report, 0), 1)
        self.assertEqual(_sentinel(report, 8), pack_bar_digits(1))
        self.assertEqual(_sentinel(report, 20), 1)

    def test_loop_returns_to_the_goto_sound(self):
        module = _theme_runtime()
        # Bar 3 (the bridge, reset in its own pass) is due at loop 26 with a
        # 2.0 s bridge, so the next bar is due 20 loops later, at loop 46.
        report = run_sim(module.to_json(), 46)
        self.assertEqual(_sentinel(report, 0), 2)

    def test_cp_is_restored_after_playwav(self):
        module = _theme_runtime()
        report = run_sim(module.to_json(), 1)
        self.assertEqual(read_dword(report, _epd(CP_ADDR)), SAVED_CP)

    def test_paused_player_does_not_advance(self):
        def prog() -> None:
            loops = _theme_loops()
            looper = SoundLooper(loops)

            def seed() -> None:
                SetMemory(SOUND_CLOCK_ADDR, SetTo, CLOCK_START)
                SetMemory(USER_PLAYER_ADDR, SetTo, LOCAL_PLAYER)
                SetMemory(CP_ADDR, SetTo, SAVED_CP)
                looper.play("theme")
                looper.pause()

            once(seed)
            looper.player()
            emitVarStore(looper.current_bar.id, {"addr": SENTINEL + 0})
            emitVarStore(looper.current_loop.id, {"addr": SENTINEL + 4})
            Trigger(
                conds=[],
                acts=[SetMemory(SOUND_CLOCK_ADDR, Add, -TICK_MS)],
                preserved=True,
            )

        module = build_module(prog)
        report = run_sim(module.to_json(), 20)
        self.assertEqual(_sentinel(report, 0), 0, "a paused player never advances")
        self.assertEqual(_sentinel(report, 4), 0xFFFFFFFF)

    def test_setbar_starts_at_the_following_bar(self):
        module = _theme_runtime(bar=1)
        report = run_sim(module.to_json(), 1)
        self.assertEqual(_sentinel(report, 0), 2, "bar 1 set -> next pass plays 2")
        self.assertEqual(_sentinel(report, 8), pack_bar_digits(2))

    def test_digit_carry_at_ten(self):
        def loops_factory() -> SoundLoops:
            # 200 ms bars keep the sim short: the digits must still carry at
            # 9 -> 10 exactly like the source's decimal increments.
            loops = SoundLoops()
            loops.manual_add_loop("ABCDE", 12, 1.0, 0.2, 0.2, goto=1)
            return loops

        module = _runtime_module(
            loops_factory, lambda loops, looper: looper.play("ABCDE")
        )
        observed = {}
        for ticks in range(1, 40):
            report = run_sim(module.to_json(), ticks)
            observed[ticks] = (_sentinel(report, 0), _sentinel(report, 8))
        bars = {bar for bar, _digits in observed.values()}
        for bar in range(1, 13):
            self.assertIn(bar, bars, f"bar {bar} must be reached")
        for ticks, (bar, digits) in observed.items():
            if bar == 0:
                continue
            self.assertEqual(
                digits,
                pack_bar_digits(bar),
                f"loop {ticks}: bar {bar} must carry the packed digits",
            )
        self.assertEqual(pack_bar_digits(9), 0x90000)
        self.assertEqual(pack_bar_digits(10), 0x100)

    def test_setbar_var_argument_packs_the_digits_at_runtime(self):
        holder = {}

        def factory() -> SoundLoops:
            loops = SoundLoops()
            loops.manual_add_loop("ABCDE", 12, 1.0, 1.5, 2.0, goto=1)
            return loops

        def play(loops, looper) -> None:
            bar = Var(7)
            holder["bar"] = bar
            looper.play("ABCDE", bar)

        module = _runtime_module(factory, play)
        report = run_sim(module.to_json(), 1)
        # setbar(7) sets the *last played* position; the first due pass
        # advances to bar 8 and packs "008".
        self.assertEqual(_sentinel(report, 8), pack_bar_digits(8))
        self.assertEqual(_sentinel(report, 0), 8)

    def test_bars_slot_mirrors_the_current_bar(self):
        module = _theme_runtime()
        for loops in (1, 5, 11):
            report = run_sim(module.to_json(), loops)
            self.assertEqual(_sentinel(report, 20), _sentinel(report, 0))


def _digits_kernel_module():
    """The isolated digits kernel: `_digits_from` on a runtime bar."""

    def prog() -> None:
        looper = SoundLooper(SoundLoops())
        looper._digits_from(Var(123))

    return build_module(prog)


class TestRuntimeCost(unittest.TestCase):
    def test_digits_kernel_is_two_divmod_ladders(self):
        # RT2 pins the isolated digits kernel, not a full due pass: the ticket
        # measured 130T/116C for the four div/mod ladders and 69T/58C for two
        # divmod(10) ladders. The action counts (not part of the ticket's T/C
        # table) are 323 static / 597 executed, down from 374 / 657.
        module = _digits_kernel_module()
        static = run_static(module.to_json())["static_cost"]
        self.assertEqual(
            (static["triggers"], static["conditions"]),
            (69, 58),
            "two constant-divisor divmod ladders (was 130T/116C)",
        )
        executed = run_sim(module.to_json(), 1)["executed_cost"]
        self.assertEqual(
            (executed["triggers"], executed["conditions"]),
            (75, 58),
            "one due pass of the digits kernel (was 137T/116C)",
        )

    def test_idle_loop_is_two_conditions(self):
        def prog() -> None:
            loops = SoundLoops()
            loops.manual_add_loop("ABCDE", 2, 1.0, 2.0, 3.0)
            looper = SoundLooper(loops)
            looper.player()

        module = build_module(prog)
        report = run_sim(module.to_json(), 2)
        cost = report["executed_cost"]
        self.assertEqual(
            (cost["triggers"], cost["conditions"], cost["actions"]),
            (2, 3, 0),
            "idle = probe (2 conds) + the when gate, no clock read",
        )

    def test_static_cost_is_reported(self):
        module = _theme_runtime()
        static = run_static(module.to_json())["static_cost"]
        self.assertGreater(static["triggers"], 1)


if __name__ == "__main__":
    unittest.main()
