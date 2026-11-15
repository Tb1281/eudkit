"""Parity test for the ``CP_ADDR`` export surface.

TypeScript's package root (``ts/index.ts``) re-exports ``CP_ADDR`` from
``ts/cp.ts``. Python must expose the same public seam: ``from py import
CP_ADDR`` (or ``from eudkit import CP_ADDR`` when installed) has to resolve to
the same value as ``py.cp.CP_ADDR``.
"""

from __future__ import annotations

import unittest

import py
from py.cp import CP_ADDR as CP_ADDR_FROM_MODULE

#: Byte address of the runtime ``CurrentPlayer`` register.
CP_ADDR_EXPECTED = 0x6509B0


class TestCPAddrExport(unittest.TestCase):
    def test_cp_addr_importable_from_package_root(self) -> None:
        from py import CP_ADDR

        self.assertEqual(CP_ADDR, CP_ADDR_EXPECTED)
        self.assertIs(CP_ADDR, CP_ADDR_FROM_MODULE)

    def test_cp_addr_listed_in_public_all(self) -> None:
        self.assertIn("CP_ADDR", py.__all__)


if __name__ == "__main__":
    unittest.main()
