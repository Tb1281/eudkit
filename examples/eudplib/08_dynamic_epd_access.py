"""08. Dynamic (Runtime Var) EPD Read & Write Patterns — eudplib baseline.

Topic: Given a runtime ``EUDVariable`` ``target_epd`` computed in-game
(e.g. ``EPD(0x58A364 + 0x200) + offset_var``), safely read and write
arbitrary EPDs at runtime.

In eudplib, ``f_dwread_epd`` / ``f_dwwrite_epd`` / ``f_wread_epd`` handle
runtime-computed EPD pointers transparently:
- ``f_dwread_epd(target_epd)`` sets CurrentPlayer (``0x6509B0``) to
  ``target_epd``, runs a shared 32-bit binary bit-test read table into a
  return ``EUDVariable``, and restores CP from ``cpcache``.
- ``f_wread_epd(target_epd, subp)`` reads a 16-bit word at byte sub-offset
  ``subp`` (0..2 constant or runtime variable).
- ``f_dwwrite_epd(target_epd, write_val)`` uses ``VProc`` +
  ``QueueAssignTo(EPD(v.getDestAddr()))`` to patch the write action's
  destination EPD and value before executing it.
"""

from eudplib import *


def onPluginStart():
    # Seed a known value at EPD(0x58A364 + 0x200) + 3 for demonstration.
    DoActions(SetMemoryEPD(EPD(0x58A364 + 0x200) + 3, SetTo, 0xCAFEBABE))

    # Runtime offset and dynamic target EPD: EPD(0x58A364 + 0x200) + offset_var
    offset_var = EUDVariable(3)
    target_epd = EUDVariable(EPD(0x58A364 + 0x200))
    target_epd += offset_var

    # 1) Dynamic 32-bit dword read at runtime EPD
    val = f_dwread_epd(target_epd)

    # 1b) Dynamic 16-bit word read at runtime EPD + subp (0 = low word, 2 = high word)
    low_word = f_wread_epd(target_epd, 0)
    high_word = f_wread_epd(target_epd, 2)

    # 2) Dynamic 32-bit dword write of runtime Var value to runtime EPD
    write_val = EUDVariable(0x12345678)
    f_dwwrite_epd(target_epd, write_val)

    return val, low_word, high_word


def run():
    return onPluginStart()
