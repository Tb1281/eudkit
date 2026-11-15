"""Fixture for P2-01 cross-file tests: a lazy FixedArray declared at module
scope in a separate file, used from the main test module."""

from py.array import lazy_fixed_array

arr = lazy_fixed_array("dword", 3)
