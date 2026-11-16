"""11. Math, random, locations, and tech/upgrades — eudplib baseline.

Shared Intent:
    Compute signed floor division, integer square root, integer power,
    ``atan2`` / ``lengthdir``, binary search, bounded LCG random draws, update
    and draw on a location, and read/write per-player tech & upgrade state.

eudplib Pattern Notes:
    - Math helpers live in ``eudplib.eudlib`` (``f_div_floor``, ``f_sqrt``,
      ``f_pow``, ``f_atan2``, ``f_lengthdir``, ``EUDBinaryMin``).
    - Random helpers (``f_srand``, ``f_randomize``, ``f_dwrand``) use one
      module-global LCG state variable.
    - Location helpers (``f_setloc``, ``f_getlocTL``) mutate StarCraft's
      location table (``0x58DC4C + 20 * id``); ``f_getlocTL`` returns
      ``(left, top)`` despite its historical docstring.
    - Tech and upgrade tables use ``P1.tech[...]`` / ``P1.upgrade[...]`` (or
      byte reads/writes at ``0x58CF44`` / ``0x58D2B0``).
"""

from eudplib import *

q = EUDVariable(0)
root = EUDVariable(0)
p = EUDVariable(0)
dx = EUDVariable(0)
dy = EUDVariable(0)
roll = EUDVariable(0)
up_lvl = EUDVariable(0)


def onPluginStart():
    vx = EUDVariable(40)
    vy = EUDVariable(30)
    base = EUDVariable(3)
    neg_seven = EUDVariable(-7)

    qq, _r = f_div_floor(neg_seven, 3)
    q << qq
    root << f_sqrt(vx * vx + vy * vy)
    p << f_pow(base, 3)

    angle = f_atan2(vy, vx)
    ldx, ldy = f_lengthdir(100, angle)
    dx << ldx
    dy << ldy

    found = EUDBinaryMin(lambda x: x >= 10, 0, 15)
    f_srand(42)
    f_randomize()
    roll << 1 + f_dwrand() % 6

    f_setloc(0, 64, 64, 192, 192)
    lx, ly = f_getlocTL(0)
    if EUDIf()(found >= 10):
        dx += lx - lx + ly - ly
    EUDEndIf()

    P1.upgrade[EncodeUpgrade("Terran Infantry Weapons")] = 2
    P1.tech[EncodeTech("Stim Packs")] = 1
    up_lvl << P1.upgrade[EncodeUpgrade("Terran Infantry Weapons")]


def run():
    onPluginStart()
