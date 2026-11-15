/**
 * Player name table helpers and the layout-A nickname matcher (`T-WP8`).
 *
 * The name record is 36 bytes (9 dwords) at `0x57EEE8`; nicknames are matched
 * as the exact UTF-8 bytes `FixedString` writes (`encodeUtf8` is the only
 * encoding authority). Each `(player, name)` arm is one trigger whose final
 * dword mask includes the NUL byte, so a prefix such as `"Armo"` can never
 * match `"Armoha"`.
 *
 * Red before the module exists: the `../player.ts` import fails.
 */

import { execFileSync } from "node:child_process";
import assert from "node:assert/strict";
import test from "node:test";

import {
  DeathsX,
  EudslError,
  Exactly,
  SetDeaths,
  SetTo,
  buildModule,
} from "../index.ts";
import {
  PLAYER_NAME_BYTES,
  PLAYER_NAME_EPDS,
  PLAYER_NAME_MAX_BYTES,
  PLAYER_NAME_TABLE,
  matchNames,
  match_names,
  playerNameAddr,
  playerNameEpd,
  player_name_addr,
  player_name_epd,
} from "../player.ts";
import { encodeUtf8 } from "../string.ts";
import { REPO_ROOT } from "./helpers.ts";

// `EPD(0x57EEE8)` wraps to this unsigned value; HIR stores the u32 form.
const PLAYER0_EPD = 4_294_955_745;

function stmts(mod: ReturnType<typeof buildModule>): any[] {
  return JSON.parse(mod.toJson()).module.stmts;
}

function chain(node: any): any[] {
  const out: any[] = [];
  while (node !== undefined && node !== null) {
    out.push(node);
    const other = node.else_body ?? [];
    node = other.length > 0 ? other[0] : null;
  }
  return out;
}

function condChains(mod: ReturnType<typeof buildModule>): any[][][] {
  return stmts(mod)
    .filter((node) => node.kind === "when")
    .map((node) => chain(node).map((arm) => arm.conds));
}

function callback(player: number, _name: string): void {
  SetDeaths(9000 + player, SetTo, 1, 0);
}

function build(names: readonly string[], players: readonly number[] = [0]) {
  return buildModule(() => {
    matchNames(names, callback, players);
  });
}

function assertCode(fn: () => unknown, code: string): void {
  assert.throws(fn, (err: unknown) => err instanceof EudslError && err.code === code);
}

test("player name constants", () => {
  assert.equal(PLAYER_NAME_TABLE, 0x57eee8);
  assert.equal(PLAYER_NAME_EPDS, 9);
  assert.equal(PLAYER_NAME_BYTES, 36);
  assert.equal(PLAYER_NAME_BYTES, 4 * PLAYER_NAME_EPDS);
  assert.equal(PLAYER_NAME_MAX_BYTES, 35);
});

test("playerNameEpd pins the unsigned EPD literals", () => {
  assert.equal(playerNameEpd(0), 4_294_955_745);
  assert.equal(playerNameEpd(3), 4_294_955_772);
  assert.equal(playerNameEpd(7), 4_294_955_808);
  assert.equal(playerNameEpd(11), 4_294_955_844);
  assert.equal(player_name_epd(3), playerNameEpd(3));
});

test("playerNameAddr uses the 36-byte record stride", () => {
  assert.equal(playerNameAddr(0), 0x57eee8);
  assert.equal(playerNameAddr(3), 0x57ef54);
  assert.equal(playerNameAddr(11), (PLAYER_NAME_TABLE + PLAYER_NAME_BYTES * 11) >>> 0);
  assert.equal(player_name_addr(3), playerNameAddr(3));
});

test("helper operand guards", () => {
  for (const bad of [12, -1]) {
    assertCode(() => playerNameEpd(bad), "EUDSL-RANGE");
    assertCode(() => playerNameAddr(bad), "EUDSL-RANGE");
  }
  for (const bad of [true, "3", 3.5, null]) {
    assertCode(() => playerNameEpd(bad as unknown as number), "EUDSL-TYPE");
    assertCode(() => playerNameAddr(bad as unknown as number), "EUDSL-TYPE");
  }
});

test("Armo emits two conditions, the second a NUL-only arm", () => {
  const chains = condChains(build(["Armo"]));
  assert.equal(chains.length, 1);
  const conds = chains[0][0];
  assert.equal(conds.length, 2);
  assert.equal(conds[0].player, PLAYER0_EPD);
  assert.equal(conds[0].amount, 0x6f6d7241); // "Armo" little-endian
  assert.equal(conds[0].locid, 0xffffffff);
  assert.equal(conds[0].comparison, Exactly);
  assert.equal(conds[1].player, (PLAYER0_EPD + 1) >>> 0);
  assert.equal(conds[1].amount, 0);
  assert.equal(conds[1].locid, 0xff);
  assert.equal(conds[1].comparison, Exactly);
});

test("a six-byte name masks through its NUL lane", () => {
  const conds = condChains(build(["Armoha"]))[0][0];
  // len(b)//4 + 1 == 1 whole word + the word holding the NUL lane.
  assert.equal(conds.length, 2);
  assert.equal(conds[0].locid, 0xffffffff);
  assert.equal(conds[0].amount, 0x6f6d7241); // "Armo"
  assert.equal(conds[1].locid, 0xffffff);
  assert.equal(conds[1].amount, 0x6168); // "ha", NUL at lane 2
  assert.equal(conds[1].player, (PLAYER0_EPD + 1) >>> 0);
});

test("a four-byte name still gets its NUL-only arm", () => {
  const conds = condChains(build(["Armo"]))[0][0];
  assert.equal(conds.length, 2);
  assert.equal(conds[conds.length - 1].amount, 0);
  assert.equal(conds[conds.length - 1].locid, 0xff);
});

test("a 35-byte name uses all nine words and a full final mask", () => {
  const conds = condChains(build(["A".repeat(35)]))[0][0];
  assert.equal(conds.length, 9);
  conds.forEach((cond, w) => {
    assert.equal(cond.player, (PLAYER0_EPD + w) >>> 0);
  });
  assert.equal(conds[conds.length - 1].locid, 0xffffffff);
  assert.equal(conds[conds.length - 1].amount, 0x00414141);
});

test("a Korean name is byte-pinned through encodeUtf8", () => {
  const text = "마린";
  assert.deepEqual(encodeUtf8(text, "test"), [0xeb, 0xa7, 0x88, 0xeb, 0xa6, 0xb0]);
  const conds = condChains(build([text]))[0][0];
  assert.equal(conds.length, 2);
  assert.equal(conds[0].amount, 0xeb88a7eb);
  assert.equal(conds[0].locid, 0xffffffff);
  // Bytes A6 B0 occupy lanes 0..1 of word 1; the NUL is lane 2.
  assert.equal(conds[1].amount, 0xb0a6);
  assert.equal(conds[1].locid, 0xffffff);
});

test("two players emit two independent chains in caller order", () => {
  const chains = condChains(build(["A", "B"], [2, 0]));
  assert.equal(chains.length, 2);
  assert.equal(chains[0].length, 2);
  assert.equal(chains[1].length, 2);
  assert.equal(chains[0][0][0].player, playerNameEpd(2));
  assert.equal(chains[1][0][0].player, playerNameEpd(0));
  assert.equal(chains[0][0][0].amount, 0x41);
  assert.equal(chains[1][1][0].amount, 0x42);
});

test("the default players list is a fresh [0..7] array per call", () => {
  const chains = condChains(
    buildModule(() => {
      matchNames(["A"], callback);
    }),
  );
  assert.equal(chains.length, 8);
  assert.equal(chains[0][0][0].player, playerNameEpd(0));
  assert.equal(chains[7][0][0].player, playerNameEpd(7));
});

test("names container and contents", () => {
  assertCode(() => build([]), "EUDSL-BOUNDS");
  assertCode(() => build("Armo" as unknown as string[]), "EUDSL-TYPE");
  assertCode(() => build(["Armo", 3] as unknown as string[]), "EUDSL-TYPE");
  assertCode(() => build(["Armo", "Armo"]), "EUDSL-BOUNDS");
});

test("name length bound accepts 35 and rejects 36 bytes", () => {
  assertCode(() => build(["A".repeat(36)]), "EUDSL-BOUNDS");
  build(["A".repeat(35)]);
});

test("players validation", () => {
  assertCode(() => build(["A"], []), "EUDSL-BOUNDS");
  assertCode(() => build(["A"], [0, 0]), "EUDSL-BOUNDS");
  assertCode(() => build(["A"], new Set([0, 1]) as unknown as number[]), "EUDSL-TYPE");
  assertCode(() => build(["A"], [true] as unknown as number[]), "EUDSL-TYPE");
  assertCode(() => build(["A"], [12]), "EUDSL-RANGE");
});

test("onMatch must be callable", () => {
  assertCode(
    () =>
      buildModule(() => {
        matchNames(["A"], null as unknown as (p: number, n: string) => void, [0]);
      }),
    "EUDSL-TYPE",
  );
});

test("outside buildModule is a scope error", () => {
  assertCode(() => matchNames(["A"], callback, [0]), "EUDSL-SCOPE");
  assertCode(() => match_names(["A"], callback, [0]), "EUDSL-SCOPE");
});

test("NUL and lone surrogates are rejected by encodeUtf8", () => {
  assertCode(() => build(["A\x00B"]), "EUDSL-TYPE");
  assertCode(() => build([String.fromCharCode(0xd800)]), "EUDSL-TYPE");
});

test("the matcher HIR is byte-identical to the Python frontend", () => {
  const ts = buildModule(() => {
    matchNames(
      ["Armo", "Armoha"],
      (player, name) => {
        SetDeaths(9000 + player, SetTo, name === "Armo" ? 1 : 2, 0);
      },
      [0, 1],
    );
  }).toJson();

  const script = `
from py.player import match_names
from py import build_module, SetDeaths, SetTo

def on_match(player, name):
    SetDeaths(9000 + player, SetTo, 1 if name == "Armo" else 2, 0)

def prog():
    match_names(["Armo", "Armoha"], on_match, players=[0, 1])

module = build_module(prog)
print(module.to_json(), end="")
`;
  const py = execFileSync("python3", ["-c", script], {
    cwd: REPO_ROOT,
    encoding: "utf8",
    env: { ...process.env, PYTHONPATH: "." },
  });
  assert.equal(ts, py);
});
