/**
 * Typed handles over StarCraft's 5-dword location table plus build-time geometry.
 *
 * `locf/locf.py` treats a location as an index into a game-side record; this
 * module wraps that record in `Location`. The record layout is fixed: dword 0
 * is `left`, 1 is `top`, 2 is `right`, 3 is `bottom`, and dword 4 is never
 * touched. The id and every coordinate may be a build-time `int` or a runtime
 * `Var`; runtime stores go through `memsetEPD` because `SetMemory(..., Var)`
 * writes the variable's address, not its value.
 *
 * The drawing helpers are build-time only: they compute a dot path with
 * `Math.cos`/`Math.sin` and emit four location writes plus one `CreateUnit`
 * per dot. They deliberately do not absorb `draw.py`'s module-global `Loc` or
 * its `dots` NameError. Each helper clobbers the target record and leaves it
 * at the last dot, matching `euddraft`'s single-`Loc` model.
 */

import { memsetEPD } from "./blockio.ts";
import {
  LOCATION_STRIDE_DWORDS,
  LOCATION_TABLE_ADDR,
  PLAYER_VALUES,
} from "./camera.ts";
import { withEpd, type EpdCursor } from "./cp.ts";
import { Add, CreateUnit, SetMemory, SetTo, Subtract } from "./hir.ts";
import {
  Expr,
  ReadonlyVar,
  Var,
  VarImpl,
  toVar,
  type VarOperand,
} from "./var.ts";
import { EPD, EudslError, describeValue, readEpd } from "./view.ts";

export type LocationOperand = number | VarImpl | ReadonlyVar | Expr;
export type CoordOperand = number | VarImpl | ReadonlyVar | Expr;

export const LOCATION_COUNT = 256;
export const LOCATION_RECORD_DWORDS = LOCATION_STRIDE_DWORDS;

const SET = SetTo;
const ADD = Add;
const SUB = Subtract;

function isPlainInt(value: unknown): value is number {
  return typeof value === "number" && Number.isInteger(value);
}

function requireId(value: unknown, what = "location"): number | VarImpl {
  if (isPlainInt(value)) {
    if (value < 0 || value >= LOCATION_COUNT) {
      throw new EudslError(
        "EUDSL-RANGE",
        `${what} id must be 0..${LOCATION_COUNT - 1}, got ${value}`,
      );
    }
    return value;
  }
  return toVar(value as VarOperand);
}

function constId(value: unknown, what = "location"): number {
  const checked = requireId(value, what);
  if (typeof checked !== "number") {
    throw new EudslError(
      "EUDSL-TYPE",
      `${what}: location id must be a plain integer, got ${describeValue(value)}`,
    );
  }
  return checked;
}

function coord(value: unknown, what: string): number | VarImpl {
  if (isPlainInt(value)) {
    if (value < 0 || value > 0xffff) {
      throw new EudslError(
        "EUDSL-BOUNDS",
        `${what}: coordinate must be 0..65535, got ${value}`,
      );
    }
    return value;
  }
  return toVar(value as VarOperand);
}

function constCoord(value: unknown, what: string): number {
  const checked = coord(value, what);
  if (typeof checked !== "number") {
    throw new EudslError(
      "EUDSL-TYPE",
      `${what}: coordinate must be a plain integer, got ${describeValue(value)}`,
    );
  }
  return checked;
}

/** Byte address of `location`'s record: `0x58DC4C + 20 * id`. */
export function locationAddr(location: number): number {
  const value = constId(location, "locationAddr()");
  return (LOCATION_TABLE_ADDR + 4 * LOCATION_STRIDE_DWORDS * value) >>> 0;
}

export const location_addr = locationAddr;

/** EPD of `location`'s first dword. */
export function locationEpd(location: number): number {
  return EPD(locationAddr(location));
}

export const location_epd = locationEpd;

/** Handle over one 5-dword location record. */
export class Location {
  private readonly _id: number | VarImpl;
  private _epd: VarImpl | null = null;

  constructor(location: LocationOperand) {
    this._id = requireId(location, "Location()");
  }

  get id(): number | VarImpl {
    return this._id;
  }

  get epd(): number | VarImpl {
    if (typeof this._id === "number") {
      return locationEpd(this._id);
    }
    if (this._epd === null) {
      const epd = new Var(0);
      epd.set(this._id.mul(LOCATION_STRIDE_DWORDS));
      epd.iadd(EPD(LOCATION_TABLE_ADDR));
      this._epd = epd;
    }
    return this._epd;
  }

  get addr(): number | null {
    return typeof this._id === "number" ? locationAddr(this._id) : null;
  }

  private readMany(offsets: readonly number[]): Map<number, VarImpl> {
    const out = new Map<number, VarImpl>();
    if (typeof this._id === "number") {
      const base = this.epd as number;
      for (const offset of offsets) {
        out.set(offset, readEpd(base + offset, 32, 0));
      }
      return out;
    }

    withEpd(this.epd, (cp: EpdCursor) => {
      for (const offset of offsets) {
        out.set(offset, cp.at(offset).read());
      }
    });
    return out;
  }

  private apply(entries: ReadonlyArray<readonly [number, number | VarImpl, number]>): void {
    const runtimeId = typeof this._id !== "number";
    const needRead = new Set<number>();
    for (const [offset, value, modifier] of entries) {
      if (
        modifier !== SET &&
        !(isPlainInt(value) && value === 0) &&
        (runtimeId || !isPlainInt(value))
      ) {
        needRead.add(offset);
      }
    }
    const offsets = [...needRead].sort((a, b) => a - b);
    const current =
      offsets.length === 0 ? new Map<number, VarImpl>() : this.readMany(offsets);
    const baseAddr = runtimeId ? null : locationAddr(this._id as number);

    for (const [offset, value, modifier] of entries) {
      if (modifier !== SET && isPlainInt(value) && value === 0) continue;
      if (baseAddr !== null && isPlainInt(value)) {
        SetMemory(baseAddr + 4 * offset, modifier, value);
        continue;
      }

      let stored: number | VarImpl;
      if (modifier === SET) {
        stored = value;
      } else {
        const total = new Var(0);
        total.set(current.get(offset)!);
        if (modifier === ADD) {
          total.iadd(value);
        } else {
          total.isub(value);
        }
        stored = total;
      }
      const epd = this.epd;
      const dest = typeof epd === "number" ? epd + offset : epd.add(offset);
      memsetEPD(dest, stored, 1);
    }
  }

  private static coords(
    values: readonly CoordOperand[],
    what: string,
  ): Array<number | VarImpl> {
    if (values.length !== 2 && values.length !== 4) {
      throw new EudslError(
        "EUDSL-TYPE",
        `${what} takes 2 or 4 coordinates, got ${values.length}`,
      );
    }
    return values.map((value) => coord(value, what));
  }

  /** Set the record. `set(x, y)` mirrors the point into all four dwords. */
  set(...coords: CoordOperand[]): void {
    let values = Location.coords(coords, "Location.set()");
    if (values.length === 2) {
      const [x, y] = values;
      values = [x, y, x, y];
    }
    this.apply(values.map((value, offset) => [offset, value, SET] as const));
  }

  /** Set four edges, checking `left <= right`/`top <= bottom` if constant. */
  setRect(
    left: CoordOperand,
    top: CoordOperand,
    right: CoordOperand,
    bottom: CoordOperand,
  ): void {
    const values = Location.coords(
      [left, top, right, bottom],
      "Location.setRect()",
    );
    if (values.every((value) => isPlainInt(value))) {
      if ((values[0] as number) > (values[2] as number) || (values[1] as number) > (values[3] as number)) {
        throw new EudslError(
          "EUDSL-RANGE",
          "Location.setRect(): left <= right and top <= bottom required, " +
            `got ${values[0]}, ${values[1]}, ${values[2]}, ${values[3]}`,
        );
      }
    }
    this.apply(values.map((value, offset) => [offset, value, SET] as const));
  }

  set_rect(
    left: CoordOperand,
    top: CoordOperand,
    right: CoordOperand,
    bottom: CoordOperand,
  ): void {
    this.setRect(left, top, right, bottom);
  }

  /**
   * Add to the record, skipping zero constants. `add(x, y)` adjusts the
   * top-left corner; `add(l, t, r, b)` adjusts all four dwords.
   */
  add(...coords: CoordOperand[]): void {
    const values = Location.coords(coords, "Location.add()");
    const offsets = values.length === 2 ? [0, 1] : [0, 1, 2, 3];
    this.apply(offsets.map((offset) => [offset, values[offset], ADD] as const));
  }

  /** Grow the record by `(-l, -t, +r, +b)`; `dilate(x, y)` mirrors. */
  dilate(...coords: CoordOperand[]): void {
    let values = Location.coords(coords, "Location.dilate()");
    if (values.length === 2) {
      const [x, y] = values;
      values = [x, y, x, y];
    }
    this.apply([
      [0, values[0], SUB],
      [1, values[1], SUB],
      [2, values[2], ADD],
      [3, values[3], ADD],
    ]);
  }

  /** Center the record on `(x, y)` with the given half extents. */
  move(
    x: CoordOperand,
    y: CoordOperand,
    halfW: CoordOperand = 0,
    halfH: CoordOperand = 0,
  ): void {
    const cx = coord(x, "Location.move()");
    const cy = coord(y, "Location.move()");
    const hw = half(halfW, "Location.move()");
    const hh = half(halfH, "Location.move()");
    this.apply([
      [0, signed(cx, hw, -1), SET],
      [1, signed(cy, hh, -1), SET],
      [2, signed(cx, hw, 1), SET],
      [3, signed(cy, hh, 1), SET],
    ]);
  }

  /** Return fresh `(left, top, right, bottom)` reads. */
  bounds(): [VarImpl, VarImpl, VarImpl, VarImpl] {
    const values = this.readMany([0, 1, 2, 3]);
    return [values.get(0)!, values.get(1)!, values.get(2)!, values.get(3)!];
  }

  /** Return `((left + right) >> 1, (top + bottom) >> 1)`. */
  center(): [VarImpl, VarImpl] {
    const [left, top, right, bottom] = this.bounds();
    const cx = new Var(0);
    cx.set(left.add(right).shr(1));
    const cy = new Var(0);
    cy.set(top.add(bottom).shr(1));
    return [cx, cy];
  }

  /** Return `(left, top)` -- the code's order, not eudplib's docstring. */
  topLeft(): [VarImpl, VarImpl] {
    const values = this.readMany([0, 1]);
    return [values.get(0)!, values.get(1)!];
  }

  top_left(): [VarImpl, VarImpl] {
    return this.topLeft();
  }

  valueOf(): number {
    if (typeof this._id !== "number") {
      throw new EudslError(
        "EUDSL-TYPE",
        "Location with a runtime id has no build-time int; use `loc.id`",
      );
    }
    return this._id;
  }
}

function half(value: unknown, what: string): number | VarImpl {
  if (isPlainInt(value)) {
    if (value < 0 || value > 0xffff) {
      throw new EudslError(
        "EUDSL-RANGE",
        `${what}: half extent must be 0..65535, got ${value}`,
      );
    }
    return value;
  }
  return toVar(value as VarOperand);
}

function signed(a: number | VarImpl, b: number | VarImpl, sign: number): number | VarImpl {
  if (isPlainInt(a) && isPlainInt(b)) {
    return (a + sign * b) >>> 0;
  }
  const out = new Var(0);
  out.set(a);
  if (sign > 0) {
    out.iadd(b);
  } else {
    out.isub(b);
  }
  return out;
}

/** Read `(left, top)` for a raw location id or handle. */
export function getlocTl(location: LocationOperand): [VarImpl, VarImpl] {
  if (location instanceof Location) {
    return location.topLeft();
  }
  return new Location(location).topLeft();
}

export const getloc_tl = getlocTl;

function intOption(
  what: string,
  name: string,
  value: unknown,
  minimum: number,
  maximum: number,
): number {
  if (!isPlainInt(value)) {
    throw new EudslError(
      "EUDSL-TYPE",
      `${what}: ${name} must be a plain integer, got ${describeValue(value)}`,
    );
  }
  if (value < minimum || value > maximum) {
    throw new EudslError(
      "EUDSL-RANGE",
      `${what}: ${name} must be in [${minimum}, ${maximum}], got ${value}`,
    );
  }
  return value;
}

function playerOption(what: string, value: unknown): number {
  if (!isPlainInt(value)) {
    throw new EudslError(
      "EUDSL-TYPE",
      `${what}: player must be a plain integer, got ${describeValue(value)}`,
    );
  }
  if (!(PLAYER_VALUES as readonly number[]).includes(value)) {
    throw new EudslError(
      "EUDSL-RANGE",
      `${what}: player must be one of [${PLAYER_VALUES.join(", ")}], got ${value}`,
    );
  }
  return value;
}

/** Round half away from zero so negative coordinates mirror positive ones. */
function roundHalf(value: number): number {
  return value >= 0 ? Math.trunc(value + 0.5) : Math.trunc(value - 0.5);
}

function emitDot(
  baseAddr: number,
  location: number,
  unit: number,
  player: number,
  x: number,
  y: number,
): void {
  SetMemory(baseAddr, SetTo, x);
  SetMemory(baseAddr + 4, SetTo, y);
  SetMemory(baseAddr + 8, SetTo, x);
  SetMemory(baseAddr + 12, SetTo, y);
  CreateUnit(1, unit, location, player);
}

function emitPath(
  baseAddr: number,
  location: number,
  unit: number,
  player: number,
  points: ReadonlyArray<readonly [number, number]>,
): void {
  for (const [x, y] of points) {
    emitDot(baseAddr, location, unit, player, x, y);
  }
}

function closedPath(
  vertices: ReadonlyArray<readonly [number, number]>,
  dotsPerEdge: number,
): Array<[number, number]> {
  const points: Array<[number, number]> = [];
  const count = vertices.length;
  for (let index = 0; index < count; index += 1) {
    const [x0, y0] = vertices[index];
    const [x1, y1] = vertices[(index + 1) % count];
    for (let step = 0; step < dotsPerEdge; step += 1) {
      const t = step / dotsPerEdge;
      points.push([
        roundHalf(x0 + (x1 - x0) * t),
        roundHalf(y0 + (y1 - y0) * t),
      ]);
    }
  }
  return points;
}

function circlePoints(
  centerX: number,
  centerY: number,
  radius: number,
  dots: number,
): Array<[number, number]> {
  const points: Array<[number, number]> = [];
  for (let index = 0; index < dots; index += 1) {
    const angle = (2 * Math.PI * index) / dots;
    points.push([
      centerX + roundHalf(radius * Math.cos(angle)),
      centerY + roundHalf(radius * Math.sin(angle)),
    ]);
  }
  return points;
}

function constPoints(
  points: ReadonlyArray<readonly [number, number]>,
  what: string,
): Array<[number, number]> {
  let raw: ReadonlyArray<readonly [number, number]>;
  try {
    raw = [...points];
  } catch {
    throw new EudslError("EUDSL-TYPE", `${what}: points must be a sequence`);
  }
  return raw.map((point) => {
    if (!Array.isArray(point) || point.length !== 2) {
      throw new EudslError(
        "EUDSL-TYPE",
        `${what}: each point must be an (x, y) pair, got ${describeValue(point)}`,
      );
    }
    return [
      constCoord(point[0], `${what}: x`),
      constCoord(point[1], `${what}: y`),
    ];
  });
}

/** Create `dots` units on a build-time circle. */
export function drawCircle(
  location: LocationOperand,
  unit: number,
  player: number,
  centerX: number,
  centerY: number,
  opts: { radius?: number; dots?: number } = {},
): void {
  const what = "drawCircle()";
  const loc = constId(location, what);
  const u = intOption(what, "unit", unit, 0, 227);
  const p = playerOption(what, player);
  const cx = constCoord(centerX, `${what}: centerX`);
  const cy = constCoord(centerY, `${what}: centerY`);
  const r = intOption(what, "radius", opts.radius ?? 128, 0, 0xffff);
  const dots = intOption(what, "dots", opts.dots ?? 32, 1, 0xffff);
  emitPath(
    locationAddr(loc),
    loc,
    u,
    p,
    circlePoints(cx, cy, r, dots),
  );
}

export const draw_circle = drawCircle;

/** Create `dots` units along the inclusive segment `(x0, y0)-(x1, y1)`. */
export function drawLine(
  location: LocationOperand,
  unit: number,
  player: number,
  x0: number,
  y0: number,
  x1: number,
  y1: number,
  opts: { dots?: number } = {},
): void {
  const what = "drawLine()";
  const loc = constId(location, what);
  const u = intOption(what, "unit", unit, 0, 227);
  const p = playerOption(what, player);
  const sx = constCoord(x0, `${what}: x0`);
  const sy = constCoord(y0, `${what}: y0`);
  const ex = constCoord(x1, `${what}: x1`);
  const ey = constCoord(y1, `${what}: y1`);
  const dots = intOption(what, "dots", opts.dots ?? 32, 2, 0xffff);
  const points: Array<[number, number]> = [];
  for (let index = 0; index < dots; index += 1) {
    points.push([
      roundHalf(sx + ((ex - sx) * index) / (dots - 1)),
      roundHalf(sy + ((ey - sy) * index) / (dots - 1)),
    ]);
  }
  emitPath(locationAddr(loc), loc, u, p, points);
}

export const draw_line = drawLine;

/** Create units along the closed polygon through `points`. */
export function drawPolygon(
  location: LocationOperand,
  unit: number,
  player: number,
  points: ReadonlyArray<readonly [number, number]>,
  opts: { dotsPerEdge?: number } = {},
): void {
  const what = "drawPolygon()";
  const loc = constId(location, what);
  const u = intOption(what, "unit", unit, 0, 227);
  const p = playerOption(what, player);
  const dotsPerEdge = intOption(
    what,
    "dotsPerEdge",
    opts.dotsPerEdge ?? 8,
    1,
    0xffff,
  );
  const vertices = constPoints(points, what);
  if (vertices.length < 3) {
    throw new EudslError(
      "EUDSL-RANGE",
      `${what}: a polygon needs at least 3 points, got ${vertices.length}`,
    );
  }
  emitPath(
    locationAddr(loc),
    loc,
    u,
    p,
    closedPath(vertices, dotsPerEdge),
  );
}

export const draw_polygon = drawPolygon;

/** Create units along the regular star polygon `{vertices/density}`. */
export function drawStar(
  location: LocationOperand,
  unit: number,
  player: number,
  centerX: number,
  centerY: number,
  opts: {
    vertices?: number;
    density?: number;
    radius?: number;
    dotsPerEdge?: number;
  } = {},
): void {
  const what = "drawStar()";
  const loc = constId(location, what);
  const u = intOption(what, "unit", unit, 0, 227);
  const p = playerOption(what, player);
  const cx = constCoord(centerX, `${what}: centerX`);
  const cy = constCoord(centerY, `${what}: centerY`);
  const vertices = intOption(what, "vertices", opts.vertices ?? 5, 3, 0xffff);
  const density = intOption(
    what,
    "density",
    opts.density ?? 2,
    1,
    vertices - 1,
  );
  if (gcd(vertices, density) !== 1) {
    throw new EudslError(
      "EUDSL-RANGE",
      `${what}: density ${density} must be coprime with vertices ${vertices}`,
    );
  }
  const radius = intOption(what, "radius", opts.radius ?? 128, 0, 0xffff);
  const dotsPerEdge = intOption(
    what,
    "dotsPerEdge",
    opts.dotsPerEdge ?? 8,
    1,
    0xffff,
  );
  const corners: Array<[number, number]> = [];
  for (let index = 0; index < vertices; index += 1) {
    const angle = (2 * Math.PI * index * density) / vertices;
    corners.push([
      cx + roundHalf(radius * Math.cos(angle)),
      cy + roundHalf(radius * Math.sin(angle)),
    ]);
  }
  emitPath(
    locationAddr(loc),
    loc,
    u,
    p,
    closedPath(corners, dotsPerEdge),
  );
}

export const draw_star = drawStar;

function gcd(a: number, b: number): number {
  let x = a;
  let y = b;
  while (y !== 0) {
    const next = x % y;
    x = y;
    y = next;
  }
  return x;
}
