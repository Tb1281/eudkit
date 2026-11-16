"""12. Containers, camera follow, and engine helpers — eudplib baseline.

Shared Intent:
    Demonstrate fixed-capacity containers (``EUDQueue``, ``EUDDeque``,
    ``EUDStack``, ``ObjPool``), per-frame camera following, and euddraft-style
    engine helpers (``noAirCollision`` / ``unlimiter``).

eudplib Pattern Notes:
    - ``EUDQueue`` / ``EUDDeque`` / ``EUDStack`` / ``ObjPool`` live in
      ``eudplib.collections`` (note: ``EUDStack`` returns an EUDStruct
      factory rather than a direct capacity constructor, and ``ObjPool``
      allocates struct casts rather than raw index slots).
    - Engine patches like ``cammove``, ``eudTurbo``, ``noAirCollision``, and
      ``unlimiter`` are configured externally as euddraft plugins (INI / YAML)
      rather than called directly inside trigger code (with ``eudTurbo``
      writing ``0x6509A0 = 0`` manually if not using the plugin).
"""

from eudplib import *


class _Item(EUDStruct):
    _fields_ = ["val"]


q = EUDQueue(4)()
dq = EUDDeque(4)()
stack_cls = EUDStack()
st = stack_cls(4)
pool = ObjPool(4)
out = EUDVariable(0)


def onPluginStart():
    q.append(10)
    dq.appendleft(5)
    st.push(77)
    obj = pool.alloc(_Item)
    if EUDIf()(obj != 0):
        obj.val = 123
        out << obj.val
        pool.free(_Item, obj)
    EUDEndIf()


def beforeTriggerExec():
    if EUDIfNot()(q.empty()):
        out << q.popleft()
    EUDEndIf()


def run():
    onPluginStart()
    beforeTriggerExec()
