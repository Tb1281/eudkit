"""Ticket #51: expose the runtime `else_body` arm on `when`.

The HIR field, lowering and opt pass already exist; these pins cover the two
frontend surfaces and their dispatch:

* callback form `when(conds, body, else_body)`;
* context-manager form `with when(conds) as w: ...; w.otherwise(fn)` (alias
  `w.else_`);
* `when(False, A, B)` queues `when([], B)` unconditionally, while the two-arg
  `when(False, A)` still drops the block;
* `when(True, A, B)` / `when([], A, B)` emit the then-only node and never run
  `B`;
* an empty else body omits the `else_body` key, so then-only HIR stays
  byte-identical;
* the else arm is the false path of the gate trigger, so `when([a, b], A, B)`
  runs `B` iff `NOT(a AND b)`.

Cost pins (one page transfer per arm, four vars):

* then-only and the unconditional-then/unconditional-else forms: 4T/9,640 B;
* both one-transfer arms: 6T/14,460 B;
* empty then body with a non-empty else arm: 5T/12,048 B.

The same probes live in `ts/tests/test_ticket51.ts`.
"""

from __future__ import annotations

import json
import subprocess
import textwrap
import unittest
from typing import Any, Callable

from py import EudslError, Var, build_module, when
from py.tests.helpers import REPO_ROOT, run_sim, run_static

STATIC_KEYS = ("triggers", "conditions", "actions", "total_bytes")

THEN_ONLY_STATIC = (4, 1, 69, 9_640)
BOTH_ARMS_STATIC = (6, 1, 72, 14_460)
UNCONDITIONAL_STATIC = (4, 0, 69, 9_640)
EMPTY_THEN_STATIC = (5, 1, 69, 12_048)


def _static(hir_json: str) -> tuple[int, ...]:
    cost = run_static(hir_json)["static_cost"]
    return tuple(cost[key] for key in STATIC_KEYS)


def _when_nodes(module: Any) -> list[dict[str, Any]]:
    return [
        stmt
        for stmt in json.loads(module.to_json())["module"]["stmts"]
        if stmt["kind"] == "when"
    ]


def _run_case(
    make_call: Callable[[Any, Any, Any, Any], None],
    *,
    gate_init: int = 0,
) -> tuple[Any, dict[str, int], dict[str, int]]:
    """Build one four-var gate fixture and simulate a single loop pass."""
    ids: dict[str, int] = {}

    def prog() -> None:
        gate = Var(gate_init)
        src = Var(1)
        then_hit = Var(0)
        else_hit = Var(0)
        ids.update(
            gate=gate.id,
            src=src.id,
            then_hit=then_hit.id,
            else_hit=else_hit.id,
        )
        make_call(gate, src, then_hit, else_hit)

    module = build_module(prog)
    values = run_sim(module.to_json(), 1)["var_values"]
    return module, ids, {name: values[var_id] for name, var_id in ids.items()}


def _callback(gate: Any, src: Any, then_hit: Any, else_hit: Any) -> None:
    when(
        [gate.eq(1)],
        lambda: then_hit.set(src),
        lambda: else_hit.set(src),
    )


class Ticket51CallbackTest(unittest.TestCase):
    def test_callback_else_runs_when_gate_is_false(self) -> None:
        _module, _ids, values = _run_case(_callback, gate_init=0)
        self.assertEqual(values["then_hit"], 0)
        self.assertEqual(values["else_hit"], 1)

    def test_callback_else_is_skipped_when_gate_is_true(self) -> None:
        _module, _ids, values = _run_case(_callback, gate_init=1)
        self.assertEqual(values["then_hit"], 1)
        self.assertEqual(values["else_hit"], 0)

    def test_callback_else_hir_has_else_body(self) -> None:
        module, ids, _values = _run_case(_callback, gate_init=0)
        nodes = _when_nodes(module)
        self.assertEqual(len(nodes), 1)
        node = nodes[0]
        self.assertIn("else_body", node)
        self.assertEqual(
            [stmt["dst_var"] for stmt in node["body"]], [ids["then_hit"]]
        )
        self.assertEqual(
            [stmt["dst_var"] for stmt in node["else_body"]], [ids["else_hit"]]
        )
        self.assertEqual(_static(module.to_json()), BOTH_ARMS_STATIC)

    def test_empty_then_body_with_else_arm_costs_five_triggers(self) -> None:
        module, ids, values = _run_case(
            lambda gate, src, _then_hit, else_hit: when(
                [gate.eq(1)],
                lambda: None,
                lambda: else_hit.set(src),
            ),
            gate_init=0,
        )
        node = _when_nodes(module)[0]
        self.assertEqual(node["body"], [])
        self.assertIn("else_body", node)
        self.assertEqual(
            [stmt["dst_var"] for stmt in node["else_body"]], [ids["else_hit"]]
        )
        self.assertEqual(_static(module.to_json()), EMPTY_THEN_STATIC)
        self.assertEqual(values["else_hit"], 1)


class Ticket51ContextManagerTest(unittest.TestCase):
    def _build_with_otherwise(self, alias: bool) -> Any:
        def prog() -> None:
            gate = Var(0)
            src = Var(1)
            then_hit = Var(0)
            else_hit = Var(0)
            with when([gate.eq(1)]) as w:
                then_hit.set(src)
                attach = w.else_ if alias else w.otherwise
                attach(lambda: else_hit.set(src))

        return build_module(prog)

    def test_context_manager_otherwise_is_identical_to_callback(self) -> None:
        callback, _ids, _values = _run_case(_callback, gate_init=0)
        module = self._build_with_otherwise(alias=False)
        self.assertEqual(module.to_json(), callback.to_json())
        self.assertEqual(_static(module.to_json()), BOTH_ARMS_STATIC)

    def test_context_manager_else_alias_is_identical(self) -> None:
        callback, _ids, _values = _run_case(_callback, gate_init=0)
        module = self._build_with_otherwise(alias=True)
        self.assertEqual(module.to_json(), callback.to_json())
        self.assertEqual(_static(module.to_json()), BOTH_ARMS_STATIC)

    def test_duplicate_otherwise_is_a_type_error(self) -> None:
        def prog() -> None:
            gate = Var(1)
            hit = Var(0)
            with when([gate.eq(1)]) as w:
                hit.iadd(1)
                w.otherwise(lambda: None)
                w.otherwise(lambda: None)

        with self.assertRaises(EudslError) as ctx:
            build_module(prog)
        self.assertEqual(ctx.exception.code, "EUDSL-TYPE")


class Ticket51BooleanDispatchTest(unittest.TestCase):
    def test_when_false_runs_the_else_arm_unconditionally(self) -> None:
        module, ids, values = _run_case(
            lambda _gate, src, then_hit, else_hit: when(
                False,
                lambda: then_hit.set(src),
                lambda: else_hit.set(src),
            ),
            gate_init=0,
        )
        self.assertEqual(values["then_hit"], 0)
        self.assertEqual(values["else_hit"], 1)
        nodes = _when_nodes(module)
        self.assertEqual(len(nodes), 1)
        node = nodes[0]
        self.assertEqual(node["conds"], [])
        self.assertNotIn("else_body", node)
        self.assertEqual(
            [stmt["dst_var"] for stmt in node["body"]], [ids["else_hit"]]
        )
        self.assertEqual(_static(module.to_json()), UNCONDITIONAL_STATIC)

    def test_when_true_and_empty_list_ignore_the_else_arm(self) -> None:
        rows: list[tuple[str, Callable[[Any, Any, Any, Any], None]]] = [
            (
                "true",
                lambda _gate, src, then_hit, else_hit: when(
                    True,
                    lambda: then_hit.set(src),
                    lambda: else_hit.set(src),
                ),
            ),
            (
                "empty-list",
                lambda _gate, src, then_hit, else_hit: when(
                    [],
                    lambda: then_hit.set(src),
                    lambda: else_hit.set(src),
                ),
            ),
        ]
        for name, make_call in rows:
            with self.subTest(form=name):
                module, ids, values = _run_case(make_call, gate_init=0)
                self.assertEqual(values["then_hit"], 1)
                self.assertEqual(values["else_hit"], 0)
                nodes = _when_nodes(module)
                self.assertEqual(len(nodes), 1)
                node = nodes[0]
                self.assertEqual(node["conds"], [])
                self.assertNotIn("else_body", node)
                self.assertEqual(
                    [stmt["dst_var"] for stmt in node["body"]], [ids["then_hit"]]
                )
                self.assertEqual(
                    _static(module.to_json()), UNCONDITIONAL_STATIC
                )

    def test_two_arg_false_still_drops(self) -> None:
        module, _ids, values = _run_case(
            lambda _gate, src, then_hit, _else_hit: when(
                False, lambda: then_hit.set(src)
            ),
            gate_init=0,
        )
        self.assertEqual(values["then_hit"], 0)
        self.assertEqual(values["else_hit"], 0)
        self.assertEqual(_when_nodes(module), [])

    def test_false_with_empty_else_still_drops(self) -> None:
        """The empty else arm must not leave an empty ``when`` node behind."""
        module, _ids, values = _run_case(
            lambda _gate, src, then_hit, _else_hit: when(
                False,
                lambda: then_hit.set(src),
                lambda: None,
            ),
            gate_init=0,
        )
        self.assertEqual(values["then_hit"], 0)
        self.assertEqual(_when_nodes(module), [])

    def test_false_drop_outside_builder_is_a_scope_error(self) -> None:
        for form in (
            lambda: when(False, lambda: None),
            lambda: when(False, lambda: None, lambda: None),
        ):
            with self.subTest(form=form):
                with self.assertRaises(EudslError) as ctx:
                    form()
                self.assertEqual(ctx.exception.code, "EUDSL-SCOPE")


class Ticket51EmptyElseTest(unittest.TestCase):
    def test_empty_else_body_is_then_only(self) -> None:
        with_else, _ids, _values = _run_case(
            lambda gate, src, then_hit, _else_hit: when(
                [gate.eq(1)],
                lambda: then_hit.set(src),
                lambda: None,
            ),
            gate_init=0,
        )
        then_only, _ids2, _values2 = _run_case(
            lambda gate, src, then_hit, _else_hit: when(
                [gate.eq(1)], lambda: then_hit.set(src)
            ),
            gate_init=0,
        )
        self.assertEqual(with_else.to_json(), then_only.to_json())
        node = _when_nodes(with_else)[0]
        self.assertNotIn("else_body", node)
        self.assertEqual(_static(with_else.to_json()), THEN_ONLY_STATIC)

    def test_non_callable_else_is_a_type_error(self) -> None:
        with self.assertRaises(EudslError) as ctx:
            build_module(lambda: when([Var(1).eq(1)], lambda: None, 1))
        self.assertEqual(ctx.exception.code, "EUDSL-TYPE")


class Ticket51MultiCondTest(unittest.TestCase):
    def test_multi_cond_else_is_not_all(self) -> None:
        rows = [
            (0, 1, 0, 1),
            (1, 0, 0, 1),
            (0, 0, 0, 1),
            (1, 1, 1, 0),
        ]
        for a_init, b_init, want_then, want_else in rows:
            with self.subTest(a=a_init, b=b_init):
                ids: dict[str, int] = {}

                def prog() -> None:
                    a = Var(a_init)
                    b = Var(b_init)
                    src = Var(1)
                    then_hit = Var(0)
                    else_hit = Var(0)
                    ids.update(
                        then_hit=then_hit.id,
                        else_hit=else_hit.id,
                    )
                    when(
                        [a.eq(1), b.eq(1)],
                        lambda: then_hit.set(src),
                        lambda: else_hit.set(src),
                    )

                values = run_sim(build_module(prog).to_json(), 1)["var_values"]
                self.assertEqual(values[ids["then_hit"]], want_then)
                self.assertEqual(values[ids["else_hit"]], want_else)


def _build_parity_module() -> Any:
    def prog() -> None:
        gate = Var(1)
        src = Var(1)
        then_hit = Var(0)
        else_hit = Var(0)
        when(
            [gate.eq(1)],
            lambda: then_hit.set(src),
            lambda: else_hit.set(src),
        )

        gate2 = Var(0)
        src2 = Var(2)
        then2 = Var(0)
        when([gate2.eq(1)], lambda: then2.set(src2))

        gate3 = Var(0)
        src3 = Var(3)
        then3 = Var(0)
        else3 = Var(0)
        when(
            False,
            lambda: then3.set(src3),
            lambda: else3.set(src3),
        )

        gate4 = Var(1)
        src4 = Var(4)
        then4 = Var(0)
        else4 = Var(0)
        when(
            True,
            lambda: then4.set(src4),
            lambda: else4.set(src4),
        )

        gate5 = Var(1)
        src5 = Var(5)
        then5 = Var(0)
        else5 = Var(0)
        with when([gate5.eq(1)]) as w:
            then5.set(src5)
            w.otherwise(lambda: else5.set(src5))

        gate6 = Var(1)
        src6 = Var(6)
        then6 = Var(0)
        when([gate6.eq(1)], lambda: then6.set(src6), lambda: None)

        gate7 = Var(1)
        src7 = Var(7)
        then7 = Var(0)
        when([gate7.eq(1)], lambda: then7.set(src7))

        gate8 = Var(0)
        src8 = Var(8)
        then8 = Var(0)
        when(
            False,
            lambda: then8.set(src8),
            lambda: None,
        )

    return build_module(prog)


_TS_PARITY_SCRIPT = textwrap.dedent(
    """
    import { Var, buildModule, when } from "./ts/index.ts";

    const mod = buildModule(() => {
      const gate = Var(1);
      const src = Var(1);
      const thenHit = Var(0);
      const elseHit = Var(0);
      when(
        [gate.eq(1)],
        () => thenHit.set(src),
        () => elseHit.set(src),
      );

      const gate2 = Var(0);
      const src2 = Var(2);
      const then2 = Var(0);
      when([gate2.eq(1)], () => then2.set(src2));

      const gate3 = Var(0);
      const src3 = Var(3);
      const then3 = Var(0);
      const else3 = Var(0);
      when(
        false,
        () => then3.set(src3),
        () => else3.set(src3),
      );

      const gate4 = Var(1);
      const src4 = Var(4);
      const then4 = Var(0);
      const else4 = Var(0);
      when(
        true,
        () => then4.set(src4),
        () => else4.set(src4),
      );

      const gate5 = Var(1);
      const src5 = Var(5);
      const then5 = Var(0);
      const else5 = Var(0);
      when(
        [gate5.eq(1)],
        () => then5.set(src5),
        () => else5.set(src5),
      );

      const gate6 = Var(1);
      const src6 = Var(6);
      const then6 = Var(0);
      when([gate6.eq(1)], () => then6.set(src6), () => {});

      const gate7 = Var(1);
      const src7 = Var(7);
      const then7 = Var(0);
      when([gate7.eq(1)], () => then7.set(src7));

      const gate8 = Var(0);
      const src8 = Var(8);
      const then8 = Var(0);
      when(
        false,
        () => then8.set(src8),
        () => {},
      );
    });

    process.stdout.write(mod.toJson());
    """
)


class Ticket51ParityTest(unittest.TestCase):
    def test_py_ts_hir_parity_for_else(self) -> None:
        py_hir = _build_parity_module().to_json()
        res = subprocess.run(
            ["node", "--input-type=module", "-e", _TS_PARITY_SCRIPT],
            cwd=str(REPO_ROOT),
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertEqual(py_hir, res.stdout)


if __name__ == "__main__":
    unittest.main()
