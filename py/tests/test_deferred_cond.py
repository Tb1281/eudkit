"""Ticket #14: comparisons are intent until their evaluation site.

`Var.eq/ne/ge/le/gt/lt` used to build a non-constant operand into a temporary
at comparison-construction time. A `while_loop` check trigger re-evaluates its
stored condition list every pass, so a construction-time temp froze the
comparison; a `with_cp`-backed field condition carried the CP hop and read a
stale cursor (or aliased a later field). These tests pin the fixed behavior:
the temp-building acts are emitted at the evaluation site, and CP-backed
conditions resolve to an absolute EPD.
"""

from __future__ import annotations

import hashlib
import json
import operator
import subprocess
import textwrap
import unittest
from pathlib import Path

from py import (
    Always,
    CUnit,
    EudslError,
    FieldSpec,
    Never,
    SetDeaths,
    SetTo,
    Trigger,
    TriggerFieldRef,
    Var,
    build_module,
    defineMemView,
    on_falling,
    on_rising,
    once,
    until,
    when,
    while_loop,
    with_cp,
)
from py.tests.helpers import REPO_ROOT, run_sim
from py.view import make_memview

# Loop-pass counts for the multi-pass matrices, and the raw-Trigger gate static
# cost — one pin shared by the tests below (review nit #6).
_PASS_COUNTS = (1, 2, 3, 5)
_RETAINED_FALLBACK_TRIGGERS = 5
_RETAINED_FALLBACK_CONDITIONS = 3
_RETAINED_FALLBACK_ACTIONS = 5
_RETAINED_FALLBACK_TOTAL_BYTES = 12_048


def _loop_counts(make_cond, *, step: int = 1):
    """Run one counted loop and return the ids of `i` and `iters` before sim."""
    ids: dict[str, int] = {}

    def prog() -> None:
        i = Var(0)
        n = Var(3)
        guard = Var(10)
        iters = Var(0)
        ids.update(i=i.id, n=n.id, guard=guard.id, iters=iters.id)

        def body() -> None:
            i.iadd(step)
            iters.iadd(1)
            guard.isub(1)

        # `guard.ge(1)` is a constant condition, so it stays live on every
        # pass and caps the pre-fix freeze at 10 iterations instead of the
        # simulator's infinite-loop guard.
        while_loop([make_cond(i, n), guard.ge(1)], body)

    report = run_sim(build_module(prog).to_json(), 1)
    return ids, report


class DeferredCondWhileLoopTest(unittest.TestCase):
    def test_while_loop_variable_rhs_recomputes_each_pass(self) -> None:
        rows = [
            ("lt", lambda i, n: i.lt(n), 3, 3),
            ("le", lambda i, n: i.le(n), 4, 4),
            ("ne", lambda i, n: i.ne(n), 3, 3),
            ("ne_const", lambda i, n: i.ne(3), 3, 3),
            ("gt", lambda i, n: i.gt(n), 0, 0),
            ("ge", lambda i, n: i.ge(n), 0, 0),
            ("eq", lambda i, n: i.eq(n), 0, 0),
        ]
        for name, make_cond, want_i, want_iters in rows:
            with self.subTest(cond=name):
                ids, report = _loop_counts(make_cond)
                values = report["var_values"]
                self.assertEqual(values[ids["i"]], want_i)
                self.assertEqual(values[ids["iters"]], want_iters)

    def test_while_loop_expr_rhs_recomputes_each_pass(self) -> None:
        ids, report = _loop_counts(lambda i, n: i.lt(n + 0))
        values = report["var_values"]
        self.assertEqual(values[ids["i"]], 3)
        self.assertEqual(values[ids["iters"]], 3)

    def test_while_loop_tail_rebuilds_after_body_mutation(self) -> None:
        ids, report = _loop_counts(lambda i, n: i.lt(n), step=2)
        values = report["var_values"]
        self.assertEqual(values[ids["i"]], 4)
        self.assertEqual(values[ids["iters"]], 2)


class DeferredCondCPScopeTest(unittest.TestCase):
    def test_cp_cond_built_inside_used_after(self) -> None:
        ids: dict[str, int] = {}

        def prog() -> None:
            SetDeaths(7002, SetTo, 777, 0)
            hit = Var(0)
            ids["hit"] = hit.id
            with with_cp(7000) as cp:
                cond = cp.view(CUnit).hp.eq(777)
            when([cond], lambda: hit.iadd(1))

        report = run_sim(build_module(prog).to_json(), 1)
        self.assertEqual(report["var_values"][ids["hit"]], 1)

    def test_two_cp_conds_in_one_gate_do_not_alias(self) -> None:
        ids: dict[str, int] = {}

        def prog() -> None:
            SetDeaths(7000, SetTo, 0x11, 0)
            SetDeaths(7005, SetTo, 0x22, 0)
            hit = Var(0)
            ids["hit"] = hit.id
            with with_cp(7000) as cp:
                c1 = cp.at(0).eq(0x11)
                c2 = cp.at(5).eq(0x22)
            when([c1, c2], lambda: hit.iadd(1))

        report = run_sim(build_module(prog).to_json(), 1)
        self.assertEqual(report["var_values"][ids["hit"]], 1)

    def test_single_cp_cond_control_fires(self) -> None:
        ids: dict[str, int] = {}

        def prog() -> None:
            SetDeaths(7000, SetTo, 0x11, 0)
            hit = Var(0)
            ids["hit"] = hit.id
            with with_cp(7000) as cp:
                c1 = cp.at(0).eq(0x11)
            when([c1], lambda: hit.iadd(1))

        report = run_sim(build_module(prog).to_json(), 1)
        self.assertEqual(report["var_values"][ids["hit"]], 1)


def _build_parity_module():
    def prog() -> None:
        a = Var(1)
        b = Var(5)
        out = Var(0)
        when([a.lt(b)], lambda: out.iadd(1))

        v = Var(7)
        w = Var(7)
        eq_out = Var(0)
        when([v.eq(w)], lambda: eq_out.iadd(1))

        raw_hit = Var(0)
        Trigger(conds=[CUnit(1000).hp.ne(500)], acts=[raw_hit.act_add(1)])

        c = Var(0)
        n = Var(3)
        cg = Var(2)

        def body_var_rhs() -> None:
            c.iadd(1)
            cg.isub(1)

        while_loop([c.lt(n), cg.ge(1)], body_var_rhs)

        d = Var(0)
        m = Var(4)
        dg = Var(2)

        def body_expr_rhs() -> None:
            d.iadd(1)
            dg.isub(1)

        while_loop([d.lt(m + 0), dg.ge(1)], body_expr_rhs)

        SetDeaths(7002, SetTo, 777, 0)
        hit = Var(0)
        with with_cp(7000) as cp:
            cond = cp.view(CUnit).hp.eq(777)
        when([cond], lambda: hit.iadd(1))

        SetDeaths(7000, SetTo, 0x11, 0)
        SetDeaths(7005, SetTo, 0x22, 0)
        with with_cp(7000) as pair:
            c1 = pair.at(0).eq(0x11)
            c2 = pair.at(5).eq(0x22)
        when([c1, c2], lambda: hit.iadd(1))

    return build_module(prog)


_TS_PARITY_SCRIPT = textwrap.dedent(
    """
    import {
      CUnit,
      SetDeaths,
      SetTo,
      Trigger,
      Var,
      buildModule,
      when,
      whileLoop,
      withCP,
    } from "./ts/index.ts";

    const mod = buildModule(() => {
      const a = Var(1);
      const b = Var(5);
      const out = Var(0);
      when([a.lt(b)], () => out.iadd(1));

      const v = Var(7);
      const w = Var(7);
      const eqOut = Var(0);
      when([v.eq(w)], () => eqOut.iadd(1));

      const rawHit = Var(0);
      new Trigger({ conds: [CUnit(1000).hp.ne(500)], acts: [rawHit.act_add(1)] });

      const c = Var(0);
      const n = Var(3);
      const cg = Var(2);
      whileLoop([c.lt(n), cg.ge(1)], () => {
        c.iadd(1);
        cg.isub(1);
      });

      const d = Var(0);
      const m = Var(4);
      const dg = Var(2);
      whileLoop([d.lt(m.add(0)), dg.ge(1)], () => {
        d.iadd(1);
        dg.isub(1);
      });

      SetDeaths(7002, SetTo, 777, 0);
      const hit = Var(0);
      let cond;
      withCP(7000, (cp) => {
        cond = cp.view(CUnit).hp.eq(777);
      });
      when([cond], () => hit.iadd(1));

      SetDeaths(7000, SetTo, 0x11, 0);
      SetDeaths(7005, SetTo, 0x22, 0);
      let c1;
      let c2;
      withCP(7000, (pair) => {
        c1 = pair.at(0).eq(0x11);
        c2 = pair.at(5).eq(0x22);
      });
      when([c1, c2], () => hit.iadd(1));
    });

    process.stdout.write(mod.toJson());
    """
)


class DeferredCondParityTest(unittest.TestCase):
    def test_hir_parity_for_each_case(self) -> None:
        py_hir = _build_parity_module().to_json()
        res = subprocess.run(
            ["node", "--input-type=module", "-e", _TS_PARITY_SCRIPT],
            cwd=str(REPO_ROOT),
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertEqual(py_hir, res.stdout)


# ---------------------------------------------------------------------------
# Ticket #15: `MemFieldHandle.ne` middle constants defer to their use site.
# ---------------------------------------------------------------------------

_MASKED_SCHEMA = defineMemView("Masked", 4, {"v": FieldSpec(0, 1, mask=0xFF00)})
_LOW_MASK_SCHEMA = defineMemView("LowMask", 4, {"v": FieldSpec(2, 2, mask=0x00FF)})


def _masked_ne_hits(raw: int, rhs: int) -> int:
    ids: dict[str, int] = {}

    def prog() -> None:
        SetDeaths(3000, SetTo, raw, 0)
        view = make_memview(_MASKED_SCHEMA, 3000)
        hit = Var(0)
        ids["hit"] = hit.id
        when(view.v.ne(rhs), lambda: hit.iadd(1))

    report = run_sim(build_module(prog).to_json(), 1)
    return report["var_values"][ids["hit"]]


def _cp_extreme_hits(rhs: int, field_value: int) -> int:
    ids: dict[str, int] = {}

    def prog() -> None:
        # A non-zero base dword distinguishes the absolute target (7005) from
        # the re-anchored live CP (7000) after the scope exits.
        SetDeaths(7000, SetTo, 0x99, 0)
        SetDeaths(7005, SetTo, field_value, 0)
        hit = Var(0)
        ids["hit"] = hit.id
        with with_cp(7000) as cp:
            cond = cp.at(5).ne(rhs)
        when([cond], lambda: hit.iadd(1))

    report = run_sim(build_module(prog).to_json(), 1)
    return report["var_values"][ids["hit"]]


class DeferredNeMemoryFieldTest(unittest.TestCase):
    def test_marker_is_not_exported_from_the_package(self) -> None:
        import py as eudkit_pkg

        self.assertFalse(hasattr(eudkit_pkg, "DeferredNe"))

    def test_while_loop_memory_field_ne_recomputes_each_pass(self) -> None:
        ids: dict[str, int] = {}

        def prog() -> None:
            CUnit(1000).hp.set(0)
            guard = Var(10)
            iters = Var(0)
            ids["iters"] = iters.id

            def body() -> None:
                CUnit(1000).hp.set(500)
                iters.iadd(1)
                guard.isub(1)

            while_loop([CUnit(1000).hp.ne(500), guard.ge(1)], body)

        module = build_module(prog)
        report = run_sim(module.to_json(), 1)
        self.assertEqual(report["var_values"][ids["iters"]], 1)
        # One shared gate flag per site; the tail rebuild must not allocate a
        # second.
        self.assertEqual(len(json.loads(module.to_json())["module"]["vars"]), 3)

    def test_while_loop_cp_backed_field_ne_recomputes_each_pass(self) -> None:
        ids: dict[str, int] = {}

        def prog() -> None:
            SetDeaths(7005, SetTo, 0x11, 0)
            guard = Var(10)
            iters = Var(0)
            ids["iters"] = iters.id
            with with_cp(7000) as cp:
                cond = cp.at(5).ne(0x99)

            def body() -> None:
                SetDeaths(7005, SetTo, 0x99, 0)
                iters.iadd(1)
                guard.isub(1)

            while_loop([cond, guard.ge(1)], body)

        report = run_sim(build_module(prog).to_json(), 1)
        self.assertEqual(report["var_values"][ids["iters"]], 1)

    def test_cp_backed_field_ne_extremes_used_after_scope(self) -> None:
        self.assertEqual(_cp_extreme_hits(0, 0x0), 0)
        self.assertEqual(_cp_extreme_hits(0xFFFFFFFF, 0xFFFFFFFF), 0)
        self.assertEqual(_cp_extreme_hits(0, 0x1), 1)

    def test_masked_field_ne_honours_field_spec_mask(self) -> None:
        # `v` is the second byte (mask 0xFF00) of the dword at 3000.
        self.assertEqual(_masked_ne_hits(0x00001200, 0x1200), 0)
        self.assertEqual(_masked_ne_hits(0x00001201, 0x1200), 0)
        self.assertEqual(_masked_ne_hits(0x00003400, 0x1200), 1)
        self.assertEqual(_masked_ne_hits(0x00000100, 0x100), 0)
        # Same convention as `eq`: outside-mask bits never matter.
        self.assertEqual(_masked_ne_hits(0x00000000, 0x100), 1)
        self.assertEqual(_masked_ne_hits(0x00000200, 0x100), 1)

    def test_masked_field_ne_rejects_mask_bits_below_shift(self) -> None:
        def prog() -> None:
            view = make_memview(_LOW_MASK_SCHEMA, 3000)
            when(view.v.ne(5), lambda: SetDeaths(9000, SetTo, 1, 0))

        with self.assertRaises(EudslError) as ctx:
            build_module(prog)
        self.assertEqual(ctx.exception.code, "EUDSL-BOUNDS")

    def test_while_loop_memory_field_ne_extremes_stay_frozen_free(self) -> None:
        for rhs, start, end in ((0, 1, 0), (0xFFFFFFFF, 0, 0xFFFFFFFF)):
            with self.subTest(rhs=rhs):
                ids: dict[str, int] = {}

                def prog() -> None:
                    CUnit(1000).hp.set(start)
                    guard = Var(10)
                    iters = Var(0)
                    ids["iters"] = iters.id

                    def body() -> None:
                        CUnit(1000).hp.set(end)
                        iters.iadd(1)
                        guard.isub(1)

                    while_loop([CUnit(1000).hp.ne(rhs), guard.ge(1)], body)

                report = run_sim(build_module(prog).to_json(), 1)
                self.assertEqual(report["var_values"][ids["iters"]], 1)

    def test_memory_field_ne_extremes_are_direct_1t_conditions(self) -> None:
        for rhs in (0, 0xFFFFFFFF):
            with self.subTest(rhs=rhs):
                def prog() -> None:
                    when(
                        CUnit(1000).hp.ne(rhs),
                        lambda: SetDeaths(9000, SetTo, 1, 0),
                    )

                report = run_sim(build_module(prog).to_json(), 1)
                self.assertEqual(report["static_cost"]["triggers"], 1)


# ---------------------------------------------------------------------------
# Ticket #18: `MemFieldHandle.ne` derives its extreme constant from `bitMask`,
# not from the field width. A custom-narrow mask can never hold the width-max
# constant, so `ne(width_max)` is a tautology and must always fire.
# ---------------------------------------------------------------------------

_MASKED_WIDE_SCHEMA = defineMemView("MaskedWide", 4, {"v": FieldSpec(0, 2, mask=0x1F0000)})
_MASKED_FILLING_SCHEMA = defineMemView("MaskedFilling", 4, {"v": FieldSpec(1, 1, mask=0xFF00)})


def _memview_cond_probe(schema, raw: int, make_conds) -> tuple[Any, dict, int]:
    """Build ``when(make_conds(v), hit += 1)`` over ``schema`` and simulate it."""
    ids: dict[str, int] = {}

    def prog() -> None:
        SetDeaths(3000, SetTo, raw, 0)
        view = make_memview(schema, 3000)
        hit = Var(0)
        ids["hit"] = hit.id
        when(make_conds(view.v), lambda: hit.iadd(1))

    module = build_module(prog)
    return module, run_sim(module.to_json(), 1), ids["hit"]


def _masked_ne_union_hits(raw: int) -> int:
    """The `lt` ∪ `gt` oracle for `v != 0xFF` (two separate `when` sites)."""
    ids: dict[str, int] = {}

    def prog() -> None:
        SetDeaths(3000, SetTo, raw, 0)
        view = make_memview(_MASKED_SCHEMA, 3000)
        hit = Var(0)
        ids["hit"] = hit.id
        when(view.v.lt(0xFF), lambda: hit.iadd(1))
        when(view.v.gt(0xFF), lambda: hit.iadd(1))

    report = run_sim(build_module(prog).to_json(), 1)
    return report["var_values"][ids["hit"]]


class MaskedNeExtremeTest(unittest.TestCase):
    def test_masked_ne_extreme_is_always_unequal(self) -> None:
        # 0xFF is not representable inside mask 0xFF00, so no raw can equal it.
        for raw in (0x0000, 0x0100, 0xFF00):
            with self.subTest(raw=raw):
                _, report, hit = _memview_cond_probe(
                    _MASKED_SCHEMA, raw, lambda v: v.ne(0xFF)
                )
                self.assertEqual(report["var_values"][hit], 1)

    def test_masked_ne_extreme_is_the_complement_of_eq(self) -> None:
        for raw in (0x0000, 0x0100, 0xFF00):
            with self.subTest(raw=raw):
                _, eq_report, eq_hit = _memview_cond_probe(
                    _MASKED_SCHEMA, raw, lambda v: v.eq(0xFF)
                )
                _, ne_report, ne_hit = _memview_cond_probe(
                    _MASKED_SCHEMA, raw, lambda v: v.ne(0xFF)
                )
                eq_hits = eq_report["var_values"][eq_hit]
                ne_hits = ne_report["var_values"][ne_hit]
                self.assertEqual(eq_hits, 0)
                self.assertEqual(eq_hits + ne_hits, 1)

    def test_masked_ne_extreme_agrees_with_lt_and_gt_flag_form(self) -> None:
        for raw in (0x0000, 0x0100, 0xFF00):
            with self.subTest(raw=raw):
                _, report, hit = _memview_cond_probe(
                    _MASKED_SCHEMA, raw, lambda v: v.ne(0xFF)
                )
                self.assertEqual(report["var_values"][hit], 1)
                self.assertEqual(_masked_ne_union_hits(raw), 1)
                self.assertEqual(_masked_ne_union_hits(raw), report["var_values"][hit])

    def test_masked_ne_mask_width_extreme_second_shape(self) -> None:
        for raw in (0x00000000, 0x00100000, 0x001F0000):
            with self.subTest(raw=raw):
                _, report, hit = _memview_cond_probe(
                    _MASKED_WIDE_SCHEMA, raw, lambda v: v.ne(0xFFFF)
                )
                self.assertEqual(report["var_values"][hit], 1)

    def test_masked_ne_mask_derived_max_stays_eager(self) -> None:
        for raw, want in ((0x0000, 1), (0x0100, 1), (0xFF00, 0)):
            with self.subTest(raw=raw):
                module, report, hit = _memview_cond_probe(
                    _MASKED_SCHEMA, raw, lambda v: v.ne(0xFF00)
                )
                self.assertEqual(report["var_values"][hit], want)
                # Direct eager extreme: setup act + the gate. The flag form
                # would add three more triggers.
                self.assertEqual(report["static_cost"]["triggers"], 2)
                doc = json.loads(module.to_json())["module"]
                when_stmts = [s for s in doc["stmts"] if s["kind"] == "when"]
                self.assertEqual(len(when_stmts), 1)
                self.assertEqual(len(when_stmts[0]["conds"]), 1)
                cond = when_stmts[0]["conds"][0]
                # The mask-derived max (0xFF00) takes the eager extreme branch:
                # a direct `DeathsX(AtMost, 0xFEFF, mask=0xFF00)`.
                self.assertEqual(cond["condtype"], 15)
                self.assertEqual(cond["comparison"], 1)
                self.assertEqual(cond["amount"], 0xFEFF)
                self.assertEqual(cond["locid"], 0xFF00)

    def test_masked_ne_width_filling_mask_unchanged(self) -> None:
        for raw, want in ((0xFE00, 1), (0xFF00, 0)):
            with self.subTest(raw=raw):
                module, report, hit = _memview_cond_probe(
                    _MASKED_FILLING_SCHEMA, raw, lambda v: v.ne(0xFF)
                )
                self.assertEqual(report["var_values"][hit], want)
                self.assertEqual(report["static_cost"]["triggers"], 2)
                doc = json.loads(module.to_json())["module"]
                when_stmts = [s for s in doc["stmts"] if s["kind"] == "when"]
                self.assertEqual(len(when_stmts), 1)
                self.assertEqual(len(when_stmts[0]["conds"]), 1)
                cond = when_stmts[0]["conds"][0]
                # The pre-change shape: a direct `DeathsX(AtMost, 0xFE00,
                # mask=0xFF00)` and no extra trigger.
                self.assertEqual(cond["condtype"], 15)
                self.assertEqual(cond["comparison"], 1)
                self.assertEqual(cond["amount"], 0xFE00)
                self.assertEqual(cond["locid"], 0xFF00)


_TS_WRAPPER_SCRIPT = textwrap.dedent(
    """
    import {
      CUnit,
      MemView,
      SetDeaths,
      SetTo,
      Var,
      buildModule,
      defineMemView,
      when,
      whileLoop,
      withCP,
    } from "./ts/index.ts";

    const masked = defineMemView("Masked", 4, {
      v: { offset: 0, size: 1, mask: 0xff00 },
    });
    const lowMask = defineMemView("LowMask", 4, {
      v: { offset: 2, size: 2, mask: 0x00ff },
    });

    function build(caseName) {
      return buildModule(() => {
        if (caseName === "canonical") {
          when(CUnit(1000).hp.ne(500), () => SetDeaths(9000, SetTo, 1, 0));
        } else if (caseName === "loop") {
          CUnit(1000).hp.set(0);
          const guard = Var(10);
          const iters = Var(0);
          whileLoop([CUnit(1000).hp.ne(500), guard.ge(1)], () => {
            CUnit(1000).hp.set(500);
            iters.iadd(1);
            guard.isub(1);
          });
        } else if (caseName === "cpLoop") {
          SetDeaths(7005, SetTo, 0x11, 0);
          const guard = Var(10);
          const iters = Var(0);
          let cond;
          withCP(7000, (cp) => {
            cond = cp.at(5).ne(0x99);
          });
          whileLoop([cond, guard.ge(1)], () => {
            SetDeaths(7005, SetTo, 0x99, 0);
            iters.iadd(1);
            guard.isub(1);
          });
        } else if (caseName === "cpExtremes") {
          SetDeaths(7000, SetTo, 0x99, 0);
          SetDeaths(7005, SetTo, 0x0, 0);
          const hit = Var(0);
          let c0;
          let cMax;
          withCP(7000, (cp) => {
            c0 = cp.at(5).ne(0);
            cMax = cp.at(5).ne(0xffffffff);
          });
          when([c0, cMax], () => hit.iadd(1));
        } else if (caseName === "masked") {
          SetDeaths(3000, SetTo, 0x00001200, 0);
          const view = MemView(masked, 3000);
          const hit = Var(0);
          when(view.v.ne(0x1200), () => hit.iadd(1));
        } else if (caseName === "lowMask") {
          const view = MemView(lowMask, 3000);
          when(view.v.ne(5), () => SetDeaths(9000, SetTo, 1, 0));
        } else if (caseName === "twoMarkers") {
          CUnit(1000).hp.set(499);
          CUnit(1000).shield.set(0);
          const hit = Var(0);
          when([CUnit(1000).hp.ne(500), CUnit(1000).shield.ne(7)], () => hit.iadd(1));
        }
      });
    }

    const name = process.argv[1];
    process.stdout.write(build(name).toJson());
    """
)


def _run_ts_wrapper(case_name: str) -> str:
    res = subprocess.run(
        ["node", "--input-type=module", "-e", _TS_WRAPPER_SCRIPT, case_name],
        cwd=str(REPO_ROOT),
        check=True,
        capture_output=True,
        text=True,
    )
    return res.stdout


def _build_ne_parity_module():
    def prog() -> None:
        when(CUnit(1000).hp.ne(500), lambda: SetDeaths(9000, SetTo, 1, 0))

        CUnit(1000).hp.set(0)
        guard = Var(10)
        iters = Var(0)

        def body() -> None:
            CUnit(1000).hp.set(500)
            iters.iadd(1)
            guard.isub(1)

        while_loop([CUnit(1000).hp.ne(500), guard.ge(1)], body)

        SetDeaths(7005, SetTo, 0x11, 0)
        guard2 = Var(10)
        iters2 = Var(0)
        with with_cp(7000) as cp:
            cond = cp.at(5).ne(0x99)

        def body2() -> None:
            SetDeaths(7005, SetTo, 0x99, 0)
            iters2.iadd(1)
            guard2.isub(1)

        while_loop([cond, guard2.ge(1)], body2)

        SetDeaths(7000, SetTo, 0x99, 0)
        SetDeaths(7005, SetTo, 0x0, 0)
        hit = Var(0)
        with with_cp(7000) as cp2:
            c0 = cp2.at(5).ne(0)
            c_max = cp2.at(5).ne(0xFFFFFFFF)
        when([c0, c_max], lambda: hit.iadd(1))

        SetDeaths(3000, SetTo, 0x00001200, 0)
        view = make_memview(_MASKED_SCHEMA, 3000)
        when(view.v.ne(0x1200), lambda: hit.iadd(1))

        # Ticket #18: masked extremes are derived from `bitMask`, so the
        # width-max constant defers (flag form) while the mask-derived max
        # stays a direct eager extreme, in both frontends.
        narrow = make_memview(_MASKED_SCHEMA, 3100)
        when(narrow.v.ne(0xFF), lambda: hit.iadd(1))
        when(narrow.v.ne(0xFF00), lambda: hit.iadd(1))
        wide = make_memview(_MASKED_WIDE_SCHEMA, 3200)
        when(wide.v.ne(0xFFFF), lambda: hit.iadd(1))
        filling = make_memview(_MASKED_FILLING_SCHEMA, 3300)
        when(filling.v.ne(0xFF), lambda: hit.iadd(1))

    return build_module(prog)


_TS_NE_PARITY_SCRIPT = textwrap.dedent(
    """
    import {
      CUnit,
      MemView,
      SetDeaths,
      SetTo,
      Var,
      buildModule,
      defineMemView,
      when,
      whileLoop,
      withCP,
    } from "./ts/index.ts";

    const masked = defineMemView("Masked", 4, {
      v: { offset: 0, size: 1, mask: 0xff00 },
    });
    const maskedWide = defineMemView("MaskedWide", 4, {
      v: { offset: 0, size: 2, mask: 0x1f0000 },
    });
    const maskedFilling = defineMemView("MaskedFilling", 4, {
      v: { offset: 1, size: 1, mask: 0xff00 },
    });

    const mod = buildModule(() => {
      when(CUnit(1000).hp.ne(500), () => SetDeaths(9000, SetTo, 1, 0));

      CUnit(1000).hp.set(0);
      const guard = Var(10);
      const iters = Var(0);
      whileLoop([CUnit(1000).hp.ne(500), guard.ge(1)], () => {
        CUnit(1000).hp.set(500);
        iters.iadd(1);
        guard.isub(1);
      });

      SetDeaths(7005, SetTo, 0x11, 0);
      const guard2 = Var(10);
      const iters2 = Var(0);
      let cond;
      withCP(7000, (cp) => {
        cond = cp.at(5).ne(0x99);
      });
      whileLoop([cond, guard2.ge(1)], () => {
        SetDeaths(7005, SetTo, 0x99, 0);
        iters2.iadd(1);
        guard2.isub(1);
      });

      SetDeaths(7000, SetTo, 0x99, 0);
      SetDeaths(7005, SetTo, 0x0, 0);
      const hit = Var(0);
      let c0;
      let cMax;
      withCP(7000, (cp2) => {
        c0 = cp2.at(5).ne(0);
        cMax = cp2.at(5).ne(0xffffffff);
      });
      when([c0, cMax], () => hit.iadd(1));

      SetDeaths(3000, SetTo, 0x00001200, 0);
      const view = MemView(masked, 3000);
      when(view.v.ne(0x1200), () => hit.iadd(1));

      const narrow = MemView(masked, 3100);
      when(narrow.v.ne(0xff), () => hit.iadd(1));
      when(narrow.v.ne(0xff00), () => hit.iadd(1));
      const wide = MemView(maskedWide, 3200);
      when(wide.v.ne(0xffff), () => hit.iadd(1));
      const filling = MemView(maskedFilling, 3300);
      when(filling.v.ne(0xff), () => hit.iadd(1));
    });

    process.stdout.write(mod.toJson());
    """
)


class DeferredNeHirDriftTest(unittest.TestCase):
    def test_when_memory_field_ne_uses_the_flag_form(self) -> None:
        def prog() -> None:
            when(
                CUnit(1000).hp.ne(500),
                lambda: SetDeaths(9000, SetTo, 1, 0),
            )

        module = build_module(prog)
        report = run_sim(module.to_json(), 1)
        static = report["static_cost"]
        self.assertEqual(static["triggers"], 4)
        self.assertEqual(static["trigger_bytes"], 9_632)
        self.assertEqual(static["total_bytes"], 9_636)
        self.assertEqual(report["executed_cost"]["triggers"], 4)

        doc = json.loads(module.to_json())["module"]
        kinds = [stmt["kind"] for stmt in doc["stmts"]]
        self.assertEqual(kinds.count("trigger"), 1)
        self.assertEqual(kinds.count("when"), 3)
        self.assertEqual([v["init"] for v in doc["vars"]], [0])

        self.assertEqual(module.to_json(), _run_ts_wrapper("canonical"))


class DeferredNeParityTest(unittest.TestCase):
    def test_py_ts_hir_parity_for_every_new_case(self) -> None:
        py_hir = _build_ne_parity_module().to_json()
        res = subprocess.run(
            ["node", "--input-type=module", "-e", _TS_NE_PARITY_SCRIPT],
            cwd=str(REPO_ROOT),
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertEqual(py_hir, res.stdout)


def _build_ne_gate_parity_module():
    def prog() -> None:
        body = lambda: SetDeaths(9000, SetTo, 1, 0)
        when(CUnit(1000).hp.ne(500), body)
        once(CUnit(1000).hp.ne(500), body)
        until(CUnit(1000).hp.ne(500), body)
        on_rising(CUnit(1000).hp.ne(500), body)
        on_falling(CUnit(1000).hp.ne(500), body)

        CUnit(1000).hp.set(0)
        guard = Var(10)
        iters = Var(0)

        def loop_body() -> None:
            CUnit(1000).hp.set(500)
            iters.iadd(1)
            guard.isub(1)

        while_loop([CUnit(1000).hp.ne(500), guard.ge(1)], loop_body)

        hit = Var(0)
        Trigger(conds=[CUnit(1000).hp.ne(500)], acts=[hit.act_add(1)])

    return build_module(prog)


_TS_NE_GATE_PARITY_SCRIPT = textwrap.dedent(
    """
    import {
      CUnit,
      SetDeaths,
      SetTo,
      Trigger,
      Var,
      buildModule,
      onFalling,
      onRising,
      once,
      until,
      when,
      whileLoop,
    } from "./ts/index.ts";

    const mod = buildModule(() => {
      const body = () => SetDeaths(9000, SetTo, 1, 0);
      when(CUnit(1000).hp.ne(500), body);
      once(CUnit(1000).hp.ne(500), body);
      until(CUnit(1000).hp.ne(500), body);
      onRising(CUnit(1000).hp.ne(500), body);
      onFalling(CUnit(1000).hp.ne(500), body);

      CUnit(1000).hp.set(0);
      const guard = Var(10);
      const iters = Var(0);
      whileLoop([CUnit(1000).hp.ne(500), guard.ge(1)], () => {
        CUnit(1000).hp.set(500);
        iters.iadd(1);
        guard.isub(1);
      });

      const hit = Var(0);
      new Trigger({ conds: [CUnit(1000).hp.ne(500)], acts: [hit.act_add(1)] });
    });

    process.stdout.write(mod.toJson());
    """
)


class NeGateExpansionTest(unittest.TestCase):
    """Ticket #16: memory-field `.ne` expands to the 4T flag form at gate sites."""

    def _when_ne_hits(self, seed: int, mutate_hp: int | None = None):
        ids: dict[str, int] = {}

        def prog() -> None:
            CUnit(1000).hp.set(seed)
            hit = Var(0)
            ids["hit"] = hit.id

            def body() -> None:
                hit.iadd(1)
                if mutate_hp is not None:
                    CUnit(1000).hp.set(mutate_hp)

            when(CUnit(1000).hp.ne(500), body)

        report = run_sim(build_module(prog).to_json(), 1)
        return report, report["var_values"][ids["hit"]]

    def test_when_ne_uses_the_flag_form(self) -> None:
        def prog() -> None:
            when(CUnit(1000).hp.ne(500), lambda: SetDeaths(9000, SetTo, 1, 0))

        module = build_module(prog)
        report = run_sim(module.to_json(), 1)
        static = report["static_cost"]
        self.assertEqual(static["triggers"], 4)
        self.assertEqual(static["trigger_bytes"], 9_632)
        self.assertEqual(static["total_bytes"], 9_636)
        self.assertEqual(report["executed_cost"]["triggers"], 4)

        doc = json.loads(module.to_json())["module"]
        kinds = [stmt["kind"] for stmt in doc["stmts"]]
        self.assertEqual(kinds.count("trigger"), 1)
        self.assertEqual(kinds.count("when"), 3)
        self.assertEqual(len(doc["vars"]), 1)

    def test_flag_form_truth_table(self) -> None:
        for seed, want in (
            (0, 1),
            (499, 1),
            (500, 0),
            (501, 1),
            (0xFFFFFFFF, 1),
        ):
            with self.subTest(seed=seed):
                report, hit = self._when_ne_hits(seed)
                self.assertEqual(hit, want)
        # A body that mutates the compared field still fires once.
        _, hit = self._when_ne_hits(499, mutate_hp=600)
        self.assertEqual(hit, 1)

    def _two_cond_hits(self, hp: int, shield: int, *, fallback: bool):
        ids: dict[str, int] = {}

        def prog() -> None:
            CUnit(1000).hp.set(hp)
            CUnit(1000).shield.set(shield)
            hit = Var(0)
            ids["hit"] = hit.id
            if fallback:
                marker = CUnit(1000).hp.ne(500)
                Trigger(
                    conds=[marker, CUnit(1000).shield.eq(7)],
                    acts=[hit.act_add(1)],
                )
            else:
                when(
                    [CUnit(1000).hp.ne(500), CUnit(1000).shield.eq(7)],
                    lambda: hit.iadd(1),
                )

        report = run_sim(build_module(prog).to_json(), 1)
        return report, report["var_values"][ids["hit"]]

    def test_two_cond_gate_with_flag_form(self) -> None:
        gate_report = None
        for hp, shield, want in ((499, 7, 1), (499, 8, 0), (500, 7, 0)):
            with self.subTest(hp=hp, shield=shield):
                gate_report, gate_hit = self._two_cond_hits(hp, shield, fallback=False)
                fallback_report, fallback_hit = self._two_cond_hits(
                    hp, shield, fallback=True
                )
                self.assertEqual(gate_hit, want)
                self.assertEqual(fallback_hit, want)
                self.assertEqual(gate_hit, fallback_hit)
                self.assertEqual(
                    fallback_report["static_cost"]["triggers"],
                    _RETAINED_FALLBACK_TRIGGERS,
                )
        assert gate_report is not None
        self.assertEqual(gate_report["static_cost"]["triggers"], 5)

    def test_while_loop_expands_ne_to_the_4t_flag_gate(self) -> None:
        ids: dict[str, int] = {}

        def prog() -> None:
            CUnit(1000).hp.set(0)
            guard = Var(10)
            iters = Var(0)
            ids["iters"] = iters.id

            def body() -> None:
                CUnit(1000).hp.set(500)
                iters.iadd(1)
                guard.isub(1)

            while_loop([CUnit(1000).hp.ne(500), guard.ge(1)], body)

        module = build_module(prog)
        # Ticket #17: exactly one body run per pass at every pass count — each
        # pass rebuilds the gate flag from live memory via the entry prelude
        # before the entry check, so the loop never spins a second time on a
        # stale value.
        for passes in _PASS_COUNTS:
            report = run_sim(module.to_json(), passes)
            self.assertEqual(
                report["var_values"][ids["iters"]], passes, f"passes={passes}"
            )
            static = report["static_cost"]
            # Ticket #64: same 4T flag gate as `when`, emitted at entry and as
            # the last body statements; the tail jump lands on a refreshed gate.
            self.assertEqual(static["triggers"], 10)
            self.assertEqual(static["conditions"], 6)
            self.assertEqual(static["actions"], 12)
            self.assertEqual(static["total_bytes"], 24_092)
            executed = report["executed_cost"]
            self.assertEqual(executed["triggers"], 11)
            self.assertEqual(executed["conditions"], 7)
            self.assertEqual(executed["actions"], 9)

        doc = json.loads(module.to_json())["module"]
        kinds = [stmt["kind"] for stmt in doc["stmts"]]
        # One reset trigger + two `when`s per emission point (entry, tail).
        self.assertEqual(kinds.count("when"), 2)
        self.assertEqual(kinds.count("trigger"), 2)

        # The same marker in a one-shot gate is the flag form.
        def gate_prog() -> None:
            when(CUnit(1000).hp.ne(500), lambda: SetDeaths(9000, SetTo, 1, 0))

        gate = run_sim(build_module(gate_prog).to_json(), 1)
        self.assertEqual(gate["static_cost"]["triggers"], 4)

    def test_while_loop_two_gate_conds_each_get_one_flag(self) -> None:
        ids: dict[str, int] = {}

        def prog() -> None:
            CUnit(1000).hp.set(0)
            CUnit(1000).shield.set(0)
            guard = Var(10)
            iters = Var(0)
            ids["iters"] = iters.id

            def body() -> None:
                CUnit(1000).hp.set(500)
                CUnit(1000).shield.set(7)
                iters.iadd(1)
                guard.isub(1)

            while_loop(
                [CUnit(1000).hp.ne(500), CUnit(1000).shield.ne(7), guard.ge(1)],
                body,
            )

        module = build_module(prog)
        # One shared flag per gate site: guard + iters + 2 flags.
        self.assertEqual(len(json.loads(module.to_json())["module"]["vars"]), 4)
        static = run_sim(module.to_json(), 1)["static_cost"]
        self.assertEqual(static["triggers"], 16)
        self.assertEqual(static["conditions"], 11)
        self.assertEqual(static["actions"], 20)
        self.assertEqual(static["total_bytes"], 38_540)
        for passes in _PASS_COUNTS:
            report = run_sim(module.to_json(), passes)
            self.assertEqual(
                report["var_values"][ids["iters"]], passes, f"passes={passes}"
            )

    def test_while_loop_gate_nested_in_gate_recomputes(self) -> None:
        ids: dict[str, int] = {}

        def prog() -> None:
            CUnit(1000).hp.set(0)
            CUnit(1000).shield.set(0)
            outer_guard = Var(10)
            inner_guard = Var(3)
            outs = Var(0)
            iters = Var(0)
            ids.update(outs=outs.id, iters=iters.id)

            def inner_body() -> None:
                CUnit(1000).shield.set(7)
                iters.iadd(1)
                inner_guard.isub(1)

            def body() -> None:
                CUnit(1000).hp.set(500)
                CUnit(1000).shield.set(0)
                inner_guard.set(3)
                outs.iadd(1)
                while_loop(
                    [CUnit(1000).shield.ne(7), inner_guard.ge(1)], inner_body
                )
                outer_guard.isub(1)

            while_loop([CUnit(1000).hp.ne(500), outer_guard.ge(1)], body)

        module = build_module(prog)
        # Two gate sites (outer, inner) share one flag each: 2 guards + 2
        # counters + 2 flags.
        self.assertEqual(len(json.loads(module.to_json())["module"]["vars"]), 6)
        for passes in _PASS_COUNTS:
            report = run_sim(module.to_json(), passes)
            self.assertEqual(report["var_values"][ids["outs"]], passes)
            self.assertEqual(report["var_values"][ids["iters"]], passes)

    def test_raw_trigger_uses_the_gate(self) -> None:
        def raw(seed: int):
            def prog_ids(ids: dict[str, int]) -> None:
                CUnit(1000).hp.set(seed)
                hit = Var(0)
                ids["hit"] = hit.id
                Trigger(conds=[CUnit(1000).hp.ne(500)], acts=[hit.act_add(1)])

            return prog_ids

        for seed, want in (
            (499, [1, 2, 3, 5]),
            (500, [0, 0, 0, 0]),
            (501, [1, 2, 3, 5]),
        ):
            with self.subTest(seed=seed):
                self.assertEqual(_hits_over_passes(raw(seed)), want)

        ids: dict[str, int] = {}
        module = build_module(lambda: raw(499)(ids))
        static = run_sim(module.to_json(), 1)["static_cost"]
        self.assertEqual(static["triggers"], _RETAINED_FALLBACK_TRIGGERS)
        self.assertEqual(static["actions"], _RETAINED_FALLBACK_ACTIONS)

        def gate_prog() -> None:
            when(CUnit(1000).hp.ne(500), lambda: SetDeaths(9000, SetTo, 1, 0))

        gate = run_sim(build_module(gate_prog).to_json(), 1)
        self.assertEqual(gate["static_cost"]["triggers"], 4)

    def test_masked_and_cp_ne_use_the_flag_form_correctly(self) -> None:
        def masked(raw: int):
            def prog(ids: dict[str, int]) -> None:
                SetDeaths(3000, SetTo, raw, 0)
                view = make_memview(_MASKED_SCHEMA, 3000)
                hit = Var(0)
                ids["hit"] = hit.id
                when(view.v.ne(0x100), lambda: hit.iadd(1))

            return prog

        for raw, want in (
            (0x100, [0, 0, 0, 0]),
            (0x000, [1, 2, 3, 5]),
            (0x200, [1, 2, 3, 5]),
        ):
            with self.subTest(raw=raw):
                ids: dict[str, int] = {}
                module = build_module(lambda: masked(raw)(ids))
                self.assertEqual(_hits_over_passes(masked(raw)), want)
                self.assertEqual(run_sim(module.to_json(), 1)["static_cost"]["triggers"], 5)

        def cp(field_value: int):
            def prog(ids: dict[str, int]) -> None:
                SetDeaths(7000, SetTo, 0x99, 0)
                SetDeaths(7005, SetTo, field_value, 0)
                hit = Var(0)
                ids["hit"] = hit.id
                with with_cp(7000) as cpc:
                    cond = cpc.at(5).ne(0x99)
                when([cond], lambda: hit.iadd(1))

            return prog

        for field_value, want in (
            (0x11, [1, 2, 3, 5]),
            (0x99, [0, 0, 0, 0]),
        ):
            with self.subTest(field_value=field_value):
                ids = {}
                module = build_module(lambda: cp(field_value)(ids))
                self.assertEqual(_hits_over_passes(cp(field_value)), want)
                self.assertEqual(run_sim(module.to_json(), 1)["static_cost"]["triggers"], 5)

    def test_multiple_markers_in_one_gate(self) -> None:
        for hp, shield, want in (
            (499, 0, 1),
            (499, 7, 0),
            (500, 0, 0),
            (500, 7, 0),
        ):
            with self.subTest(hp=hp, shield=shield):
                ids: dict[str, int] = {}

                def prog() -> None:
                    CUnit(1000).hp.set(hp)
                    CUnit(1000).shield.set(shield)
                    hit = Var(0)
                    ids["hit"] = hit.id
                    when(
                        [CUnit(1000).hp.ne(500), CUnit(1000).shield.ne(7)],
                        lambda: hit.iadd(1),
                    )

                report = run_sim(build_module(prog).to_json(), 1)
                self.assertEqual(report["var_values"][ids["hit"]], want)
                self.assertEqual(report["static_cost"]["triggers"], 8)

        def prog() -> None:
            CUnit(1000).hp.set(499)
            CUnit(1000).shield.set(0)
            hit = Var(0)
            when(
                [CUnit(1000).hp.ne(500), CUnit(1000).shield.ne(7)],
                lambda: hit.iadd(1),
            )

        self.assertEqual(build_module(prog).to_json(), _run_ts_wrapper("twoMarkers"))

    def test_py_ts_hir_parity_for_every_expansion_case(self) -> None:
        body = lambda: SetDeaths(9000, SetTo, 1, 0)
        constructs = {
            "when": lambda: when(CUnit(1000).hp.ne(500), body),
            "once": lambda: once(CUnit(1000).hp.ne(500), body),
            "until": lambda: until(CUnit(1000).hp.ne(500), body),
            "on_rising": lambda: on_rising(CUnit(1000).hp.ne(500), body),
            "on_falling": lambda: on_falling(CUnit(1000).hp.ne(500), body),
        }
        for name, build in constructs.items():
            with self.subTest(construct=name):
                report = run_sim(build_module(build).to_json(), 1)
                self.assertLess(
                    report["static_cost"]["triggers"],
                    38,
                    f"{name} must not use the 38T fallback",
                )

        py_hir = _build_ne_gate_parity_module().to_json()
        res = subprocess.run(
            ["node", "--input-type=module", "-e", _TS_NE_GATE_PARITY_SCRIPT],
            cwd=str(REPO_ROOT),
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertEqual(py_hir, res.stdout)


# ---------------------------------------------------------------------------
# Ticket #17: the deferred-comparison state is rebuilt on EVERY
# materialization, including the one-shot build-time control sites
# (`when`/`once`/`until`/`on_rising`/`on_falling`, false-at-entry
# `while_loop`). The one-shot sites reset the accumulator; `while_loop`
# rebuilds a gate flag via the entry/tail prelude (#64). Raw `Trigger` now
# uses the same gate when a builder is active (#66); the `_raw_site` Var-RHS
# `DeferredCond` rows below still materialize.
# ---------------------------------------------------------------------------

def _hits_over_passes(make_body, *, key: str = "hit") -> list[int]:
    """Cumulative ``key`` counter after 1 / 2 / 3 / 5 whole-module loop passes.

    The module is built once (deferred temporaries are allocated at build
    time) and then simulated for each pass count, so a stale accumulator shows
    up as a rising tail instead of hiding behind a single pass.
    """
    ids: dict[str, int] = {}
    module = build_module(lambda: make_body(ids)).to_json()
    return [run_sim(module, passes)["var_values"][ids[key]] for passes in _PASS_COUNTS]


def _raw_site(op: str, a_init: int, b_init: int):
    def build(ids: dict[str, int]) -> None:
        a = Var(a_init)
        b = Var(b_init)
        hit = Var(0)
        ids["hit"] = hit.id
        Trigger(conds=[getattr(a, op)(b)], acts=[hit.act_add(1)])

    return build


def _when_site(op: str, a_init: int, b_init: int):
    def build(ids: dict[str, int]) -> None:
        a = Var(a_init)
        b = Var(b_init)
        hit = Var(0)
        ids["hit"] = hit.id
        when([getattr(a, op)(b)], lambda: hit.iadd(1))

    return build


class DeferredCondMultiPassTest(unittest.TestCase):
    def test_deferred_cond_multi_pass_matrix(self) -> None:
        rows = [
            # raw Trigger, Var operands
            ("raw eq equal", _raw_site("eq", 5, 5), [1, 2, 3, 5]),
            ("raw eq unequal", _raw_site("eq", 5, 6), [0, 0, 0, 0]),
            ("raw ne equal", _raw_site("ne", 5, 5), [0, 0, 0, 0]),
            ("raw ne unequal", _raw_site("ne", 5, 6), [1, 2, 3, 5]),
            # when, Var operands
            ("when eq equal", _when_site("eq", 5, 5), [1, 2, 3, 5]),
            ("when eq unequal", _when_site("eq", 5, 6), [0, 0, 0, 0]),
            ("when ne equal", _when_site("ne", 5, 5), [0, 0, 0, 0]),
            ("when ne unequal", _when_site("ne", 5, 6), [1, 2, 3, 5]),
        ]
        for name, build, want in rows:
            with self.subTest(case=name):
                self.assertEqual(_hits_over_passes(build), want)

        def raw_expr(a_init: int, b_init: int):
            def build(ids: dict[str, int]) -> None:
                a = Var(a_init)
                b = Var(b_init)
                hit = Var(0)
                ids["hit"] = hit.id
                Trigger(conds=[a.eq(b + 1)], acts=[hit.act_add(1)])

            return build

        def when_expr(a_init: int, b_init: int):
            def build(ids: dict[str, int]) -> None:
                a = Var(a_init)
                b = Var(b_init)
                hit = Var(0)
                ids["hit"] = hit.id
                when([a.eq(b + 1)], lambda: hit.iadd(1))

            return build

        def raw_mid_const(init: int, const: int):
            def build(ids: dict[str, int]) -> None:
                a = Var(init)
                hit = Var(0)
                ids["hit"] = hit.id
                Trigger(conds=[a.ne(const)], acts=[hit.act_add(1)])

            return build

        def when_mid_const(init: int, const: int):
            def build(ids: dict[str, int]) -> None:
                a = Var(init)
                hit = Var(0)
                ids["hit"] = hit.id
                when([a.ne(const)], lambda: hit.iadd(1))

            return build

        rows = [
            # Expression operand
            ("raw expr equal", raw_expr(5, 4), [1, 2, 3, 5]),
            ("raw expr unequal", raw_expr(5, 5), [0, 0, 0, 0]),
            ("when expr equal", when_expr(5, 4), [1, 2, 3, 5]),
            ("when expr unequal", when_expr(5, 5), [0, 0, 0, 0]),
            # Middle constant (ne_const path)
            ("raw mid const equal", raw_mid_const(5, 5), [0, 0, 0, 0]),
            ("raw mid const unequal", raw_mid_const(6, 5), [1, 2, 3, 5]),
            ("when mid const equal", when_mid_const(5, 5), [0, 0, 0, 0]),
            ("when mid const unequal", when_mid_const(6, 5), [1, 2, 3, 5]),
        ]
        for name, build, want in rows:
            with self.subTest(case=name):
                self.assertEqual(_hits_over_passes(build), want)

        # Cost pin for the one-shot `when` site: the reset adds exactly one act
        # (76 -> 77) and nothing else moves.
        ids: dict[str, int] = {}
        module = build_module(lambda: _when_site("eq", 7, 7)(ids)).to_json()
        static = run_sim(module, 1)["static_cost"]
        self.assertEqual(static["triggers"], 5)
        self.assertEqual(static["conditions"], 1)
        self.assertEqual(static["actions"], 77)
        self.assertEqual(static["total_bytes"], 12_048)


class TemporalMultiPassTest(unittest.TestCase):
    def test_temporal_multi_pass_matrix(self) -> None:
        constructs = {
            "once": once,
            "until": until,
            "on_rising": on_rising,
            "on_falling": on_falling,
        }

        def site(kind: str, op: str, a_init: int, b_init: int):
            def build(ids: dict[str, int]) -> None:
                a = Var(a_init)
                b = Var(b_init)
                hit = Var(0)
                ids["hit"] = hit.id
                constructs[kind]([getattr(a, op)(b)], lambda: hit.iadd(1))

            return build

        rows = [
            # `once` latches after its first fire, so an always-true condition
            # stays at one hit; an always-false one must never fire.
            ("once eq equal", site("once", "eq", 5, 5), [1, 1, 1, 1]),
            ("once ne equal", site("once", "ne", 5, 5), [0, 0, 0, 0]),
            # `until` runs the body while the condition is false.
            ("until ne equal", site("until", "ne", 5, 5), [1, 2, 3, 5]),
            # Edges must not see a stale accumulator on later passes.
            ("on_rising eq equal", site("on_rising", "eq", 5, 5), [1, 1, 1, 1]),
            ("on_rising ne equal", site("on_rising", "ne", 5, 5), [0, 0, 0, 0]),
            ("on_falling eq equal", site("on_falling", "eq", 5, 5), [0, 0, 0, 0]),
            ("on_falling ne equal", site("on_falling", "ne", 5, 5), [0, 0, 0, 0]),
        ]
        for name, build, want in rows:
            with self.subTest(case=name):
                self.assertEqual(_hits_over_passes(build), want)

    def test_while_loop_false_at_entry_multi_pass(self) -> None:
        def build(ids: dict[str, int]) -> None:
            CUnit(1000).hp.set(500)
            guard = Var(10)
            iters = Var(0)
            ids["iters"] = iters.id

            def body() -> None:
                iters.iadd(1)
                guard.isub(1)

            while_loop([CUnit(1000).hp.ne(500), guard.ge(1)], body)

        self.assertEqual(_hits_over_passes(build, key="iters"), [0, 0, 0, 0])


class RawTriggerGateMultiPassTest(unittest.TestCase):
    def test_raw_trigger_gate_multi_pass(self) -> None:
        def raw(seed: int):
            def build(ids: dict[str, int]) -> None:
                CUnit(1000).hp.set(seed)
                hit = Var(0)
                ids["hit"] = hit.id
                Trigger(conds=[CUnit(1000).hp.ne(500)], acts=[hit.act_add(1)])

            return build

        for seed, want in (
            (499, [1, 2, 3, 5]),
            (500, [0, 0, 0, 0]),
            (501, [1, 2, 3, 5]),
        ):
            with self.subTest(seed=seed):
                self.assertEqual(_hits_over_passes(raw(seed)), want)

        ids: dict[str, int] = {}
        module = build_module(lambda: raw(500)(ids))
        static = run_sim(module.to_json(), 1)["static_cost"]
        self.assertEqual(static["triggers"], _RETAINED_FALLBACK_TRIGGERS)
        self.assertEqual(static["conditions"], _RETAINED_FALLBACK_CONDITIONS)
        # The gate replaces 35–36 prelude triggers and 3 vars with
        # 3 triggers + 1 flag.
        self.assertEqual(static["actions"], _RETAINED_FALLBACK_ACTIONS)
        self.assertEqual(static["total_bytes"], _RETAINED_FALLBACK_TOTAL_BYTES)

    def test_detached_raw_trigger_keeps_the_scope_error(self) -> None:
        # The gate needs an active module builder; a detached Trigger keeps the
        # pre-existing `materialize()` path, whose Var allocation needs a
        # builder. This is HEAD-compatible, not a new success path.
        with self.assertRaises(EudslError) as ctx:
            Trigger(conds=[CUnit(1000).hp.ne(500)], acts=[])
        self.assertEqual(ctx.exception.code, "EUDSL-SCOPE")

        empty = Trigger(conds=[], acts=[])
        self.assertIsInstance(empty.id, int)


class MaskedDeferredNeMultiPassTest(unittest.TestCase):
    def test_masked_deferred_ne_multi_pass(self) -> None:
        def raw(seed: int):
            def build(ids: dict[str, int]) -> None:
                SetDeaths(3000, SetTo, seed, 0)
                view = make_memview(_MASKED_SCHEMA, 3000)
                hit = Var(0)
                ids["hit"] = hit.id
                Trigger(conds=[view.v.ne(0x100)], acts=[hit.act_add(1)])

            return build

        self.assertEqual(_hits_over_passes(raw(0x0100)), [0, 0, 0, 0])
        self.assertEqual(_hits_over_passes(raw(0x0000)), [1, 2, 3, 5])

        def loop(seed: int, rhs: int = 0x100, settle: int | None = None):
            def build(ids: dict[str, int]) -> None:
                SetDeaths(3000, SetTo, seed, 0)
                view = make_memview(_MASKED_SCHEMA, 3000)
                guard = Var(10)
                iters = Var(0)
                ids["iters"] = iters.id

                def body() -> None:
                    if settle is not None:
                        SetDeaths(3000, SetTo, settle, 0)
                    iters.iadd(1)
                    guard.isub(1)

                while_loop([view.v.ne(rhs), guard.ge(1)], body)

            return build

        # The mask lane decides the comparison: the dirty bit outside the mask
        # must not start the loop, a settled lane stops it after one pass, and
        # a lane that never settles stays guard-capped at 10 per pass.
        self.assertEqual(
            _hits_over_passes(loop(0x1201, 0x1200, 0x1200), key="iters"),
            [0, 0, 0, 0],
        )
        self.assertEqual(
            _hits_over_passes(loop(0x1200, 0x1200, 0x1200), key="iters"),
            [0, 0, 0, 0],
        )
        self.assertEqual(
            _hits_over_passes(loop(0x3400, 0x1200, 0x1200), key="iters"),
            [1, 2, 3, 5],
        )
        self.assertEqual(
            _hits_over_passes(loop(0x0000, 0x0100, 0x0000), key="iters"),
            [10, 10, 10, 10],
        )
        self.assertEqual(_hits_over_passes(loop(0x0100), key="iters"), [0, 0, 0, 0])


# ---------------------------------------------------------------------------
# Ticket #21: one representable-value rule for both field surfaces.
#
# A field's value space is the set of bit patterns representable inside
# `bitMask`, expressed in field units. These tests pin the four symptoms the
# scan grouped as F1 (crash), F3 (width-derived max), F4 (`eq` false positive)
# and F5 (unrepresentable `ne` still paying the flag form), plus the shared
# `_field_value_space` helper and the negative-constant decision.
# ---------------------------------------------------------------------------

# A `Var`'s value lives at byte 20 of its `TriggerPage` action slot; byte 22 is
# the high word of that dword (`shift = 16`), the field `F4` measured.
_REF_VALUE_OFFSET = 20
_REF_HIGH_WORD_OFFSET = 22

_WORD_AT2_SCHEMA = defineMemView("WordAt2", 4, {"w": FieldSpec(2, 2)})


def _ref(
    var_id: int,
    byte_offset: int = _REF_VALUE_OFFSET,
    size: int = 4,
    mask: int | None = None,
):
    return TriggerFieldRef(-1, byte_offset, size, 0, var_id, mask)


def _ref_cond_hits(raw: int, make_cond, *, raw_trigger: bool = False):
    """Build one `TriggerFieldRef` comparison over a `Var` and simulate it."""
    ids: dict[str, int] = {}

    def prog() -> None:
        value = Var(0)
        value.set(raw)
        hit = Var(0)
        ids["hit"] = hit.id
        cond = make_cond(_ref(value.id, size=4))
        if raw_trigger:
            Trigger(conds=[cond], acts=[hit.act_add(1)])
        else:
            when(cond, lambda: hit.iadd(1))

    module = build_module(prog)
    return module, run_sim(module.to_json(), 1), ids["hit"]


def _mem_word_hits(raw: int, make_cond) -> tuple[Any, dict, int]:
    ids: dict[str, int] = {}

    def prog() -> None:
        SetDeaths(3000, SetTo, raw, 0)
        view = make_memview(_WORD_AT2_SCHEMA, 3000)
        hit = Var(0)
        ids["hit"] = hit.id
        when(make_cond(view.w), lambda: hit.iadd(1))

    module = build_module(prog)
    return module, run_sim(module.to_json(), 1), ids["hit"]


class TriggerFieldRefValueSpaceTest(unittest.TestCase):
    """F1: `TriggerFieldRef.ne(middle constant)` defers instead of crashing."""

    def test_trigger_field_ref_ne_middle_constant_defers(self) -> None:
        for raw, want in ((500, 0), (0, 1), (501, 1)):
            with self.subTest(raw=raw):
                _, report, hit = _ref_cond_hits(raw, lambda r: r.ne(500))
                self.assertEqual(report["var_values"][hit], want)

        module, report, _ = _ref_cond_hits(500, lambda r: r.ne(500))
        static = report["static_cost"]
        # The `when` gate flag form: the `value.set(raw)` setup act, the flag
        # reset, `when(lt)`, `when(gt)` and the caller's gate.
        self.assertEqual(static["triggers"], 5)
        self.assertEqual(static["conditions"], 3)
        self.assertEqual(static["actions"], 5)
        self.assertEqual(static["total_bytes"], 12_052)

        kinds = [stmt["kind"] for stmt in json.loads(module.to_json())["module"]["stmts"]]
        self.assertEqual(kinds.count("trigger"), 2)
        self.assertEqual(kinds.count("when"), 3)

    def test_trigger_field_ref_ne_raw_trigger_gate(self) -> None:
        for raw, want in ((500, 0), (501, 1)):
            with self.subTest(raw=raw):
                _, report, hit = _ref_cond_hits(
                    raw, lambda r: r.ne(500), raw_trigger=True
                )
                self.assertEqual(report["var_values"][hit], want)

        _, report, _ = _ref_cond_hits(500, lambda r: r.ne(500), raw_trigger=True)
        static = report["static_cost"]
        # The raw-Trigger gate, 4 B over the `MemFieldHandle` pin (12,048) for
        # the field-reference target form.
        self.assertEqual(static["triggers"], 5)
        self.assertEqual(static["conditions"], 3)
        self.assertEqual(static["actions"], 5)
        self.assertEqual(static["total_bytes"], 12_052)

    def test_trigger_field_ref_ne_in_while_loop_rebuilds(self) -> None:
        ids: dict[str, int] = {}

        def prog() -> None:
            value = Var(0)
            guard = Var(10)
            iters = Var(0)
            ids["iters"] = iters.id
            ref = _ref(value.id, size=4)

            def body() -> None:
                value.set(500)
                iters.iadd(1)
                guard.isub(1)

            while_loop([ref.ne(500), guard.ge(1)], body)

        module = build_module(prog)
        report = run_sim(module.to_json(), 1)
        # #17's freeze-free contract on the sibling surface: the tail rebuild
        # re-reads live storage, so the second pass sees 500 and stops.
        self.assertEqual(report["var_values"][ids["iters"]], 1)
        # One shared gate flag per site (value + guard + iters + flag).
        self.assertEqual(len(json.loads(module.to_json())["module"]["vars"]), 4)


class MaskedTriggerFieldRefValueSpaceTest(unittest.TestCase):
    """F3: `gt`/`ne` derive `max_val` from `bitMask`, not from the width."""

    def _masked(self, raw: int, make_cond):
        ids: dict[str, int] = {}

        def prog() -> None:
            value = Var(0)
            value.set(raw)
            hit = Var(0)
            ids["hit"] = hit.id
            ref = _ref(value.id, byte_offset=_REF_VALUE_OFFSET, size=1, mask=0xFF00)
            when(make_cond(ref), lambda: hit.iadd(1))

        module = build_module(prog)
        return module, run_sim(module.to_json(), 1), ids["hit"]

    def test_trigger_field_ref_gt_derives_max_from_mask(self) -> None:
        for raw in (0x0100, 0x1200, 0xFF00):
            with self.subTest(raw=raw):
                _, report, hit = self._masked(raw, lambda r: r.gt(0xFF))
                self.assertEqual(report["var_values"][hit], 1)
        _, report, _ = self._masked(0x0100, lambda r: r.gt(0xFF))
        # One direct `AtLeast(0x100) & 0xFF00` cond: setup act + the gate.
        self.assertEqual(report["static_cost"]["triggers"], 2)

    def test_masked_trigger_field_ref_ne_is_always_unequal(self) -> None:
        # 0xFF is not representable inside mask 0xFF00: `ne` is a tautology,
        # folded to an always-true direct cond (2T instead of the 5T gate).
        for raw in (0x0000, 0x0100, 0x1200, 0xFF00):
            with self.subTest(raw=raw):
                _, report, hit = self._masked(raw, lambda r: r.ne(0xFF))
                self.assertEqual(report["var_values"][hit], 1)
        _, report, _ = self._masked(0x0000, lambda r: r.ne(0xFF))
        self.assertEqual(report["static_cost"]["triggers"], 2)

    def test_mask_derived_max_still_eager(self) -> None:
        for raw, want in ((0x0000, 1), (0x0100, 1), (0xFF00, 0)):
            with self.subTest(raw=raw):
                _, report, hit = self._masked(raw, lambda r: r.ne(0xFF00))
                self.assertEqual(report["var_values"][hit], want)


class UnrepresentableConstantFoldTest(unittest.TestCase):
    """F4/F5: unrepresentable constants fold instead of lying or overpaying."""

    def test_eq_unrepresentable_constant_is_never_on_mem_field(self) -> None:
        for raw in (0x0000FFFF, 0x00000000, 0x0000FF00, 0x00010000):
            with self.subTest(raw=raw):
                _, report, hit = _mem_word_hits(raw, lambda w: w.eq(0x10000))
                self.assertEqual(report["var_values"][hit], 0)

        _, report, _ = _mem_word_hits(0x0000FFFF, lambda w: w.eq(0x10000))
        self.assertEqual(report["static_cost"]["triggers"], 2)
        self.assertEqual(report["static_cost"]["total_bytes"], 4_820)

    def test_eq_unrepresentable_constant_is_never_on_trigger_field_ref(self) -> None:
        for raw in (0x0000FFFF, 0x00000000, 0x0000FF00, 0x00010000):
            with self.subTest(raw=raw):
                ids: dict[str, int] = {}

                def prog() -> None:
                    value = Var(raw)
                    hit = Var(0)
                    ids["hit"] = hit.id
                    ref = _ref(value.id, byte_offset=_REF_HIGH_WORD_OFFSET, size=2)
                    when(ref.eq(0x10000), lambda: hit.iadd(1))

                report = run_sim(build_module(prog).to_json(), 1)
                self.assertEqual(report["var_values"][ids["hit"]], 0)

    def test_ne_unrepresentable_constant_folds_to_always_true(self) -> None:
        ids: dict[str, int] = {}

        def prog() -> None:
            SetDeaths(3000, SetTo, 0x0000, 0)
            view = make_memview(_MASKED_SCHEMA, 3000)
            hit = Var(0)
            ids["hit"] = hit.id
            when(view.v.ne(0xFF), lambda: hit.iadd(1))

        module = build_module(prog)
        report = run_sim(module.to_json(), 1)
        self.assertEqual(report["var_values"][ids["hit"]], 1)
        static = report["static_cost"]
        # The F5 pin: 2T / 1C / 2A / 4,820 B, not the 5T / 3C / 5A / 12,048 B
        # flag form.
        self.assertEqual(static["triggers"], 2)
        self.assertEqual(static["conditions"], 1)
        self.assertEqual(static["actions"], 2)
        self.assertEqual(static["total_bytes"], 4_820)

    def test_ne_unrepresentable_constant_on_default_mask_word(self) -> None:
        # 0x10000 cannot be held by a 16-bit field: `ne` is a tautology.
        for raw in (0x0000FFFF, 0x00000000, 0x00010000):
            with self.subTest(raw=raw):
                _, report, hit = _mem_word_hits(raw, lambda w: w.ne(0x10000))
                self.assertEqual(report["var_values"][hit], 1)
        _, report, _ = _mem_word_hits(0x0000FFFF, lambda w: w.ne(0x10000))
        self.assertEqual(report["static_cost"]["triggers"], 2)


class NegativeConstantValueSpaceTest(unittest.TestCase):
    """Negative constants normalise to 32 bits before the representability test.

    `-1` is `0xFFFFFFFF`, which a 16-bit field cannot hold, so `word.eq(-1)` is
    `Never()` and `word.ne(-1)` is always true. This is a deliberate break from
    the old shift-wrap behaviour, which compared the high word against 0xFFFF.
    Migration: write the field-width all-ones constant (`0xFFFF`) to mean all
    ones; it stays representable and eager.
    """

    def test_negative_constant_on_narrow_field_is_unrepresentable(self) -> None:
        for raw in (0xFFFF0000, 0x0000FFFF, 0x00000000):
            with self.subTest(raw=raw):
                _, report, hit = _mem_word_hits(raw, lambda w: w.eq(-1))
                self.assertEqual(report["var_values"][hit], 0)

        for raw, want in ((0xFFFF0000, 1), (0x0000FFFF, 1), (0x00000000, 1)):
            with self.subTest(probe="ne", raw=raw):
                _, report, hit = _mem_word_hits(raw, lambda w: w.ne(-1))
                self.assertEqual(report["var_values"][hit], want)

        _, report, _ = _mem_word_hits(0xFFFF0000, lambda w: w.ne(-1))
        self.assertEqual(report["static_cost"]["triggers"], 2)

    def test_dword_negative_constant_stays_representable(self) -> None:
        def hits(make_cond, hp: int) -> int:
            ids: dict[str, int] = {}

            def prog() -> None:
                CUnit(1000).hp.set(hp)
                hit = Var(0)
                ids["hit"] = hit.id
                when(CUnit(1000).hp.ne(make_cond()), lambda: hit.iadd(1))

            report = run_sim(build_module(prog).to_json(), 1)
            return report["var_values"][ids["hit"]]

        # `hp.ne(-1)` must keep meaning `hp.ne(0xFFFFFFFF)`.
        self.assertEqual(hits(lambda: -1, 0xFFFFFFFF), 0)
        self.assertEqual(hits(lambda: 0xFFFFFFFF, 0xFFFFFFFF), 0)
        self.assertEqual(hits(lambda: -1, 0), 1)

    def test_field_value_space_unit(self) -> None:
        from py.view import _field_value_space

        self.assertEqual(_field_value_space(0xFFFFFFFF, 0, 4), 0xFFFFFFFF)
        self.assertEqual(_field_value_space(0xFFFF0000, 16, 2), 0xFFFF)
        self.assertEqual(_field_value_space(0xFF00, 0, 1), 0xFF00)
        self.assertEqual(_field_value_space(0x1F0000, 0, 2), 0x1F0000)
        # Invalid mask (bits below the shift); callers validate before use.
        self.assertEqual(_field_value_space(0x00FF, 16, 2), 0)


# ---------------------------------------------------------------------------
# Ticket #23: finish the field value-space rule for ordering comparisons.
#
# `ge`/`le`/`gt`/`lt` fold by direction after 32-bit normalisation, on both
# field surfaces and both frontends, and `_field_value_space` derives the space
# from the mask alone (no default-mask shortcut), so straddling fields stop
# reporting the nominal width.
# ---------------------------------------------------------------------------

_DWORD_AT2_SCHEMA = defineMemView("DwordAt2", 4, {"d": FieldSpec(2, 4)})
_DWORD_AT1_SCHEMA = defineMemView("DwordAt1", 4, {"d": FieldSpec(1, 4)})
_WORD_AT3_SCHEMA = defineMemView("WordAt3", 4, {"w": FieldSpec(3, 2)})
_CP_MASKED_SCHEMA = defineMemView("CpMasked", 4, {"v": FieldSpec(4, 1, mask=0xFF00)})

_ORDERING_OPS = {
    "ge": operator.ge,
    "le": operator.le,
    "gt": operator.gt,
    "lt": operator.lt,
}
# The scan's probe constants: two above the 16-bit space, then the in-space
# max, then negatives (which normalise to 0xFFFFFFFF / 0xFFFFFFFE), then 0.
_ORDERING_CONSTANTS = (0x10000, 0x10001, 0xFFFF, -1, -2, 0)
# Raws whose high word is the field: 0, 1, 0, 0x7FFF, 0xFFFF.
_ORDERING_RAWS = (0x00000000, 0x00010000, 0x0000FFFF, 0x7FFF0000, 0xFFFF0000)


def _mem_hits(schema, raw: int, make_cond):
    """Build one `MemFieldHandle` comparison over a schema view and simulate it."""
    ids: dict[str, int] = {}

    def prog() -> None:
        SetDeaths(3000, SetTo, raw, 0)
        view = make_memview(schema, 3000)
        hit = Var(0)
        ids["hit"] = hit.id
        when(make_cond(view), lambda: hit.iadd(1))

    module = build_module(prog)
    return module, run_sim(module.to_json(), 1), ids["hit"]


def _ref_hits(
    raw: int,
    make_cond,
    *,
    byte_offset: int = _REF_HIGH_WORD_OFFSET,
    size: int = 2,
    mask: int | None = None,
):
    """Build one `TriggerFieldRef` comparison over a `Var` and simulate it."""
    ids: dict[str, int] = {}

    def prog() -> None:
        value = Var(0)
        value.set(raw)
        hit = Var(0)
        ids["hit"] = hit.id
        ref = _ref(value.id, byte_offset=byte_offset, size=size, mask=mask)
        when(make_cond(ref), lambda: hit.iadd(1))

    module = build_module(prog)
    return module, run_sim(module.to_json(), 1), ids["hit"]


class OrderingValueSpaceTest(unittest.TestCase):
    """Ticket #23: ordering comparisons fold against the true value space."""

    def test_ordering_value_space_truth_table(self) -> None:
        # 4 ops x 6 constants x 5 raws = 120 rows per surface. The scan
        # measured 35/120 wrong on the base commit for both surfaces.
        for op, fn in _ORDERING_OPS.items():
            for const in _ORDERING_CONSTANTS:
                c = const & 0xFFFFFFFF
                for raw in _ORDERING_RAWS:
                    value = (raw >> 16) & 0xFFFF
                    want = 1 if fn(value, c) else 0

                    with self.subTest(surface="MemFieldHandle", op=op, const=const, raw=raw):
                        _, report, hit = _mem_hits(
                            _WORD_AT2_SCHEMA,
                            raw,
                            lambda view: getattr(view.w, op)(const),
                        )
                        self.assertEqual(report["var_values"][hit], want)

                    with self.subTest(surface="TriggerFieldRef", op=op, const=const, raw=raw):
                        _, report, hit = _ref_hits(
                            raw, lambda ref: getattr(ref, op)(const)
                        )
                        self.assertEqual(report["var_values"][hit], want)

    def test_ordering_straddle_shortcut_removed(self) -> None:
        # A straddling field's width is nominal but its space is not: the
        # helper must return `bitMask >> shift`, not the full width.
        mem_cases = (
            ("FieldSpec(2, 4)", _DWORD_AT2_SCHEMA, "d", 0x10000),
            ("FieldSpec(1, 4)", _DWORD_AT1_SCHEMA, "d", 0x1000000),
            ("FieldSpec(3, 2)", _WORD_AT3_SCHEMA, "w", 0x100),
        )
        for label, schema, field_name, const in mem_cases:
            with self.subTest(label=label):
                _, report, hit = _mem_hits(
                    schema,
                    0,
                    lambda view, f=field_name, c=const: getattr(view, f).eq(c),
                )
                self.assertEqual(report["var_values"][hit], 0)

        ref_cases = (
            ("TriggerFieldRef(-1, 22, 4)", 22, 4, 0x10000),
            ("TriggerFieldRef(-1, 23, 2)", 23, 2, 0x100),
        )
        for label, offset, size, const in ref_cases:
            with self.subTest(label=label):
                _, report, hit = _ref_hits(
                    0,
                    lambda ref, c=const: ref.eq(c),
                    byte_offset=offset,
                    size=size,
                )
                self.assertEqual(report["var_values"][hit], 0)

        from py.view import _field_value_space

        self.assertEqual(_field_value_space(0xFFFF0000, 16, 4), 0xFFFF)
        self.assertEqual(_field_value_space(0xFFFFFF00, 8, 4), 0xFFFFFF)
        self.assertEqual(_field_value_space(0xFF000000, 24, 2), 0xFF)

    def test_ordering_folds_are_target_free(self) -> None:
        word = make_memview(_WORD_AT2_SCHEMA, 3000).w
        ref = _ref(0, byte_offset=_REF_HIGH_WORD_OFFSET, size=2)

        for surface in (word, ref):
            with self.subTest(label=type(surface).__name__):
                self.assertEqual(surface.ge(0x10000), Never())
                self.assertEqual(surface.le(0x10000), Always())
                self.assertEqual(surface.gt(-1), Never())
                self.assertEqual(surface.lt(-1), Always())
                self.assertEqual(surface.lt(0), Never())
                # Already-folded directions keep their target-free form too.
                self.assertEqual(surface.gt(0xFFFF), Never())
                self.assertEqual(surface.le(-2), Always())

                # Anti-over-fold: representable constants stay eager.
                for cond in (
                    surface.ge(0),
                    surface.le(0xFFFF),
                    surface.gt(0xFFFE),
                    surface.lt(0xFFFF),
                ):
                    self.assertNotEqual(cond, Never())
                    self.assertNotEqual(cond, Always())

    def test_ordering_non_contiguous_mask_does_not_over_fold(self) -> None:
        # The ordering folds ask a numeric question, not the `c & ~max` bit
        # test: on mask 0x0F0F, `ge(0x10)` is a valid threshold.
        ref = _ref(0, byte_offset=20, size=1, mask=0x0F0F)
        self.assertNotEqual(ref.ge(0x10), Never())

        for raw, want in ((0x0000, 0), (0x0010, 0), (0x0100, 1), (0x0F0F, 1)):
            with self.subTest(raw=raw):
                _, report, hit = _ref_hits(
                    raw,
                    lambda r: r.ge(0x10),
                    byte_offset=20,
                    size=1,
                    mask=0x0F0F,
                )
                self.assertEqual(report["var_values"][hit], want)

    def test_ordering_representable_hir_sha_unchanged(self) -> None:
        # No golden moves: every constant here is representable, so the fold
        # helper must leave the HIR byte-identical (same sha on 2ee9659).
        def prog() -> None:
            SetDeaths(3000, SetTo, 0x12345678, 0)
            word = make_memview(_WORD_AT2_SCHEMA, 3000)
            value = Var(0)
            ref = _ref(value.id, byte_offset=_REF_HIGH_WORD_OFFSET, size=2)
            for cond in (
                word.w.eq(3),
                word.w.ne(5),
                word.w.ge(6),
                word.w.le(0xFFFF),
                word.w.gt(2),
                word.w.lt(9),
                ref.eq(3),
                ref.ne(5),
                ref.ge(6),
                ref.le(0xFFFF),
                ref.gt(2),
                ref.lt(9),
            ):
                when(cond, lambda: SetDeaths(9000, SetTo, 1, 0))
            when(CUnit(1000).hp.le(0xFFFFFFFF), lambda: SetDeaths(9000, SetTo, 1, 0))

        hir = build_module(prog).to_json()
        self.assertEqual(
            hashlib.sha256(hir.encode()).hexdigest(),
            "777a72c25003fb527c74b42dc56c9535495cdd4939b6bfe2fd12a0e7249bdcc8",
        )

    def test_ordering_and_ne_always_fold_use_always(self) -> None:
        masked = make_memview(_MASKED_SCHEMA, 3000).v
        word = make_memview(_WORD_AT2_SCHEMA, 3000).w
        self.assertEqual(masked.ne(0xFF), Always())
        self.assertEqual(word.ne(0x10000), Always())

        # Sentinel `with_cp(0)` path: the fold lands before the target is
        # resolved, so no CP hop is materialised. Before the fix this was
        # 3T / 1C / 4A / 7,228 B; `Always()` is 2T / 1C / 2A / 4,820 B
        # (4,816 B plus this harness's 4 B module overhead).
        def sentinel() -> None:
            hit = Var(0)
            with with_cp(0) as cp:
                when(cp.view(_CP_MASKED_SCHEMA).v.ne(0xFF), lambda: hit.iadd(1))

        static = run_sim(build_module(sentinel).to_json(), 1)["static_cost"]
        self.assertEqual(static["triggers"], 2)
        self.assertEqual(static["conditions"], 1)
        self.assertEqual(static["actions"], 2)
        self.assertEqual(static["total_bytes"], 4_820)

        # The absolute path already resolved without a hop; it does not move.
        def absolute() -> None:
            hit = Var(0)
            with with_cp(7000) as cp:
                when(cp.view(_CP_MASKED_SCHEMA).v.ne(0xFF), lambda: hit.iadd(1))

        static = run_sim(build_module(absolute).to_json(), 1)["static_cost"]
        self.assertEqual(static["triggers"], 2)
        self.assertEqual(static["conditions"], 1)
        self.assertEqual(static["actions"], 2)
        self.assertEqual(static["total_bytes"], 4_820)

    def test_ordering_invalid_mask_hits_unchanged(self) -> None:
        # `FieldSpec(2, 2, mask=0x00FF)` has no representable values (its bits
        # sit below the shift), so every ordering constant folds — but the
        # measured hit counts must stay what the eager form returned, and the
        # cond form moves from DeathsX to Never/Always deliberately.
        for op, const, want in (
            ("ge", 5, 0),
            ("le", 5, 1),
            ("gt", 5, 0),
            ("lt", 5, 1),
        ):
            with self.subTest(op=op):
                _, report, hit = _mem_hits(
                    _LOW_MASK_SCHEMA,
                    0x00050000,
                    lambda view, o=op, c=const: getattr(view.v, o)(c),
                )
                self.assertEqual(report["var_values"][hit], want)

        def prog() -> None:
            SetDeaths(3000, SetTo, 0x00050000, 0)
            view = make_memview(_LOW_MASK_SCHEMA, 3000)
            when(view.v.ne(5), lambda: SetDeaths(9000, SetTo, 1, 0))

        with self.assertRaises(EudslError) as ctx:
            build_module(prog)
        self.assertEqual(ctx.exception.code, "EUDSL-BOUNDS")


class TriggerFieldRefValidationTest(unittest.TestCase):
    """`_validate_mask_shift` runs before folding on the sibling surface."""

    def test_trigger_field_ref_ne_rejects_mask_bits_below_shift(self) -> None:
        def prog() -> None:
            value = Var(0)
            ref = _ref(value.id, byte_offset=_REF_HIGH_WORD_OFFSET, size=2, mask=0x00FF)
            when(ref.ne(5), lambda: SetDeaths(9000, SetTo, 1, 0))

        with self.assertRaises(EudslError) as ctx:
            build_module(prog)
        self.assertEqual(ctx.exception.code, "EUDSL-BOUNDS")

    def test_trigger_field_ref_eq_does_not_validate(self) -> None:
        # Only `ne` and `read()` validate; `eq` stays representability-only.
        def prog() -> None:
            value = Var(0)
            ref = _ref(value.id, byte_offset=_REF_HIGH_WORD_OFFSET, size=2, mask=0x00FF)
            when(ref.eq(5), lambda: SetDeaths(9000, SetTo, 1, 0))

        build_module(prog)


def _build_value_space_parity_module():
    def prog() -> None:
        hit = Var(0)

        # F1: deferred middle-constant `ne` on a `TriggerFieldRef`.
        value = Var(0)
        value.set(500)
        ref = _ref(value.id, size=4)
        when(ref.ne(500), lambda: hit.iadd(1))
        Trigger(conds=[ref.ne(501)], acts=[hit.act_add(1)])

        guard = Var(10)
        iters = Var(0)

        def body() -> None:
            value.set(500)
            iters.iadd(1)
            guard.isub(1)

        while_loop([ref.ne(500), guard.ge(1)], body)

        # F3: mask-derived max on `gt` and the folded `ne`.
        masked_ref = _ref(value.id, byte_offset=_REF_VALUE_OFFSET, size=1, mask=0xFF00)
        when(masked_ref.gt(0xFF), lambda: hit.iadd(1))
        when(masked_ref.ne(0xFF), lambda: hit.iadd(1))

        # F4: default-mask overflow `eq` on both surfaces.
        word_view = make_memview(_WORD_AT2_SCHEMA, 3000)
        when(word_view.w.eq(0x10000), lambda: hit.iadd(1))
        high = _ref(value.id, byte_offset=_REF_HIGH_WORD_OFFSET, size=2)
        when(high.eq(0x10000), lambda: hit.iadd(1))

        # F5: unrepresentable `ne` folds on both surfaces.
        masked_view = make_memview(_MASKED_SCHEMA, 3000)
        when(masked_view.v.ne(0xFF), lambda: hit.iadd(1))
        when(word_view.w.ne(0x10000), lambda: hit.iadd(1))

        # Negative constants normalise before the test.
        when(word_view.w.eq(-1), lambda: hit.iadd(1))
        when(word_view.w.ne(-1), lambda: hit.iadd(1))
        when(CUnit(1000).hp.ne(-1), lambda: hit.iadd(1))

        # Ticket #23: ordering folds after 32-bit normalisation.
        when(word_view.w.ge(0x10000), lambda: hit.iadd(1))
        when(word_view.w.le(0x10000), lambda: hit.iadd(1))
        when(word_view.w.gt(-1), lambda: hit.iadd(1))
        when(word_view.w.lt(-1), lambda: hit.iadd(1))
        when(word_view.w.lt(0), lambda: hit.iadd(1))
        when(high.ge(0x10000), lambda: hit.iadd(1))

        # Straddle: the space comes from the mask, not the nominal width.
        dword_view = make_memview(_DWORD_AT2_SCHEMA, 3000)
        when(dword_view.d.eq(0x10000), lambda: hit.iadd(1))

        # Non-contiguous mask: a numeric threshold below the max stays eager.
        noncontiguous = _ref(value.id, byte_offset=_REF_VALUE_OFFSET, size=1, mask=0x0F0F)
        when(noncontiguous.ge(0x10), lambda: hit.iadd(1))

        # Sentinel `Always()` fold: no CP hop, no target.
        with with_cp(0) as sentinel_cp:
            when(
                sentinel_cp.view(_CP_MASKED_SCHEMA).v.ne(0xFF),
                lambda: hit.iadd(1),
            )

    return build_module(prog)


_TS_VALUE_SPACE_PARITY_SCRIPT = textwrap.dedent(
    """
    import {
      CUnit,
      MemView,
      SetDeaths,
      SetTo,
      Trigger,
      TriggerFieldRef,
      Var,
      buildModule,
      defineMemView,
      when,
      whileLoop,
      withCP,
    } from "./ts/index.ts";

    const wordAt2 = defineMemView("WordAt2", 4, {
      w: { offset: 2, size: 2 },
    });
    const masked = defineMemView("Masked", 4, {
      v: { offset: 0, size: 1, mask: 0xff00 },
    });
    const dwordAt2 = defineMemView("DwordAt2", 4, {
      d: { offset: 2, size: 4 },
    });
    const cpMasked = defineMemView("CpMasked", 4, {
      v: { offset: 4, size: 1, mask: 0xff00 },
    });

    const mod = buildModule(() => {
      const hit = Var(0);

      const value = Var(0);
      value.set(500);
      const ref = new TriggerFieldRef(-1, 20, 4, 0, value.id);
      when(ref.ne(500), () => hit.iadd(1));
      new Trigger({ conds: [ref.ne(501)], acts: [hit.act_add(1)] });

      const guard = Var(10);
      const iters = Var(0);
      whileLoop([ref.ne(500), guard.ge(1)], () => {
        value.set(500);
        iters.iadd(1);
        guard.isub(1);
      });

      const maskedRef = new TriggerFieldRef(-1, 20, 1, 0, value.id, 0xff00);
      when(maskedRef.gt(0xff), () => hit.iadd(1));
      when(maskedRef.ne(0xff), () => hit.iadd(1));

      const wordView = MemView(wordAt2, 3000);
      when(wordView.w.eq(0x10000), () => hit.iadd(1));
      const high = new TriggerFieldRef(-1, 22, 2, 0, value.id);
      when(high.eq(0x10000), () => hit.iadd(1));

      const maskedView = MemView(masked, 3000);
      when(maskedView.v.ne(0xff), () => hit.iadd(1));
      when(wordView.w.ne(0x10000), () => hit.iadd(1));

      when(wordView.w.eq(-1), () => hit.iadd(1));
      when(wordView.w.ne(-1), () => hit.iadd(1));
      when(CUnit(1000).hp.ne(-1), () => hit.iadd(1));

      // Ticket #23: ordering folds after 32-bit normalisation.
      when(wordView.w.ge(0x10000), () => hit.iadd(1));
      when(wordView.w.le(0x10000), () => hit.iadd(1));
      when(wordView.w.gt(-1), () => hit.iadd(1));
      when(wordView.w.lt(-1), () => hit.iadd(1));
      when(wordView.w.lt(0), () => hit.iadd(1));
      when(high.ge(0x10000), () => hit.iadd(1));

      // Straddle: the space comes from the mask, not the nominal width.
      const dwordView = MemView(dwordAt2, 3000);
      when(dwordView.d.eq(0x10000), () => hit.iadd(1));

      // Non-contiguous mask: a numeric threshold below the max stays eager.
      const noncontiguous = new TriggerFieldRef(-1, 20, 1, 0, value.id, 0x0f0f);
      when(noncontiguous.ge(0x10), () => hit.iadd(1));

      // Sentinel `Always()` fold: no CP hop, no target.
      withCP(0, (sentinelCp) => {
        when(sentinelCp.view(cpMasked).v.ne(0xff), () => hit.iadd(1));
      });
    });

    process.stdout.write(mod.toJson());
    """
)


class ValueSpaceParityTest(unittest.TestCase):
    def test_py_ts_hir_parity_for_every_new_case(self) -> None:
        py_hir = _build_value_space_parity_module().to_json()
        res = subprocess.run(
            ["node", "--input-type=module", "-e", _TS_VALUE_SPACE_PARITY_SCRIPT],
            cwd=str(REPO_ROOT),
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertEqual(py_hir, res.stdout)


if __name__ == "__main__":
    unittest.main()
