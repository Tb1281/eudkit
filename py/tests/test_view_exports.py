"""Root export parity for ``read_epd`` / ``read_epd_pointer``.

TypeScript's package root (``ts/index.ts``) re-exports ``view.ts`` wholesale,
so ``readEpd`` / ``readEpdPointer`` are reachable from the root. Python's
curated barrel used to omit both, forcing callers through ``py.view``. This
pins the promoted surface: the snake names and their camel aliases, the
``__all__`` entries, and alias identity with the canonical functions.

Mirror of ``ts/tests/test_view_exports.ts``; the cross-frontend HIR check for a
one-call program lives there.
"""

from __future__ import annotations

import unittest

import py
from py import (
    EPD,
    build_module,
    readEpd,
    readEpdPointer,
    read_epd,
    read_epd_pointer,
)
from py.view import read_epd as view_read_epd
from py.view import read_epd_pointer as view_read_epd_pointer

#: eudplib's own test reads the dword pointer at this address.
PTR = 0x58D740

_ROUTE_NAMES = ("read_epd", "read_epd_pointer", "readEpd", "readEpdPointer")


class TestViewRootExports(unittest.TestCase):
    def test_all_four_names_resolve_from_the_root(self) -> None:
        self.assertIs(read_epd, view_read_epd)
        self.assertIs(read_epd_pointer, view_read_epd_pointer)

    def test_camel_aliases_are_the_same_function_objects(self) -> None:
        self.assertIs(readEpd, read_epd)
        self.assertIs(readEpdPointer, read_epd_pointer)

    def test_every_name_is_listed_in_public_all(self) -> None:
        for name in _ROUTE_NAMES:
            with self.subTest(name=name):
                self.assertIn(name, py.__all__)

    def test_root_read_epd_emits_the_same_hir_as_the_view_function(self) -> None:
        root_hir = build_module(lambda: read_epd(EPD(PTR), 2, 0)).to_json()
        view_hir = build_module(lambda: view_read_epd(EPD(PTR), 2, 0)).to_json()
        self.assertEqual(root_hir, view_hir)

    def test_root_read_epd_pointer_emits_the_same_hir_as_the_view_function(
        self,
    ) -> None:
        root_hir = build_module(lambda: read_epd_pointer(EPD(PTR))).to_json()
        view_hir = build_module(lambda: view_read_epd_pointer(EPD(PTR))).to_json()
        self.assertEqual(root_hir, view_hir)

    def test_root_aliases_call_through_without_a_wrapper(self) -> None:
        alias_hir = build_module(
            lambda: (
                readEpd(EPD(PTR), 2, 0),
                readEpdPointer(EPD(PTR)),
            )
        ).to_json()
        canonical_hir = build_module(
            lambda: (
                read_epd(EPD(PTR), 2, 0),
                read_epd_pointer(EPD(PTR)),
            )
        ).to_json()
        self.assertEqual(alias_hir, canonical_hir)


if __name__ == "__main__":
    unittest.main()
