"""Explicit-entry contract (ADR 0012): the root source assigns exactly one
compiled module to ``module``; the CLI reads that binding. The old implicit
``last_module`` capture hook must not return to the public API.
"""

from __future__ import annotations

import unittest

import py
from py import hir


class TestEntryContract(unittest.TestCase):
    def test_last_module_is_not_public(self) -> None:
        self.assertFalse(hasattr(py, "last_module"))
        self.assertFalse(hasattr(hir, "last_module"))


if __name__ == "__main__":
    unittest.main()
