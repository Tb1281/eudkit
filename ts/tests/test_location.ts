/**
 * Typed location handles and build-time geometry (`T-ABS-LOCATION`).
 *
 * Mirrors `py/tests/test_location.py` item for item: the public surface, the
 * 5-dword record layout, the constant/runtime store split, and the exact
 * geometry act shape from §3/§4/§7.2. Exact static/executed cost pins live in
 * `crates/eudkit-cli/tests/location_runtime.rs`.
 */

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import {
  Add,
  EudslError,
  SetTo,
  Subtract,
  Var,
  VarImpl,
  buildModule,
} from "../index.ts";
import {
  LOCATION_COUNT,
  LOCATION_RECORD_DWORDS,
  Location,
  drawCircle,
  drawLine,
  drawPolygon,
  drawStar,
  draw_circle,
  draw_line,
  draw_polygon,
  draw_star,
  getlocTl,
  getloc_tl,
  locationAddr,
  locationEpd,
  location_addr,
  location_epd,
  type CoordOperand,
  type LocationOperand,
} from "../location.ts";
import { LOCATION_STRIDE_DWORDS, LOCATION_TABLE_ADDR } from "../camera.ts";
import { EPD } from "../view.ts";

// `tsc` pins §3.2 by assigning each export to its declared signature.
const _locationAddr: (location: number) => number = locationAddr;
const _location_addr: typeof locationAddr = location_addr;
const _locationEpd: (location: number) => number = locationEpd;
const _location_epd: typeof locationEpd = location_epd;
const _getlocTl: (location: LocationOperand) => [VarImpl, VarImpl] = getlocTl;
const _getloc_tl: typeof getlocTl = getloc_tl;
const _drawCircle: (
  location: LocationOperand,
  unit: number,
  player: number,
  centerX: number,
  centerY: number,
  opts?: { radius?: number; dots?: number },
) => void = drawCircle;
const _draw_circle: typeof drawCircle = draw_circle;
const _drawLine: (
  location: LocationOperand,
  unit: number,
  player: number,
  x0: number,
  y0: number,
  x1: number,
  y1: number,
  opts?: { dots?: number },
) => void = drawLine;
const _draw_line: typeof drawLine = draw_line;
const _drawPolygon: (
  location: LocationOperand,
  unit: number,
  player: number,
  points: ReadonlyArray<readonly [number, number]>,
  opts?: { dotsPerEdge?: number },
) => void = drawPolygon;
const _draw_polygon: typeof drawPolygon = draw_polygon;
const _drawStar: (
  location: LocationOperand,
  unit: number,
  player: number,
  centerX: number,
  centerY: number,
  opts?: {
    vertices?: number;
    density?: number;
    radius?: number;
    dotsPerEdge?: number;
  },
) => void = drawStar;
const _draw_star: typeof drawStar = draw_star;

const CONST_LOC = new Location(7);
const _setRect: (
  left: CoordOperand,
  top: CoordOperand,
  right: CoordOperand,
  bottom: CoordOperand,
) => void = CONST_LOC.setRect;
const _set_rect: typeof CONST_LOC.setRect = CONST_LOC.set_rect;
const _topLeft: () => [VarImpl, VarImpl] = CONST_LOC.topLeft;
const _top_left: typeof CONST_LOC.topLeft = CONST_LOC.top_left;
const _id: number | VarImpl = CONST_LOC.id;
const _epd: number | VarImpl = CONST_LOC.epd;
const _addr: number | null = CONST_LOC.addr;

void _locationAddr;
void _location_addr;
void _locationEpd;
void _location_epd;
void _getlocTl;
void _getloc_tl;
void _drawCircle;
void _draw_circle;
void _drawLine;
void _draw_line;
void _drawPolygon;
void _draw_polygon;
void _drawStar;
void _draw_star;
void _setRect;
void _set_rect;
void _topLeft;
void _top_left;
void _id;
void _epd;
void _addr;

const ALIASES: ReadonlyArray<readonly [unknown, unknown]> = [
  [location_addr, locationAddr],
  [location_epd, locationEpd],
  [getloc_tl, getlocTl],
  [draw_circle, drawCircle],
  [draw_line, drawLine],
  [draw_polygon, drawPolygon],
  [draw_star, drawStar],
];

type Module = any;

function moduleOf(body: () => void): Module {
  return JSON.parse(buildModule(body).toJson()).module;
}

function flatten(nodes: Module[]): Module[] {
  const out: Module[] = [];
  for (const node of nodes) {
    out.push(node);
    out.push(...flatten(node.body ?? []));
    out.push(...flatten(node.else_body ?? []));
  }
  return out;
}

function allNodes(module: Module): Module[] {
  return [
    ...flatten(module.stmts),
    ...(module.funcs ?? []).flatMap((func: Module) => flatten(func.body)),
  ];
}

function acts(module: Module): Module[] {
  const out: Module[] = [];
  for (const node of allNodes(module)) out.push(...(node.acts ?? []));
  return out;
}

function topActs(module: Module): Module[] {
  return module.stmts.flatMap((node: Module) => node.acts ?? []);
}

function dots(actList: Module[], baseAddr: number): Array<[number, number]> {
  const out: Array<[number, number]> = [];
  for (let index = 0; index < actList.length; index += 5) {
    const chunk = actList.slice(index, index + 5);
    assert.deepEqual(
      chunk.map((act) => act.acttype),
      [45, 45, 45, 45, 44],
    );
    assert.deepEqual(
      chunk.slice(0, 4).map((act) => act.dest),
      [EPD(baseAddr), EPD(baseAddr + 4), EPD(baseAddr + 8), EPD(baseAddr + 12)],
    );
    const x = chunk[0].value;
    const y = chunk[1].value;
    assert.deepEqual([chunk[2].value, chunk[3].value], [x, y]);
    assert.ok(chunk[4].mask >= 0 && chunk[4].mask < 256);
    out.push([x, y]);
  }
  return out;
}

function assertCode(fn: () => unknown, code: string): void {
  assert.throws(fn, (err: unknown) => err instanceof EudslError && err.code === code);
}

function roundHalf(value: number): number {
  return value >= 0 ? Math.trunc(value + 0.5) : Math.trunc(value - 0.5);
}

test("addresses follow the five-dword stride", () => {
  assert.equal(LOCATION_COUNT, 256);
  assert.equal(LOCATION_RECORD_DWORDS, LOCATION_STRIDE_DWORDS);
  assert.equal(LOCATION_RECORD_DWORDS, 5);
  assert.equal(locationAddr(0), LOCATION_TABLE_ADDR);
  assert.equal(locationAddr(1), LOCATION_TABLE_ADDR + 20);
  assert.equal(locationAddr(255), LOCATION_TABLE_ADDR + 20 * 255);
  for (const value of [0, 1, 7, 255]) {
    assert.equal(locationEpd(value), EPD(locationAddr(value)));
  }
});

test("domain errors use range and type", () => {
  assertCode(() => locationAddr(256), "EUDSL-RANGE");
  assertCode(() => locationAddr(-1), "EUDSL-RANGE");
  assertCode(() => locationAddr(true as unknown as number), "EUDSL-TYPE");
  assertCode(() => new Location(1.5), "EUDSL-TYPE");
  assertCode(() => new Location(256), "EUDSL-RANGE");
});

test("two-coordinate set mirrors into four dwords", () => {
  const module = moduleOf(() => new Location(7).set(100, 200));
  assert.deepEqual(
    acts(module).map((act) => [act.dest, act.modifier, act.value]),
    [
      [locationEpd(7), SetTo, 100],
      [locationEpd(7) + 1, SetTo, 200],
      [locationEpd(7) + 2, SetTo, 100],
      [locationEpd(7) + 3, SetTo, 200],
    ],
  );
  assert.equal(module.stmts.length, 1);
  assert.equal(module.stmts[0].kind, "trigger");
});

test("four-coordinate set writes each dword", () => {
  const actList = acts(moduleOf(() => new Location(7).set(1, 2, 3, 4)));
  assert.deepEqual(
    actList.map((act) => act.value),
    [1, 2, 3, 4],
  );
  assert.ok(actList.every((act) => act.modifier === SetTo));
});

test("wrong arity emits nothing", () => {
  const module = moduleOf(() => {
    new Location(7).set(100, 200);
    assertCode(() => new Location(7).set(1, 2, 3), "EUDSL-TYPE");
  });
  assert.equal(acts(module).length, 4);
});

test("setRect checks order for constants", () => {
  assertCode(() => new Location(7).setRect(10, 10, 5, 5), "EUDSL-RANGE");
  assert.deepEqual(
    acts(moduleOf(() => new Location(7).setRect(10, 20, 30, 40))).map(
      (act) => act.value,
    ),
    [10, 20, 30, 40],
  );
});

test("add skips zero constants", () => {
  const one = acts(moduleOf(() => new Location(7).add(0, 5)));
  assert.equal(one.length, 1);
  assert.deepEqual([one[0].dest, one[0].modifier, one[0].value], [
    locationEpd(7) + 1,
    Add,
    5,
  ]);

  const four = acts(moduleOf(() => new Location(7).add(1, 2, 3, 4)));
  assert.deepEqual(
    four.map((act) => [act.dest, act.modifier, act.value]),
    [
      [locationEpd(7), Add, 1],
      [locationEpd(7) + 1, Add, 2],
      [locationEpd(7) + 2, Add, 3],
      [locationEpd(7) + 3, Add, 4],
    ],
  );
});

test("dilate subtracts left/top and adds right/bottom", () => {
  const actList = acts(moduleOf(() => new Location(7).dilate(2, 3)));
  assert.deepEqual(
    actList.map((act) => [act.dest, act.modifier, act.value]),
    [
      [locationEpd(7), Subtract, 2],
      [locationEpd(7) + 1, Subtract, 3],
      [locationEpd(7) + 2, Add, 2],
      [locationEpd(7) + 3, Add, 3],
    ],
  );
});

test("move centers the record", () => {
  const actList = acts(moduleOf(() => new Location(7).move(500, 600, 10, 20)));
  assert.deepEqual(
    actList.map((act) => act.value),
    [490, 580, 510, 620],
  );
  assertCode(() => new Location(7).move(0, 0, -1, 0), "EUDSL-RANGE");
});

test("bounds reads four dwords in order", () => {
  const module = moduleOf(() => new Location(7).bounds());
  const readers = (module.funcs ?? []).map((func: Module) => {
    const players = flatten(func.body).flatMap((node) =>
      (node.conds ?? []).map((cond: Module) => cond.player),
    );
    return [...new Set<number>(players)];
  });
  assert.deepEqual(readers, [
    [locationEpd(7)],
    [locationEpd(7) + 1],
    [locationEpd(7) + 2],
    [locationEpd(7) + 3],
  ]);
  assert.equal(module.stmts.length, 4);
});

test("topLeft and getlocTl return left/top", () => {
  for (const build of [
    () => new Location(7).topLeft(),
    () => getlocTl(7),
  ]) {
    const module = moduleOf(build);
    const readers = (module.funcs ?? []).map((func: Module) => {
      const players = flatten(func.body).flatMap((node) =>
        (node.conds ?? []).map((cond: Module) => cond.player),
      );
      return [...new Set<number>(players)];
    });
    assert.deepEqual(readers, [[locationEpd(7)], [locationEpd(7) + 1]]);
  }
});

test("center emits two shift folds", () => {
  const module = moduleOf(() => new Location(7).center());
  const shifts = module.stmts.filter(
    (node: Module) =>
      node.kind === "trigger" &&
      (node.acts ?? []).some(
        (act: Module) => act.modifier === Subtract && act.value === 1,
      ),
  );
  assert.equal(shifts.length, 2);
});

test("runtime id routes through the memset loop and cannot valueOf", () => {
  const module = moduleOf(() => {
    const ident = Var(3);
    const loc = new Location(ident);
    assertCode(() => loc.valueOf(), "EUDSL-TYPE");
    loc.set(1, 2);
  });
  assert.ok(allNodes(module).some((node) => node.kind === "while"));
});

test("runtime coordinates store values, not addresses", () => {
  const module = moduleOf(() => {
    const x = Var(11);
    const y = Var(22);
    new Location(7).set(x, y);
  });
  assert.ok(allNodes(module).some((node) => node.kind === "while"));
  assert.ok(
    !topActs(module).some(
      (act) =>
        act.acttype === 45 &&
        act.dest >= locationEpd(7) &&
        act.dest < locationEpd(7) + 4,
    ),
  );
});

test("constant index protocols", () => {
  const loc = new Location(7);
  assert.equal(loc.valueOf(), 7);
  assert.equal(loc.id, 7);
  assert.equal(loc.addr, locationAddr(7));
  assert.equal(loc.epd, locationEpd(7));

  moduleOf(() => {
    assert.equal(new Location(Var(7)).addr, null);
  });
});

test("circle emits five acts per dot", () => {
  const module = moduleOf(() =>
    drawCircle(7, 1, 0, 1000, 1000, { radius: 100, dots: 8 }),
  );
  const actList = topActs(module);
  assert.equal(actList.length, 40);
  assert.equal(actList.filter((act) => act.acttype === 44).length, 8);
  const expected: Array<[number, number]> = [];
  for (let index = 0; index < 8; index += 1) {
    const angle = (2 * Math.PI * index) / 8;
    expected.push([
      1000 + roundHalf(100 * Math.cos(angle)),
      1000 + roundHalf(100 * Math.sin(angle)),
    ]);
  }
  assert.deepEqual(dots(actList, locationAddr(7)), expected);
});

test("line is inclusive", () => {
  const module = moduleOf(() => drawLine(7, 1, 0, 0, 0, 100, 100, { dots: 3 }));
  assert.deepEqual(dots(topActs(module), locationAddr(7)), [
    [0, 0],
    [50, 50],
    [100, 100],
  ]);
});

test("polygon does not duplicate shared vertices", () => {
  const module = moduleOf(() =>
    drawPolygon(7, 1, 0, [[0, 0], [100, 0], [50, 100]], { dotsPerEdge: 1 }),
  );
  const actList = topActs(module);
  assert.equal(actList.filter((act) => act.acttype === 44).length, 3);
  assert.deepEqual(dots(actList, locationAddr(7)), [
    [0, 0],
    [100, 0],
    [50, 100],
  ]);
});

test("star walks the star polygon", () => {
  const module = moduleOf(() =>
    drawStar(7, 1, 0, 512, 512, {
      vertices: 5,
      density: 2,
      radius: 64,
      dotsPerEdge: 1,
    }),
  );
  const actList = topActs(module);
  assert.equal(actList.filter((act) => act.acttype === 44).length, 5);
  // density 3 is coprime with 5 and stays legal; density 5 is excluded.
  moduleOf(() =>
    drawStar(7, 1, 0, 512, 512, {
      vertices: 5,
      density: 3,
      dotsPerEdge: 1,
    }),
  );
  assertCode(
    () => drawStar(7, 1, 0, 512, 512, { vertices: 5, density: 5 }),
    "EUDSL-RANGE",
  );
});

test("geometry rejects bad arguments before emitting", () => {
  const rejected: ReadonlyArray<readonly [() => void, string]> = [
    [() => drawCircle(7, 1, 0, 1000, 1000, { dots: 0 }), "EUDSL-RANGE"],
    [() => drawCircle(7, 1, 0, 1000, 1000, { radius: -1 }), "EUDSL-RANGE"],
    [() => drawCircle(7, 228, 0, 1000, 1000), "EUDSL-RANGE"],
    [() => drawCircle(7, 1, 12, 1000, 1000), "EUDSL-RANGE"],
    [() => drawCircle(7, 1, 0, 1000.5, 1000), "EUDSL-TYPE"],
    [() => drawPolygon(7, 1, 0, [[0, 0], [1, 1]]), "EUDSL-RANGE"],
    [() => drawLine(7, 1, 0, 0, 0, 1, 1, { dots: 1 }), "EUDSL-RANGE"],
    [() => drawStar(7, 1, 0, 0, 0, { vertices: 4, density: 2 }), "EUDSL-RANGE"],
  ];
  for (const [fn, code] of rejected) {
    const module = moduleOf(() => assertCode(fn, code));
    assert.deepEqual(module.stmts, []);
  }
});

test("every camelCase alias is the snake_case twin", () => {
  for (const [snake, camel] of ALIASES) assert.equal(snake, camel);
});

test("camelCase and snake_case methods emit the same HIR", () => {
  assert.deepEqual(
    moduleOf(() => new Location(7).setRect(1, 2, 3, 4)),
    moduleOf(() => new Location(7).set_rect(1, 2, 3, 4)),
  );
  assert.deepEqual(
    moduleOf(() => new Location(7).topLeft()),
    moduleOf(() => new Location(7).top_left()),
  );
});

test("no trigger-rewiring primitives", () => {
  const source = readFileSync(new URL("../location.ts", import.meta.url), "utf8");
  for (const forbidden of ["SetNextPtr", "Switch(", "Forward("]) {
    assert.ok(!source.includes(forbidden), `forbidden ${forbidden}`);
  }
});

test("geometry is the only Math.cos/Math.sin site and there is no Math.random", () => {
  const source = readFileSync(new URL("../location.ts", import.meta.url), "utf8");
  assert.ok(!source.includes("Math.random"));
  const geometryStart = source.indexOf("function circlePoints");
  assert.ok(geometryStart >= 0);
  for (const needle of ["Math.cos(", "Math.sin("]) {
    let index = source.indexOf(needle);
    while (index >= 0) {
      assert.ok(index > geometryStart, `${needle} outside geometry helpers`);
      index = source.indexOf(needle, index + 1);
    }
  }
});

test("camera table is reused, not redefined", () => {
  assert.equal(locationAddr(0), LOCATION_TABLE_ADDR);
  assert.equal(
    new Location(3).epd,
    EPD(LOCATION_TABLE_ADDR) + LOCATION_STRIDE_DWORDS * 3,
  );
});
