"""Periodic BGM helper (`play_bgm`) and the general MPQ embed surface.

Covers the euddraft ``plugins/bgmplayer.py`` port: the file is embedded as
``bgm``, the first loop plays immediately, replays wait ``length`` seconds
(minus eudplib's 42 ms start-latency compensation), the idle loop never reads
the clock, and CurrentPlayer is restored after PlayWAV.
"""

from __future__ import annotations

import json
import unittest

from py import (
    Add,
    EudslError,
    SetMemory,
    SetTo,
    Trigger,
    Var,
    build_module,
    embedFile,
    once,
    play_bgm,
)
from py.bgm import (
    BGM_ARCHIVE_NAME,
    BGM_CLOCK_ADDR,
    CP_ADDR,
    START_LATENCY_MS,
)
from py.camera import USER_PLAYER_ADDR
from py.hir import emitVarStore
from py.tests.helpers import EPD_BASE, read_dword, run_sim, run_static

SENTINEL_ADDR = 0x600000
CLOCK_START = 1_000_000
LOCAL_PLAYER = 3
SAVED_CP = 7


def _epd(addr: int) -> int:
    return (addr - EPD_BASE) // 4


def _module_json(body) -> dict:
    return json.loads(build_module(body).to_json())


def _when_body(module: dict) -> list[dict]:
    when = next(s for s in module["module"]["stmts"] if s["kind"] == "when")
    return when["body"]


def _once_body(module: dict) -> list[dict]:
    once = next(s for s in module["module"]["stmts"] if s["kind"] == "once")
    return once["body"]


def _reads_addr(stmts: list[dict], addr: int, funcs: list[dict] | None = None) -> bool:
    """True when a read of ``addr`` appears in ``stmts`` or a subroutine they call.

    Full-dword reads are one reader body per distinct EPD: the body carries the
    EPD as a literal `player` field on each of its 32 bit tests, and the caller
    reaches it through a ``call``. Partial reads stay inline and keep the
    bit-test condition shape.
    """
    epd = _epd(addr) & 0xFFFFFFFF
    by_id = {func["id"]: func for func in (funcs or [])}
    for stmt in stmts:
        if any(
            cond.get("condtype") == 15 and cond.get("player") == epd
            for cond in (stmt.get("conds") or [])
        ):
            return True
        if stmt.get("kind") == "call":
            body = by_id.get(stmt.get("func"), {}).get("body") or []
            if _reads_addr(body, addr):
                return True
    return False


def _probe(module: dict) -> dict:
    """The top-level clock probe trigger (one AtMost condition on 0x51CE8C)."""
    for stmt in module["module"]["stmts"]:
        if stmt["kind"] != "trigger":
            continue
        for cond in stmt["conds"]:
            if cond["player"] == _epd(BGM_CLOCK_ADDR) & 0xFFFFFFFF:
                return stmt
    raise AssertionError("no clock probe trigger found")


def _play_module(period_seconds: float = 1.0, clock_start: int = CLOCK_START,
                 local_player: int = LOCAL_PLAYER, saved_cp: int = SAVED_CP,
                 tick_ms: int = 100, sentinel: bool = True):
    """Module that arms the helper, ticks the countdown clock, and optionally
    exposes the returned threshold at ``SENTINEL_ADDR``."""

    def prog() -> None:
        once(
            lambda: (
                SetMemory(BGM_CLOCK_ADDR, SetTo, clock_start),
                SetMemory(USER_PLAYER_ADDR, SetTo, local_player),
                SetMemory(CP_ADDR, SetTo, saved_cp),
            )
        )
        threshold = play_bgm("assets/bgm.wav", period_seconds)
        if sentinel:
            emitVarStore(threshold.id, {"addr": SENTINEL_ADDR})
        Trigger(conds=[], acts=[SetMemory(BGM_CLOCK_ADDR, Add, -tick_ms)],
                preserved=True)

    return build_module(prog)


class TestPlayBgmHir(unittest.TestCase):
    def test_embeds_file_as_bgm_and_returns_threshold(self):
        mod = build_module(lambda: play_bgm("audio/track.wav", 1.25))
        doc = json.loads(mod.to_json())
        self.assertEqual(
            doc["module"]["files"],
            [{"name": BGM_ARCHIVE_NAME, "path": "audio/track.wav"}],
        )

    def test_probe_is_always_due_on_the_first_loop(self):
        doc = _module_json(lambda: play_bgm("bgm.wav", 1.0))
        probe = _probe(doc)
        self.assertEqual(len(probe["conds"]), 1)
        cond = probe["conds"][0]
        self.assertEqual(cond["condtype"], 15)  # Deaths / Memory wire form
        self.assertEqual(cond["player"], _epd(BGM_CLOCK_ADDR) & 0xFFFFFFFF)
        self.assertEqual(cond["comparison"], 1)  # AtMost
        self.assertEqual(cond["amount"], 0xFFFFFFFF)

    def test_playwav_runs_between_cp_switch_and_restore(self):
        doc = _module_json(lambda: play_bgm("bgm.wav", 1.0))
        body = _when_body(doc)
        cp_stores = [
            (i, stmt)
            for i, stmt in enumerate(body)
            if stmt.get("kind") == "varstore" and stmt.get("dst_addr") == CP_ADDR
        ]
        plays = [
            (i, stmt)
            for i, stmt in enumerate(body)
            if stmt.get("kind") == "act" and stmt.get("wav") == BGM_ARCHIVE_NAME
        ]
        self.assertEqual(len(cp_stores), 2, "one CP switch and one restore")
        self.assertEqual(len(plays), 1)
        switch_at, restore_at = cp_stores[0][0], cp_stores[1][0]
        play_at = plays[0][0]
        self.assertLess(switch_at, play_at, "CP is switched before PlayWAV")
        self.assertLess(play_at, restore_at, "CP is restored after PlayWAV")
        self.assertEqual(plays[0][1]["acttype"], 8)  # PlayWAV

    def test_threshold_is_patched_into_the_probe_condition(self):
        doc = _module_json(lambda: play_bgm("bgm.wav", 1.0))
        probe = _probe(doc)
        body = _when_body(doc)
        # Condition slot base (+8) plus the amount field offset (+8).
        amount_offset = 16
        patches = [
            stmt
            for stmt in body
            if stmt.get("kind") == "varstore"
            and stmt.get("dst_trigger") == probe["id"]
            and stmt.get("dst_offset") == amount_offset
        ]
        self.assertEqual(len(patches), 1, "the next threshold patches the probe")

    def test_timer_arithmetic_includes_the_42ms_adjustment(self):
        period_ms = 1000
        doc = _module_json(lambda: play_bgm("bgm.wav", period_ms / 1000))
        threshold_id = doc["module"]["vars"][0]["id"]
        body = _when_body(doc)
        subtracts = [
            act
            for stmt in body
            if stmt.get("kind") == "act"
            for act in [stmt]
            if act.get("modifier") == 8  # Add two's complement == Subtract
            and any(
                r.get("target_var") == threshold_id
                for r in act.get("relocs", [])
            )
        ]
        self.assertEqual(len(subtracts), 1)
        adjustment = period_ms - 42
        self.assertEqual(subtracts[0]["value"], (-adjustment) & 0xFFFFFFFF)

    def test_start_latency_compensation_is_42ms(self):
        # Pins the eudplib literal; every schedule subtracts exactly this.
        self.assertEqual(START_LATENCY_MS, 42)

    def test_local_player_id_is_read_once_at_start(self):
        doc = _module_json(lambda: play_bgm("bgm.wav", 1.0))
        funcs = doc["module"].get("funcs", [])
        self.assertTrue(
            _reads_addr(_once_body(doc), USER_PLAYER_ADDR, funcs),
            "the local player id must be read by the one-time init block",
        )
        self.assertFalse(
            _reads_addr(_when_body(doc), USER_PLAYER_ADDR, funcs),
            "the play path must reuse the cached id, not re-read 0x512684",
        )

    def test_init_read_precedes_the_probe_and_gate(self):
        doc = _module_json(lambda: play_bgm("bgm.wav", 1.0))
        kinds = [stmt["kind"] for stmt in doc["module"]["stmts"]]
        self.assertLess(kinds.index("once"), kinds.index("trigger"))
        self.assertLess(kinds.index("trigger"), kinds.index("when"))

    def test_exact_period_skips_the_zero_adjustment_act(self):
        doc = _module_json(lambda: play_bgm("bgm.wav", 42 / 1000))
        threshold_id = doc["module"]["vars"][0]["id"]
        body = _when_body(doc)
        subtracts = [
            stmt
            for stmt in body
            if stmt.get("kind") == "act"
            and any(
                r.get("target_var") == threshold_id
                for r in stmt.get("relocs", [])
            )
        ]
        self.assertEqual(subtracts, [], "period == 42 ms needs no adjustment")

    def test_period_below_the_start_latency_adds_the_deficit(self):
        # period < 42 ms leaves a negative adjustment; `isub` emits the
        # two's-complement Add of `42 - period`, so the threshold is pushed
        # *up* and the next loop is already due — eudplib's
        # `f_starttimer(< 42)` behaviour.
        period_ms = 20
        doc = _module_json(lambda: play_bgm("bgm.wav", period_ms / 1000))
        threshold_id = doc["module"]["vars"][0]["id"]
        body = _when_body(doc)
        adds = [
            stmt
            for stmt in body
            if stmt.get("kind") == "act"
            and stmt.get("modifier") == 8  # Add (two's complement form)
            and any(
                r.get("target_var") == threshold_id
                for r in stmt.get("relocs", [])
            )
        ]
        self.assertEqual(len(adds), 1, "one adjustment act, as an Add")
        self.assertEqual(
            adds[0]["value"],
            (START_LATENCY_MS - period_ms) & 0xFFFFFFFF,
        )

    def test_clock_read_uses_the_shared_reader_body(self):
        # The raw clock is a full 32-bit dword read, so it must share the
        # per-address reader body (a `call`) instead of inlining 32 bit tests
        # at the play site. Inlining regressed executed actions by 134.
        doc = _module_json(lambda: play_bgm("bgm.wav", 1.0))
        body = _when_body(doc)
        funcs = doc["module"].get("funcs", [])
        self.assertTrue(_reads_addr(body, BGM_CLOCK_ADDR, funcs))
        epd = _epd(BGM_CLOCK_ADDR) & 0xFFFFFFFF
        inline = [
            stmt
            for stmt in body
            if any(cond.get("player") == epd for cond in stmt.get("conds", []))
        ]
        self.assertEqual(inline, [], "the reader body must not be inlined")
        self.assertTrue(
            any(stmt.get("kind") == "call" for stmt in body),
            "the play path reaches the reader through a call",
        )

    def test_local_player_read_uses_the_shared_reader_body(self):
        doc = _module_json(lambda: play_bgm("bgm.wav", 1.0))
        init = _once_body(doc)
        funcs = doc["module"].get("funcs", [])
        self.assertTrue(_reads_addr(init, USER_PLAYER_ADDR, funcs))
        epd = _epd(USER_PLAYER_ADDR) & 0xFFFFFFFF
        inline = [
            stmt
            for stmt in init
            if any(cond.get("player") == epd for cond in stmt.get("conds", []))
        ]
        self.assertEqual(inline, [], "the reader body must not be inlined")
        self.assertTrue(
            any(stmt.get("kind") == "call" for stmt in init),
            "the init block reaches the reader through a call",
        )

    def test_cp_switch_copies_the_cached_local_player_id(self):
        # The switch source is the *value* the init block cached; switching to
        # any other variable (e.g. the hit counter) plays the track for the
        # wrong player while every ordering and restore test still passes.
        doc = _module_json(lambda: play_bgm("bgm.wav", 1.0))
        transfers = [s for s in _once_body(doc) if s["kind"] == "vartransfer"]
        self.assertEqual(len(transfers), 1, "the init block caches one value")
        cached_local_player = transfers[0]["dst_var"]
        self.assertTrue(
            _reads_addr(
                _once_body(doc), USER_PLAYER_ADDR, doc["module"].get("funcs", [])
            ),
            "the cached value must come from the local-player dword",
        )
        switches = [
            stmt
            for stmt in _when_body(doc)
            if stmt.get("kind") == "varstore" and stmt.get("dst_addr") == CP_ADDR
        ]
        self.assertEqual(len(switches), 2, "one CP switch and one restore")
        self.assertEqual(
            switches[0]["src_var"],
            cached_local_player,
            "the CP switch must copy the cached local-player id",
        )


class TestEmbedSurface(unittest.TestCase):
    def test_embed_is_general_and_deduplicates_identical_requests(self):
        def prog() -> None:
            embedFile("bgm", "a.wav")
            embedFile("bgm", "a.wav")
            embedFile("data", "blob.bin")

        doc = _module_json(prog)
        self.assertEqual(
            doc["module"]["files"],
            [
                {"name": "bgm", "path": "a.wav"},
                {"name": "data", "path": "blob.bin"},
            ],
        )

    def test_duplicate_name_with_a_different_path_is_rejected(self):
        def prog() -> None:
            embedFile("bgm", "a.wav")
            embedFile("bgm", "b.wav")

        with self.assertRaises(EudslError) as caught:
            build_module(prog)
        self.assertEqual(caught.exception.code, "EUDSL-BOUNDS")

    def test_empty_name_or_path_is_rejected(self):
        for name, path in (("", "a.wav"), ("bgm", "")):
            with self.subTest(name=name, path=path):
                with self.assertRaises(EudslError) as caught:
                    build_module(lambda: embedFile(name, path))
                self.assertEqual(caught.exception.code, "EUDSL-BOUNDS")

    def test_embed_outside_build_module_is_rejected(self):
        with self.assertRaises(EudslError) as caught:
            embedFile("bgm", "a.wav")
        self.assertEqual(caught.exception.code, "EUDSL-SCOPE")


class TestPlayBgmRuntime(unittest.TestCase):
    def test_first_play_is_immediate(self):
        mod = _play_module(1.0)
        report = run_sim(mod.to_json(), 1)
        self.assertEqual(
            read_dword(report, _epd(SENTINEL_ADDR)),
            CLOCK_START - (1000 - 42),
            "the first loop always plays and schedules the next threshold",
        )

    def test_no_replay_before_the_timer_hits(self):
        mod = _play_module(1.0)
        first = CLOCK_START - (1000 - 42)
        for loops in (2, 10):
            report = run_sim(mod.to_json(), loops)
            self.assertEqual(
                read_dword(report, _epd(SENTINEL_ADDR)),
                first,
                f"loop {loops} must not replay before the 1000 ms period",
            )

    def test_replay_after_the_period(self):
        mod = _play_module(1.0)
        # 100 ms per loop: the probe hits when the raw clock has fallen by
        # 1000 ms minus the 42 ms compensation, i.e. on loop 11.
        report = run_sim(mod.to_json(), 11)
        self.assertEqual(
            read_dword(report, _epd(SENTINEL_ADDR)),
            CLOCK_START - 1000 - (1000 - 42),
        )
        report = run_sim(mod.to_json(), 21)
        self.assertEqual(
            read_dword(report, _epd(SENTINEL_ADDR)),
            CLOCK_START - 2000 - (1000 - 42),
        )

    def test_cp_is_restored_after_playwav(self):
        for saved_cp in (0, 7, 9):
            with self.subTest(saved_cp=saved_cp):
                mod = _play_module(1.0, saved_cp=saved_cp)
                report = run_sim(mod.to_json(), 1)
                self.assertEqual(
                    read_dword(report, _epd(CP_ADDR)),
                    saved_cp,
                    "PlayWAV must leave CurrentPlayer as it found it",
                )

    def test_exact_period_threshold_needs_no_adjustment(self):
        mod = _play_module(42 / 1000)
        report = run_sim(mod.to_json(), 1)
        self.assertEqual(
            read_dword(report, _epd(SENTINEL_ADDR)),
            CLOCK_START,
            "a 42 ms period schedules the raw clock verbatim",
        )

    def test_a_period_below_the_start_latency_stays_due(self):
        # 20 ms period: the -22 ms adjustment pushes the threshold 22 ms past
        # the raw clock, so the very next loop plays again.
        mod = _play_module(20 / 1000)
        report = run_sim(mod.to_json(), 1)
        self.assertEqual(read_dword(report, _epd(SENTINEL_ADDR)), CLOCK_START + 22)
        report = run_sim(mod.to_json(), 2)
        self.assertEqual(
            read_dword(report, _epd(SENTINEL_ADDR)),
            CLOCK_START - 100 + 22,
            "the already-due schedule replays on the next loop",
        )

    def test_idle_cycle_cost_never_reads_the_clock(self):
        # No sentinel copy: the last-loop executed cost is the helper plus the
        # one-action clock tick that advances the simulation.
        mod = _play_module(1.0, sentinel=False)
        report = run_sim(mod.to_json(), 2)
        cost = report["executed_cost"]
        self.assertEqual(
            (cost["triggers"], cost["conditions"], cost["actions"]),
            (3, 2, 1),
            "armed/idle loop = probe + when gate + the test's clock tick",
        )
        self.assertLess(
            cost["conditions"],
            32,
            "an idle loop must not pay for the 32-test clock read",
        )

    def test_static_cost_is_reported(self):
        mod = _play_module(1.0, sentinel=False)
        static = run_static(mod.to_json())["static_cost"]
        self.assertGreater(static["triggers"], 1)
        self.assertEqual(static["blob_bytes"], 4)

    def test_helper_cost_is_pinned_to_the_shared_dword_reader_shape(self):
        # FE2 routed bgm's direct reads through `read_epd`, which inlined the
        # 32-test reader instead of sharing the full-dword kernel. The helper
        # must keep the pre-FE2 (shared-reader) shape: 2 fewer triggers, 6
        # fewer actions, and 4,820 fewer bytes than the inline regression.
        mod = build_module(lambda: play_bgm("bgm.wav", 1.0))
        doc = json.loads(mod.to_json())
        static = run_static(mod.to_json())["static_cost"]
        self.assertEqual(
            (
                static["triggers"],
                static["conditions"],
                static["actions"],
                static["trigger_bytes"],
                static["total_bytes"],
            ),
            (108, 98, 180, 260_064, 260_068),
        )
        self.assertEqual(len(doc["module"].get("funcs", [])), 3)

        report = run_sim(mod.to_json(), 1)
        cost = report["executed_cost"]
        self.assertEqual(
            (cost["triggers"], cost["conditions"], cost["actions"]),
            (110, 98, 212),
            "the shared reader must not add executed actions per play",
        )


class TestLengthValidation(unittest.TestCase):
    def test_rejects_non_numbers_and_non_positive_lengths(self):
        for value in (0, 0.0, -1.0, -0.001, "1.0", None, True, float("nan"),
                      float("inf")):
            with self.subTest(value=value):
                with self.assertRaises(EudslError) as caught:
                    build_module(lambda: play_bgm("bgm.wav", value))
                self.assertIn(caught.exception.code, {"EUDSL-TYPE", "EUDSL-RANGE"})

    def test_truncates_fractional_milliseconds(self):
        doc = _module_json(lambda: play_bgm("bgm.wav", 1.0009))
        probe = _probe(doc)
        # The initial probe stays all-ones; the schedule is 1000 ms (truncated),
        # so the subtraction act carries -(1000 - 42).
        body = _when_body(doc)
        subtracts = [
            stmt
            for stmt in body
            if stmt.get("kind") == "act" and stmt.get("modifier") == 8
        ]
        self.assertEqual(len(subtracts), 1)
        self.assertEqual(subtracts[0]["value"], (-(1000 - 42)) & 0xFFFFFFFF)


if __name__ == "__main__":
    unittest.main()
