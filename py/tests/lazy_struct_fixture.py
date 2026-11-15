"""Fixture for P2-02 cross-file tests: a lazy struct instance declared at
module scope in a separate file, used from the main test module.
"""

from py.struct import TriggerStruct, dword, lazy_struct

Point = TriggerStruct("Point", {"x": dword(), "y": dword()})

s = lazy_struct(Point, {"x": 3, "y": 4})
