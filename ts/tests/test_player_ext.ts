/**
 * Player state, existence, and all-player broadcasts (`T-ABS-PLAYER`).
 *
 * Mirrors `py/tests/test_player_ext.py` item for item. The existing
 * name-matcher contract lives in `test_player.ts` and stays untouched; this
 * module pins the new surface from §3/§4 of the ticket:
 *
 * - `turbo` is one `SetMemory(0x6509A0, SetTo, 0)` action;
 * - `userPlayer` reads the low byte with an 8-bit read; `gameTick` keeps the
 *   shared 32-bit reader body;
 * - `playerExists` is the constant table predicate (two triggers) or an
 *   in-range-guarded condition probe without `CurrentPlayer`/`withEpd`;
 * - `forEachPlayer` saves/restores `CurrentPlayer` once per site and emits
 *   the body once (loop) or once per selected player (unrolled);
 * - `Player`/`PlayerField` mirror the `scdata/player.py` dword arrays and
 *   reject runtime values; `PVariable` is an eight-slot `FixedArray`;
 * - every broadcast helper is a gated `forEachPlayer` over one stock action
 *   and validates its raw location/unit arguments before emitting.
 *
 * Exact static/executed cost pins for these sites live in
 * `crates/eudkit-cli/tests/player_runtime.rs` so both frontends are measured
 * through the same lowering pipeline.
 */

import assert from "node:assert/strict";
import test from "node:test";

import {
  Add,
  AtLeast,
  AtMost,
  Deaths,
  EudslError,
  Exactly,
  Never,
  SetDeaths,
  SetTo,
  Subtract,
  Var,
  VarImpl,
  buildModule,
  when,
  type HirCond,
} from "../index.ts";
import {
  PLAYER_MAX_ID,
  PLAYER_SLOT_BYTES,
  PLAYER_SLOT_COUNT,
  PLAYER_TABLE_ADDR,
  PVariable,
  Player,
  PlayerField,
  TURBO_ADDR,
  centerViewAll,
  center_view_all,
  displayTextAll,
  display_text_all,
  forEachPlayer,
  for_each_player,
  gameTick,
  game_tick,
  isUser,
  is_user,
  minimapPingAll,
  minimap_ping_all,
  playWavAll,
  play_wav_all,
  playerExists,
  player_exists,
  setMissionObjectivesAll,
  set_mission_objectives_all,
  talkingPortraitAll,
  talking_portrait_all,
  turbo,
  userPlayer,
  user_player,
} from "../player.ts";
import { CP_ADDR } from "../cp.ts";
import { EPD, EPD_CURRENT_PLAYER } from "../view.ts";
import type { CondLike } from "../trigger.ts";

// `tsc` pins §3.2 by assigning each export to its declared signature.
const _turbo: () => void = turbo;
const _userPlayer: () => VarImpl = userPlayer;
const _userPlayerAlias: typeof userPlayer = user_player;
const _gameTick: () => VarImpl = gameTick;
const _gameTickAlias: typeof gameTick = game_tick;
const _isUser: (player: number | VarImpl) => HirCond = isUser;
const _isUserAlias: typeof isUser = is_user;
const _playerExists: (player: number | VarImpl) => VarImpl = playerExists;
const _playerExistsAlias: typeof playerExists = player_exists;
const _forEachPlayer: (
  body: (player: VarImpl) => unknown,
  opts?: { activeOnly?: boolean; players?: readonly number[] },
) => void = forEachPlayer;
const _forEachPlayerAlias: typeof forEachPlayer = for_each_player;
const _displayTextAll: (text: string) => void = displayTextAll;
const _displayTextAllAlias: typeof displayTextAll = display_text_all;
const _playWavAll: (wav: string) => void = playWavAll;
const _playWavAllAlias: typeof playWavAll = play_wav_all;
const _minimapPingAll: (location: number) => void = minimapPingAll;
const _minimapPingAllAlias: typeof minimapPingAll = minimap_ping_all;
const _centerViewAll: (location: number) => void = centerViewAll;
const _centerViewAllAlias: typeof centerViewAll = center_view_all;
const _setMissionObjectivesAll: (text: string) => void = setMissionObjectivesAll;
const _setMissionObjectivesAllAlias: typeof setMissionObjectivesAll =
  set_mission_objectives_all;
const _talkingPortraitAll: (unit: number, time: number) => void =
  talkingPortraitAll;
const _talkingPortraitAllAlias: typeof talkingPortraitAll = talking_portrait_all;
const _player: (player: number) => Player = (player) => new Player(player);
const _pvariable: (init?: number) => PVariable = (init) => new PVariable(init);

void _turbo;
void _userPlayer;
void _userPlayerAlias;
void _gameTick;
void _gameTickAlias;
void _isUser;
void _isUserAlias;
void _playerExists;
void _playerExistsAlias;
void _forEachPlayer;
void _forEachPlayerAlias;
void _displayTextAll;
void _displayTextAllAlias;
void _playWavAll;
void _playWavAllAlias;
void _minimapPingAll;
void _minimapPingAllAlias;
void _centerViewAll;
void _centerViewAllAlias;
void _setMissionObjectivesAll;
void _setMissionObjectivesAllAlias;
void _talkingPortraitAll;
void _talkingPortraitAllAlias;
void _player;
void _pvariable;

const _ne: (rhs: number) => CondLike = new Player(0).minerals.ne;
void _ne;

//: §4.2 — the `scdata/player.py` dword arrays, one element per player.
const FIELD_ADDRS: Readonly<Record<string, number>> = {
  minerals: 0x57f0f0,
  gas: 0x57f120,
  cumulative_gas: 0x57f150,
  cumulative_minerals: 0x57f180,
  zerg_control_available: 0x582144,
  zerg_control_used: 0x582174,
  zerg_control_max: 0x5821a4,
  terran_supply_available: 0x5821d4,
  terran_supply_used: 0x582204,
  terran_supply_max: 0x582234,
  protoss_psi_available: 0x582264,
  protoss_psi_used: 0x582294,
  protoss_psi_max: 0x5822c4,
  unit_score: 0x581e44,
  building_score: 0x582024,
  kill_score: 0x581f04,
  razing_score: 0x582054,
  custom_score: 0x5822f4,
};

const CAMEL_FIELDS: readonly (readonly [string, string])[] = [
  ["cumulativeMinerals", "cumulative_minerals"],
  ["cumulativeGas", "cumulative_gas"],
  ["zergControlAvailable", "zerg_control_available"],
  ["zergControlUsed", "zerg_control_used"],
  ["zergControlMax", "zerg_control_max"],
  ["terranSupplyAvailable", "terran_supply_available"],
  ["terranSupplyUsed", "terran_supply_used"],
  ["terranSupplyMax", "terran_supply_max"],
  ["protossPsiAvailable", "protoss_psi_available"],
  ["protossPsiUsed", "protoss_psi_used"],
  ["protossPsiMax", "protoss_psi_max"],
  ["unitScore", "unit_score"],
  ["buildingScore", "building_score"],
  ["killScore", "kill_score"],
  ["razingScore", "razing_score"],
  ["customScore", "custom_score"],
  ["mineral", "minerals"],
  ["cumulativeMineral", "cumulative_minerals"],
];

const NEW_EXPORTS = new Set([
  "PLAYER_TABLE_ADDR",
  "PLAYER_SLOT_BYTES",
  "PLAYER_SLOT_COUNT",
  "PLAYER_MAX_ID",
  "TURBO_ADDR",
  "turbo",
  "user_player",
  "userPlayer",
  "game_tick",
  "gameTick",
  "is_user",
  "isUser",
  "player_exists",
  "playerExists",
  "for_each_player",
  "forEachPlayer",
  "Player",
  "PlayerField",
  "PVariable",
  "display_text_all",
  "displayTextAll",
  "play_wav_all",
  "playWavAll",
  "minimap_ping_all",
  "minimapPingAll",
  "center_view_all",
  "centerViewAll",
  "set_mission_objectives_all",
  "setMissionObjectivesAll",
  "talking_portrait_all",
  "talkingPortraitAll",
]);

type Module = any;

function moduleOf(body: () => void): Module {
  return JSON.parse(buildModule(body).toJson()).module;
}

function stmts(body: () => void): Module[] {
  return moduleOf(body).stmts;
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

function conds(module: Module): Module[] {
  return allNodes(module).flatMap((node) => node.conds ?? []);
}

function acts(module: Module): Module[] {
  const out: Module[] = [];
  for (const node of allNodes(module)) {
    if (node.kind === "act") out.push(node);
    out.push(...(node.acts ?? []));
  }
  return out;
}

function assertCode(fn: () => unknown, code: string): void {
  assert.throws(fn, (err: unknown) => err instanceof EudslError && err.code === code);
}

test("documented constants", () => {
  assert.equal(PLAYER_TABLE_ADDR, 0x51a280);
  assert.equal(PLAYER_SLOT_BYTES, 12);
  assert.equal(PLAYER_SLOT_COUNT, 8);
  assert.equal(PLAYER_MAX_ID, 11);
  assert.equal(TURBO_ADDR, 0x6509a0);
});

test("every new export is present and aliases are the same callables", () => {
  const surface: Record<string, unknown> = {
    PLAYER_TABLE_ADDR,
    PLAYER_SLOT_BYTES,
    PLAYER_SLOT_COUNT,
    PLAYER_MAX_ID,
    TURBO_ADDR,
    turbo,
    user_player,
    userPlayer,
    game_tick,
    gameTick,
    is_user,
    isUser,
    player_exists,
    playerExists,
    for_each_player,
    forEachPlayer,
    Player,
    PlayerField,
    PVariable,
    display_text_all,
    displayTextAll,
    play_wav_all,
    playWavAll,
    minimap_ping_all,
    minimapPingAll,
    center_view_all,
    centerViewAll,
    set_mission_objectives_all,
    setMissionObjectivesAll,
    talking_portrait_all,
    talkingPortraitAll,
  };
  for (const name of NEW_EXPORTS) {
    assert.ok(name in surface, `missing export ${name}`);
  }

  const pairs: readonly (readonly [unknown, unknown])[] = [
    [userPlayer, user_player],
    [gameTick, game_tick],
    [isUser, is_user],
    [playerExists, player_exists],
    [forEachPlayer, for_each_player],
    [displayTextAll, display_text_all],
    [playWavAll, play_wav_all],
    [minimapPingAll, minimap_ping_all],
    [centerViewAll, center_view_all],
    [setMissionObjectivesAll, set_mission_objectives_all],
    [talkingPortraitAll, talking_portrait_all],
  ];
  for (const [camel, snake] of pairs) {
    assert.equal(camel, snake);
  }
});

test("PlayerField camelCase aliases share one handle", () => {
  const player = new Player(3);
  for (const [camel, snake] of CAMEL_FIELDS) {
    assert.equal(
      (player as unknown as Record<string, unknown>)[camel],
      (player as unknown as Record<string, unknown>)[snake],
    );
  }
});

test("turbo is one SetMemory action in one trigger", () => {
  const nodes = stmts(() => {
    turbo();
  });
  assert.equal(nodes.length, 1);
  assert.equal(nodes[0].kind, "trigger");
  assert.equal(nodes[0].acts.length, 1);
  const act = nodes[0].acts[0];
  assert.equal(act.acttype, 45);
  assert.equal(act.modifier, SetTo);
  assert.equal(act.value, 0);
  assert.equal(act.dest, EPD(TURBO_ADDR));
  assert.equal(EPD(TURBO_ADDR), 203151);
});

test("userPlayer reads the low byte and gameTick keeps its shared body", () => {
  const module = moduleOf(() => {
    userPlayer();
    userPlayer();
    gameTick();
  });
  assert.equal(module.funcs.length, 1);
  // `gameTick` still uses the shared full-dword reader.
  const tick = module.funcs[0];
  assert.equal(tick.body.length, 33);
  const tickConds = tick.body.flatMap((trigger: Module) => trigger.conds ?? []);
  assert.equal(tickConds.length, 32);
  assert.deepEqual(new Set(tickConds.map((cond: Module) => cond.condtype)), new Set([15]));
  assert.equal(tickConds[0].player, EPD(0x57f23c) >>> 0);

  // Each `userPlayer()` is an inline 8-bit read: one accumulator reset plus
  // one bit test per byte lane.
  const triggers = module.stmts.filter((node: Module) => node.kind === "trigger");
  assert.equal(triggers.length, 18);
  for (const offset of [0, 9]) {
    assert.deepEqual(triggers[offset].conds, []);
    const bitConds = triggers
      .slice(offset + 1, offset + 9)
      .map((trigger: Module) => trigger.conds[0]);
    assert.equal(bitConds.length, 8);
    assert.deepEqual(
      new Set(bitConds.map((cond: Module) => cond.player)),
      new Set([EPD(0x512684) >>> 0]),
    );
    assert.deepEqual(
      new Set(bitConds.map((cond: Module) => cond.amount)),
      new Set(Array.from({ length: 8 }, (_, bit) => 1 << bit)),
    );
  }
});

test("two userPlayer sites use independent read results", () => {
  const module = moduleOf(() => {
    userPlayer();
    userPlayer();
  });
  assert.equal(module.funcs, undefined);
  const triggers = module.stmts.filter((node: Module) => node.kind === "trigger");
  assert.equal(triggers.length, 18);
  const firstResult = triggers[0].acts[0].relocs[0].target_var;
  const secondResult = triggers[9].acts[0].relocs[0].target_var;
  assert.notEqual(firstResult, secondResult);
});

test("isUser constant is one condition against the shared read", () => {
  const module = moduleOf(() => {
    when(isUser(3), () => {
      SetDeaths(9000, SetTo, 1, 0);
    });
  });
  const top = module.stmts.filter((node: Module) => node.kind === "when");
  assert.equal(top.length, 1);
  const cond = top[0].conds[0];
  assert.equal(cond.condtype, 15);
  assert.equal(cond.comparison, Exactly);
  assert.equal(cond.amount, 3);
  assert.equal(cond.player, 0);
  const triggers = module.stmts.filter((node: Module) => node.kind === "trigger");
  assert.equal(triggers.length, 9);
  const readResult = triggers[0].acts[0].relocs[0].target_var;
  assert.equal(cond.relocs[0].target_var, readResult);
});

test("isUser runtime is a var equality condition", () => {
  const module = moduleOf(() => {
    when(isUser(Var(4)), () => {
      SetDeaths(9000, SetTo, 1, 0);
    });
  });
  const top = module.stmts.filter((node: Module) => node.kind === "when");
  const cond = top[0].conds[0];
  assert.equal(cond.condtype, 15);
  assert.equal(cond.comparison, Exactly);
  assert.ok(cond.relocs.length > 0);
});

test("isUser rejects non-operands", () => {
  for (const bad of [true, "3", 3.5, null]) {
    assertCode(
      () =>
        buildModule(() => {
          when(isUser(bad as unknown as number), () => {});
        }),
      "EUDSL-TYPE",
    );
  }
});

test("playerExists constant emits two triggers and the table condition", () => {
  for (let player = 0; player < PLAYER_SLOT_COUNT; player += 1) {
    const module = moduleOf(() => {
      playerExists(player);
    });
    const nodes = module.stmts;
    assert.deepEqual(
      nodes.map((node: Module) => node.kind),
      ["trigger", "when"],
    );
    const base = PLAYER_TABLE_ADDR + PLAYER_SLOT_BYTES * player;
    const cond = nodes[1].conds[0];
    assert.equal(cond.player, EPD(base + 8) >>> 0);
    assert.equal(cond.amount, ~(base + 4) >>> 0);
    assert.equal(cond.comparison, Exactly);
    assert.equal(cond.condtype, 15);

    const setter = nodes[0].acts[0];
    assert.equal(setter.acttype, 45);
    assert.equal(setter.modifier, SetTo);
    assert.equal(setter.value, 1);
    const cleared = nodes[1].body[0];
    assert.equal(cleared.value, 0);
    assert.equal(setter.relocs[0].target_var, cleared.relocs[0].target_var);
  }
});

test("playerExists constant guards the slot range and operand type", () => {
  for (const bad of [8, -1, 9]) {
    assertCode(
      () =>
        buildModule(() => {
          playerExists(bad);
        }),
      "EUDSL-RANGE",
    );
  }
  for (const bad of [true, "0", null, 0.5]) {
    assertCode(
      () =>
        buildModule(() => {
          playerExists(bad as unknown as number);
        }),
      "EUDSL-TYPE",
    );
  }
});

test("playerExists runtime is a condition probe without a CP read", () => {
  const module = moduleOf(() => {
    when(playerExists(Var(3)).eq(1), () => {});
  });
  // One in-range guard plus the caller's own condition; no CP reader body.
  assert.equal(module.funcs, undefined);
  const nodes = module.stmts;
  assert.deepEqual(
    nodes.map((node: Module) => node.kind),
    ["when", "when"],
  );

  const guard = nodes[0];
  assert.equal(guard.conds[0].amount, PLAYER_SLOT_COUNT - 1);
  assert.equal(guard.conds[0].comparison, AtMost);

  const body = guard.body;
  assert.equal(body[0].value, 1);
  assert.equal(body[0].modifier, SetTo);
  const outVar = body[0].relocs[0].target_var;
  assert.equal(nodes[1].conds[0].relocs[0].target_var, outVar);

  const probe = body.find((node: Module) => node.kind === "trigger");
  assert.equal(probe.conds[0].condtype, 15);
  assert.equal(probe.conds[0].comparison, Exactly);
  assert.equal(probe.acts[0].value, 0);
  assert.equal(probe.acts[0].relocs[0].target_var, outVar);

  const stores = body.filter((node: Module) => node.kind === "varstore");
  assert.equal(stores.length, 2);
  [12, 16].forEach((fieldOffset, index) => {
    assert.equal(stores[index].dst_trigger, probe.id);
    assert.equal(stores[index].dst_offset, fieldOffset);
  });

  const all = allNodes(module);
  for (const node of all) {
    for (const reloc of node.relocs ?? []) {
      assert.ok(!("target_trigger" in reloc));
    }
    for (const cond of node.conds ?? []) {
      for (const reloc of cond.relocs ?? []) {
        assert.ok(!("target_trigger" in reloc));
      }
    }
    for (const act of node.acts ?? []) {
      for (const reloc of act.relocs ?? []) {
        assert.ok(!("target_trigger" in reloc));
      }
    }
  }

  const cpStores = all.filter(
    (node) => node.kind === "varstore" && node.dst_addr === CP_ADDR,
  );
  assert.equal(cpStores.length, 0);
});

test("playerExists runtime rejects non-operands", () => {
  for (const bad of [true, "1", null, 1.5]) {
    assertCode(
      () =>
        buildModule(() => {
          when(playerExists(bad as unknown as number).eq(1), () => {});
        }),
      "EUDSL-TYPE",
    );
  }
});

test("forEachPlayer loop emits the body once and restores CP unconditionally", () => {
  const module = moduleOf(() => {
    forEachPlayer((player) => {
      SetDeaths(9000, SetTo, player, 0);
    });
  });
  const nodes = module.stmts;
  assert.deepEqual(
    nodes.map((node: Module) => node.kind),
    ["call", "while", "varstore"],
  );

  const loop = nodes[1];
  assert.equal(loop.conds[0].amount, PLAYER_SLOT_COUNT - 1);
  assert.equal(loop.conds[0].comparison, AtMost);

  const markers = acts(module).filter((act) => act.dest === 9000);
  assert.equal(markers.length, 1);

  const currentId = loop.conds[0].relocs[0].target_var;
  const loopNodes = flatten(loop.body);
  const seeds = loopNodes.filter(
    (node) =>
      node.kind === "varstore" &&
      node.dst_addr === CP_ADDR &&
      node.src_var === currentId,
  );
  assert.equal(seeds.length, 1);

  const restore = nodes[2];
  assert.equal(restore.dst_addr, CP_ADDR);
  assert.equal(restore.src_var, nodes[0].dst);
});

test("forEachPlayer unrolled mode gates once per selected player", () => {
  const module = moduleOf(() => {
    forEachPlayer(
      (player) => {
        SetDeaths(9000, SetTo, player, 0);
      },
      { players: [0, 2] },
    );
  });
  const nodes = module.stmts;
  assert.deepEqual(
    nodes.map((node: Module) => node.kind),
    [
      "call",
      "trigger",
      "when",
      "when",
      "trigger",
      "when",
      "when",
      "varstore",
    ],
  );
  [0, 2].forEach((player, index) => {
    const guard = nodes[2 + 3 * index];
    const run = nodes[3 + 3 * index];
    const base = PLAYER_TABLE_ADDR + PLAYER_SLOT_BYTES * player;
    const cond = guard.conds[0];
    assert.equal(cond.player, EPD(base + 8) >>> 0);
    assert.equal(cond.amount, ~(base + 4) >>> 0);

    const bodyActs = flatten(run.body).filter((item) => item.kind === "act");
    assert.equal(bodyActs[0].dest, EPD_CURRENT_PLAYER);
    assert.equal(bodyActs[0].value, player);
    assert.equal(bodyActs[1].dest, 9000);
  });
  assert.equal(nodes[7].src_var, nodes[0].dst);
});

test("forEachPlayer unrolled ungated mode has no gate", () => {
  const module = moduleOf(() => {
    forEachPlayer(
      (player) => {
        SetDeaths(9000, SetTo, player, 0);
      },
      { activeOnly: false, players: [0, 2] },
    );
  });
  const nodes = module.stmts;
  assert.deepEqual(
    nodes.map((node: Module) => node.kind),
    ["call", "trigger", "varstore"],
  );
  const triggerActs = nodes[1].acts;
  assert.deepEqual(
    triggerActs.map((act: Module) => act.dest),
    [EPD_CURRENT_PLAYER, 9000, EPD_CURRENT_PLAYER, 9000],
  );
  assert.deepEqual(
    triggerActs.slice(0, 2).map((act: Module) => act.value),
    [0, 0],
  );
});

test("forEachPlayer loop ungated mode has no playerExists read", () => {
  const module = moduleOf(() => {
    forEachPlayer(
      (player) => {
        SetDeaths(9000, SetTo, player, 0);
      },
      { activeOnly: false },
    );
  });
  assert.equal(module.funcs.length, 1);
  const funcPlayers = new Set(
    module.funcs.flatMap((func: Module) =>
      flatten(func.body).flatMap((node: Module) =>
        (node.conds ?? []).map((cond: Module) => cond.player),
      ),
    ),
  );
  assert.deepEqual(funcPlayers, new Set([EPD_CURRENT_PLAYER]));
});

test("forEachPlayer validation leaves no partial emission", () => {
  const cases: readonly (readonly [() => unknown, string])[] = [
    [
      () => forEachPlayer(() => {}, { players: new Set([0, 1]) as unknown as number[] }),
      "EUDSL-TYPE",
    ],
    [
      () => forEachPlayer(() => {}, { players: "0" as unknown as number[] }),
      "EUDSL-TYPE",
    ],
    [() => forEachPlayer(() => {}, { players: [0, 0] }), "EUDSL-BOUNDS"],
    [() => forEachPlayer(() => {}, { players: [] }), "EUDSL-BOUNDS"],
    [() => forEachPlayer(() => {}, { players: [12] }), "EUDSL-RANGE"],
    [
      () => forEachPlayer(() => {}, { players: [true] as unknown as number[] }),
      "EUDSL-TYPE",
    ],
    [
      () => forEachPlayer(null as unknown as (player: VarImpl) => unknown),
      "EUDSL-TYPE",
    ],
  ];

  const module = moduleOf(() => {
    for (const [call, expected] of cases) {
      assertCode(call, expected);
    }
    SetDeaths(9000, SetTo, 1, 0);
  });
  // Only the marker trigger survives: every rejected call emitted nothing.
  assert.deepEqual(
    module.stmts.map((node: Module) => node.kind),
    ["trigger"],
  );
});

test("every Player field maps to the documented address", () => {
  for (const player of [0, 7, 11]) {
    const handles = new Player(player);
    assert.equal(handles.index, player);
    for (const [name, addr] of Object.entries(FIELD_ADDRS)) {
      const field = (handles as unknown as Record<string, PlayerField>)[name];
      assert.ok(field instanceof PlayerField, name);
      assert.equal(field.player, player);
      assert.equal(field.baseAddr, addr);
      assert.equal(field.addr, (addr + 4 * player) >>> 0);
      assert.equal(field.epd, (EPD(addr) + player) >>> 0);
    }
  }
});

test("Player index guards", () => {
  for (const bad of [12, -1]) {
    assertCode(
      () =>
        buildModule(() => {
          new Player(bad);
        }),
      "EUDSL-RANGE",
    );
  }
  for (const bad of [true, "3", null, 3.5]) {
    assertCode(
      () =>
        buildModule(() => {
          new Player(bad as unknown as number);
        }),
      "EUDSL-TYPE",
    );
  }
  for (const player of [0, 7, 8, 11]) {
    assert.equal(new Player(player).index, player);
  }
});

test("PlayerField set/iadd/isub emit one Deaths act", () => {
  const expectedEpd = (EPD(FIELD_ADDRS.minerals) + 3) >>> 0;
  const cases: readonly (readonly [string, number, number])[] = [
    ["set", SetTo, 500],
    ["iadd", Add, 3],
    ["isub", Subtract, 2],
  ];
  for (const [method, modifier, value] of cases) {
    const module = moduleOf(() => {
      (new Player(3).minerals as unknown as Record<string, (v: number) => void>)[
        method
      ](value);
    });
    const nodes = module.stmts;
    assert.equal(nodes.length, 1);
    assert.equal(nodes[0].kind, "trigger");
    const act = nodes[0].acts[0];
    assert.equal(act.acttype, 45);
    assert.equal(act.modifier, modifier);
    assert.equal(act.value, value);
    assert.equal(act.dest, expectedEpd);
  }
});

test("PlayerField comparisons emit one condition", () => {
  const expectedEpd = (EPD(FIELD_ADDRS.minerals) + 3) >>> 0;
  const cases: readonly (readonly [string, number, number, number])[] = [
    ["eq", Exactly, 777, 777],
    ["ge", AtLeast, 5, 5],
    ["le", AtMost, 5, 5],
    ["gt", AtLeast, 5, 6],
    ["lt", AtMost, 5, 4],
  ];
  for (const [method, comparison, rhs, amount] of cases) {
    const module = moduleOf(() => {
      const cond = (new Player(3).minerals as unknown as Record<
        string,
        (v: number) => HirCond
      >)[method](rhs);
      when(cond, () => {});
    });
    const cond = module.stmts.find((node: Module) => node.kind === "when").conds[0];
    assert.equal(cond.condtype, 15);
    assert.equal(cond.comparison, comparison);
    assert.equal(cond.amount, amount);
    assert.equal(cond.player, expectedEpd);
  }
});

test("PlayerField ne delegates and extreme gt/lt become Never", () => {
  const neModule = moduleOf(() => {
    when(new Player(3).minerals.ne(0), () => {});
  });
  const neCond = neModule.stmts.find((node: Module) => node.kind === "when").conds[0];
  assert.equal(neCond.comparison, AtLeast);
  assert.equal(neCond.amount, 1);

  for (const [value, method] of [
    [0xffffffff, "gt"],
    [0, "lt"],
  ] as const) {
    const module = moduleOf(() => {
      const cond = (new Player(3).minerals as unknown as Record<
        string,
        (v: number) => HirCond
      >)[method](value);
      when(cond, () => {});
    });
    const cond = module.stmts.find((node: Module) => node.kind === "when").conds[0];
    assert.equal(cond.condtype, 23);
  }
});

test("PlayerField runtime values are type errors", () => {
  for (const method of ["set", "iadd", "isub"]) {
    assertCode(
      () =>
        buildModule(() => {
          (new Player(3).minerals as unknown as Record<string, (v: unknown) => void>)[
            method
          ](Var(5));
        }),
      "EUDSL-TYPE",
    );
  }
  for (const method of ["eq", "ne", "ge", "le", "gt", "lt"]) {
    assertCode(
      () =>
        buildModule(() => {
          const cond = (new Player(3).minerals as unknown as Record<
            string,
            (v: unknown) => unknown
          >)[method](Var(5));
          when(cond as HirCond, () => {});
        }),
      "EUDSL-TYPE",
    );
  }
  assertCode(
    () =>
      buildModule(() => {
        new Player(3).minerals.set(true as unknown as number);
      }),
    "EUDSL-TYPE",
  );
});

test("Player.exists delegates to playerExists", () => {
  assert.deepEqual(
    moduleOf(() => {
      new Player(3).exists();
    }),
    moduleOf(() => {
      playerExists(3);
    }),
  );
});

test("PVariable storage is an eight-slot FixedArray", () => {
  buildModule(() => {
    const variable = new PVariable();
    assert.equal(variable.storage.length, PLAYER_SLOT_COUNT);
    assert.equal(variable.storage.vars.length, PLAYER_SLOT_COUNT);
  });
});

test("PVariable constant get and set", () => {
  let held!: VarImpl;
  let storageSlot!: VarImpl;
  const module = moduleOf(() => {
    const variable = new PVariable();
    variable.set(3, 9);
    held = variable.get(3);
    storageSlot = variable.storage.vars[3];
  });
  assert.ok(held instanceof VarImpl);
  assert.equal(held, storageSlot);
  const emitted = acts(module);
  assert.equal(emitted.length, 1);
  assert.equal(emitted[0].value, 9);
  assert.equal(emitted[0].modifier, SetTo);
});

test("PVariable init zero emits no statements", () => {
  const module = moduleOf(() => {
    new PVariable();
  });
  assert.deepEqual(module.stmts, []);
  assert.equal(module.vars.length, PLAYER_SLOT_COUNT);
});

test("PVariable init nonzero fills every slot", () => {
  const module = moduleOf(() => {
    new PVariable(5);
  });
  const nodes = module.stmts;
  assert.equal(nodes.length, 1);
  const slotActs = nodes[0].acts;
  assert.equal(slotActs.length, PLAYER_SLOT_COUNT);
  assert.deepEqual(new Set(slotActs.map((act: Module) => act.value)), new Set([5]));
  assert.deepEqual(
    new Set(slotActs.map((act: Module) => act.modifier)),
    new Set([SetTo]),
  );
  assert.deepEqual(
    slotActs.map((act: Module) => act.relocs[0].target_var).sort((a: number, b: number) => a - b),
    Array.from({ length: PLAYER_SLOT_COUNT }, (_, index) => index),
  );
});

test("PVariable constant index guards", () => {
  for (const bad of [8, -1]) {
    assertCode(
      () =>
        buildModule(() => {
          new PVariable().get(bad);
        }),
      "EUDSL-RANGE",
    );
    assertCode(
      () =>
        buildModule(() => {
          new PVariable().set(bad, 0);
        }),
      "EUDSL-RANGE",
    );
  }
  for (const bad of [true, "0", null]) {
    assertCode(
      () =>
        buildModule(() => {
          new PVariable().get(bad as unknown as number);
        }),
      "EUDSL-TYPE",
    );
  }
});

test("PVariable runtime index and value paths", () => {
  let held!: VarImpl;
  const module = moduleOf(() => {
    const variable = new PVariable();
    const index = Var(2);
    held = variable.get(index);
    variable.set(index, 9);
    variable.set(index, Var(7));
  });
  assert.ok(held instanceof VarImpl);
  assert.ok(conds(module).length > 0);
  assert.ok(acts(module).length > 0);
});

test("PVariable init and value guards", () => {
  assertCode(
    () =>
      buildModule(() => {
        new PVariable(true as unknown as number);
      }),
    "EUDSL-TYPE",
  );
  assertCode(
    () =>
      buildModule(() => {
        new PVariable().set(0, true as unknown as number);
      }),
    "EUDSL-TYPE",
  );
  assertCode(
    () =>
      buildModule(() => {
        new PVariable().set(0, "9" as unknown as number);
      }),
    "EUDSL-TYPE",
  );
});

const ACTTYPE_NAMES: Readonly<Record<number, string>> = {
  8: "PlayWAV",
  9: "DisplayText",
  10: "CenterView",
  12: "SetMissionObjectives",
  28: "MinimapPing",
  29: "TalkingPortrait",
};

function loopModule(call: () => void): Module {
  const module = moduleOf(call);
  const whileStmts = module.stmts.filter((node: Module) => node.kind === "while");
  assert.equal(whileStmts.length, 1);
  assert.equal(whileStmts[0].conds[0].amount, PLAYER_SLOT_COUNT - 1);
  assert.equal(module.stmts.at(-1).dst_addr, CP_ADDR);
  return module;
}

test("each broadcast helper emits one loop over the stock action", () => {
  const cases: readonly (readonly [() => void, number, Record<string, unknown>])[] = [
    [() => displayTextAll("hello"), 9, { text: "hello", flags: 4 }],
    [() => playWavAll("sound"), 8, { wav: "sound", flags: 4 }],
    [() => minimapPingAll(3), 28, { mask: 3, flags: 4 }],
    [() => centerViewAll(4), 10, { mask: 4, flags: 4 }],
    [() => setMissionObjectivesAll("obj"), 12, { text: "obj", flags: 4 }],
    [() => talkingPortraitAll(5, 100), 29, { unit: 5, time: 100, flags: 20 }],
  ];
  for (const [call, acttype, expected] of cases) {
    const module = loopModule(call);
    const matches = acts(module).filter((act) => act.acttype === acttype);
    assert.equal(matches.length, 1, ACTTYPE_NAMES[acttype]);
    for (const [key, value] of Object.entries(expected)) {
      assert.equal(matches[0][key], value);
    }
  }
});

test("broadcast bad arguments are rejected before the loop", () => {
  const cases: readonly (readonly [() => unknown, string])[] = [
    [() => displayTextAll(3 as unknown as string), "EUDSL-TYPE"],
    [() => playWavAll(3 as unknown as string), "EUDSL-TYPE"],
    [() => setMissionObjectivesAll(3 as unknown as string), "EUDSL-TYPE"],
    [() => minimapPingAll(256), "EUDSL-RANGE"],
    [() => minimapPingAll(-1), "EUDSL-RANGE"],
    [() => minimapPingAll(true as unknown as number), "EUDSL-TYPE"],
    [() => centerViewAll(256), "EUDSL-RANGE"],
    [() => talkingPortraitAll(999, 0), "EUDSL-RANGE"],
    [() => talkingPortraitAll(0, -1), "EUDSL-RANGE"],
    [() => talkingPortraitAll(0, 0x1_0000_0000), "EUDSL-RANGE"],
  ];

  const module = moduleOf(() => {
    for (const [call, expected] of cases) {
      assertCode(call, expected);
    }
    SetDeaths(9000, SetTo, 1, 0);
  });
  assert.deepEqual(
    module.stmts.map((node: Module) => node.kind),
    ["trigger"],
  );
});
