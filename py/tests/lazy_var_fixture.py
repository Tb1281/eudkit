"""Fixture for P1-02 cross-file tests: a lazy Var declared at module scope
in a separate file, used from the main test module."""

from py.var import lazy_var

g = lazy_var(5)
