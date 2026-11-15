"""Ticket #2 tests for the Python DSL (`py/`):
Self-targeted TriggerPage scalar variables, VProc batching, 1A constant ops,
and Python operator overloads.
Compatible with both `pytest` and `python3 -m unittest`.
"""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from py import (
    ACT_COUNT,
    DisplayText,
    Trigger,
    Var,
    buildModule,
)

from py.tests.helpers import run_sim
REPO_ROOT = Path(__file__).resolve().parent.parent.parent
EUDKIT_COMPILE = REPO_ROOT / "target" / "debug" / "eudkit-compile"
BASEMAP_SCX = REPO_ROOT / "tests" / "outputmap" / "basemap" / "basemap.scx"
class TestTicket2PythonDsl(unittest.TestCase):
    def test_64_scalar_vars_pack_into_single_self_targeted_trigger_page(self) -> None:
        def prog() -> None:
            for i in range(ACT_COUNT):
                v = Var(100 + i, storage="page")
                self.assertEqual(v.id, i)
                self.assertEqual(v.pageIndex, 0)
                self.assertEqual(v.slotIndex, i)
                self.assertEqual(v.value.epdOffset, 5)
                self.assertEqual(v.value.initialValue, 100 + i)

        mod = buildModule(prog)
        report = run_sim(mod.to_json())
        self.assertEqual(report["static_cost"]["triggers"], 1)
        self.assertEqual(report["static_cost"]["trigger_bytes"], 2408)
        self.assertTrue(report["self_targeted_invariant_holds"])
        self.assertEqual(report["var_values"], [100 + i for i in range(64)])

    def test_1a_constant_mutations_and_1c_conditions_with_operator_overloads(self) -> None:
        def prog() -> None:
            x = Var(10, storage="page")
            y = Var(20, storage="page")
            z = Var(0, storage="page")
            ok = Var(0, storage="page")

            x.set(100)
            x += 25
            y -= 5
            z.set(42)

            Trigger(
                conds=[
                    x == 125,
                    x >= 125,
                    x <= 125,
                    x > 124,
                    x < 126,
                    y == 15,
                    z != 0,
                ],
                acts=[ok.act_set(1)],
            )

        mod = buildModule(prog)
        report = run_sim(mod.to_json())
        self.assertEqual(report["static_cost"]["triggers"], 3)
        self.assertEqual(report["executed_cost"]["triggers"], 2)
        self.assertEqual(report["executed_cost"]["visited_items"], [0, 1])
        self.assertEqual(report["var_values"], [125, 15, 42, 1])
        self.assertTrue(report["self_targeted_invariant_holds"])

    def test_coalesced_multi_variable_vproc_batch_fires_trigger_page_once(self) -> None:
        def prog() -> None:
            x = Var(10)
            y = Var(20)
            z = Var(30)
            a = Var(0)
            b = Var(0)
            c = Var(5)

            a.set(x)
            b.set(y)
            c += z

        mod = buildModule(prog)
        report = run_sim(mod.to_json())
        self.assertEqual(report["static_cost"]["triggers"], 3)
        self.assertEqual(report["executed_cost"]["triggers"], 3)
        self.assertEqual(report["executed_cost"]["visited_items"], [0, 2, 1])
        self.assertEqual(report["var_values"], [10, 20, 30, 10, 20, 35])
        self.assertTrue(report["self_targeted_invariant_holds"])

    def test_python_operator_overloads_and_var_to_var_comparisons(self) -> None:
        def prog() -> None:
            x = Var(12)
            y = Var(5)
            v_sum = Var(0)
            v_diff = Var(0)
            v_prod = Var(0)
            v_quot = Var(0)
            v_rem = Var(0)
            v_and = Var(0)
            v_or = Var(0)
            v_xor = Var(0)
            v_shl = Var(0)
            v_shr = Var(0)
            v_not = Var(0)
            v_neg = Var(0)
            cmp_ok = Var(0)

            v_sum.set(x + y)          # 17
            v_diff.set(x - y)         # 7
            v_prod.set(x * 3)         # 36
            v_quot.set(x // 5)        # 2
            v_rem.set(x % 5)          # 2
            v_and.set(x & 0b1010)     # 8
            v_or.set(x | 0b0011)      # 15
            v_xor.set(x ^ 0b0110)     # 10
            v_shl.set(y << 2)         # 20
            v_shr.set(x >> 2)         # 3
            v_not.set(~y)             # 0xFFFFFFFF - 5 = 4294967290
            v_neg.set(-y)             # 0xFFFFFFFF - 5 + 1 = 4294967291

            Trigger(
                conds=[
                    v_sum == 17,
                    v_diff == 7,
                    x > y,
                    y < x,
                    x >= y,
                    y <= x,
                    x != y,
                ],
                acts=[cmp_ok.act_set(999)],
            )

        mod = buildModule(prog)
        report = run_sim(mod.to_json())
        self.assertEqual(
            report["var_values"][:15],
            [12, 5, 17, 7, 36, 2, 2, 8, 15, 10, 20, 3, 4294967290, 4294967291, 999],
        )
        self.assertTrue(report["self_targeted_invariant_holds"])

    def test_end_to_end_scx_compilation_with_vars(self) -> None:
        def prog() -> None:
            x = Var(10)
            y = Var(20)
            z = Var(0)
            x += 5
            z.set(x + y)
            Trigger(
                conds=[z == 35],
                acts=[DisplayText("Python Ticket 2 SCX")],
            )

        mod = buildModule(prog)
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            hir_path = tmp_path / "prog.hir.json"
            out_scx = tmp_path / "out.scx"
            hir_path.write_text(mod.to_json(), encoding="utf-8")

            subprocess.run(
                [str(EUDKIT_COMPILE), "map", str(hir_path), str(BASEMAP_SCX), str(out_scx)],
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertTrue(out_scx.is_file())
            self.assertGreater(out_scx.stat().st_size, 0)

if __name__ == "__main__":
    unittest.main()
