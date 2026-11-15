"""Issue #87 (C7): CP-cursor byte-lane writer for variable-width bare ``{}``.

The variable-width decimal writer borrows ``CurrentPlayer`` (``curpl``) for the
duration of a format run: it seeds the register with the dword holding the next
content byte, keeps ``curpl = base + (pos >> 2)`` while it emits digits, and
restores 0 at the end of the run (ADR 0004). These tests render real values
through the in-memory simulator and pin the borrow shape in HIR; the TypeScript
mirror is ``ts/tests/test_c7_cp_cursor.ts`` and covers the same cases.
"""

from __future__ import annotations

import json
import unittest
from typing import Any

from py import FixedString, SetMemory, Var, build_module, with_epd
from py.cp import read_current_player_register
from py.hir import AddrReloc, SetTo, emitVarStore
from py.tests.helpers import EPD_BASE, flatten_stmts, read_dword, read_text, run_sim
from py.view import EPD_CURRENT_PLAYER

CP_ADDR = 0x6509B0
SET_DEATHS = 45
SET_TO = 7

#: External observation dword (outside the synthetic payload).
OBSERVE = 0x6D0000


def _observe(addr: int) -> None:
    """Store the live CurrentPlayer value into an external observation dword."""
    saved = read_current_player_register()
    emitVarStore(saved.id, AddrReloc(addr), SetTo)


def _epd(addr: int) -> int:
    return (addr - EPD_BASE) // 4


def _module(fmt: str, values: list[int], size: int = 32) -> Any:
    def prog() -> None:
        s = FixedString(size)
        s.write(fmt, *[Var(v) for v in values])

    return build_module(prog)


def _render(fmt: str, values: list[int], size: int = 32) -> str:
    return read_text(run_sim(_module(fmt, values, size).to_json()), 0)


def _hir(fmt: str, values: list[int], size: int = 32) -> dict[str, Any]:
    return json.loads(_module(fmt, values, size).to_json())


def _acts(stmts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for stmt in flatten_stmts(stmts):
        out.extend(stmt.get("acts") or [])
        if stmt.get("kind") == "act":
            out.append(stmt)
    return out


def _cp_borrow_counts(hir: dict[str, Any]) -> tuple[int, int]:
    # Variable-width decimal writes now go through one shared `func` body per
    # module, so its seed is emitted once in `module.funcs` rather than at each
    # call site. Counting func bodies too keeps the contract this test pins:
    # one seed site and one reset per format run, regardless of placeholder
    # count.
    module = hir["module"]
    stmts = list(module["stmts"])
    for fn in module.get("funcs") or []:
        stmts.extend(fn.get("body") or [])
    seeds = sum(
        1
        for stmt in flatten_stmts(stmts)
        if stmt.get("kind") == "varstore" and stmt.get("dst_addr") == CP_ADDR
    )
    resets = sum(
        1
        for act in _acts(stmts)
        if act.get("acttype") == SET_DEATHS
        and act.get("dest") == EPD_CURRENT_PLAYER
        and act.get("value") == 0
        and act.get("modifier") == SET_TO
    )
    return seeds, resets


class CpCursorWriterTest(unittest.TestCase):
    def test_value_sweep_renders_every_width(self) -> None:
        # 0, 1, 2 and 10 digits: the cursor must survive every lane wrap.
        for value in (0, 7, 42, 999, 1234567890):
            with self.subTest(value=value):
                self.assertEqual(_render("{}", [value]), str(value))

    def test_adjacent_placeholders_do_not_collide_across_lanes(self) -> None:
        self.assertEqual(_render("{} {}", [42, 7]), "42 7")
        self.assertEqual(_render("a={} b={}", [42, 7]), "a=42 b=7")

    def test_literal_around_placeholder_keeps_the_cursor_synced(self) -> None:
        self.assertEqual(_render("ab{}cd", [1234]), "ab1234cd")

    def test_long_static_prefix_uses_the_constant_cursor_index(self) -> None:
        # A static prefix longer than one dword seeds the cursor at index 1
        # instead of zero (the constant-index seed path).
        self.assertEqual(_render("abcd{}", [1234]), "abcd1234")

    def test_bare_decimal_borrows_once_and_restores_curpl(self) -> None:
        # One seed and one reset per run, even with several placeholders: the
        # lane-3 advance keeps the cursor valid for the whole run.
        for fmt, values in (("{}", [42]), ("{} {}", [42, 7]), ("a={} b={}", [42, 7])):
            with self.subTest(fmt=fmt):
                self.assertEqual(_cp_borrow_counts(_hir(fmt, values)), (1, 1))

        # The known-lane path (`{:x}`) never touches curpl.
        self.assertEqual(_cp_borrow_counts(_hir("{:x}", [42])), (0, 0))


class CpCursorAmbientTest(unittest.TestCase):
    """ADR 0004: the C7 borrow is cursor-only, but ``with_epd`` restores.

    The writer itself still resets CP to 0 (its documented cursor-only
    restore). Wrapping the format run in ``with_epd`` adds the unconditional
    ambient save/restore, so a non-zero ambient value survives a render.
    """

    def _write(self, fmt: str, values: list[int], size: int = 32) -> None:
        s = FixedString(size)
        s.write(fmt, *[Var(value) for value in values])

    def test_writer_restores_zero_not_the_ambient_value(self) -> None:
        def prog() -> None:
            SetMemory(CP_ADDR, SetTo, 77)
            self._write("{}", [42])
            _observe(OBSERVE)

        report = run_sim(build_module(prog).to_json())
        self.assertEqual(read_dword(report, _epd(OBSERVE)), 0)

    def test_with_epd_restores_the_ambient_value_around_a_format_run(self) -> None:
        def prog() -> None:
            SetMemory(CP_ADDR, SetTo, 77)
            with_epd(1000, lambda cp: self._write("{}", [42]))
            _observe(OBSERVE)

        report = run_sim(build_module(prog).to_json())
        self.assertEqual(read_text(report, 0), "42")
        self.assertEqual(read_dword(report, _epd(OBSERVE)), 77)


if __name__ == "__main__":
    unittest.main()
