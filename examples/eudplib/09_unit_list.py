"""09. Unit-list iteration — eudplib baseline.

Shared Intent:
    Walk the live global CUnit list, count visits, and stop after the first
    unit whose ``hp`` field is zero.

eudplib Pattern Notes:
    - ``EUDLoopUnit()`` follows the engine's live ``U`` list at run time and
      yields the unit pointer/EPD pair.
    - ``f_dwread_epd(epd + 2)`` reads a CUnit dword field; ``hp`` lives at
      byte offset 8, i.e. dword offset 2.
    - eudplib has no first-class ``D`` walker: dying units require the
      ``unit.dying`` block of a ``UnitGroup`` or a hand-written ``prev`` walk.
"""

from eudplib import *

visited = EUDVariable(0)
stopped = EUDVariable(0)


def onPluginStart():
    visited << 0
    stopped << 0

    for ptr, epd in EUDLoopUnit():
        visited += 1
        if EUDIf()(f_dwread_epd(epd + 2) == 0):
            stopped << 1
            EUDBreak()
        EUDEndIf()


def run():
    onPluginStart()
