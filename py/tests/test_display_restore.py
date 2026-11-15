"""displayAt relative-offset and cursor restore (eudplib parity, issue #44 resolved).

The sim executes SetDeaths acts only and does NOT model the game's
synchronous DisplayText line increment, so the sim-visible pointer after a
displayAt is the RESTORE value. displayAt restores the saved pointer
verbatim, making the call transparent to the line pointer: the pointer
after the call equals the pointer before it.
"""

from __future__ import annotations

import json
import unittest

from py import (
    SetMemory,
    SetTo,
    FixedString,
    Var,
    build_module,
    once,
)
from py.tests.helpers import EPD_BASE, read_dword, run_sim

DISPLAY_LINE_ADDR = 0x640B58
DISPLAY_LINE_EPD = (DISPLAY_LINE_ADDR - EPD_BASE) >> 2


RESTORE_CASES = [
    (0, 0),
    (1, 1),
    (5, 5),
    (11, 11),
    (15, 15),
]


class TestDisplayRestore(unittest.TestCase):
    def test_restore_edges(self):
        for saved, restored in RESTORE_CASES:
            with self.subTest(saved=saved, restored=restored):
                def prog(saved=saved):
                    s = FixedString(8)
                    s.write("hi")
                    SetMemory(DISPLAY_LINE_ADDR, SetTo, saved)
                    s.displayAt(3)

                report = run_sim(build_module(prog).to_json(), 3)
                self.assertEqual(
                    read_dword(report, DISPLAY_LINE_EPD) & 0xFFFFFFFF,
                    restored,
                    f"sim-visible restore must equal saved={saved} "
                    f"(displayAt is transparent to the line pointer)",
                )

    def test_two_displayat_calls_chain(self):
        def prog():
            s = FixedString(8)
            s.write("hi")
            SetMemory(DISPLAY_LINE_ADDR, SetTo, 5)
            s.displayAt(3)
            s.displayAt(4)

        report = run_sim(build_module(prog).to_json(), 3)
        self.assertEqual(
            read_dword(report, DISPLAY_LINE_EPD),
            5,
            "second displayAt saves the first restore (5) and restores "
            "it unchanged; displayAt neither accumulates nor leaks",
        )

    def test_printat_shares_displayat_restore(self):
        def prog():
            s = FixedString(8)
            SetMemory(DISPLAY_LINE_ADDR, SetTo, 7)
            s.printAt(3, "x")

        report = run_sim(build_module(prog).to_json(), 3)
        self.assertEqual(
            read_dword(report, DISPLAY_LINE_EPD),
            7,
            "printAt delegates to displayAt, so it restores saved "
            "verbatim too",
        )

    def test_displayat_display_across_loops_no_drift(self):
        def prog():
            a = FixedString(8)
            b = FixedString(8)
            once(lambda: SetMemory(DISPLAY_LINE_ADDR, SetTo, 5))
            a.write("AAA")
            b.write("BBB")
            a.displayAt(1)
            b.display()

        report = run_sim(build_module(prog).to_json(), 4)
        self.assertEqual(
            read_dword(report, DISPLAY_LINE_EPD),
            5,
            "displayAt must restore the saved pointer verbatim across "
            "loops",
        )

    def test_runtime_line_displayat_emits_relative_and_restore(self):
        def prog():
            s = FixedString(8)
            s.write("hi")
            s.displayAt(Var(2))

        parsed = json.loads(build_module(prog).to_json())
        varstores = [
            stmt
            for stmt in parsed["module"]["stmts"]
            if stmt.get("kind") == "varstore"
            and stmt.get("dst_addr") == DISPLAY_LINE_ADDR
        ]
        self.assertEqual(
            len(varstores),
            2,
            "runtime-line displayAt writes pointer twice (Add + restore SetTo)",
        )


if __name__ == "__main__":
    unittest.main()
