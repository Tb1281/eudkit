"""Typed handles over StarCraft's 5-dword location table plus build-time geometry.

``locf/locf.py`` treats a location as an index into a game-side record; this
module wraps that record in :class:`Location`. The record layout is fixed:
dword 0 is ``left``, 1 is ``top``, 2 is ``right``, 3 is ``bottom``, and dword 4
is never touched. The id and every coordinate may be a build-time ``int`` or a
runtime `Var`; runtime stores go through :func:`py.blockio.memsetEPD` because
``SetMemory(..., Var)`` writes the variable's address, not its value.

The drawing helpers are build-time only: they compute a dot path with
``math.cos``/``math.sin`` and emit four location writes plus one ``CreateUnit``
per dot. They deliberately do not absorb ``draw.py``'s module-global ``Loc`` or
its ``dots`` NameError. Each helper clobbers the target record and leaves it at
the last dot, matching ``euddraft``'s single-``Loc`` model.
"""

from __future__ import annotations

import math
from typing import Sequence

from .blockio import memsetEPD
from .camera import (
    LOCATION_STRIDE_DWORDS,
    LOCATION_TABLE_ADDR,
    PLAYER_VALUES,
)
from .cp import with_epd
from .hir import CreateUnit, SetMemory, SetTo, Add, Subtract
from .var import Expr, ReadonlyVar, Var, to_var
from .view import (
    EPD,
    EudslError,
    _describe_value,
    _is_plain_int,
    read_epd,
)

LocationOperand = int | Var | ReadonlyVar | Expr
CoordOperand = int | Var | ReadonlyVar | Expr

LOCATION_COUNT: int = 256
LOCATION_RECORD_DWORDS: int = LOCATION_STRIDE_DWORDS

_SET = SetTo
_ADD = Add
_SUB = Subtract


def _require_id(value: object, what: str = "location") -> int | Var:
    """A location id is ``0..255`` as an ``int`` or a runtime `Var` handle."""
    if _is_plain_int(value):
        if not 0 <= value < LOCATION_COUNT:
            raise EudslError(
                "EUDSL-RANGE",
                f"{what} id must be 0..{LOCATION_COUNT - 1}, got {value}",
            )
        return value
    return to_var(value)


def _const_id(value: object, what: str = "location") -> int:
    """Require a build-time id for an address-based operation."""
    checked = _require_id(value, what)
    if not _is_plain_int(checked):
        raise EudslError(
            "EUDSL-TYPE",
            f"{what}: location id must be a plain integer, got {_describe_value(value)}",
        )
    return checked


def _coord(value: object, what: str) -> int | Var:
    """A location coordinate is ``0..0xFFFF`` as an ``int`` or a runtime var."""
    if _is_plain_int(value):
        if not 0 <= value <= 0xFFFF:
            raise EudslError(
                "EUDSL-BOUNDS",
                f"{what}: coordinate must be 0..65535, got {value}",
            )
        return value
    return to_var(value)


def _const_coord(value: object, what: str) -> int:
    """Require a build-time coordinate for the geometry helpers."""
    checked = _coord(value, what)
    if not _is_plain_int(checked):
        raise EudslError(
            "EUDSL-TYPE",
            f"{what}: coordinate must be a plain integer, got {_describe_value(value)}",
        )
    return checked


def location_addr(location: int) -> int:
    """Byte address of ``location``'s record: ``0x58DC4C + 20 * id``."""
    value = _const_id(location, "location_addr()")
    return LOCATION_TABLE_ADDR + 4 * LOCATION_STRIDE_DWORDS * value


def location_epd(location: int) -> int:
    """EPD of ``location``'s first dword."""
    return EPD(location_addr(location))


locationAddr = location_addr
locationEpd = location_epd


class Location:
    """Handle over one 5-dword location record.

    A constant id keeps every mutator on the packed ``SetMemory`` fast path; a
    runtime id routes each store through ``memsetEPD``. Reads share the 32-bit
    dword reader for a constant id and bind ``CurrentPlayer`` for a runtime id.
    """

    def __init__(self, location: LocationOperand) -> None:
        self._id = _require_id(location, "Location()")

    @property
    def id(self) -> int | Var:
        return self._id

    @property
    def epd(self) -> int | Var:
        """EPD of the record, recomputed for a runtime id on every access."""
        if _is_plain_int(self._id):
            return location_epd(self._id)
        epd = Var(0)
        epd.set(self._id.mul(LOCATION_STRIDE_DWORDS))
        epd.iadd(EPD(LOCATION_TABLE_ADDR))
        return epd

    @property
    def addr(self) -> int | None:
        return location_addr(self._id) if _is_plain_int(self._id) else None

    def _read_many(
        self,
        offsets: Sequence[int],
        base_epd: Var | None = None,
    ) -> dict[int, Var]:
        """Read several record dwords, binding CP once for a runtime id."""
        if _is_plain_int(self._id):
            base = self.epd
            return {offset: read_epd(base + offset, 32, 0) for offset in offsets}

        out: dict[int, Var] = {}

        def body(cp: object) -> None:
            for offset in offsets:
                out[offset] = cp.at(offset).read()  # type: ignore[attr-defined]

        with_epd(self.epd if base_epd is None else base_epd, body)
        return out

    def _apply(self, entries: Sequence[tuple[int, int | Var, int]]) -> None:
        """Emit ``(offset, value, modifier)`` stores in the given order."""
        runtime_id = not _is_plain_int(self._id)
        need_read = {
            offset
            for offset, value, modifier in entries
            if modifier != _SET
            and not (_is_plain_int(value) and value == 0)
            and (runtime_id or not _is_plain_int(value))
        }
        runtime_epd: Var | None = None
        if runtime_id:
            for _offset, value, modifier in entries:
                if modifier == _SET or not (
                    _is_plain_int(value) and value == 0
                ):
                    runtime_epd = self.epd
                    break
        current = (
            self._read_many(sorted(need_read), runtime_epd) if need_read else {}
        )
        base_addr = None if runtime_id else location_addr(self._id)  # type: ignore[arg-type]

        for offset, value, modifier in entries:
            if modifier != _SET and _is_plain_int(value) and value == 0:
                continue
            if base_addr is not None and _is_plain_int(value):
                SetMemory(base_addr + 4 * offset, modifier, value)
                continue

            if modifier == _SET:
                stored: int | Var = value
            else:
                total = Var(0)
                total.set(current[offset])
                if modifier == _ADD:
                    total.iadd(value)
                else:
                    total.isub(value)
                stored = total
            dest = (
                runtime_epd + offset
                if runtime_epd is not None
                else self.epd + offset  # type: ignore[operator]
            )
            memsetEPD(dest, stored, 1)

    @staticmethod
    def _coords(coords: Sequence[CoordOperand], what: str) -> tuple[int | Var, ...]:
        if len(coords) not in (2, 4):
            raise EudslError(
                "EUDSL-TYPE",
                f"{what} takes 2 or 4 coordinates, got {len(coords)}",
            )
        return tuple(_coord(value, what) for value in coords)

    def set(self, *coords: CoordOperand) -> None:
        """Set the record. ``set(x, y)`` mirrors the point into all four dwords."""
        values = self._coords(coords, "Location.set()")
        if len(values) == 2:
            x, y = values
            values = (x, y, x, y)
        self._apply([(offset, value, _SET) for offset, value in enumerate(values)])

    def set_rect(
        self,
        left: CoordOperand,
        top: CoordOperand,
        right: CoordOperand,
        bottom: CoordOperand,
    ) -> None:
        """Set four edges, checking ``left <= right``/``top <= bottom`` if constant."""
        values = self._coords((left, top, right, bottom), "Location.set_rect()")
        if all(_is_plain_int(value) for value in values):
            if values[0] > values[2] or values[1] > values[3]:
                raise EudslError(
                    "EUDSL-RANGE",
                    "Location.set_rect(): left <= right and top <= bottom required, "
                    f"got {values[0]}, {values[1]}, {values[2]}, {values[3]}",
                )
        self._apply([(offset, value, _SET) for offset, value in enumerate(values)])

    def add(self, *coords: CoordOperand) -> None:
        """Add to the record, skipping zero constants.

        ``add(x, y)`` mirrors the point across all four dwords as
        ``(x, y, x, y)``; ``add(l, t, r, b)`` adjusts each dword.
        """
        values = self._coords(coords, "Location.add()")
        if len(values) == 2:
            x, y = values
            values = (x, y, x, y)
        self._apply(
            [(offset, value, _ADD) for offset, value in enumerate(values)]
        )

    def dilate(self, *coords: CoordOperand) -> None:
        """Grow the record by ``(-l, -t, +r, +b)``; ``dilate(x, y)`` mirrors."""
        values = self._coords(coords, "Location.dilate()")
        if len(values) == 2:
            x, y = values
            values = (x, y, x, y)
        self._apply(
            [
                (0, values[0], _SUB),
                (1, values[1], _SUB),
                (2, values[2], _ADD),
                (3, values[3], _ADD),
            ]
        )

    def move(
        self,
        x: CoordOperand,
        y: CoordOperand,
        half_w: CoordOperand = 0,
        half_h: CoordOperand = 0,
    ) -> None:
        """Center the record on ``(x, y)`` with the given half extents."""
        x = _coord(x, "Location.move()")
        y = _coord(y, "Location.move()")
        half_w = _half(half_w, "Location.move()")
        half_h = _half(half_h, "Location.move()")
        self._apply(
            [
                (0, _signed(x, half_w, -1), _SET),
                (1, _signed(y, half_h, -1), _SET),
                (2, _signed(x, half_w, 1), _SET),
                (3, _signed(y, half_h, 1), _SET),
            ]
        )

    def bounds(self) -> tuple[Var, Var, Var, Var]:
        """Return fresh ``(left, top, right, bottom)`` reads."""
        values = self._read_many((0, 1, 2, 3))
        return values[0], values[1], values[2], values[3]

    def center(self) -> tuple[Var, Var]:
        """Return ``((left + right) >> 1, (top + bottom) >> 1)``."""
        left, top, right, bottom = self.bounds()
        cx = Var(0)
        cx.set(left.add(right).shr(1))
        cy = Var(0)
        cy.set(top.add(bottom).shr(1))
        return cx, cy

    def top_left(self) -> tuple[Var, Var]:
        """Return ``(left, top)`` -- the code's order, not eudplib's docstring."""
        values = self._read_many((0, 1))
        return values[0], values[1]

    setRect = set_rect
    topLeft = top_left

    def __int__(self) -> int:
        if not _is_plain_int(self._id):
            raise EudslError(
                "EUDSL-TYPE",
                "Location with a runtime id has no build-time int; use `loc.id`",
            )
        return self._id

    def __index__(self) -> int:
        return int(self)


def _half(value: object, what: str) -> int | Var:
    """A non-negative half extent; runtime values skip the build-time check."""
    if _is_plain_int(value):
        if not 0 <= value <= 0xFFFF:
            raise EudslError(
                "EUDSL-RANGE",
                f"{what}: half extent must be 0..65535, got {value}",
            )
        return value
    return to_var(value)


def _signed(a: int | Var, b: int | Var, sign: int) -> int | Var:
    """``a + sign * b``, folding two constants into one 32-bit value."""
    if _is_plain_int(a) and _is_plain_int(b):
        return (a + sign * b) & 0xFFFFFFFF
    out = Var(0)
    out.set(a)
    if sign > 0:
        out.iadd(b)
    else:
        out.isub(b)
    return out


def getloc_tl(location: LocationOperand) -> tuple[Var, Var]:
    """Read ``(left, top)`` for a raw location id or handle."""
    if isinstance(location, Location):
        return location.top_left()
    return Location(location).top_left()


getlocTl = getloc_tl


def _int_option(what: str, name: str, value: object, minimum: int, maximum: int) -> int:
    """Validate one build-time integer argument as a plain int in a range."""
    if not _is_plain_int(value):
        raise EudslError(
            "EUDSL-TYPE",
            f"{what}: {name} must be a plain integer, got {_describe_value(value)}",
        )
    if not minimum <= value <= maximum:
        raise EudslError(
            "EUDSL-RANGE",
            f"{what}: {name} must be in [{minimum}, {maximum}], got {value}",
        )
    return value


def _player_option(what: str, value: object) -> int:
    """Validate a stock player id."""
    if not _is_plain_int(value):
        raise EudslError(
            "EUDSL-TYPE",
            f"{what}: player must be a plain integer, got {_describe_value(value)}",
        )
    if value not in PLAYER_VALUES:
        raise EudslError(
            "EUDSL-RANGE",
            f"{what}: player must be one of {list(PLAYER_VALUES)}, got {value}",
        )
    return value


def _round(value: float) -> int:
    """Round half away from zero so negative coordinates mirror positive ones."""
    return int(value + 0.5) if value >= 0 else int(value - 0.5)


def _emit_dot(
    base_addr: int,
    location: int,
    unit: int,
    player: int,
    x: int,
    y: int,
) -> None:
    """Emit one dot's four location writes followed by its ``CreateUnit``."""
    SetMemory(base_addr, SetTo, x)
    SetMemory(base_addr + 4, SetTo, y)
    SetMemory(base_addr + 8, SetTo, x)
    SetMemory(base_addr + 12, SetTo, y)
    CreateUnit(1, unit, location, player)


def _emit_path(
    base_addr: int,
    location: int,
    unit: int,
    player: int,
    points: Sequence[tuple[int, int]],
) -> None:
    for x, y in points:
        _emit_dot(base_addr, location, unit, player, x, y)


def _closed_path(
    vertices: Sequence[tuple[int, int]],
    dots_per_edge: int,
) -> list[tuple[int, int]]:
    """Sample each edge endpoint-exclusive so shared vertices are emitted once."""
    points: list[tuple[int, int]] = []
    count = len(vertices)
    for index in range(count):
        x0, y0 = vertices[index]
        x1, y1 = vertices[(index + 1) % count]
        for step in range(dots_per_edge):
            t = step / dots_per_edge
            points.append(
                (
                    _round(x0 + (x1 - x0) * t),
                    _round(y0 + (y1 - y0) * t),
                )
            )
    return points


def _circle_points(
    center_x: int,
    center_y: int,
    radius: int,
    dots: int,
) -> list[tuple[int, int]]:
    return [
        (
            center_x + _round(radius * math.cos(2 * math.pi * index / dots)),
            center_y + _round(radius * math.sin(2 * math.pi * index / dots)),
        )
        for index in range(dots)
    ]


def draw_circle(
    location: LocationOperand,
    unit: int,
    player: int,
    center_x: int,
    center_y: int,
    *,
    radius: int = 128,
    dots: int = 32,
) -> None:
    """Create ``dots`` units on a build-time circle."""
    what = "draw_circle()"
    loc = _const_id(location, what)
    unit = _int_option(what, "unit", unit, 0, 227)
    player = _player_option(what, player)
    center_x = _const_coord(center_x, f"{what}: center_x")
    center_y = _const_coord(center_y, f"{what}: center_y")
    radius = _int_option(what, "radius", radius, 0, 0xFFFF)
    dots = _int_option(what, "dots", dots, 1, 0xFFFF)
    points = _circle_points(center_x, center_y, radius, dots)
    _emit_path(location_addr(loc), loc, unit, player, points)


def draw_line(
    location: LocationOperand,
    unit: int,
    player: int,
    x0: int,
    y0: int,
    x1: int,
    y1: int,
    *,
    dots: int = 32,
) -> None:
    """Create ``dots`` units along the inclusive segment ``(x0, y0)-(x1, y1)``."""
    what = "draw_line()"
    loc = _const_id(location, what)
    unit = _int_option(what, "unit", unit, 0, 227)
    player = _player_option(what, player)
    x0 = _const_coord(x0, f"{what}: x0")
    y0 = _const_coord(y0, f"{what}: y0")
    x1 = _const_coord(x1, f"{what}: x1")
    y1 = _const_coord(y1, f"{what}: y1")
    dots = _int_option(what, "dots", dots, 2, 0xFFFF)
    points = [
        (
            _round(x0 + (x1 - x0) * index / (dots - 1)),
            _round(y0 + (y1 - y0) * index / (dots - 1)),
        )
        for index in range(dots)
    ]
    _emit_path(location_addr(loc), loc, unit, player, points)


def draw_polygon(
    location: LocationOperand,
    unit: int,
    player: int,
    points: Sequence[tuple[int, int]],
    *,
    dots_per_edge: int = 8,
) -> None:
    """Create units along the closed polygon through ``points``."""
    what = "draw_polygon()"
    loc = _const_id(location, what)
    unit = _int_option(what, "unit", unit, 0, 227)
    player = _player_option(what, player)
    dots_per_edge = _int_option(what, "dots_per_edge", dots_per_edge, 1, 0xFFFF)
    vertices = _const_points(points, what)
    if len(vertices) < 3:
        raise EudslError(
            "EUDSL-RANGE",
            f"{what}: a polygon needs at least 3 points, got {len(vertices)}",
        )
    path = _closed_path(vertices, dots_per_edge)
    _emit_path(location_addr(loc), loc, unit, player, path)


def _const_points(
    points: Sequence[tuple[int, int]],
    what: str,
) -> list[tuple[int, int]]:
    """Validate a point sequence into build-time coordinates."""
    try:
        raw = list(points)
    except TypeError:
        raise EudslError("EUDSL-TYPE", f"{what}: points must be a sequence") from None
    out: list[tuple[int, int]] = []
    for point in raw:
        if not isinstance(point, (tuple, list)) or len(point) != 2:
            raise EudslError(
                "EUDSL-TYPE",
                f"{what}: each point must be an (x, y) pair, got {_describe_value(point)}",
            )
        out.append(
            (
                _const_coord(point[0], f"{what}: x"),
                _const_coord(point[1], f"{what}: y"),
            )
        )
    return out


def draw_star(
    location: LocationOperand,
    unit: int,
    player: int,
    center_x: int,
    center_y: int,
    *,
    vertices: int = 5,
    density: int = 2,
    radius: int = 128,
    dots_per_edge: int = 8,
) -> None:
    """Create units along the regular star polygon ``{vertices/density}``."""
    what = "draw_star()"
    loc = _const_id(location, what)
    unit = _int_option(what, "unit", unit, 0, 227)
    player = _player_option(what, player)
    center_x = _const_coord(center_x, f"{what}: center_x")
    center_y = _const_coord(center_y, f"{what}: center_y")
    vertices = _int_option(what, "vertices", vertices, 3, 0xFFFF)
    density = _int_option(what, "density", density, 1, vertices - 1)
    if math.gcd(vertices, density) != 1:
        raise EudslError(
            "EUDSL-RANGE",
            f"{what}: density {density} must be coprime with vertices {vertices}",
        )
    radius = _int_option(what, "radius", radius, 0, 0xFFFF)
    dots_per_edge = _int_option(what, "dots_per_edge", dots_per_edge, 1, 0xFFFF)
    corners = [
        (
            center_x
            + _round(radius * math.cos(2 * math.pi * index * density / vertices)),
            center_y
            + _round(radius * math.sin(2 * math.pi * index * density / vertices)),
        )
        for index in range(vertices)
    ]
    _emit_path(
        location_addr(loc),
        loc,
        unit,
        player,
        _closed_path(corners, dots_per_edge),
    )


#: camelCase aliases (same objects); the snake_case names remain canonical.
drawCircle = draw_circle
drawLine = draw_line
drawPolygon = draw_polygon
drawStar = draw_star

__all__ = [
    "LocationOperand",
    "CoordOperand",
    "LOCATION_COUNT",
    "LOCATION_RECORD_DWORDS",
    "location_addr",
    "locationAddr",
    "location_epd",
    "locationEpd",
    "Location",
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
