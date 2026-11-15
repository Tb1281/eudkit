"""``lazyFixedArray`` public API tests (globals P2-01).

``lazyFixedArray`` is the thin public factory over the P1-01 deferred
base: a handle declared anywhere (no builder needed) that resolves to
a fully eager binding on first use inside ``build_module``, one binding
per builder. Binding creation is atomic (R3): one full eager
``FixedArray`` construction — page alignment plus ALL element vars, or
ALL struct instances. Eager ``FixedArray`` is unchanged;
``lazyFixedArray`` is opt-in only.

Shape note: the factory returns a genuine deferred-mode
``FixedArrayImpl``, not a wrapper (``set_all`` dispatches on
``isinstance(src, FixedArrayImpl)``).

Compatible with both `pytest` and `python3 -m unittest`.
"""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from py import (
    FixedArray,
    Var,
    arrayByte,
    arrayDword,
    build_module,
    lazyFixedArray,
    lazy_fixed_array,
)
from py.tests import lazy_fixed_array_fixture as fixture
from py.struct import TriggerStruct, dword
from py.temporal import when
from py.view import EudslError

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
EUDKIT_COMPILE = REPO_ROOT / "target" / "debug" / "eudkit-compile"


def run_dump_ir(hir_json: str) -> str:
    # Full pipeline comparison: `dump-ir` runs optimize_module then
    # lowering, so it exercises the opt desugar and the storage
    # analysis. The comparison is the ENTIRE dump-ir output (storage
    # report including the per-var Allocations listing, plus the
    # rendered LIR).
    with tempfile.TemporaryDirectory(prefix="eudkit-py-p201-") as tmpdir:
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


def assert_code(test: unittest.TestCase, fn: object, code: str) -> None:
    with test.assertRaises(EudslError) as cm:
        fn()  # type: ignore[operator]
    test.assertEqual(cm.exception.code, code)


class LazyFixedArrayTest(unittest.TestCase):
    def test_module_scope_declaration_and_pure_reads(self) -> None:
        a = lazyFixedArray(arrayDword, 3)
        self.assertEqual(a.length, 3)
        self.assertEqual(a.elemType, "dword")
        self.assertEqual(a.elem_type, "dword")
        self.assertEqual(a.mask, 0xFFFFFFFF)
        self.assertEqual(a.bits, 2)
        self.assertEqual(a.pageCount, 1)
        self.assertEqual(a.page_count, 1)
        self.assertEqual(a.pages, 1)
        self.assertFalse(a.isConst)
        self.assertFalse(a.is_const)
        big = lazyFixedArray(arrayDword, 65)
        self.assertEqual(big.pageCount, 2)
        Hero = TriggerStruct("Hero", {"hp": dword()})
        heroes = lazyFixedArray(Hero, 2)
        self.assertEqual(heroes.pageCount, 2)

    def test_eager_errors_at_declaration(self) -> None:
        assert_code(self, lambda: lazyFixedArray(arrayDword, 0), "EUDSL-BOUNDS")
        assert_code(self, lambda: lazyFixedArray(arrayDword, -1), "EUDSL-BOUNDS")
        assert_code(self, lambda: lazyFixedArray(arrayDword, 1.5), "EUDSL-BOUNDS")
        assert_code(self, lambda: lazyFixedArray("nope", 3), "EUDSL-BOUNDS")

    def test_resolving_use_outside_builder_throws(self) -> None:
        a = lazyFixedArray(arrayDword, 2)
        assert_code(self, lambda: a.get(0), "EUDSL-SCOPE")
        assert_code(self, lambda: a.set(0, 1), "EUDSL-SCOPE")
        assert_code(self, lambda: a.vars, "EUDSL-SCOPE")
        assert_code(self, lambda: a.instances, "EUDSL-SCOPE")

    def test_corpus_basic_flow(self) -> None:
        a = lazyFixedArray(arrayDword, 3)

        def body() -> None:
            a.set(0, 11)
            a.set(2, 22)
            out = Var(0)
            out.set(a.get(0))
            when(a.eq(0, 11), lambda: out.iadd(1))

        def twin_body() -> None:
            arr = FixedArray(arrayDword, 3)
            arr.set(0, 11)
            arr.set(2, 22)
            out = Var(0)
            out.set(arr.get(0))
            when(arr.eq(0, 11), lambda: out.iadd(1))

        self.assertEqual(
            run_dump_ir(build_module(body).to_json()),
            run_dump_ir(build_module(twin_body).to_json()),
        )

    def test_corpus_byte_elements(self) -> None:
        a = lazyFixedArray(arrayByte, 5)

        def body() -> None:
            a.set(1, 7)
            a.set(4, 9)
            out = Var(0)
            out.set(a.get(4))

        def twin_body() -> None:
            arr = FixedArray(arrayByte, 5)
            arr.set(1, 7)
            arr.set(4, 9)
            out = Var(0)
            out.set(arr.get(4))

        self.assertEqual(
            run_dump_ir(build_module(body).to_json()),
            run_dump_ir(build_module(twin_body).to_json()),
        )

    def test_corpus_struct_elements(self) -> None:
        Hero = TriggerStruct("Hero", {"hp": dword(), "mp": "word"})
        a = lazyFixedArray(Hero, 2)

        def body() -> None:
            a.get(0).hp.set(100)
            a.get(1).mp.set(50)
            out = Var(0)
            out.set(a.get(0).hp)

        def twin_body() -> None:
            arr = FixedArray(Hero, 2)
            arr.get(0).hp.set(100)
            arr.get(1).mp.set(50)
            out = Var(0)
            out.set(arr.get(0).hp)

        self.assertEqual(
            run_dump_ir(build_module(body).to_json()),
            run_dump_ir(build_module(twin_body).to_json()),
        )

    def test_corpus_dynamic_index(self) -> None:
        a = lazyFixedArray(arrayDword, 4)

        def body() -> None:
            idx = Var(1)
            a.set(idx, 99)
            out = Var(0)
            out.set(a.get(idx))

        def twin_body() -> None:
            idx = Var(1)
            arr = FixedArray(arrayDword, 4)
            arr.set(idx, 99)
            out = Var(0)
            out.set(arr.get(idx))

        self.assertEqual(
            run_dump_ir(build_module(body).to_json()),
            run_dump_ir(build_module(twin_body).to_json()),
        )

    def test_corpus_clear_set_all_eq(self) -> None:
        a = lazyFixedArray(arrayDword, 4)

        def body() -> None:
            src = FixedArray(arrayDword, 4)
            a.clear()
            a.set_all(src)
            out = Var(0)
            when(a.eq(0, 0), lambda: out.iadd(1))

        def twin_body() -> None:
            src = FixedArray(arrayDword, 4)
            arr = FixedArray(arrayDword, 4)
            arr.clear()
            arr.set_all(src)
            out = Var(0)
            when(arr.eq(0, 0), lambda: out.iadd(1))

        self.assertEqual(
            run_dump_ir(build_module(body).to_json()),
            run_dump_ir(build_module(twin_body).to_json()),
        )

    def test_copy_source_both_directions(self) -> None:
        a = lazyFixedArray(arrayDword, 3)

        def body() -> None:
            e = FixedArray(arrayDword, 3)
            e.set_all(a)
            a.set_all(e)
            out = Var(0)
            out.set(e.get(0))

        def twin_body() -> None:
            e = FixedArray(arrayDword, 3)
            arr = FixedArray(arrayDword, 3)
            e.set_all(arr)
            arr.set_all(e)
            out = Var(0)
            out.set(e.get(0))

        self.assertEqual(
            run_dump_ir(build_module(body).to_json()),
            run_dump_ir(build_module(twin_body).to_json()),
        )

    def test_out_of_order_first_use(self) -> None:
        A = lazyFixedArray(arrayDword, 2)
        B = lazyFixedArray(arrayDword, 2)

        def body() -> None:
            B.set(0, 22)
            A.set(0, 11)

        def twin_b_first() -> None:
            b = FixedArray(arrayDword, 2)
            a = FixedArray(arrayDword, 2)
            b.set(0, 22)
            a.set(0, 11)

        def twin_a_first() -> None:
            a = FixedArray(arrayDword, 2)
            b = FixedArray(arrayDword, 2)
            b.set(0, 22)
            a.set(0, 11)

        lazy_ir = run_dump_ir(build_module(body).to_json())
        self.assertEqual(lazy_ir, run_dump_ir(build_module(twin_b_first).to_json()))
        self.assertNotEqual(lazy_ir, run_dump_ir(build_module(twin_a_first).to_json()))

    def test_conditional_first_use(self) -> None:
        a = lazyFixedArray(arrayDword, 2)

        def body() -> None:
            flag = Var(1)
            when(flag.eq(1), lambda: a.set(0, 5))
            out = Var(0)
            out.set(a.get(0))

        # Twin in first-use order: flag first, then the array (the lazy
        # binding happens inside the when, after flag allocation).
        def twin_body() -> None:
            flag = Var(1)
            arr = FixedArray(arrayDword, 2)
            when(flag.eq(1), lambda: arr.set(0, 5))
            out = Var(0)
            out.set(arr.get(0))

        self.assertEqual(
            run_dump_ir(build_module(body).to_json()),
            run_dump_ir(build_module(twin_body).to_json()),
        )

    def test_never_used_emits_nothing(self) -> None:
        _unused = lazyFixedArray(arrayDword, 4)

        def body() -> None:
            out = Var(0)
            out.set(1)

        def twin_body() -> None:
            out = Var(0)
            out.set(1)

        self.assertEqual(
            run_dump_ir(build_module(body).to_json()),
            run_dump_ir(build_module(twin_body).to_json()),
        )

    def test_two_builder_separate_bindings(self) -> None:
        a = lazyFixedArray(arrayDword, 2)

        def body1() -> None:
            a.set(0, 1)
            v = Var(0)
            v.set(a.get(0))

        def body2() -> None:
            v = Var(0)
            a.set(0, 1)
            v.set(a.get(0))

        def twin1() -> None:
            arr = FixedArray(arrayDword, 2)
            arr.set(0, 1)
            v = Var(0)
            v.set(arr.get(0))

        def twin2() -> None:
            v = Var(0)
            arr = FixedArray(arrayDword, 2)
            arr.set(0, 1)
            v.set(arr.get(0))

        self.assertEqual(
            run_dump_ir(build_module(body1).to_json()),
            run_dump_ir(build_module(twin1).to_json()),
        )
        self.assertEqual(
            run_dump_ir(build_module(body2).to_json()),
            run_dump_ir(build_module(twin2).to_json()),
        )

    def test_cross_file_use(self) -> None:
        def body() -> None:
            fixture.arr.set(0, 5)
            fixture.arr.set(2, 9)
            out = Var(0)
            out.set(fixture.arr.get(2))

        def twin_body() -> None:
            arr = FixedArray(arrayDword, 3)
            arr.set(0, 5)
            arr.set(2, 9)
            out = Var(0)
            out.set(arr.get(2))

        self.assertEqual(
            run_dump_ir(build_module(body).to_json()),
            run_dump_ir(build_module(twin_body).to_json()),
        )

    def test_post_build_single_record_rule(self) -> None:
        single = lazyFixedArray(arrayDword, 2)
        build_module(lambda: single.set(0, 7))
        # Resolved under exactly one builder: cached record, no throw.
        self.assertEqual(len(single.vars), 2)

        multi = lazyFixedArray(arrayDword, 2)
        build_module(lambda: multi.set(0, 1))
        build_module(lambda: multi.set(0, 2))
        # Resolved under two builders: post-build read is ambiguous.
        assert_code(self, lambda: multi.vars, "EUDSL-SCOPE")

    def test_snake_case_alias_is_same_factory(self) -> None:
        self.assertIs(lazy_fixed_array, lazyFixedArray)
