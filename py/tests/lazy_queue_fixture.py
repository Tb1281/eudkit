"""Fixture for P3 cross-file tests: lazy ``Queue``, ``Deque``, ``Stack``,
and ``UnitGroup`` declarations at module scope in a separate file, used
from the main test module."""

from py.queue import lazy_deque, lazy_queue, lazy_stack, lazy_unit_group

q = lazy_queue("dword", 3)
dq = lazy_deque("word", 2)
st = lazy_stack(2)
ug = lazy_unit_group(2, autoPrune=False)
