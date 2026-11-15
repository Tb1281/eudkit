"""Ticket #27: the Python schema field index is keyed by schema identity.

`_build_schema_index` used to cache by layout *name* while the TypeScript
surface caches by schema identity (`WeakMap<MemViewSchema, …>` in
`ts/view.ts`). Two distinct `defineMemView` layouts that happen to share a
`name` — a re-declared derivative layout, or a renamed copy of a stock layout —
collided in the cache: the first index built won, and the second schema's
fields silently resolved to the *first* schema's offsets. The map still built;
it answered the wrong question about a different dword. That is a silent
wrong-answer bug, not a crash.

The fixtures below use the unique layout name ``Cycle11Twin`` (and
``Cycle11TwinBuild`` for the cross-build case) because the pre-fix cache is
global and name-keyed: a common name such as ``Unit`` would both be polluted by
and pollute every other test in the same process.

No TypeScript case is added: TS already keyed the cache by schema identity, so
these tests are the Python-side parity statement.
"""

from __future__ import annotations

import json
import unittest
from typing import Any

from py import (
    FieldSpec,
    Trigger,
    Var,
    build_module,
    defineMemView,
    memsetEPD,
)
from py.view import MemViewInstance

from py.tests.helpers import run_sim

ETW = "Cycle11Twin"


def _twin_module(mem_base: int = 0x2000) -> Any:
    """Build the measured fixture: two same-named layouts at offsets 0 and 4.

    A is touched first on purpose: it is what seeds the cache entry, so without
    that step B would be the first schema to build an index and the bug would
    be invisible (pre-fix, whichever schema builds first wins the name).
    """
    A = defineMemView(ETW, 8, {"a": FieldSpec(0, 4)})
    B = defineMemView(ETW, 8, {"a": FieldSpec(4, 4)})

    def prog() -> None:
        # Touch A's index first — load-bearing (see docstring).
        MemViewInstance(A, 0x3000).a.eq(7)

        # EPD-indexed memory, same dword base for both layouts: A.a reads
        # mem_base (0) and B.a reads mem_base + 1 (42).
        memsetEPD(mem_base, 0, 1)
        memsetEPD(mem_base + 1, 42, 1)

        hit = Var(0)
        Trigger(
            conds=[MemViewInstance(B, mem_base).a.eq(42)],
            acts=[hit.act_add(1)],
        )

    return build_module(prog)


def _condition_trigger(doc: dict[str, Any]) -> dict[str, Any]:
    triggers = [
        stmt
        for stmt in doc["module"]["stmts"]
        if stmt.get("kind") == "trigger" and stmt.get("conds")
    ]
    assert len(triggers) == 1, triggers
    return triggers[0]


def _second_build_target(name: str) -> int:
    """Two builds, two fresh same-named schemas: return the second's target.

    The first build's schemas are released before the second build runs, so a
    name-keyed cache carries the first build's index into the second. An
    identity-keyed cache must not.
    """

    def build(a_offset: int, b_offset: int) -> Any:
        A = defineMemView(name, 8, {"a": FieldSpec(a_offset, 4)})
        B = defineMemView(name, 8, {"a": FieldSpec(b_offset, 4)})

        def prog() -> None:
            # Touch A's index first — load-bearing (see module docstring).
            MemViewInstance(A, 0x3000).a.eq(7)
            hit = Var(0)
            Trigger(
                conds=[MemViewInstance(B, 0x2000).a.eq(42)],
                acts=[hit.act_add(1)],
            )

        return build_module(prog)

    # First build: both layouts agree, so its index says offset 0.
    build(0, 0)
    # Second build: B moves to offset 4. A name-keyed cache would still answer
    # with the first build's index (0x2000) — the cross-build aliasing case.
    second = json.loads(build(0, 4).to_json())
    cond = _condition_trigger(second)["conds"][0]
    return int(cond["player"])


class SchemaIndexIdentityTests(unittest.TestCase):
    def test_same_named_layouts_resolve_their_own_offsets(self) -> None:
        mod = _twin_module()
        report = run_sim(mod.to_json(), 1)
        # B.a is at EPD 0x2001 (42); A.a would be EPD 0x2000 (0), i.e. 0 hits.
        self.assertEqual(report["var_values"][0], 1)

    def test_same_named_layouts_hir_target_offsets(self) -> None:
        doc = json.loads(_twin_module().to_json())
        cond = _condition_trigger(doc)["conds"][0]
        # DeathsX on B's own dword: base 0x2000 + offset 4 bytes = 0x2001.
        self.assertEqual(cond["condtype"], 15)
        self.assertEqual(cond["player"], 0x2001)

    def test_index_is_not_shared_within_one_build(self) -> None:
        self.assertEqual(_second_build_target("Cycle11TwinBuild"), 0x2001)


if __name__ == "__main__":
    unittest.main()
