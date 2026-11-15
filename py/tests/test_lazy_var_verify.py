"""``lazyVar`` verification corpus + differential probes (globals P1-03).

P1-02 proved HIR equality between lazy programs and their eager twins.
This file proves the full pipeline: every corpus program must compile
byte-identical through ``dump-ir`` (storage report including the
per-var Allocations listing, plus the rendered LIR), not just at the
HIR level. It also files the section 8 differential probes that P1-02
did not cover at pipeline level: never-used ``total_vars`` via
dump-ir, and the single-entry-point probe.

Shape note: ``lazyVar`` is a thin factory over ``Var`` directly
(P1-01 deferred base, P1-02 factory). There is no ``LazyVarImpl``
subclass; docs describe the factory shape.

Single-entry-point note: ``build_module`` is a single entry point,
one per map (user rule, 2026-11-04). A nested ``build_module`` call
must throw ``EUDSL-SCOPE`` — the scope-violation code already used
for outside-``build_module`` allocation. The nested-call enforcement
is a separate item; the probe below pins the contract and fails
until that enforcement lands.

Compatible with both `pytest` and `python3 -m unittest`.
"""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any

from py import Var, build_module, lazyVar, lazy_var
from py.tests import lazy_var_fixture as fixture
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
    with tempfile.TemporaryDirectory(prefix="eudkit-py-p103-") as tmpdir:
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


class LazyVarVerifyTest(unittest.TestCase):
    def test_corpus_basic_flow(self) -> None:
        g = lazyVar(10)

        def body() -> None:
            g.iadd(5)
            out = Var(0)
            out.set(g)
            when(g.eq(15), lambda: out.iadd(1))

        def twin_body() -> None:
            v = Var(10)
            v.iadd(5)
            out = Var(0)
            out.set(v)
            when(v.eq(15), lambda: out.iadd(1))

        self.assertEqual(
            run_dump_ir(build_module(body).to_json()),
            run_dump_ir(build_module(twin_body).to_json()),
        )

    def test_corpus_masked_var(self) -> None:
        m = lazyVar(3, mask=0xFF)

        def body() -> None:
            m.iadd(1)
            out = Var(0)
            out.set(m)

        def twin_body() -> None:
            v = Var(3, explicit_mask=0xFF)
            v.iadd(1)
            out = Var(0)
            out.set(v)

        self.assertEqual(
            run_dump_ir(build_module(body).to_json()),
            run_dump_ir(build_module(twin_body).to_json()),
        )

    def test_corpus_page_var(self) -> None:
        p = lazyVar(0, storage="page")

        def body() -> None:
            p.iadd(1)
            out = Var(0)
            out.set(p)

        def twin_body() -> None:
            v = Var(0, storage="page")
            v.iadd(1)
            out = Var(0)
            out.set(v)

        self.assertEqual(
            run_dump_ir(build_module(body).to_json()),
            run_dump_ir(build_module(twin_body).to_json()),
        )

    def test_corpus_out_of_order(self) -> None:
        a = lazyVar(11)
        b = lazyVar(22)

        def body() -> None:
            b.iadd(1)
            a.iadd(1)
            out = Var(0)
            out.set(a)
            out.set(b)

        def twin_body() -> None:
            bb = Var(22)
            aa = Var(11)
            bb.iadd(1)
            aa.iadd(1)
            out = Var(0)
            out.set(aa)
            out.set(bb)

        self.assertEqual(
            run_dump_ir(build_module(body).to_json()),
            run_dump_ir(build_module(twin_body).to_json()),
        )

    def test_corpus_conditional_first_use(self) -> None:
        g = lazyVar(3)

        def body() -> None:
            flag = Var(0)
            when(flag.eq(0), lambda: g.iadd(1))
            out = Var(0)
            out.set(g)

        def twin_body() -> None:
            flag = Var(0)
            v = Var(3)
            when(flag.eq(0), lambda: v.iadd(1))
            out = Var(0)
            out.set(v)

        self.assertEqual(
            run_dump_ir(build_module(body).to_json()),
            run_dump_ir(build_module(twin_body).to_json()),
        )

    def test_corpus_cross_file(self) -> None:
        def body() -> None:
            fixture.g += 1
            out = Var(0)
            out.set(fixture.g)

        def twin_body() -> None:
            v = Var(5)
            v += 1
            out = Var(0)
            out.set(v)

        self.assertEqual(
            run_dump_ir(build_module(body).to_json()),
            run_dump_ir(build_module(twin_body).to_json()),
        )

    def test_corpus_snake_case_alias_in_expressions(self) -> None:
        # The snake_case alias at pipeline level, with the handle used
        # as an Expr operand and in a condition.
        g = lazy_var(5)

        def body() -> None:
            h = Var(0)
            h.set(g.add(1))
            when(g.eq(6), lambda: h.iadd(10))

        # Eager twin in first-use order: `h` is allocated before the
        # first resolving use of `g` (inside `set`), so `h` takes id 0.
        def twin_body() -> None:
            h = Var(0)
            v = Var(5)
            h.set(v.add(1))
            when(v.eq(6), lambda: h.iadd(10))

        self.assertEqual(
            run_dump_ir(build_module(body).to_json()),
            run_dump_ir(build_module(twin_body).to_json()),
        )

    def test_corpus_handle_in_two_statements(self) -> None:
        g = lazyVar(7)

        def body() -> None:
            g.iadd(1)
            flag = Var(0)
            when(flag.eq(0), lambda: g.iadd(2))
            out = Var(0)
            out.set(g)

        def twin_body() -> None:
            v = Var(7)
            v.iadd(1)
            flag = Var(0)
            when(flag.eq(0), lambda: v.iadd(2))
            out = Var(0)
            out.set(v)

        self.assertEqual(
            run_dump_ir(build_module(body).to_json()),
            run_dump_ir(build_module(twin_body).to_json()),
        )

    def test_same_handle_under_two_builders(self) -> None:
        g = lazyVar(4)

        def body1() -> None:
            g.iadd(1)

        def body2() -> None:
            first = Var(0)
            first.set(1)
            g.iadd(1)

        def twin1() -> None:
            v = Var(4)
            v.iadd(1)

        def twin2() -> None:
            first = Var(0)
            first.set(1)
            v = Var(4)
            v.iadd(1)

        self.assertEqual(
            run_dump_ir(build_module(body1).to_json()),
            run_dump_ir(build_module(twin1).to_json()),
        )
        self.assertEqual(
            run_dump_ir(build_module(body2).to_json()),
            run_dump_ir(build_module(twin2).to_json()),
        )

    def test_never_used_changes_nothing_at_pipeline_level(self) -> None:
        unused = lazyVar(9)

        def body() -> None:
            Var(0).set(1)

        with_unused = build_module(body)
        without_unused = build_module(body)
        a = run_dump_ir(with_unused.to_json())
        b = run_dump_ir(without_unused.to_json())
        self.assertEqual(a, b)
        self.assertIn(
            "Total Variables: 1", a, "unused handle must not add a variable"
        )

    def test_nested_build_module_throws(self) -> None:
        # Single-entry-point probe (supersedes the design's
        # innermost-builder probe): a nested `build_module` call must
        # throw `EUDSL-SCOPE`. Blocked on the nested-call enforcement
        # item; this probe fails until that enforcement lands.
        def outer() -> None:
            a = Var(1)
            a.iadd(1)

            def inner() -> None:
                b = Var(2)
                b.iadd(1)

            build_module(inner)

        with self.assertRaises(EudslError) as cm:
            build_module(outer)
        self.assertEqual(cm.exception.code, "EUDSL-SCOPE")


if __name__ == "__main__":
    unittest.main()
