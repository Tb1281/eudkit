"""Ticket #28: TS field assignment writes through, matching py byte for byte.

Python's ``MemViewInstance.__setattr__`` resolves the field and emits its
``set`` act, so ``u.hp = 1500`` is a write. The TS proxy used to have only a
``get`` trap: the assignment created an own property that shadowed the handle
and the write was dropped. This parity test builds the same write-through
program on both surfaces, asserts byte-identical HIR, and checks the simulated
memory.

The layout name ``SetTrapUnit`` is unique only so the two surfaces' fixtures
stay easy to tell apart: py's schema index cache is keyed by schema identity
since #27, so a name shared with another test could not pick up another index.
"""

from __future__ import annotations

import subprocess
import textwrap
import unittest

from py import FieldSpec, Var, build_module, defineMemView, when
from py.tests.helpers import REPO_ROOT, run_sim
from py.view import make_memview

# hp: dword at byte 0 (EPD base + 0), mp: word at byte 4 (EPD base + 1).
SetTrapUnit = defineMemView(
    "SetTrapUnit",
    8,
    {
        "hp": FieldSpec(0, 4),
        "mp": FieldSpec(4, 2),
    },
)

_BASE_EPD = 3000


def _build_parity_module():
    ids: dict[str, int] = {}

    def prog() -> None:
        u = make_memview(SetTrapUnit, _BASE_EPD)
        u.hp = 1500
        u.mp = 50

        hit = Var(0)
        ids["hit"] = hit.id
        when([u.hp.eq(1500)], lambda: hit.iadd(1))
        when([u.mp.ge(10)], lambda: hit.iadd(1))

    return build_module(prog), ids


_TS_PARITY_SCRIPT = textwrap.dedent(
    """
    import {
      MemView,
      Var,
      buildModule,
      defineMemView,
      when,
    } from "./ts/index.ts";

    const SetTrapUnit = defineMemView("SetTrapUnit", 8, {
      hp: { offset: 0, size: 4 },
      mp: { offset: 4, size: 2 },
    });

    const mod = buildModule(() => {
      const u = MemView(SetTrapUnit, 3000);
      u.hp = 1500;
      u.mp = 50;
      const hit = Var(0);
      when([u.hp.eq(1500)], () => hit.iadd(1));
      when([u.mp.ge(10)], () => hit.iadd(1));
    });

    process.stdout.write(mod.toJson());
    """
)


class MemViewSetParityTest(unittest.TestCase):
    def test_hir_parity_and_write_through(self) -> None:
        py_mod, ids = _build_parity_module()
        py_hir = py_mod.to_json()
        res = subprocess.run(
            ["node", "--input-type=module", "-e", _TS_PARITY_SCRIPT],
            cwd=str(REPO_ROOT),
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertEqual(py_hir, res.stdout)

        report = run_sim(py_hir, 1)
        self.assertEqual(report["external_epds"]["3000"], 1500)
        self.assertEqual(report["external_epds"]["3001"], 50)
        self.assertEqual(report["var_values"][ids["hit"]], 2)


if __name__ == "__main__":
    unittest.main()
