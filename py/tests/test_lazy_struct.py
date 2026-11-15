"""``lazyStruct`` public API tests (globals P2-02).

``lazyStruct`` is the thin public factory over a deferred struct handle: a
handle declared anywhere (no builder needed) that resolves to a fully
eager binding on first use inside ``build_module``, one binding per
builder. Eager ``TriggerStruct.instantiate`` is unchanged; ``lazyStruct``
is opt-in only. Binding creation is atomic: one full eager
``instantiate`` (page fit plus ALL K field vars together).
Compatible with both `pytest` and `python3 -m unittest`.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any

from py import FixedArray, TriggerStruct, Var, build_module, lazyStruct, lazy_struct
from py.tests import lazy_struct_fixture as fixture
from py.struct import TriggerStructInstance, bit, byte, dword, word
from py.temporal import when
from py.view import EudslError

from py.tests.helpers import run_sim
REPO_ROOT = Path(__file__).resolve().parent.parent.parent
EUDKIT_COMPILE = REPO_ROOT / "target" / "debug" / "eudkit-compile"

Point = TriggerStruct("Point", {"x": dword(), "y": dword()})
Single = TriggerStruct("Single", {"v": dword()})
Small = TriggerStruct("Small", {"b": byte(), "w": word(), "f": bit(3, 5)})
def run_dump_ir(hir_json: str) -> str:
    with tempfile.TemporaryDirectory(prefix="eudkit-py-p202-") as tmpdir:
        hir_path = Path(tmpdir) / "prog.hir.json"
        hir_path.write_text(hir_json, encoding="utf-8")
        out = subprocess.check_output(
            [str(EUDKIT_COMPILE), "dump-ir", str(hir_path)],
            text=True,
        )
        assert "LIR v3.0" in out, "dump-ir output must contain an LIR section"
        assert (
            "Allocations:" in out
        ), "dump-ir output must contain the Allocations listing"
        return out


def hir_json(mod: Any) -> dict:
    return json.loads(mod.to_json())


def decls(mod: Any) -> list:
    return [[v["id"], v["init"]] for v in hir_json(mod)["module"]["vars"]]


def assert_code(testcase: unittest.TestCase, fn: Any, code: str) -> None:
    with testcase.assertRaises(EudslError) as cm:
        fn()
    testcase.assertEqual(cm.exception.code, code)


class LazyStructTest(unittest.TestCase):
    def test_declaration_outside_build_module_allocates_nothing(self) -> None:
        handle = lazyStruct(Point, {"x": 7})

        def body() -> None:
            v = Var(0)
            v.set(1)

        with_handle = build_module(body)
        without_handle = build_module(body)
        # Identical output proves the unused handle consumed no ids,
        # emitted no decls, and emitted no statement.
        self.assertEqual(hir_json(with_handle), hir_json(without_handle))
        self.assertEqual(decls(with_handle), [[0, 0]])
        self.assertIsInstance(handle, TriggerStructInstance)

    def test_pure_reads_never_resolve_resolving_reads_throw(self) -> None:
        handle = lazyStruct(Point, {"x": 3, "y": 4})
        # Pure reads: schema, field names, field count — no builder needed.
        self.assertIs(handle._schema, Point)
        self.assertEqual(list(handle._field_names), ["x", "y"])
        self.assertEqual(handle.fieldCount, 2)
        self.assertEqual(handle.field_count, 2)
        # Resolving reads outside a builder throw.
        assert_code(self, lambda: handle.pageIndex, "EUDSL-SCOPE")
        assert_code(self, lambda: handle.page_index, "EUDSL-SCOPE")
        assert_code(self, lambda: handle.slotIndex, "EUDSL-SCOPE")
        assert_code(self, lambda: handle.x, "EUDSL-SCOPE")
        assert_code(self, lambda: handle.fields, "EUDSL-SCOPE")

    def test_builder_time_fields_and_slot_index_reads_resolve(self) -> None:
        slot_lazy = lazyStruct(Point)
        lazy_slot: dict[str, Any] = {}

        def lazy_slot_body() -> None:
            # Reading slotIndex is the first resolving use: it must create
            # the binding before any field write.
            lazy_slot["slot"] = slot_lazy.slotIndex
            slot_lazy.x.set(1)

        eager_slot: dict[str, Any] = {}

        def eager_slot_body() -> None:
            e = Point.instantiate()
            eager_slot["slot"] = e.slotIndex
            e.x.set(1)

        lazy_slot_mod = build_module(lazy_slot_body)
        eager_slot_mod = build_module(eager_slot_body)
        self.assertEqual(hir_json(lazy_slot_mod), hir_json(eager_slot_mod))
        self.assertEqual(lazy_slot["slot"], eager_slot["slot"])

        fields_lazy = lazyStruct(Point)
        lazy_fields: dict[str, Any] = {}

        def lazy_fields_body() -> None:
            # Reading fields is the first resolving use: it must create the
            # binding before any field write.
            lazy_fields["fields"] = [field.id for field in fields_lazy.fields]
            fields_lazy.x.set(1)

        eager_fields: dict[str, Any] = {}

        def eager_fields_body() -> None:
            e = Point.instantiate()
            eager_fields["fields"] = [field.id for field in e.fields]
            e.x.set(1)

        lazy_fields_mod = build_module(lazy_fields_body)
        eager_fields_mod = build_module(eager_fields_body)
        self.assertEqual(hir_json(lazy_fields_mod), hir_json(eager_fields_mod))
        self.assertEqual(lazy_fields["fields"], eager_fields["fields"])

    def test_first_use_binding_equals_eager_twin(self) -> None:
        lazy = lazyStruct(Point)

        def body() -> None:
            lazy.x.set(5)
            lazy.y.iadd(1)

        def twin_body() -> None:
            e = Point.instantiate()
            e.x.set(5)
            e.y.iadd(1)

        mod = build_module(body)
        twin = build_module(twin_body)
        self.assertEqual(hir_json(mod), hir_json(twin))
        self.assertEqual(decls(mod), [[0, 0], [1, 0]])

    def test_inits_dict_and_masking_match_eager_twin(self) -> None:
        # Note: eager masking applies the raw value against the shifted
        # mask, so the bit(3,5) init must already be shifted (0b111 << 5).
        lazy = lazyStruct(Small, {"b": 0x1FF, "w": 0x12345, "f": 0b111 << 5})

        def body() -> None:
            lazy.b.set(1)

        def twin_body() -> None:
            e = Small.instantiate({"b": 0x1FF, "w": 0x12345, "f": 0b111 << 5})
            e.b.set(1)

        mod = build_module(body)
        twin = build_module(twin_body)
        self.assertEqual(hir_json(mod), hir_json(twin))
        # Masked inits: byte keeps 0xff, word keeps 0x2345, bit(3,5) keeps 0xe0.
        self.assertEqual(decls(mod), [[0, 0xFF], [1, 0x2345], [2, 0xE0]])

    def test_positional_and_kwarg_init_forms(self) -> None:
        pos = lazyStruct(Point, 5, 7)
        kw = lazyStruct(Point, x=5, y=7)

        def pos_body() -> None:
            pos.x.iadd(1)

        def kw_body() -> None:
            kw.x.iadd(1)

        def twin_body() -> None:
            e = Point.instantiate({"x": 5, "y": 7})
            e.x.iadd(1)

        twin = build_module(twin_body)
        self.assertEqual(hir_json(build_module(pos_body)), hir_json(twin))
        self.assertEqual(hir_json(build_module(kw_body)), hir_json(twin))

    def test_non_number_instance_unknown_field_inits_throw_eagerly(self) -> None:
        # Unknown-field validation is pure: it throws at the call, outside
        # any builder, with no allocation attempted.
        assert_code(
            self, lambda: lazyStruct(Point, {"z": 1}), "EUDSL-UNKNOWN-FIELD"
        )

        # Var/Expr/instance values need a builder to exist, so those throws
        # are asserted inside a build; the throw is still at the lazyStruct
        # call, before any binding is created.
        def body() -> None:
            v = Var(1)
            assert_code(self, lambda: lazyStruct(Point, {"x": v}), "EUDSL-TYPE")
            assert_code(
                self, lambda: lazyStruct(Point, {"x": v.add(1)}), "EUDSL-TYPE"
            )
            eager = Point.instantiate()
            assert_code(self, lambda: lazyStruct(Point, eager), "EUDSL-TYPE")

        build_module(body)

    def test_set_and_copy_from_paths_match_eager_twins(self) -> None:
        lazy = lazyStruct(Point, {"x": 1, "y": 2})

        def body() -> None:
            lazy.set({"x": 10})
            e = Point.instantiate({"x": 20, "y": 30})
            lazy.copyFrom(e)
            e.copyFrom(lazy)
            target = Point.instantiate()
            target.set(lazy)

        def twin_body() -> None:
            l = Point.instantiate({"x": 1, "y": 2})
            l.set({"x": 10})
            e = Point.instantiate({"x": 20, "y": 30})
            l.copyFrom(e)
            e.copyFrom(l)
            target = Point.instantiate()
            target.set(l)

        self.assertEqual(hir_json(build_module(body)), hir_json(build_module(twin_body)))

    def test_fixed_array_set_dispatches_lazy_struct_handle(self) -> None:
        lazy = lazyStruct(Point, {"x": 1, "y": 2})

        def body() -> None:
            arr = FixedArray(Point, 2)
            arr.set(1, lazy)
            out = Var(0)
            out.set(arr.get(1).x)

        def twin_body() -> None:
            arr = FixedArray(Point, 2)
            eager = Point.instantiate({"x": 1, "y": 2})
            arr.set(1, eager)
            out = Var(0)
            out.set(arr.get(1).x)

        mod = build_module(body)
        twin = build_module(twin_body)
        self.assertEqual(hir_json(mod), hir_json(twin))
        self.assertEqual(run_dump_ir(mod.to_json()), run_dump_ir(twin.to_json()))

    def test_field_use_in_when_matches_eager_twin(self) -> None:
        lazy = lazyStruct(Point)

        def body() -> None:
            out = Var(0)
            when(lazy.x.eq(6), lambda: out.set(1))

        def twin_body() -> None:
            # First-use order: `out` before the binding (touched in the cond).
            out = Var(0)
            e = Point.instantiate()
            when(e.x.eq(6), lambda: out.set(1))

        self.assertEqual(hir_json(build_module(body)), hir_json(build_module(twin_body)))

    def test_out_of_order_first_use_follows_first_use_order(self) -> None:
        a = lazyStruct(Point, {"x": 11, "y": 12})
        b = lazyStruct(Point, {"x": 21, "y": 22})

        def body() -> None:
            b.x.set(1)
            a.y.set(2)

        def reordered_body() -> None:
            bb = Point.instantiate({"x": 21, "y": 22})
            aa = Point.instantiate({"x": 11, "y": 12})
            bb.x.set(1)
            aa.y.set(2)

        def declared_body() -> None:
            aa = Point.instantiate({"x": 11, "y": 12})
            bb = Point.instantiate({"x": 21, "y": 22})
            bb.x.set(1)
            aa.y.set(2)

        mod = build_module(body)
        self.assertEqual(
            run_dump_ir(mod.to_json()),
            run_dump_ir(build_module(reordered_body).to_json()),
        )
        self.assertNotEqual(
            run_dump_ir(mod.to_json()),
            run_dump_ir(build_module(declared_body).to_json()),
        )

    def test_conditional_first_use_with_const_init_matches_twin(self) -> None:
        lazy = lazyStruct(Point, {"x": 5, "y": 6})

        def body() -> None:
            flag = Var(0)
            when(flag.eq(0), lambda: lazy.x.set(9))

        def twin_body() -> None:
            flag = Var(0)
            e = Point.instantiate({"x": 5, "y": 6})
            when(flag.eq(0), lambda: e.x.set(9))

        self.assertEqual(
            run_dump_ir(build_module(body).to_json()),
            run_dump_ir(build_module(twin_body).to_json()),
        )

    def test_never_used_lazy_struct_emits_nothing(self) -> None:
        handle = lazyStruct(Point, {"x": 1, "y": 2})
        self.assertIsNotNone(handle)

        def body() -> None:
            v = Var(0)
            v.set(1)

        with_handle = build_module(body)
        without_handle = build_module(body)
        self.assertEqual(hir_json(with_handle), hir_json(without_handle))
        self.assertIn("Total Variables: 1", run_dump_ir(with_handle.to_json()))

    def test_two_builder_use_gives_separate_bindings(self) -> None:
        handle = lazyStruct(Point)

        def body1() -> None:
            handle.x.set(1)

        def body2() -> None:
            other = Var(9)
            other.set(2)
            handle.y.set(3)

        def twin1() -> None:
            e = Point.instantiate()
            e.x.set(1)

        def twin2() -> None:
            other = Var(9)
            other.set(2)
            e = Point.instantiate()
            e.y.set(3)

        mod1 = build_module(body1)
        mod2 = build_module(body2)
        self.assertEqual(hir_json(mod1), hir_json(build_module(twin1)))
        self.assertEqual(hir_json(mod2), hir_json(build_module(twin2)))
        # Different ids prove the bindings are per-builder, not shared.
        self.assertNotEqual(decls(mod1), decls(mod2)[1:])

    def test_cross_file_fixture_use_matches_eager_twin(self) -> None:
        def body() -> None:
            fixture.s.x.iadd(1)
            fixture.s.y.set(8)

        def twin_body() -> None:
            e = Point.instantiate({"x": 3, "y": 4})
            e.x.iadd(1)
            e.y.set(8)

        self.assertEqual(hir_json(build_module(body)), hir_json(build_module(twin_body)))

    def test_post_build_single_record_read_and_ambiguous(self) -> None:
        single = lazyStruct(Point)
        captured: dict[str, int] = {}

        def single_body() -> None:
            single.x.set(1)
            captured["pageIndex"] = single.pageIndex

        build_module(single_body)
        # Resolved under exactly one builder: the cached record reads post-build.
        self.assertEqual(single.pageIndex, captured["pageIndex"])
        self.assertEqual(single.x.id, 0)

        multi = lazyStruct(Point)

        def multi_body1() -> None:
            multi.x.set(1)

        def multi_body2() -> None:
            multi.y.set(2)

        build_module(multi_body1)
        build_module(multi_body2)
        assert_code(self, lambda: multi.pageIndex, "EUDSL-SCOPE")

    def test_lazy_struct_alias_and_field_assignment(self) -> None:
        self.assertIs(lazy_struct, lazyStruct)
        lazy = lazyStruct(Point)

        def body() -> None:
            lazy.x = 5
            lazy.y.set(6)

        def twin_body() -> None:
            e = Point.instantiate()
            e.x = 5
            e.y.set(6)

        self.assertEqual(hir_json(build_module(body)), hir_json(build_module(twin_body)))

    def test_byte_identity_corpus(self) -> None:
        cases: list[tuple[str, Any, Any]] = []

        s_single = lazyStruct(Single)

        def lazy_single() -> None:
            s_single.v.set(42)

        def eager_single() -> None:
            e = Single.instantiate()
            e.v.set(42)

        cases.append(("single-field", lazy_single, eager_single))

        s_multi = lazyStruct(Point, {"x": 3, "y": 4})

        def lazy_multi() -> None:
            s_multi.x.iadd(1)
            s_multi.y.set(9)

        def eager_multi() -> None:
            e = Point.instantiate({"x": 3, "y": 4})
            e.x.iadd(1)
            e.y.set(9)

        cases.append(("multi-field inits", lazy_multi, eager_multi))

        s_small = lazyStruct(Small, {"b": 0xAB, "w": 0x1234, "f": 0b101})

        def lazy_small() -> None:
            s_small.f.set(0b010)

        def eager_small() -> None:
            e = Small.instantiate({"b": 0xAB, "w": 0x1234, "f": 0b101})
            e.f.set(0b010)

        cases.append(("masked/shifted specs", lazy_small, eager_small))

        s_copy = lazyStruct(Point)

        def lazy_copy() -> None:
            e = Point.instantiate({"x": 7, "y": 8})
            s_copy.copyFrom(e)
            s_copy.set({"y": 10})

        def eager_copy() -> None:
            e = Point.instantiate({"x": 7, "y": 8})
            l = Point.instantiate()
            l.copyFrom(e)
            l.set({"y": 10})

        cases.append(("set/copyFrom paths", lazy_copy, eager_copy))

        s_when = lazyStruct(Point)

        def lazy_when() -> None:
            out = Var(0)
            when(s_when.y.eq(2), lambda: out.set(1))

        def eager_when() -> None:
            # First-use order: `out` before the binding (touched in the cond).
            out = Var(0)
            e = Point.instantiate()
            when(e.y.eq(2), lambda: out.set(1))

        cases.append(("field in when", lazy_when, eager_when))

        for name, lazy_body, eager_body in cases:
            lazy_mod = build_module(lazy_body)
            eager_mod = build_module(eager_body)
            self.assertEqual(
                run_dump_ir(lazy_mod.to_json()),
                run_dump_ir(eager_mod.to_json()),
                f"corpus case {name} must be byte-identical",
            )

    def test_sim_output_matches_eager_twin(self) -> None:
        lazy = lazyStruct(Point, {"x": 5, "y": 7})

        def lazy_body() -> None:
            out = Var(0)
            out.set(lazy.x)
            out.iadd(lazy.y)
            lazy.x.set(11)

        def eager_body() -> None:
            # First-use order: `out` before the binding (touched by out.set).
            out = Var(0)
            e = Point.instantiate({"x": 5, "y": 7})
            out.set(e.x)
            out.iadd(e.y)
            e.x.set(11)

        self.assertEqual(
            run_sim(build_module(lazy_body).to_json()),
            run_sim(build_module(eager_body).to_json()),
        )


if __name__ == "__main__":
    unittest.main()
