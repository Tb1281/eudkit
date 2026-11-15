"""Ticket #51 / cycle 5 #07: `dispatch(value, cases, fallback)`.

`dispatch` is a frontend-only multi-way integer branch over the existing HIR:

* a compile-time plain-integer `value` folds to the selected arm, the
  fallback, or nothing (no `when` node at all);
* a runtime value with `1 <= N <= 9` cases lowers to a nested
  `when(..., else_body)` chain in caller order;
* a runtime value with `N >= 10` cases lowers to a balanced `HirWhen` search
  tree (`ge(mid)` internal nodes, `eq(key)` leaves);
* keys are compile-time plain integers, normalised to 32 bits and unique;
  a miss without a fallback is a documented no-op;
* the fallback is named `fallback` (not TypeScript's reserved `default`).

The cost pins below use one fixture: a page-backed `Var` (the `1T / 64A /
2,408 B` seed) and arms `SetDeaths(9000 + k, SetTo, k, 0)`. The same probes
live in `ts/tests/test_dispatch.ts`, and the parity test compares byte-for-byte
HIR JSON between the two frontends.

Red before the construct exists: `ImportError: cannot import name 'dispatch'`.
"""

from __future__ import annotations

import json
import subprocess
import textwrap
import unittest
from typing import Any, Callable

from py import (
    AtLeast,
    EudslError,
    Exactly,
    SetDeaths,
    SetTo,
    Var,
    build_module,
    dispatch,
)
from py.tests.helpers import REPO_ROOT, flatten_stmts, run_sim, run_static

STATIC_KEYS = ("triggers", "conditions", "actions", "total_bytes")
EXEC_KEYS = ("triggers", "conditions", "actions")
FALLBACK_DEST = 9999


def _static(hir_json: str) -> tuple[int, ...]:
    cost = run_static(hir_json)["static_cost"]
    return tuple(cost[key] for key in STATIC_KEYS)


def _exec(report: dict[str, Any]) -> tuple[int, ...]:
    cost = report["executed_cost"]
    return tuple(cost[key] for key in EXEC_KEYS)


def _report(module: Any) -> dict[str, Any]:
    return run_sim(module.to_json(), 1)


def _epd_keys(report: dict[str, Any]) -> set[int]:
    return {int(epd) for epd in report.get("external_epds", {})}


def _write_arm(key: int) -> Callable[[], None]:
    return lambda: SetDeaths(9000 + key, SetTo, key, 0)


def _write_fallback() -> None:
    SetDeaths(FALLBACK_DEST, SetTo, 1, 0)


def _runtime_module(
    n: int,
    *,
    init: int = 0,
    keys: list[int] | None = None,
    fallback: bool = False,
    dest_base: int = 9000,
) -> Any:
    case_keys = list(range(n)) if keys is None else list(keys)

    def prog() -> None:
        value = Var(init, storage="page")
        cases = [
            (key, (lambda key=key: SetDeaths(dest_base + key, SetTo, key, 0)))
            for key in case_keys
        ]
        dispatch(value, cases, _write_fallback if fallback else None)

    return build_module(prog)


def _when_nodes(module: Any) -> list[dict[str, Any]]:
    stmts = json.loads(module.to_json())["module"]["stmts"]
    return [stmt for stmt in stmts if stmt.get("kind") == "when"]


def _all_when_nodes(module: Any) -> list[dict[str, Any]]:
    stmts = json.loads(module.to_json())["module"]["stmts"]
    return [stmt for stmt in flatten_stmts(stmts) if stmt.get("kind") == "when"]


def _cond_signature(node: dict[str, Any]) -> tuple[int, int]:
    cond = node["conds"][0]
    return (cond["amount"], cond["comparison"])


def _targets(n: int) -> dict[str, int]:
    return {
        "hit0": 0,
        "hit1": 1,
        "mid": n // 2,
        "last": n - 1,
        "miss": n + 5,
    }


CHAIN_PINS: dict[int, tuple[tuple[int, ...], dict[str, tuple[int, ...]]]] = {
    4: (
        (8, 4, 74, 19_264),
        {
            "hit0": (2, 1, 3),
            "hit1": (3, 2, 3),
            "mid": (4, 3, 3),
            "last": (4, 4, 1),
            "miss": (4, 4, 0),
        },
    ),
    8: (
        (16, 8, 86, 38_528),
        {
            "hit0": (2, 1, 3),
            "hit1": (3, 2, 3),
            "mid": (6, 5, 3),
            "last": (8, 8, 1),
            "miss": (8, 8, 0),
        },
    ),
    9: (
        (18, 9, 89, 43_344),
        {
            "hit0": (2, 1, 3),
            "hit1": (3, 2, 3),
            "mid": (6, 5, 3),
            "last": (9, 9, 1),
            "miss": (9, 9, 0),
        },
    ),
}

BST_PINS: dict[int, tuple[tuple[int, ...], dict[str, tuple[int, ...]]]] = {
    10: (
        (29, 19, 92, 69_832),
        {
            "hit0": (4, 4, 1),
            "hit1": (5, 4, 3),
            "mid": (5, 4, 3),
            "last": (9, 5, 9),
            "miss": (9, 5, 8),
        },
    ),
    16: (
        (47, 31, 110, 113_176),
        {
            "hit0": (5, 5, 1),
            "hit1": (6, 5, 3),
            "mid": (6, 5, 3),
            "last": (9, 5, 9),
            "miss": (9, 5, 8),
        },
    ),
    32: (
        (95, 63, 158, 228_760),
        {
            "hit0": (6, 6, 1),
            "hit1": (7, 6, 3),
            "mid": (7, 6, 3),
            "last": (11, 6, 11),
            "miss": (11, 6, 10),
        },
    ),
}

BST_FALLBACK_PINS: dict[int, tuple[tuple[int, ...], dict[str, tuple[int, ...]]]] = {
    10: (
        (49, 19, 122, 117_992),
        {
            "hit0": (5, 4, 3),
            "hit1": (6, 4, 5),
            "mid": (6, 4, 5),
            "last": (10, 5, 11),
            "miss": (10, 5, 9),
        },
    ),
}

CONTROL_HIR = (
    '{"version":"3.2","module":{"vars":[{"id":0,"init":0,"storage":"page"},'
    '{"id":1,"init":0}],"stmts":[{"kind":"trigger","id":0,"conds":[],"acts":['
    '{"mask":4294967295,"strid":0,"wavid":0,"time":0,"dest":0,"value":1,'
    '"unit":0,"acttype":45,"modifier":8,"flags":20,"padding":0,"eudx":17235,'
    '"relocs":[{"field_offset":16,"target_var":1,"target_offset":20,'
    '"mode":1}]}],"preserved":true,"flags":4}]}}'
)


class DispatchExportTest(unittest.TestCase):
    def test_dispatch_is_exported(self) -> None:
        self.assertTrue(callable(dispatch))
        self.assertEqual(dispatch.__name__, "dispatch")


class DispatchConstantFoldTest(unittest.TestCase):
    def test_constant_value_folds_to_matching_arm(self) -> None:
        def prog() -> None:
            dispatch(
                3,
                [
                    (1, lambda: SetDeaths(9001, SetTo, 1, 0)),
                    (3, lambda: SetDeaths(9003, SetTo, 3, 0)),
                ],
            )

        module = build_module(prog)
        stmts = json.loads(module.to_json())["module"]["stmts"]
        self.assertEqual([stmt["kind"] for stmt in stmts], ["act"])
        self.assertEqual(stmts[0]["dest"], 9003)
        self.assertEqual(stmts[0]["value"], 3)
        self.assertEqual(_when_nodes(module), [])
        self.assertEqual(_static(module.to_json()), (1, 0, 1, 2_408))
        report = _report(module)
        self.assertEqual(_exec(report), (1, 0, 1))
        self.assertEqual(_epd_keys(report), {9003})

    def test_constant_value_folds_to_fallback(self) -> None:
        def prog() -> None:
            dispatch(
                7,
                [(1, lambda: SetDeaths(9001, SetTo, 1, 0))],
                fallback=_write_fallback,
            )

        module = build_module(prog)
        stmts = json.loads(module.to_json())["module"]["stmts"]
        self.assertEqual([stmt["kind"] for stmt in stmts], ["act"])
        self.assertEqual(stmts[0]["dest"], FALLBACK_DEST)
        self.assertEqual(_when_nodes(module), [])
        self.assertEqual(_static(module.to_json()), (1, 0, 1, 2_408))
        self.assertEqual(_exec(_report(module)), (1, 0, 1))

    def test_constant_value_miss_without_fallback_is_noop(self) -> None:
        def prog() -> None:
            dispatch(7, [(1, lambda: SetDeaths(9001, SetTo, 1, 0))])

        module = build_module(prog)
        self.assertEqual(json.loads(module.to_json())["module"]["stmts"], [])
        self.assertEqual(_when_nodes(module), [])
        self.assertEqual(_static(module.to_json()), (0, 0, 0, 0))
        self.assertEqual(_exec(_report(module)), (0, 0, 0))

    def test_constant_fold_does_not_run_unselected_bodies(self) -> None:
        calls: list[str] = []

        def selected() -> None:
            calls.append("selected")
            SetDeaths(9003, SetTo, 3, 0)

        def unselected() -> None:
            calls.append("unselected")
            SetDeaths(9001, SetTo, 1, 0)

        def prog() -> None:
            dispatch(3, [(1, unselected), (3, selected)])

        build_module(prog)
        self.assertEqual(calls, ["selected"])


class DispatchChainTest(unittest.TestCase):
    def test_n1_runtime_shape_and_cost(self) -> None:
        module = _runtime_module(1, init=0)
        node = _when_nodes(module)[0]
        self.assertEqual(len(_when_nodes(module)), 1)
        self.assertEqual(_cond_signature(node), (0, Exactly))
        self.assertNotIn("else_body", node)
        self.assertEqual(_static(module.to_json()), (2, 1, 65, 4_816))
        self.assertEqual(_exec(_report(module)), (1, 1, 1))
        self.assertEqual(
            _exec(_report(_runtime_module(1, init=2))), (1, 1, 0)
        )

    def test_n1_runtime_fallback(self) -> None:
        module = _runtime_module(1, init=0, fallback=True)
        node = _when_nodes(module)[0]
        self.assertIn("else_body", node)
        self.assertEqual([stmt["kind"] for stmt in node["else_body"]], ["act"])
        self.assertEqual(_static(module.to_json()), (4, 1, 68, 9_632))
        self.assertEqual(_exec(_report(module)), (2, 1, 3))
        self.assertEqual(
            _exec(_report(_runtime_module(1, init=2, fallback=True))),
            (2, 1, 1),
        )

    def test_n4_else_chain_shape_and_cost(self) -> None:
        static, exec_pins = CHAIN_PINS[4]
        module = _runtime_module(4, init=0)
        self.assertEqual(_static(module.to_json()), static)
        self.assertEqual(len(_when_nodes(module)), 1)
        root = _when_nodes(module)[0]
        chain: list[tuple[int, int]] = []
        node: dict[str, Any] | None = root
        last_node: dict[str, Any] = root
        while node is not None:
            last_node = node
            chain.append(_cond_signature(node))
            else_body = node.get("else_body")
            node = (
                else_body[0]
                if else_body and else_body[0].get("kind") == "when"
                else None
            )
        self.assertEqual(chain, [(0, Exactly), (1, Exactly), (2, Exactly), (3, Exactly)])
        self.assertNotIn("else_body", last_node)
        for name, target in _targets(4).items():
            with self.subTest(case=name):
                report = _report(_runtime_module(4, init=target))
                self.assertEqual(_exec(report), exec_pins[name])

    def test_n8_else_chain_shape_and_cost(self) -> None:
        static, exec_pins = CHAIN_PINS[8]
        module = _runtime_module(8, init=0)
        self.assertEqual(_static(module.to_json()), static)
        for name, target in _targets(8).items():
            with self.subTest(case=name):
                report = _report(_runtime_module(8, init=target))
                self.assertEqual(_exec(report), exec_pins[name])

    def test_n9_else_chain_shape_and_cost(self) -> None:
        static, exec_pins = CHAIN_PINS[9]
        module = _runtime_module(9, init=0)
        self.assertEqual(_static(module.to_json()), static)
        nodes = _when_nodes(module)
        self.assertEqual(len(nodes), 1)
        node: dict[str, Any] | None = nodes[0]
        last_node: dict[str, Any] = nodes[0]
        chain: list[tuple[int, int]] = []
        while node is not None:
            last_node = node
            chain.append(_cond_signature(node))
            else_body = node.get("else_body")
            node = (
                else_body[0]
                if else_body and else_body[0].get("kind") == "when"
                else None
            )
        self.assertEqual(chain, [(key, Exactly) for key in range(9)])
        self.assertNotIn("else_body", last_node)
        for name, target in _targets(9).items():
            with self.subTest(case=name):
                report = _report(_runtime_module(9, init=target))
                self.assertEqual(_exec(report), exec_pins[name])


class DispatchBstTest(unittest.TestCase):
    def test_n10_bst_shape_and_cost(self) -> None:
        static, exec_pins = BST_PINS[10]
        module = _runtime_module(10, init=0)
        self.assertEqual(_static(module.to_json()), static)
        root = _when_nodes(module)[0]
        self.assertEqual(_cond_signature(root), (5, AtLeast))
        nodes = _all_when_nodes(module)
        self.assertEqual(len(nodes), 19)
        self.assertEqual(
            sum(1 for node in nodes if _cond_signature(node)[1] == AtLeast), 9
        )
        self.assertEqual(
            sum(1 for node in nodes if _cond_signature(node)[1] == Exactly), 10
        )
        for name, target in _targets(10).items():
            with self.subTest(case=name):
                report = _report(_runtime_module(10, init=target))
                self.assertEqual(_exec(report), exec_pins[name])

    def test_n16_bst_shape_and_cost(self) -> None:
        static, exec_pins = BST_PINS[16]
        module = _runtime_module(16, init=0)
        self.assertEqual(_static(module.to_json()), static)
        for name, target in _targets(16).items():
            with self.subTest(case=name):
                report = _report(_runtime_module(16, init=target))
                self.assertEqual(_exec(report), exec_pins[name])

    def test_n32_bst_shape_and_cost(self) -> None:
        static, exec_pins = BST_PINS[32]
        module = _runtime_module(32, init=0)
        self.assertEqual(_static(module.to_json()), static)
        for name, target in _targets(32).items():
            with self.subTest(case=name):
                report = _report(_runtime_module(32, init=target))
                self.assertEqual(_exec(report), exec_pins[name])

    def test_fallback_runs_only_on_miss(self) -> None:
        for n in (4, 9, 10):
            with self.subTest(n=n):
                hit = _report(_runtime_module(n, init=1, fallback=True))
                self.assertEqual(_epd_keys(hit), {9001})
                miss = _report(_runtime_module(n, init=n + 5, fallback=True))
                self.assertEqual(_epd_keys(miss), {FALLBACK_DEST})
        static, exec_pins = BST_FALLBACK_PINS[10]
        module = _runtime_module(10, init=0, fallback=True)
        self.assertEqual(_static(module.to_json()), static)
        for name, target in _targets(10).items():
            with self.subTest(case=name):
                report = _report(_runtime_module(10, init=target, fallback=True))
                self.assertEqual(_exec(report), exec_pins[name])

    def test_no_fallback_miss_is_noop(self) -> None:
        report = _report(_runtime_module(10, init=15))
        self.assertEqual(_epd_keys(report), set())
        self.assertEqual(_exec(report), (9, 5, 8))


class DispatchExclusivityTest(unittest.TestCase):
    def test_arms_are_exclusive_no_cascade(self) -> None:
        def prog() -> None:
            value = Var(0, storage="page")

            def arm0() -> None:
                value.set(1)
                SetDeaths(9000, SetTo, 1, 0)

            dispatch(value, [(0, arm0), (1, lambda: SetDeaths(9001, SetTo, 1, 0))])

        report = _report(build_module(prog))
        self.assertEqual(_epd_keys(report), {9000})


class DispatchValidationTest(unittest.TestCase):
    def _assert_error(
        self,
        code: str,
        call: Callable[[], None],
        *,
        contains: str | None = None,
    ) -> None:
        with self.assertRaises(EudslError) as ctx:
            build_module(call)
        self.assertEqual(ctx.exception.code, code)
        if contains is not None:
            self.assertIn(contains, str(ctx.exception))

    def test_duplicate_key_is_bounds_error(self) -> None:
        self._assert_error(
            "EUDSL-BOUNDS",
            lambda: dispatch(0, [(1, lambda: None), (1, lambda: None)]),
            contains="duplicate key",
        )
        self._assert_error(
            "EUDSL-BOUNDS",
            lambda: dispatch(0, [(-1, lambda: None), (0xFFFFFFFF, lambda: None)]),
            contains="duplicate key",
        )

    def test_empty_cases_is_bounds_error(self) -> None:
        self._assert_error("EUDSL-BOUNDS", lambda: dispatch(0, []))
        self._assert_error(
            "EUDSL-BOUNDS", lambda: dispatch(0, [], fallback=lambda: None)
        )

    def test_non_callable_arm_is_type_error(self) -> None:
        self._assert_error("EUDSL-TYPE", lambda: dispatch(0, [(0, 1)]))

    def test_non_callable_fallback_is_type_error(self) -> None:
        self._assert_error(
            "EUDSL-TYPE",
            lambda: dispatch(0, [(0, lambda: None)], fallback=1),
        )

    def test_runtime_key_is_type_error(self) -> None:
        def var_key() -> None:
            key = Var(0)
            dispatch(0, [(key, lambda: None)])

        def expr_key() -> None:
            base = Var(0)
            dispatch(0, [(base.add(1), lambda: None)])

        for call in (var_key, expr_key):
            with self.subTest(call=call.__name__):
                self._assert_error(
                    "EUDSL-TYPE",
                    call,
                    contains="runtime keys are not supported",
                )

    def test_bool_or_float_key_is_type_error(self) -> None:
        for key in (True, 1.5, "1"):
            with self.subTest(key=key):
                self._assert_error(
                    "EUDSL-TYPE", lambda key=key: dispatch(0, [(key, lambda: None)])
                )

    def test_invalid_value_is_type_error(self) -> None:
        for value in (1.5, "x", True, None):
            with self.subTest(value=value):
                self._assert_error(
                    "EUDSL-TYPE",
                    lambda value=value: dispatch(value, [(0, lambda: None)]),
                )

    def test_scope_error_outside_build_module(self) -> None:
        calls: list[str] = []

        def body() -> None:
            calls.append("body")

        def fallback() -> None:
            calls.append("fallback")

        with self.assertRaises(EudslError) as ctx:
            dispatch(0, [(0, body)], fallback=fallback)
        self.assertEqual(ctx.exception.code, "EUDSL-SCOPE")
        self.assertEqual(calls, [])


class DispatchKeyNormalizationTest(unittest.TestCase):
    def test_negative_key_normalizes(self) -> None:
        def via_negative() -> None:
            dispatch(
                0xFFFFFFFF,
                [(-1, lambda: SetDeaths(9001, SetTo, 1, 0))],
            )

        def via_masked() -> None:
            dispatch(
                0xFFFFFFFF,
                [(0xFFFFFFFF, lambda: SetDeaths(9002, SetTo, 2, 0))],
            )

        def runtime_value() -> None:
            value = Var(0xFFFFFFFF, storage="page")
            dispatch(value, [(-1, lambda: SetDeaths(9003, SetTo, 3, 0))])

        self.assertEqual(_epd_keys(_report(build_module(via_negative))), {9001})
        self.assertEqual(_epd_keys(_report(build_module(via_masked))), {9002})
        self.assertEqual(_epd_keys(_report(build_module(runtime_value))), {9003})

    def test_sparse_keys_select_correct_arm(self) -> None:
        sparse = [0, 10, 20, 100, 1_000, 2_000, 3_000, 4_000, 5_000, 6_000]
        for key in sparse:
            with self.subTest(key=key):
                report = _report(_runtime_module(10, init=key, keys=sparse))
                self.assertEqual(_epd_keys(report), {9000 + key})
        miss = _report(_runtime_module(10, init=7, keys=sparse))
        self.assertEqual(_epd_keys(miss), set())


class DispatchMaterializationTest(unittest.TestCase):
    def test_expr_value_materialized_once(self) -> None:
        def prog() -> None:
            base = Var(1)
            value = base.add(1)
            dispatch(value, [(k, _write_arm(k)) for k in range(4)])

        module = build_module(prog)
        doc = json.loads(module.to_json())
        # base + one materialised Expr temp; never one temp per case.
        self.assertEqual(len(doc["module"]["vars"]), 2)
        when_nodes = _when_nodes(module)
        self.assertEqual(len(when_nodes), 1)
        self.assertEqual(when_nodes[0]["conds"][0]["relocs"][0]["target_var"], 1)


class DispatchGoldenTest(unittest.TestCase):
    def test_then_only_hir_is_unchanged(self) -> None:
        def prog() -> None:
            page = Var(0, storage="page")
            hit = Var(0)
            hit.iadd(1)
            dispatch(0, [(0, lambda: hit.iadd(2))])

        # A dispatch-free control module: this string is the pre-#07 shape.
        def control() -> None:
            page = Var(0, storage="page")
            hit = Var(0)
            hit.iadd(1)

        self.assertEqual(build_module(control).to_json(), CONTROL_HIR)
        # The dispatch module adds exactly one root act; it does not move the
        # control shape.
        body = json.loads(build_module(prog).to_json())["module"]["stmts"]
        self.assertEqual([stmt["kind"] for stmt in body], ["trigger", "act"])


def _build_parity_module() -> Any:
    def prog() -> None:
        value = Var(0, storage="page")

        def arm(key: int) -> Callable[[], None]:
            return lambda: SetDeaths(9000 + key, SetTo, key, 0)

        def fallback() -> None:
            SetDeaths(FALLBACK_DEST, SetTo, 1, 0)

        dispatch(value, [(k, arm(k)) for k in range(1)])
        dispatch(value, [(k, arm(k)) for k in range(1)], fallback=fallback)
        for n in (4, 8):
            dispatch(value, [(k, arm(k)) for k in range(n)])
            dispatch(value, [(k, arm(k)) for k in range(n)], fallback=fallback)
        for n in (9, 10, 16, 32):
            dispatch(value, [(k, arm(k)) for k in range(n)])
        for n in (9, 10):
            dispatch(value, [(k, arm(k)) for k in range(n)], fallback=fallback)
        dispatch(3, [(1, arm(1)), (3, arm(3))])
        dispatch(7, [(1, arm(1))], fallback=fallback)
        dispatch(7, [(1, arm(1))])
        sparse = [0, 10, 20, 100, 1_000, 2_000, 3_000, 4_000, 5_000, 6_000]
        dispatch(value, [(k, arm(k)) for k in sparse])

    return build_module(prog)


_TS_PARITY_SCRIPT = textwrap.dedent(
    """
    import { SetDeaths, SetTo, Var, buildModule, dispatch } from "./ts/index.ts";

    const mod = buildModule(() => {
      const value = Var(0, undefined, undefined, "page");
      const arm = (key) => () => SetDeaths(9000 + key, SetTo, key, 0);
      const fallback = () => SetDeaths(9999, SetTo, 1, 0);
      const range = (n) =>
        Array.from({ length: n }, (_, k) => [k, arm(k)]);

      dispatch(value, range(1));
      dispatch(value, range(1), fallback);
      for (const n of [4, 8]) {
        dispatch(value, range(n));
        dispatch(value, range(n), fallback);
      }
      for (const n of [9, 10, 16, 32]) {
        dispatch(value, range(n));
      }
      for (const n of [9, 10]) {
        dispatch(value, range(n), fallback);
      }
      dispatch(3, [[1, arm(1)], [3, arm(3)]]);
      dispatch(7, [[1, arm(1)]], fallback);
      dispatch(7, [[1, arm(1)]]);
      const sparse = [0, 10, 20, 100, 1000, 2000, 3000, 4000, 5000, 6000];
      dispatch(value, sparse.map((k) => [k, arm(k)]));
    });

    process.stdout.write(mod.toJson());
    """
)


class DispatchParityTest(unittest.TestCase):
    def test_py_ts_hir_parity_for_dispatch(self) -> None:
        py_hir = _build_parity_module().to_json()
        res = subprocess.run(
            [
                "node",
                "--experimental-strip-types",
                "--input-type=module",
                "-e",
                _TS_PARITY_SCRIPT,
            ],
            cwd=str(REPO_ROOT),
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertEqual(py_hir, res.stdout)


if __name__ == "__main__":
    unittest.main()
