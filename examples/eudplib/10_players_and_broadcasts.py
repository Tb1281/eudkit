"""10. Players and broadcasts — eudplib baseline.

Shared Intent:
    Track per-player bonus scores in a ``PVariable``, increment each active
    player without disturbing ``CurrentPlayer``, broadcast a message locally
    with ``DisplayTextAll``, compare ``CurrentPlayer`` / local user, and check
    player fields.

eudplib Pattern Notes:
    - ``EUDLoopPlayer()`` iterates active players without touching ``curpl``;
      explicitly changing CP inside a loop requires saving/restoring.
    - ``DisplayTextAll(text)`` switches ``curpl`` to ``f_getuserplayerid()``
      so only the local client sees the message (no observer/CP restoration is
      guaranteed if caller assumed CP unchanged unless managed).
    - ``IsUserCP()`` compares ``CurrentPlayer == f_getuserplayerid()``.
"""

from eudplib import *

bonus = PVariable([10] * 8)
local_seen = EUDVariable(0)


def onPluginStart():
    local_seen << 0


def beforeTriggerExec():
    for p in EUDLoopPlayer():
        bonus[p] += 1

    if EUDIf()(f_playerexist(P1) == 1):
        f_getuserplayerid()
        if EUDIf()(IsUserCP()):
            local_seen << 1
        EUDEndIf()
    EUDEndIf()

    DoActions(Once(DisplayTextAll("Active players checked!")))


def run():
    onPluginStart()
    beforeTriggerExec()
