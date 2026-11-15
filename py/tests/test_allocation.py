"""Allocation tests for page-aligned containers (Layer 1).

Alignment must count page slots, not raw variable ids: non-page
variables consume ids but no page slots, so padding on the raw counter
would place page-aligned containers at the wrong pinned rank.

Pinned rank: the rank of a variable among page-pinned variables only,
in allocation order (the rank that decides TriggerPage placement). It is
derived from the module's var declarations, not from the raw variable id,
because non-page variables consume ids but no page slots.

Layer 2, option C: page-pinned variables take final TriggerPage ranks in
id order first, then unpinned transfer/store sources in id order, so
every page-pinned variable's final rank equals its DSL pinned rank.
The transfer-before-array case that Layer 1 left deferred (S1) is now a
passing lock test, and T1/T2/T3 cover the non-crossing, mixed-scalar,
and FixedString-coexistence cases.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from py import FixedArray, FixedString, TriggerStruct, Var, arrayDword, build_module, dword
from py.var import _get_next_page_slot, _get_next_var_slot

from py.tests.helpers import run_sim
REPO_ROOT = Path(__file__).resolve().parent.parent.parent
EUDKIT_COMPILE = REPO_ROOT / "target" / "debug" / "eudkit-compile"
def run_dump_ir(hir_json: str) -> str:
    with tempfile.TemporaryDirectory(prefix="eudkit-py-alloc-dump-") as tmpdir:
        hir_path = Path(tmpdir) / "prog.hir.json"
        hir_path.write_text(hir_json, encoding="utf-8")
        return subprocess.check_output(
            [str(EUDKIT_COMPILE), "dump-ir", str(hir_path)],
            text=True,
        )


def allocation_lines(dump_ir: str) -> list:
    lines = dump_ir.split("\n")
    assert "Allocations:" in lines, "dump-ir output has no Allocations block"
    start = lines.index("Allocations:")
    out = []
    for line in lines[start + 1 :]:
        if not line.startswith("  var["):
            break
        out.append(line)
    return out


Point = TriggerStruct(
    "Point", {"x": dword(), "y": dword(), "z": dword(), "w": dword()}
)


def pinned_ranks(mod, ids):
    page_ids = [v["id"] for v in mod.vars if v.get("storage") == "page"]
    rank = {vid: i for i, vid in enumerate(page_ids)}
    out = []
    for vid in ids:
        assert vid in rank, f"var {vid} is not page-pinned"
        out.append(rank[vid])
    return out


def pinned_count(mod):
    return sum(1 for v in mod.vars if v.get("storage") == "page")


def page_of(rank):
    return rank // 64


class AllocationTests(unittest.TestCase):
    def test_counters_diverge_by_exactly_the_non_page_variables(self):
        def build():
            Var(0)
            Var(0)
            Var(0)
            self.assertEqual(_get_next_var_slot(), 3)
            self.assertEqual(_get_next_page_slot(), 0)
            self.assertEqual(_get_next_var_slot() - _get_next_page_slot(), 3)

        build_module(build)

    def test_scalar_array_after_demoted_cells_starts_at_pinned_rank_0(self):
        captured: dict[str, list] = {}

        def build():
            Var(0)
            Var(0)
            Var(0)
            arr = FixedArray(arrayDword, 4)
            captured["elems"] = [v.id for v in arr.vars]

        mod = build_module(build)
        self.assertEqual(pinned_ranks(mod, captured["elems"]), [0, 1, 2, 3])

    def test_struct_array_after_demoted_cells_is_page_aligned_per_element(self):
        captured: dict[str, list] = {}

        def build():
            Var(0)
            Var(0)
            Var(0)
            arr = FixedArray(Point, 2)
            captured["fields"] = [[f.id for f in inst.fields] for inst in arr.instances]

        mod = build_module(build)
        # One TriggerPage per element: element 0 at slots 0..3, element 1 at 64..67.
        self.assertEqual(pinned_ranks(mod, captured["fields"][0]), [0, 1, 2, 3])
        self.assertEqual(pinned_ranks(mod, captured["fields"][1]), [64, 65, 66, 67])
        # 3 demoted + 2 elements x 4 fields + 60 pads before element 1.
        self.assertEqual(len(mod.vars), 71)
        self.assertEqual(pinned_count(mod), 68)
        for fields in captured["fields"]:
            ranks = pinned_ranks(mod, fields)
            self.assertTrue(all(page_of(r) == page_of(ranks[0]) for r in ranks))

    def test_standalone_struct_after_62_page_and_2_demoted_does_not_cross_a_page(self):
        captured: dict[str, list] = {}

        def build():
            for _ in range(62):
                Var(0, storage="page")
            Var(0)
            Var(0)
            inst = Point.instantiate()
            captured["fields"] = [f.id for f in inst.fields]

        mod = build_module(build)
        # 62 page + 2 demoted, then 2 pads to the next page boundary: fields at 64..67.
        self.assertEqual(pinned_ranks(mod, captured["fields"]), [64, 65, 66, 67])
        self.assertEqual(len(mod.vars), 70)
        self.assertEqual(pinned_count(mod), 68)

    def test_standalone_struct_after_demoted_cells_packs_without_padding(self):
        captured: dict[str, list] = {}

        def build():
            Var(0)
            Var(0)
            Var(0)
            inst = Point.instantiate()
            captured["fields"] = [f.id for f in inst.fields]

        mod = build_module(build)
        # 4 fields fit in the current page, so no padding is added.
        self.assertEqual(pinned_ranks(mod, captured["fields"]), [0, 1, 2, 3])
        self.assertEqual(len(mod.vars), 7)
        self.assertEqual(pinned_count(mod), 4)

    def test_s1_layer2_option_c_transfer_source_before_64_length_array(self):
        # Formerly the Layer 1 deferred case: a preceding unpinned
        # transfer source shifted final TriggerPage ranks, so the dynamic
        # set of element 63 landed nowhere observable and the read-back
        # was 0. With pinned-first ordering the array keeps its DSL page
        # alignment and the read-back is 999, while the transfer itself
        # still delivers 1234.
        captured: dict[str, int] = {}

        def build():
            src = Var(1234)
            arr = FixedArray(arrayDword, 64)
            dst = Var(0)
            dst.set(src)
            idx = Var(63)
            arr.set(idx, 999)
            out = Var(0)
            out.set(arr.get(63))
            captured["dst"] = dst.id
            captured["out"] = out.id

        mod = build_module(build)
        report = run_sim(mod.to_json(), 2)
        self.assertEqual(report["var_values"][captured["dst"]], 1234)
        self.assertEqual(report["var_values"][captured["out"]], 999)

    def test_t1_layer2_option_c_non_crossing_array_keeps_baseline_dump_ir(self):
        # The transfer source is allocated after the array, so it already
        # ranks after every pinned variable in id order: pinned-first
        # ordering must not change any assignment. The expected lines are
        # the baseline (pre-fix) assignments for this program, pinned line
        # by line.
        def build():
            arr = FixedArray(arrayDword, 32)
            src = Var(1234)
            dst = Var(0)
            dst.set(src)

        mod = build_module(build)
        lines = allocation_lines(run_dump_ir(mod.to_json()))
        expected = [
            f"  var[{i}] init=0x0: TriggerPage(page=0, slot={i})"
            for i in range(32)
        ]
        expected.append("  var[32] init=0x4d2: TriggerPage(page=0, slot=32)")
        expected.append("  var[33] init=0x0: DwordCell(dword=0)")
        self.assertEqual(lines, expected)

    def test_t2_layer2_option_c_mixed_scalars_between_two_arrays(self):
        # Array A (32), an unpinned transfer source, a pinned scalar that
        # is itself a transfer source, then array B (64) with DSL page
        # padding. Under the old id ordering the unpinned source pushed B
        # across a page boundary; under pinned-first ordering B starts at
        # final page 1, slot 0, and its dynamic set/read-back works, as do
        # both transfers.
        captured: dict[str, int] = {}

        def build():
            arr_a = FixedArray(arrayDword, 32)
            src = Var(1234)
            pinned_scalar = Var(777, storage="page")
            arr_b = FixedArray(arrayDword, 64)
            dst = Var(0)
            dst.set(src)
            dst2 = Var(0)
            dst2.set(pinned_scalar)
            idx = Var(63)
            arr_b.set(idx, 999)
            out = Var(0)
            out.set(arr_b.get(63))
            captured["dst"] = dst.id
            captured["dst2"] = dst2.id
            captured["out"] = out.id

        mod = build_module(build)
        report = run_sim(mod.to_json(), 2)
        self.assertEqual(report["var_values"][captured["dst"]], 1234)
        self.assertEqual(report["var_values"][captured["dst2"]], 777)
        self.assertEqual(report["var_values"][captured["out"]], 999)

    def test_t3_layer2_option_c_string_before_array_with_transfer_source(self):
        # A FixedString (STR-backed, outside the page-backing seam)
        # constructed before the array, plus an unpinned transfer source
        # before the array, must not shift the array: the array still
        # reads back 999 and the transfer still delivers 1234.
        # FixedString correctness itself remains covered by the ticket 9
        # lock, which is not edited here.
        captured: dict[str, int] = {}

        def build():
            s = FixedString(16)
            s.write("hi")
            src = Var(1234)
            arr = FixedArray(arrayDword, 64)
            dst = Var(0)
            dst.set(src)
            idx = Var(63)
            arr.set(idx, 999)
            out = Var(0)
            out.set(arr.get(63))
            captured["dst"] = dst.id
            captured["out"] = out.id

        mod = build_module(build)
        report = run_sim(mod.to_json(), 2)
        self.assertEqual(report["var_values"][captured["dst"]], 1234)
        self.assertEqual(report["var_values"][captured["out"]], 999)


if __name__ == "__main__":
    unittest.main()
