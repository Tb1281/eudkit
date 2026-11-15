"""Var-scope tests: ``Var`` allocation outside ``buildModule`` must fail loud.

A ``Var`` created outside ``buildModule`` used to receive a silent fallback
id (``_fallback_var_counter``), colliding with the first in-module var and
silently corrupting output (the aligned-string-padding smoke program showed
``AB`` instead of ``ABCDEFGH``). The builder owns the id space by
construction, so outside allocation has no id space: it raises
``EUDSL-SCOPE``, exactly like ``_alloc_string`` already does.
Compatible with both `pytest` and `python3 -m unittest`.
"""

from __future__ import annotations

import unittest
from typing import Any

from py import FixedString, Var, buildModule, when
from py.var import _alloc_var_slot, _get_next_page_slot, _get_next_var_slot
from py.view import EudslError

from py.tests.helpers import run_sim
# Padded buffer dwords, little-endian: "AB\\x1a\\x1a" | "CD\\x1a\\x1a" | "EFGH" | terminator.
PADDED_BUFFER = [437928513, 437929027, 1212630597, 0]

def find_padded_buffer(epds: dict[str, int]) -> int | None:
    for key in epds:
        base = int(key)
        if all(epds.get(str(base + i)) == want for i, want in enumerate(PADDED_BUFFER)):
            return base
    return None

class TestVarScope(unittest.TestCase):
    def assertScopeError(self, fn: Any) -> None:
        with self.assertRaises(EudslError) as cm:
            fn()
        self.assertEqual(cm.exception.code, "EUDSL-SCOPE")

    def test_var_outside_build_module_raises(self) -> None:
        self.assertScopeError(lambda: Var(1))

    def test_var_slot_allocation_outside_build_module_raises(self) -> None:
        self.assertScopeError(lambda: _alloc_var_slot(0))
        self.assertScopeError(_get_next_var_slot)
        self.assertScopeError(_get_next_page_slot)

    def test_aligned_string_padding_shape_with_var_outside_fails_at_build_time(self) -> None:
        def build() -> None:
            # Raises here, before buildModule runs: no module (and no map) is produced.
            yes = Var(1)
            buildModule(lambda: None)  # unreachable; keeps the shape explicit
            _ = yes

        self.assertScopeError(build)

    def test_aligned_string_padding_shape_with_var_inside_yields_padded_buffer_at_loop_2(self) -> None:
        def body() -> None:
            yes = Var(1)
            control = FixedString(16)
            control.printAt(1, "ABCDEFGH")
            padded = FixedString(32)
            padded.write("AB")
            when(yes.eq(1), lambda: padded.append("CD"))
            padded.append("EFGH")
            padded.displayAt(2)

        mod = buildModule(body)
        # Loop 2+ steady state, not loop 1 only: the outside-var breakage
        # (terminator poisoning) is invisible at loop 1.
        report = run_sim(mod.to_json(), 2)
        self.assertIsNotNone(
            find_padded_buffer(report["external_epds"]),
            "padded buffer not found in sim external_epds at loop 2",
        )

if __name__ == "__main__":
    unittest.main()
