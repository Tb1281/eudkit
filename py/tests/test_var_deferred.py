"""Deferred-mode ``Var`` tests (globals P1-01, internal prefactor).

These tests use the INTERNAL deferred constructor flag (test-only; the
flag is not public API and stays undocumented for users). A deferred
handle allocates nothing at construction and resolves its identity on
first use through a per-builder binding, so the same handle can serve
several modules with separate bindings.
Compatible with both `pytest` and `python3 -m unittest`.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
import weakref
from pathlib import Path
from typing import Any

from py import Var, build_module
from py.trigger import _get_active_module_builder
from py.view import EudslError

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
EUDKIT_COMPILE = REPO_ROOT / "target" / "debug" / "eudkit-compile"


def run_lower(hir_json: str) -> dict:
    with tempfile.TemporaryDirectory(prefix="eudkit-py-p101-") as tmpdir:
        hir_path = Path(tmpdir) / "prog.hir.json"
        hir_path.write_text(hir_json, encoding="utf-8")
        out = subprocess.check_output(
            [str(EUDKIT_COMPILE), "lower", str(hir_path)],
            text=True,
        ).strip()
        return json.loads(out)


def hir_json(mod: Any) -> dict:
    return json.loads(mod.to_json())


def decls(mod: Any) -> list[tuple[int, int]]:
    return [(v["id"], v["init"]) for v in hir_json(mod)["module"]["vars"]]


def deferred(
    init: int = 0,
    storage: str | None = None,
    mask: int | None = None,
) -> Var:
    """Test-only helper: construct a deferred handle via the internal flag."""
    return Var(init, explicit_mask=mask, storage=storage, deferred=True)


class DeferredVarTest(unittest.TestCase):
    def test_construction_outside_builder_allocates_nothing(self) -> None:
        handle = deferred(7)

        def body() -> None:
            Var(0).set(1)

        # Rebuild the same body with and without the handle in scope: the
        # unused handle must not change the output at all.
        with_handle = build_module(body)
        without_handle = build_module(body)
        # Identical output proves the unused handle consumed no id, emitted
        # no decl, and emitted no statement.
        self.assertEqual(hir_json(with_handle), hir_json(without_handle))
        self.assertEqual(decls(with_handle), [(0, 0)])

    def test_pure_reads_never_resolve_or_allocate(self) -> None:
        handle = deferred(7)
        masked = deferred(3, mask=0xFF)
        self.assertEqual(handle.init, 7)
        self.assertEqual(handle.initialValue, 7)
        self.assertEqual(handle.initial_value, 7)
        self.assertEqual(handle.bitmask, 0xFFFFFFFF)
        self.assertEqual(masked.init, 3)
        self.assertEqual(masked.bitmask, 0xFF)
        mod = build_module(lambda: Var(0).set(1))
        self.assertEqual(decls(mod), [(0, 0)])

    def test_first_resolving_use_without_builder_raises_scope(self) -> None:
        handle = deferred(7)
        for read in (
            lambda: handle.id,
            lambda: handle.pageIndex,
            lambda: handle.page_index,
            lambda: handle.slotIndex,
            lambda: handle.slot_index,
            lambda: handle.mask,
            lambda: handle.dest,
            lambda: handle.value,
            lambda: handle.modifier,
        ):
            with self.assertRaises(EudslError) as cm:
                read()
            self.assertEqual(cm.exception.code, "EUDSL-SCOPE")
        with self.assertRaises(EudslError) as cm:
            handle.set(1)
        self.assertEqual(cm.exception.code, "EUDSL-SCOPE")

    def test_same_handle_under_two_builders_gets_separate_bindings(self) -> None:
        handle = deferred(7)
        seen_a: dict[str, int] = {}

        def body_a() -> None:
            seen_a["id"] = handle.id
            handle.set(1)
            Var(0).set(9)

        mod_a = build_module(body_a)
        # Record the second binding's id from inside its own build.
        seen_b: dict[str, int] = {}

        def body_b2() -> None:
            Var(0).set(9)
            seen_b["id"] = handle.id
            handle.set(2)

        mod_b = build_module(body_b2)
        self.assertEqual(seen_a["id"], 0)
        self.assertEqual(seen_b["id"], 1)
        self.assertEqual(decls(mod_a), [(0, 7), (1, 0)])
        self.assertEqual(decls(mod_b), [(0, 0), (1, 7)])
        # Byte-identity against eager equivalents proves neither module's
        # handle-use statements reference the other module's binding id.
        eager_a = build_module(lambda: (Var(7).set(1), Var(0).set(9)))
        eager_b = build_module(lambda: (Var(0).set(9), Var(7).set(2)))
        self.assertEqual(run_lower(mod_a.to_json()), run_lower(eager_a.to_json()))
        self.assertEqual(run_lower(mod_b.to_json()), run_lower(eager_b.to_json()))

    def test_post_build_single_builder_read_returns_cached_record(self) -> None:
        handle = deferred(7)
        recorded: dict[str, Any] = {}

        def body() -> None:
            recorded["id"] = handle.id
            recorded["page"] = handle.pageIndex
            recorded["slot"] = handle.slotIndex
            recorded["value"] = handle.value
            handle.set(3)

        build_module(body)
        self.assertEqual(handle.id, recorded["id"])
        self.assertEqual(handle.pageIndex, recorded["page"])
        self.assertEqual(handle.slotIndex, recorded["slot"])
        self.assertIs(handle.value, recorded["value"])
        # Repeated post-build reads stay cached and identical.
        self.assertEqual(handle.id, recorded["id"])
        self.assertIs(handle.value, recorded["value"])

    def test_post_build_multi_builder_read_raises_ambiguous(self) -> None:
        handle = deferred(7)
        build_module(lambda: handle.set(1))
        build_module(lambda: handle.set(2))
        with self.assertRaises(EudslError) as cm:
            handle.id
        self.assertEqual(cm.exception.code, "EUDSL-SCOPE")
        self.assertIn("ambiguous", str(cm.exception))
        with self.assertRaises(EudslError) as cm:
            handle.value
        self.assertIn("ambiguous", str(cm.exception))

    def test_post_build_never_resolved_read_raises_scope(self) -> None:
        handle = deferred(7)
        with self.assertRaises(EudslError) as cm:
            handle.id
        self.assertEqual(cm.exception.code, "EUDSL-SCOPE")
        self.assertNotIn("ambiguous", str(cm.exception))

    def test_deferred_non_const_init_raises_type_eagerly(self) -> None:
        def body() -> None:
            e = Var(0)
            with self.assertRaises(EudslError) as cm:
                Var(e, deferred=True)
            self.assertEqual(cm.exception.code, "EUDSL-TYPE")
            with self.assertRaises(EudslError) as cm:
                Var(e.add(1), deferred=True)
            self.assertEqual(cm.exception.code, "EUDSL-TYPE")

        build_module(body)

    def test_deferred_handle_with_explicit_id_raises_type(self) -> None:
        with self.assertRaises(EudslError) as cm:
            Var(0, explicit_id=5, deferred=True)
        self.assertEqual(cm.exception.code, "EUDSL-TYPE")

    def test_deferred_page_var_resolves_page_coords_per_builder(self) -> None:
        handle = deferred(0, storage="page")
        seen: dict[str, tuple[int, int]] = {}

        def body_a() -> None:
            seen["a"] = (handle.pageIndex, handle.slotIndex)

        def body_b() -> None:
            Var(0, storage="page").set(0)
            seen["b"] = (handle.pageIndex, handle.slotIndex)

        build_module(body_a)
        build_module(body_b)
        self.assertEqual(seen["a"], (0, 0))
        self.assertEqual(seen["b"], (0, 1))

    def test_builder_state_supports_weak_references(self) -> None:
        """Py WeakKeyDictionary eligibility proof for the cache key type."""
        seen: dict[str, Any] = {}

        def body() -> None:
            seen["state"] = _get_active_module_builder()

        build_module(body)
        state = seen["state"]
        self.assertIsNotNone(state)
        self.assertIsNotNone(weakref.ref(state))


if __name__ == "__main__":
    unittest.main()
