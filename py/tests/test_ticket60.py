"""Ticket #60: root act/transfer interleave must not split the transfer batch.

``crates/eudkit-opt/src/lib.rs`` kept one *pending transfer run* at the module
root, but every root action statement flushed it: an ``act, act, x.set(y),
w.set(z)`` program formed one batch, while the same four items written
``act, x.set(y), act, w.set(z)`` formed two. Two batches mean two page
setups (one extra static trigger) and two page firings per pass (two extra
executed triggers) for identical work.

These pins are red before the fix and green after. Both probe programs are
the issue's own reproduction:

* grouped root: 3T/0C/71A/7,232 B static, 3T/0C/71A executed;
* interleaved root: same as grouped (was 4T/0C/72A/9,640 B static and
  5T/0C/136A executed — +1 static trigger and +2 executed triggers);
* the same run of 8 items (4 actions, 4 transfers) stays equal at both
  shapes, so interleaving adds nothing in a longer run either.

The guard is load-bearing: a root action carrying ``relocs[].target_var``
may read or write a paged variable slot, so it keeps source order and still
ends the run (pinned at the pre-fix 4T/72A/9,640 B and 5T/136A). A
conditional root trigger is not the ``can_unpack_into_basic_block`` shape,
so it also still ends the run.

Body interleave is out of scope here (it is a separate follow-up needing the
same act↔transfer dependency analysis); these probes stay at the module root.
The same probes live in ``ts/tests/test_ticket60.ts``.
"""

from __future__ import annotations

import json
import unittest

from py import SetDeaths, SetTo, Var, build_module
from py.tests.helpers import run_sim, run_static

STATIC_KEYS = ("triggers", "conditions", "actions", "total_bytes")
EXEC_KEYS = ("triggers", "conditions", "actions")


def _root_probe(n: int, *, interleaved: bool) -> tuple[str, dict[str, list[int]]]:
    """N constant actions and N independent copies at the module root.

    ``grouped`` writes all actions first, then all copies; ``interleaved``
    alternates one action and one copy. Both describe the same work.
    """
    ids: dict[str, list[int]] = {"dst": [], "src": []}

    def build() -> None:
        srcs = [Var(10 + i) for i in range(n)]
        dsts = [Var(0) for _ in range(n)]
        if interleaved:
            for i in range(n):
                SetDeaths(0, SetTo, i + 1, 0)
                dsts[i].set(srcs[i])
        else:
            for i in range(n):
                SetDeaths(0, SetTo, i + 1, 0)
            for i in range(n):
                dsts[i].set(srcs[i])
        ids["dst"] = [dst.id for dst in dsts]
        ids["src"] = [src.id for src in srcs]

    return build_module(build).to_json(), ids


def _cost(hir_json: str) -> tuple[tuple[int, ...], tuple[int, ...]]:
    static = run_static(hir_json)["static_cost"]
    executed = run_sim(hir_json, 1)["executed_cost"]
    return (
        tuple(static[key] for key in STATIC_KEYS),
        tuple(executed[key] for key in EXEC_KEYS),
    )


def _with_dependent_second_action(hir_json: str, target_var: int) -> str:
    """Make the second interleaved root action name a variable.

    The DSL has no surface that emits an act↔var dependency between two
    independent copies at the root, so this is exercised at the HIR-JSON
    seam: add the ``relocs[].target_var`` entry lowering would produce for a
    page-slot operand.
    """
    doc = json.loads(hir_json)
    for stmt in doc["module"]["stmts"]:
        if stmt["kind"] != "trigger" or not stmt.get("acts"):
            continue
        act = stmt["acts"][0]
        if act.get("value") == 2:
            act["relocs"] = [
                {
                    "field_offset": 20,
                    "target_var": target_var,
                    "target_offset": 20,
                    "mode": 1,
                }
            ]
            return json.dumps(doc)
    raise AssertionError("second root action trigger not found")


class Ticket60RootInterleavePins(unittest.TestCase):
    def test_grouped_root_probe_cost(self) -> None:
        static, executed = _cost(_root_probe(2, interleaved=False)[0])
        self.assertEqual(static, (3, 0, 71, 7_232))
        self.assertEqual(executed, (3, 0, 71))

    def test_interleaved_root_probe_matches_the_grouped_cost(self) -> None:
        grouped = _cost(_root_probe(2, interleaved=False)[0])
        interleaved = _cost(_root_probe(2, interleaved=True)[0])
        self.assertEqual(interleaved, grouped)
        self.assertEqual(interleaved, ((3, 0, 71, 7_232), (3, 0, 71)))

    def test_interleaved_root_probe_writes_every_destination(self) -> None:
        hir, ids = _root_probe(2, interleaved=True)
        values = run_sim(hir, 1)["var_values"]
        self.assertEqual([values[var_id] for var_id in ids["dst"]], [10, 11])

    def test_longer_interleaved_root_run_stays_equal_to_grouped(self) -> None:
        grouped = _cost(_root_probe(4, interleaved=False)[0])
        interleaved_hir, ids = _root_probe(4, interleaved=True)
        self.assertEqual(_cost(interleaved_hir), grouped)
        self.assertEqual(grouped, ((3, 0, 77, 7_240), (3, 0, 77)))
        values = run_sim(interleaved_hir, 1)["var_values"]
        self.assertEqual([values[var_id] for var_id in ids["dst"]], [10, 11, 12, 13])


class Ticket60RootGuardPins(unittest.TestCase):
    def test_a_dependent_root_action_still_ends_the_transfer_run(self) -> None:
        hir, ids = _root_probe(2, interleaved=True)
        dependent = _with_dependent_second_action(hir, ids["src"][0])
        # Kept in source order: two one-transfer batches with the action
        # between them, i.e. exactly the pre-fix interleaved cost.
        self.assertEqual(_cost(dependent), ((4, 0, 72, 9_640), (5, 0, 136)))
        values = run_sim(dependent, 1)["var_values"]
        self.assertEqual([values[var_id] for var_id in ids["dst"]], [10, 11])


if __name__ == "__main__":
    unittest.main()
