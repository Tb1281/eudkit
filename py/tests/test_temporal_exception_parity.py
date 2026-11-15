"""Ticket #26: a caught body exception must not empty the map.

Every py temporal helper used to `return` from `__exit__` without touching the
builder when the body raised. `__enter__` had already pushed a scope, so the
orphan scope swallowed the partially built block *and* every statement queued
after it (``build_module`` reads only ``scope_stack[0]``), and a module whose
body raised -- with the caller catching it -- compiled to an empty map with no
error at all. The TS helpers never had this window: they flush/pop/queue in a
``finally``.

These tests pin py to the TS behaviour by asserting **byte-identical HIR**
against the equivalent TS programs, per construct and combined. Every row
builds the same module on both surfaces: the construct body sets ``v`` then
raises, the trailing top-level ``v.set(2)`` runs after the caught exception.

Fail-before/pass-after (measured on ``f474dcb``): the parity rows fail because
py emits different HIR (0 statements for the per-construct/combined/two-
construct fixtures; a missing ``while`` node for the deferred-gate row),
while ``test_body_exception_still_propagates`` passes today by design -- it is
a regression lock on the fix queueing/flushing during unwinding *and* still
letting the exception escape.
"""

from __future__ import annotations

import json
import subprocess
import textwrap
import unittest

from py import (
    Var,
    after,
    build_module,
    every,
    on_falling,
    on_rising,
    once,
    until,
    when,
    while_loop,
)
from py.tests.helpers import REPO_ROOT

# One construct per row: name -> py context-manager factory and the TS
# statement that opens the equivalent block. The py factories are called
# inside `with ...:` with the shared `v` in scope.
_PY_CONSTRUCTS = {
    "when": lambda v: when(True),
    "once": lambda v: once(True),
    "until": lambda v: until(True),
    "on_rising": lambda v: on_rising(v.ge(0)),
    "on_falling": lambda v: on_falling(v.ge(0)),
    "every": lambda v: every(2),
    "after": lambda v: after(2),
    "while_loop": lambda v: while_loop([v.le(3)]),
}

# The TS side of the same table, as the call prefix that opens each block.
_TS_CONSTRUCTS = {
    "when": "when(true",
    "once": "once(true",
    "until": "until(true",
    "on_rising": "onRising(v.ge(0)",
    "on_falling": "onFalling(v.ge(0)",
    "every": "every(2",
    "after": "after(2",
    "while_loop": "whileLoop([v.le(3)]",
}

_TS_HEADER = textwrap.dedent(
    """
    import {
      Var,
      after,
      buildModule,
      every,
      onFalling,
      onRising,
      once,
      until,
      when,
      whileLoop,
    } from "./ts/index.ts";

    function moduleWith(open) {
      const mod = buildModule(() => {
        const v = Var(0);
        try {
          open(v, () => {
            v.set(1);
            throw new Error("boom");
          });
        } catch (e) {
          if (!(e instanceof Error) || e.message !== "boom") throw e;
        }
        v.set(2);
      });
      return JSON.parse(mod.toJson());
    }
    """
)


def _run_ts(script: str) -> str:
    res = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        cwd=str(REPO_ROOT),
        check=True,
        capture_output=True,
        text=True,
    )
    return res.stdout


def _py_module(open_block) -> str:
    """Build the shared one-construct py module with `open_block(v)`."""

    def prog() -> None:
        v = Var(0)
        try:
            with open_block(v):
                v.set(1)
                raise ValueError("boom")
        except ValueError:
            pass
        v.set(2)

    return build_module(prog).to_json()


def _kinds(hir_json: str) -> list[str]:
    return [stmt["kind"] for stmt in json.loads(hir_json)["module"]["stmts"]]


_TS_PER_CONSTRUCT = _TS_HEADER + textwrap.dedent(
    """
    const out = {};
    out.when = moduleWith((v, body) => when(true, body));
    out.once = moduleWith((v, body) => once(true, body));
    out.until = moduleWith((v, body) => until(true, body));
    out.on_rising = moduleWith((v, body) => onRising(v.ge(0), body));
    out.on_falling = moduleWith((v, body) => onFalling(v.ge(0), body));
    out.every = moduleWith((v, body) => every(2, body));
    out.after = moduleWith((v, body) => after(2, body));
    out.while_loop = moduleWith((v, body) => whileLoop([v.le(3)], body));
    process.stdout.write(JSON.stringify(out));
    """
)

_TS_TWO_CONSTRUCT = _TS_HEADER + textwrap.dedent(
    """
    const mod = buildModule(() => {
      const v = Var(0);
      try {
        when(true, () => {
          v.set(1);
          throw new Error("boom");
        });
      } catch (e) {
        if (!(e instanceof Error) || e.message !== "boom") throw e;
      }
      v.set(2);
      try {
        whileLoop([v.le(3)], () => {
          v.set(3);
          throw new Error("boom");
        });
      } catch (e) {
        if (!(e instanceof Error) || e.message !== "boom") throw e;
      }
      v.set(4);
    });
    process.stdout.write(mod.toJson());
    """
)

_TS_WHILE_DEFERRED = _TS_HEADER + textwrap.dedent(
    """
    const mod = buildModule(() => {
      const v = Var(0);
      try {
        whileLoop([v.ne(5)], () => {
          v.set(1);
          throw new Error("boom");
        });
      } catch (e) {
        if (!(e instanceof Error) || e.message !== "boom") throw e;
      }
      v.set(2);
    });
    process.stdout.write(mod.toJson());
    """
)

_TS_ALL_EIGHT = _TS_HEADER + textwrap.dedent(
    """
    const mod = buildModule(() => {
      const v = Var(0);
      const runs = [
        () => when(true, () => { v.set(1); throw new Error("boom"); }),
        () => once(true, () => { v.set(1); throw new Error("boom"); }),
        () => until(true, () => { v.set(1); throw new Error("boom"); }),
        () => onRising(v.ge(0), () => { v.set(1); throw new Error("boom"); }),
        () => onFalling(v.ge(0), () => { v.set(1); throw new Error("boom"); }),
        () => every(2, () => { v.set(1); throw new Error("boom"); }),
        () => after(2, () => { v.set(1); throw new Error("boom"); }),
        () => whileLoop([v.le(3)], () => { v.set(1); throw new Error("boom"); }),
      ];
      for (const run of runs) {
        try {
          run();
        } catch (e) {
          if (!(e instanceof Error) || e.message !== "boom") throw e;
        }
      }
      v.set(2);
    });
    process.stdout.write(mod.toJson());
    """
)


class TemporalExceptionParityTest(unittest.TestCase):
    def test_exception_path_parity_per_construct(self) -> None:
        """All eight constructs: the node is queued, the scope is popped."""
        ts_hir = json.loads(_run_ts(_TS_PER_CONSTRUCT))
        self.assertEqual(set(ts_hir), set(_PY_CONSTRUCTS))
        for name, factory in _PY_CONSTRUCTS.items():
            with self.subTest(construct=name):
                py_hir = _py_module(factory)
                self.assertEqual(py_hir, json.dumps(ts_hir[name], separators=(",", ":")))
                self.assertEqual(_kinds(py_hir), _kinds(json.dumps(ts_hir[name])))

    def test_two_construct_fixture_parity(self) -> None:
        """The headline measurement: `when` then `while_loop`, both bodies raise.

        Pre-fix py emitted 0 statements here (both orphan scopes swallowed the
        map); TS emits 4 (`['when', 'trigger', 'while', 'trigger']`) because
        the intervening `v.set(2)` is flushed as a `trigger` by the next
        `push_scope`.
        """

        def prog() -> None:
            v = Var(0)
            try:
                with when(True):
                    v.set(1)
                    raise ValueError("boom")
            except ValueError:
                pass
            v.set(2)
            try:
                with while_loop([v.le(3)]):
                    v.set(3)
                    raise ValueError("boom")
            except ValueError:
                pass
            v.set(4)

        py_hir = build_module(prog).to_json()
        self.assertEqual(_kinds(py_hir), ["when", "trigger", "while", "trigger"])
        self.assertEqual(py_hir, _run_ts(_TS_TWO_CONSTRUCT))

    def test_while_loop_deferred_gate_tail_survives_exception(self) -> None:
        """`while_loop([v.ne(5)])`: keep the `while` node and its tail acts.

        `v.ne(5)` is a deferred (4T-flag) gate, so the tail re-materialisation
        actually emits a `vartransfer` on `__exit__`. That makes this row fail
        on `f474dcb` in a way the all-eight table can mask: pre-fix py emits
        only `['trigger', 'vartransfer']` (the `while` node is lost), not an
        empty map -- so assert the kind list, not just a count.
        """

        def prog() -> None:
            v = Var(0)
            try:
                with while_loop([v.ne(5)]):
                    v.set(1)
                    raise ValueError("boom")
            except ValueError:
                pass
            v.set(2)

        py_hir = build_module(prog).to_json()
        self.assertEqual(_kinds(py_hir), ["trigger", "vartransfer", "while", "trigger"])
        self.assertEqual(py_hir, _run_ts(_TS_WHILE_DEFERRED))

    def test_combined_module_parity(self) -> None:
        """Cross-construct guard: one un-fixed `__exit__` swallows everything."""

        def prog() -> None:
            v = Var(0)
            for factory in _PY_CONSTRUCTS.values():
                try:
                    with factory(v):
                        v.set(1)
                        raise ValueError("boom")
                except ValueError:
                    pass
            v.set(2)

        py_hir = build_module(prog).to_json()
        self.assertEqual(
            _kinds(py_hir),
            [
                "when",
                "once",
                "until",
                "edge",
                "edge",
                "every",
                "after",
                "while",
                "trigger",
            ],
        )
        self.assertEqual(py_hir, _run_ts(_TS_ALL_EIGHT))

    def test_body_exception_still_propagates(self) -> None:
        """Regression lock: flushing on the way out must not swallow the error.

        This row passes both before and after the fix on purpose -- it pins
        that `__exit__` keeps returning `None`, so the user's `except` (or an
        uncaught raise) still sees the original exception.
        """

        def prog() -> None:
            v = Var(0)
            with when(True):
                v.set(1)
                raise ValueError("boom")

        with self.assertRaises(ValueError) as ctx:
            build_module(prog)
        self.assertEqual(str(ctx.exception), "boom")


if __name__ == "__main__":
    unittest.main()
