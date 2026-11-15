"""T-ABS-VIEW-POOL: `ObjectPool` free-list and storage semantics."""

from __future__ import annotations

import unittest
from typing import Any, Callable

from py import Var, build_module, when
from py.queue import POOL_NULL, ObjectPool
from py.tests.helpers import run_sim
from py.view import EudslError


def _run(fn: Callable[[], None], loops: int = 1) -> dict[str, Any]:
    return run_sim(build_module(fn).to_json(), loops)


def _values(report: dict[str, Any], ids: list[int]) -> list[int]:
    return [report["var_values"][index] for index in ids]


def _code(exc: BaseException) -> str:
    return getattr(exc, "code", "")


class ObjectPoolTests(unittest.TestCase):
    def test_fresh_pool_allocates_in_order_then_returns_null(self) -> None:
        ids: list[int] = []

        def prog() -> None:
            pool = ObjectPool(4)
            ids.extend(pool.allocate().id for _ in range(5))

        report = _run(prog)
        self.assertEqual(_values(report, ids), [0, 1, 2, 3, POOL_NULL])

    def test_release_is_lifo(self) -> None:
        ids: list[int] = []

        def prog() -> None:
            pool = ObjectPool(4)
            pool.allocate()
            pool.allocate()
            pool.release(1)
            ids.append(pool.allocate().id)

        report = _run(prog)
        self.assertEqual(_values(report, ids), [1])

    def test_full_and_empty_flip_at_the_right_points(self) -> None:
        ids: dict[str, int] = {}

        def prog() -> None:
            hit = Var(0)
            ids["hit"] = hit.id
            pool = ObjectPool(2)
            when(pool.empty(), lambda: hit.iadd(1))
            pool.allocate()
            pool.allocate()
            when(pool.full(), lambda: hit.iadd(1))

        report = _run(prog)
        self.assertEqual(report["var_values"][ids["hit"]], 2)

    def test_clear_restores_the_fresh_order(self) -> None:
        ids: list[int] = []

        def prog() -> None:
            pool = ObjectPool(3)
            pool.allocate()
            pool.allocate()
            pool.clear()
            ids.append(pool.allocate().id)

        report = _run(prog)
        self.assertEqual(_values(report, ids), [0])

    def test_storage_round_trip_with_constant_and_runtime_index(self) -> None:
        ids: dict[str, int] = {}

        def prog() -> None:
            pool = ObjectPool(4)
            pool.set(1, 42)
            ids["constant"] = pool.get(1).id
            index = Var(1)
            ids["runtime"] = pool.get(index).id

        report = _run(prog)
        self.assertEqual(report["var_values"][ids["constant"]], 42)
        self.assertEqual(report["var_values"][ids["runtime"]], 42)

    def test_capacity_validation(self) -> None:
        for capacity, code in (
            (0, "EUDSL-BOUNDS"),
            (-1, "EUDSL-BOUNDS"),
            (0xFFFFFFFF, "EUDSL-RANGE"),
            (True, "EUDSL-TYPE"),
        ):
            with self.subTest(capacity=capacity):
                with self.assertRaises(EudslError) as caught:
                    build_module(lambda: ObjectPool(capacity))
                self.assertEqual(_code(caught.exception), code)

    def test_pool_null_identity(self) -> None:
        from py import queue, view

        self.assertEqual(POOL_NULL, 0xFFFFFFFF)
        self.assertIs(queue.POOL_NULL, view.POOL_NULL)


if __name__ == "__main__":
    unittest.main()
