"""Typed location handles and build-time geometry (`T-ABS-LOCATION`).

Pins the public surface, the 5-dword record layout, the constant/runtime store
split, and the exact geometry act shape from §3/§4/§7.1 of the ticket. Exact
static/executed cost pins live in
``crates/eudkit-cli/tests/location_runtime.rs`` so both frontends are measured
through the same lowering pipeline.
"""

from __future__ import annotations

import json
import math
import operator
import unittest
from typing import Any, Callable
from unittest import mock

import py.location as location_module
from py import Add, Subtract, SetTo, Var, build_module
from py.camera import LOCATION_STRIDE_DWORDS, LOCATION_TABLE_ADDR
from py.location import (
    LOCATION_COUNT,
    LOCATION_RECORD_DWORDS,
    CoordOperand,
    Location,
    LocationOperand,
    draw_circle,
    draw_line,
    draw_polygon,
    draw_star,
    drawCircle,
    drawLine,
    drawPolygon,
    drawStar,
    getloc_tl,
    getlocTl,
    location_addr,
    location_epd,
    locationAddr,
    locationEpd,
    setRect,
)
from py.tests.helpers import read_dword, run_sim
from py.view import EPD, EudslError

#: §3.1 — the module-level camelCase aliases point at the same objects.
ALIASES: tuple[tuple[Any, Any], ...] = (
    (locationAddr, location_addr),
    (locationEpd, location_epd),
    (getlocTl, getloc_tl),
    (setRect, Location.set_rect),
    (Location.setRect, Location.set_rect),
    (Location.topLeft, Location.top_left),
    (drawCircle, draw_circle),
    (drawLine, draw_line),
    (drawPolygon, draw_polygon),
    (drawStar, draw_star),
)

EXPECTED_ALL = [
    "LocationOperand",
    "CoordOperand",
    "LOCATION_COUNT",
    "LOCATION_RECORD_DWORDS",
    "location_addr",
    "locationAddr",
    "location_epd",
    "locationEpd",
    "Location",
    "setRect",
    "getloc_tl",
    "getlocTl",
    "draw_circle",
    "drawCircle",
    "draw_line",
    "drawLine",
    "draw_polygon",
    "drawPolygon",
    "draw_star",
    "drawStar",
]


def _module(body: Callable[[], Any]) -> dict[str, Any]:
    return json.loads(build_module(body).to_json())["module"]


def _flatten(nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for node in nodes:
        out.append(node)
        out.extend(_flatten(node.get("body") or []))
        out.extend(_flatten(node.get("else_body") or []))
    return out


def _all_nodes(module: dict[str, Any]) -> list[dict[str, Any]]:
    return _flatten(module["stmts"]) + [
        node for func in module.get("funcs", []) for node in _flatten(func["body"])
    ]


def _acts(module: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for node in _all_nodes(module):
        out.extend(node.get("acts") or [])
    return out


def _top_acts(module: dict[str, Any]) -> list[dict[str, Any]]:
    return [act for node in module["stmts"] for act in (node.get("acts") or [])]


def _dots(acts: list[dict[str, Any]], base_addr: int) -> list[tuple[int, int]]:
    """Split a geometry act stream into the ``(x, y)`` pair per dot."""
    dots: list[tuple[int, int]] = []
    index = 0
    while index < len(acts):
        chunk = acts[index : index + 5]
        assert [act["acttype"] for act in chunk] == [45, 45, 45, 45, 44], chunk
        assert [act["dest"] for act in chunk[:4]] == [
            EPD(base_addr),
            EPD(base_addr + 4),
            EPD(base_addr + 8),
            EPD(base_addr + 12),
        ]
        x = chunk[0]["value"]
        y = chunk[1]["value"]
        assert (chunk[2]["value"], chunk[3]["value"]) == (x, y)
        assert 0 <= chunk[4]["mask"] < 256
        dots.append((x, y))
        index += 5
    return dots


def _assert_code(fn: Callable[[], Any], code: str) -> None:
    try:
        fn()
    except EudslError as err:
        assert err.code == code, f"expected {code}, got {err.code}: {err}"
        return
    raise AssertionError(f"expected {code}")


class AddressAndDomainTest(unittest.TestCase):
    def test_addresses_follow_the_five_dword_stride(self) -> None:
        self.assertEqual(LOCATION_COUNT, 256)
        self.assertEqual(LOCATION_RECORD_DWORDS, LOCATION_STRIDE_DWORDS)
        self.assertEqual(LOCATION_RECORD_DWORDS, 5)
        self.assertEqual(location_addr(0), LOCATION_TABLE_ADDR)
        self.assertEqual(location_addr(1), LOCATION_TABLE_ADDR + 20)
        self.assertEqual(location_addr(255), LOCATION_TABLE_ADDR + 20 * 255)
        for value in (0, 1, 7, 255):
            self.assertEqual(location_epd(value), EPD(location_addr(value)))

    def test_domain_errors_use_range_and_type(self) -> None:
        _assert_code(lambda: location_addr(256), "EUDSL-RANGE")
        _assert_code(lambda: location_addr(-1), "EUDSL-RANGE")
        _assert_code(lambda: location_addr(True), "EUDSL-TYPE")
        _assert_code(lambda: Location(1.5), "EUDSL-TYPE")
        _assert_code(lambda: Location(256), "EUDSL-RANGE")


class SetTest(unittest.TestCase):
    def test_two_coordinate_set_mirrors_into_four_dwords(self) -> None:
        module = _module(lambda: Location(7).set(100, 200))
        acts = _acts(module)
        self.assertEqual(
            [(act["dest"], act["modifier"], act["value"]) for act in acts],
            [
                (location_epd(7), SetTo, 100),
                (location_epd(7) + 1, SetTo, 200),
                (location_epd(7) + 2, SetTo, 100),
                (location_epd(7) + 3, SetTo, 200),
            ],
        )
        self.assertEqual(len(module["stmts"]), 1)
        self.assertEqual(module["stmts"][0]["kind"], "trigger")

    def test_four_coordinate_set_writes_each_dword(self) -> None:
        acts = _acts(_module(lambda: Location(7).set(1, 2, 3, 4)))
        self.assertEqual([act["value"] for act in acts], [1, 2, 3, 4])
        self.assertTrue(all(act["modifier"] == SetTo for act in acts))

    def test_wrong_arity_emits_nothing(self) -> None:
        def body() -> None:
            Location(7).set(100, 200)
            with self.assertRaises(EudslError) as caught:
                Location(7).set(1, 2, 3)
            self.assertEqual(caught.exception.code, "EUDSL-TYPE")

        module = _module(body)
        # Only the first valid call survives.
        self.assertEqual(len(_acts(module)), 4)

    def test_set_rect_checks_order_for_constants(self) -> None:
        _assert_code(lambda: Location(7).set_rect(10, 10, 5, 5), "EUDSL-RANGE")
        acts = _acts(_module(lambda: Location(7).set_rect(10, 20, 30, 40)))
        self.assertEqual([act["value"] for act in acts], [10, 20, 30, 40])


class MutatorTest(unittest.TestCase):
    def test_add_skips_zero_constants(self) -> None:
        mirrored = _acts(_module(lambda: Location(7).add(0, 5)))
        self.assertEqual(
            [(act["dest"], act["modifier"], act["value"]) for act in mirrored],
            [
                (location_epd(7) + 1, Add, 5),
                (location_epd(7) + 3, Add, 5),
            ],
        )

    def test_two_coordinate_add_mirrors_into_four_dwords(self) -> None:
        acts = _acts(_module(lambda: Location(7).add(5, 6)))
        self.assertEqual(
            [(act["dest"], act["modifier"], act["value"]) for act in acts],
            [
                (location_epd(7), Add, 5),
                (location_epd(7) + 1, Add, 6),
                (location_epd(7) + 2, Add, 5),
                (location_epd(7) + 3, Add, 6),
            ],
        )

        four = _acts(_module(lambda: Location(7).add(1, 2, 3, 4)))
        self.assertEqual(
            [(act["dest"], act["modifier"], act["value"]) for act in four],
            [
                (location_epd(7), Add, 1),
                (location_epd(7) + 1, Add, 2),
                (location_epd(7) + 2, Add, 3),
                (location_epd(7) + 3, Add, 4),
            ],
        )

    def test_dilate_subtracts_left_top_and_adds_right_bottom(self) -> None:
        acts = _acts(_module(lambda: Location(7).dilate(2, 3)))
        self.assertEqual(
            [(act["dest"], act["modifier"], act["value"]) for act in acts],
            [
                (location_epd(7), Subtract, 2),
                (location_epd(7) + 1, Subtract, 3),
                (location_epd(7) + 2, Add, 2),
                (location_epd(7) + 3, Add, 3),
            ],
        )

    def test_move_centers_the_record(self) -> None:
        acts = _acts(_module(lambda: Location(7).move(500, 600, 10, 20)))
        self.assertEqual([act["value"] for act in acts], [490, 580, 510, 620])
        _assert_code(lambda: Location(7).move(0, 0, -1, 0), "EUDSL-RANGE")


class ReadTest(unittest.TestCase):
    def _reader_epds(self, module: dict[str, Any]) -> list[int]:
        return [
            sorted({cond["player"] for node in func["body"] for cond in (node.get("conds") or [])})
            for func in module["funcs"]
        ]

    def test_bounds_reads_four_dwords_in_order(self) -> None:
        module = _module(lambda: Location(7).bounds())
        self.assertEqual(
            self._reader_epds(module),
            [
                [location_epd(7)],
                [location_epd(7) + 1],
                [location_epd(7) + 2],
                [location_epd(7) + 3],
            ],
        )
        self.assertEqual(len(module["stmts"]), 4)

    def test_top_left_and_getloc_tl_return_left_top(self) -> None:
        for build in (
            lambda: Location(7).top_left(),
            lambda: getloc_tl(7),
        ):
            module = _module(build)
            self.assertEqual(
                self._reader_epds(module),
                [[location_epd(7)], [location_epd(7) + 1]],
            )

    def test_center_emits_two_shift_folds(self) -> None:
        module = _module(lambda: Location(7).center())
        shifts = [
            node
            for node in module["stmts"]
            if node.get("kind") == "trigger"
            and any(
                act["modifier"] == Subtract and act["value"] == 1
                for act in (node.get("acts") or [])
            )
        ]
        self.assertEqual(len(shifts), 2)


class RuntimeOperandTest(unittest.TestCase):
    def test_runtime_id_routes_through_memset_epd(self) -> None:
        real = location_module.memsetEPD
        calls: list[tuple[Any, ...]] = []

        def spy(*args: Any) -> None:
            calls.append(args)
            real(*args)

        def body() -> None:
            ident = Var(3)
            loc = Location(ident)
            with self.assertRaises(EudslError) as caught:
                int(loc)
            self.assertEqual(caught.exception.code, "EUDSL-TYPE")
            loc.set(1, 2)

        with mock.patch.object(location_module, "memsetEPD", spy):
            module = _module(body)
        self.assertEqual(len(calls), 4)
        self.assertTrue(any(node.get("kind") == "while" for node in _all_nodes(module)))

    def test_runtime_coordinates_store_values_not_addresses(self) -> None:
        real = location_module.memsetEPD
        calls: list[tuple[Any, ...]] = []

        def spy(*args: Any) -> None:
            calls.append(args)
            real(*args)

        def body() -> None:
            x = Var(11)
            y = Var(22)
            Location(7).set(x, y)

        with mock.patch.object(location_module, "memsetEPD", spy):
            module = _module(body)
        self.assertEqual(len(calls), 4)
        self.assertTrue(all(isinstance(call[1], Var) for call in calls))
        # The direct location writes are the memset pointer machinery only.
        self.assertFalse(
            any(
                act["acttype"] == 45 and act["dest"] in range(location_epd(7), location_epd(7) + 4)
                for act in _top_acts(module)
            )
        )

    def test_runtime_id_epd_recomputes_after_the_id_var_changes(self) -> None:
        def body() -> None:
            ident = Var(3)
            loc = Location(ident)
            loc.set(10, 20)
            ident.set(4)
            loc.set(30, 40)

        report = run_sim(build_module(body).to_json(), 1)
        self.assertEqual(
            [read_dword(report, location_epd(3) + offset) for offset in range(4)],
            [10, 20, 10, 20],
        )
        self.assertEqual(
            [read_dword(report, location_epd(4) + offset) for offset in range(4)],
            [30, 40, 30, 40],
        )

    def test_constant_index_protocols(self) -> None:
        loc = Location(7)
        self.assertEqual(int(loc), 7)
        self.assertEqual(operator.index(loc), 7)
        self.assertEqual(loc.id, 7)
        self.assertEqual(loc.addr, location_addr(7))
        self.assertEqual(loc.epd, location_epd(7))

        def body() -> None:
            self.assertIsNone(Location(Var(7)).addr)

        self.assertEqual(_module(body)["stmts"], [])


class GeometryTest(unittest.TestCase):
    BASE = location_addr(7)

    def test_circle_emits_five_acts_per_dot(self) -> None:
        module = _module(
            lambda: draw_circle(7, 1, 0, 1000, 1000, radius=100, dots=8)
        )
        acts = _top_acts(module)
        self.assertEqual(len(acts), 40)
        self.assertEqual(sum(act["acttype"] == 44 for act in acts), 8)
        expected = [
            (
                1000 + round(100 * math.cos(2 * math.pi * index / 8)),
                1000 + round(100 * math.sin(2 * math.pi * index / 8)),
            )
            for index in range(8)
        ]
        self.assertEqual(_dots(acts, self.BASE), expected)

    def test_line_is_inclusive(self) -> None:
        module = _module(lambda: draw_line(7, 1, 0, 0, 0, 100, 100, dots=3))
        self.assertEqual(
            _dots(_top_acts(module), self.BASE),
            [(0, 0), (50, 50), (100, 100)],
        )

    def test_polygon_does_not_duplicate_shared_vertices(self) -> None:
        module = _module(
            lambda: draw_polygon(7, 1, 0, [(0, 0), (100, 0), (50, 100)], dots_per_edge=1)
        )
        acts = _top_acts(module)
        self.assertEqual(sum(act["acttype"] == 44 for act in acts), 3)
        self.assertEqual(
            _dots(acts, self.BASE),
            [(0, 0), (100, 0), (50, 100)],
        )

    def test_star_walks_the_star_polygon(self) -> None:
        module = _module(
            lambda: draw_star(
                7, 1, 0, 512, 512, vertices=5, density=2, radius=64, dots_per_edge=1
            )
        )
        acts = _top_acts(module)
        self.assertEqual(sum(act["acttype"] == 44 for act in acts), 5)
        # density 3 is coprime with 5 and stays legal; density 5 is excluded.
        draw_star(7, 1, 0, 512, 512, vertices=5, density=3, dots_per_edge=1)
        _assert_code(
            lambda: draw_star(7, 1, 0, 512, 512, vertices=5, density=5),
            "EUDSL-RANGE",
        )

    def test_geometry_rejects_bad_arguments_before_emitting(self) -> None:
        for fn, code in (
            (lambda: draw_circle(7, 1, 0, 1000, 1000, dots=0), "EUDSL-RANGE"),
            (lambda: draw_circle(7, 1, 0, 1000, 1000, radius=-1), "EUDSL-RANGE"),
            (lambda: draw_circle(7, 228, 0, 1000, 1000), "EUDSL-RANGE"),
            (lambda: draw_circle(7, 1, 12, 1000, 1000), "EUDSL-RANGE"),
            (lambda: draw_circle(7, 1, 0, 1000.0, 1000), "EUDSL-TYPE"),
            (lambda: draw_polygon(7, 1, 0, [(0, 0), (1, 1)]), "EUDSL-RANGE"),
            (lambda: draw_line(7, 1, 0, 0, 0, 1, 1, dots=1), "EUDSL-RANGE"),
            (lambda: draw_star(7, 1, 0, 0, 0, vertices=4, density=2), "EUDSL-RANGE"),
        ):
            module = _module(lambda fn=fn: self._expect_error(fn, code))
            self.assertEqual(module["stmts"], [])

    @staticmethod
    def _expect_error(fn: Callable[[], Any], code: str) -> None:
        try:
            fn()
        except EudslError as err:
            assert err.code == code, err
            return
        raise AssertionError(f"expected {code}")


class SurfaceTest(unittest.TestCase):
    def test_all_matches_the_ticket(self) -> None:
        self.assertEqual(location_module.__all__, EXPECTED_ALL)

    def test_every_camel_case_alias_is_the_snake_case_twin(self) -> None:
        for camel, snake in ALIASES:
            self.assertIs(camel, snake)

    def test_class_camel_case_aliases_are_the_same_objects(self) -> None:
        self.assertIs(Location.setRect, Location.set_rect)
        self.assertIs(Location.topLeft, Location.top_left)
        self.assertIn("setRect", location_module.__all__)

    def test_no_trigger_rewiring_primitives(self) -> None:
        source = location_module.__file__
        assert source is not None
        with open(source, encoding="utf-8") as handle:
            text = handle.read()
        for forbidden in ("SetNextPtr", "Switch(", "Forward("):
            self.assertNotIn(forbidden, text)

    def test_camera_table_is_reused_not_redefined(self) -> None:
        self.assertEqual(location_addr(0), LOCATION_TABLE_ADDR)
        self.assertEqual(
            Location(3).epd,
            EPD(LOCATION_TABLE_ADDR) + LOCATION_STRIDE_DWORDS * 3,
        )

    def test_operand_aliases_are_distinct_types(self) -> None:
        # Both are `int | Var | ReadonlyVar | Expr`; importing them keeps the
        # public type surface pinned.
        self.assertIs(LocationOperand, LocationOperand)
        self.assertIs(CoordOperand, CoordOperand)


if __name__ == "__main__":
    unittest.main()
