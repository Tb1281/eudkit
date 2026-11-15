"""Player name table helpers and the layout-A nickname matcher (`T-WP8`).

The name record is 36 bytes (9 dwords) at `0x57EEE8`; nicknames are matched as
the exact UTF-8 bytes `FixedString` writes (`encode_utf8` is the only encoding
authority).  Each `(player, name)` arm is one trigger whose final dword mask
includes the NUL byte, so a prefix such as `"Armo"` can never match `"Armoha"`.

Red before the module exists: `ImportError: cannot import name 'match_names'`.
"""

from __future__ import annotations

import json
import unittest
from typing import Any

from py import (
    DeathsX,
    EudslError,
    Exactly,
    SetDeaths,
    SetTo,
    build_module,
)
from py.player import (
    PLAYER_NAME_BYTES,
    PLAYER_NAME_EPDS,
    PLAYER_NAME_MAX_BYTES,
    PLAYER_NAME_TABLE,
    match_names,
    player_name_addr,
    player_name_epd,
)
from py.string import encode_utf8

# `EPD(0x57EEE8)` wraps to this unsigned value; HIR stores the u32 form.
PLAYER0_EPD = 4294955745


def _stmts(module: Any) -> list[dict[str, Any]]:
    return json.loads(module.to_json())["module"]["stmts"]


def _chain(node: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    while node is not None:
        out.append(node)
        else_body = node.get("else_body") or []
        node = else_body[0] if else_body else None
    return out


def _conds(module: Any) -> list[list[dict[str, Any]]]:
    """Conditions of every arm, grouped per top-level player chain."""
    return [
        [arm["conds"] for arm in _chain(node)]
        for node in _stmts(module)
        if node.get("kind") == "when"
    ]


def _callback(player: int, name: str) -> None:
    SetDeaths(9000 + player, SetTo, 1, 0)


def _build(names: list[str], players: list[int] | range) -> Any:
    def prog() -> None:
        match_names(names, _callback, players)

    return build_module(prog)


class PlayerNameTableTest(unittest.TestCase):
    def test_constants(self) -> None:
        self.assertEqual(PLAYER_NAME_TABLE, 0x57EEE8)
        self.assertEqual(PLAYER_NAME_EPDS, 9)
        self.assertEqual(PLAYER_NAME_BYTES, 36)
        self.assertEqual(PLAYER_NAME_BYTES, 4 * PLAYER_NAME_EPDS)
        self.assertEqual(PLAYER_NAME_MAX_BYTES, 35)

    def test_epd_literals(self) -> None:
        self.assertEqual(player_name_epd(0), 4294955745)
        self.assertEqual(player_name_epd(3), 4294955772)
        self.assertEqual(player_name_epd(7), 4294955808)
        self.assertEqual(player_name_epd(11), 4294955844)

    def test_addr_arithmetic(self) -> None:
        self.assertEqual(player_name_addr(0), 0x57EEE8)
        self.assertEqual(player_name_addr(3), 0x57EF54)
        self.assertEqual(
            player_name_addr(11),
            (PLAYER_NAME_TABLE + PLAYER_NAME_BYTES * 11) & 0xFFFFFFFF,
        )

    def _code(self, fn: Any) -> str:
        with self.assertRaises(EudslError) as caught:
            fn()
        return caught.exception.code

    def test_helper_guards(self) -> None:
        for bad in (12, -1):
            self.assertEqual(self._code(lambda bad=bad: player_name_epd(bad)), "EUDSL-RANGE")
            self.assertEqual(self._code(lambda bad=bad: player_name_addr(bad)), "EUDSL-RANGE")
        for bad in (True, "3", 3.0, None):
            self.assertEqual(self._code(lambda bad=bad: player_name_epd(bad)), "EUDSL-TYPE")
            self.assertEqual(self._code(lambda bad=bad: player_name_addr(bad)), "EUDSL-TYPE")


class MatchNamesShapeTest(unittest.TestCase):
    def test_armo_emits_two_conditions_with_nul_arm(self) -> None:
        chains = _conds(_build(["Armo"], [0]))
        self.assertEqual(len(chains), 1)
        conds = chains[0][0]
        self.assertEqual(len(conds), 2)
        self.assertEqual(conds[0]["player"], PLAYER0_EPD)
        self.assertEqual(conds[0]["amount"], 0x6F6D7241)  # "Armo" little-endian
        self.assertEqual(conds[0]["locid"], 0xFFFFFFFF)
        self.assertEqual(conds[0]["comparison"], Exactly)
        self.assertEqual(conds[1]["player"], PLAYER0_EPD + 1)
        self.assertEqual(conds[1]["amount"], 0)
        self.assertEqual(conds[1]["locid"], 0xFF)
        self.assertEqual(conds[1]["comparison"], Exactly)

    def test_six_byte_name_has_trailing_nul_lane_mask(self) -> None:
        chains = _conds(_build(["Armoha"], [0]))
        conds = chains[0][0]
        # len(b)//4 + 1 == 1 whole word + the word holding the NUL lane.
        self.assertEqual(len(conds), 2)
        self.assertEqual(conds[0]["locid"], 0xFFFFFFFF)
        self.assertEqual(conds[0]["amount"], 0x6F6D7241)  # "Armo"
        self.assertEqual(conds[1]["locid"], 0xFFFFFF)
        self.assertEqual(conds[1]["amount"], 0x6168)  # "ha", NUL at lane 2
        self.assertEqual(conds[1]["player"], PLAYER0_EPD + 1)

    def test_four_byte_name_still_gets_nul_only_arm(self) -> None:
        chains = _conds(_build(["Armo"], [0]))
        conds = chains[0][0]
        self.assertEqual(len(conds), 2)
        self.assertEqual(conds[-1]["amount"], 0)
        self.assertEqual(conds[-1]["locid"], 0xFF)

    def test_35_byte_name_uses_all_nine_words(self) -> None:
        name = "A" * 35
        chains = _conds(_build([name], [0]))
        conds = chains[0][0]
        self.assertEqual(len(conds), 9)
        for w, cond in enumerate(conds):
            self.assertEqual(cond["player"], PLAYER0_EPD + w)
        # The NUL sits at byte 35 (lane 3 of word 8), so the last mask is full.
        self.assertEqual(conds[-1]["locid"], 0xFFFFFFFF)
        self.assertEqual(conds[-1]["amount"], 0x00414141)

    def test_korean_name_bytes_come_from_encode_utf8(self) -> None:
        text = "마린"
        self.assertEqual(encode_utf8(text, "test"), [0xEB, 0xA7, 0x88, 0xEB, 0xA6, 0xB0])
        conds = _conds(_build([text], [0]))[0][0]
        self.assertEqual(len(conds), 2)
        self.assertEqual(conds[0]["amount"], 0xEB88A7EB)
        self.assertEqual(conds[0]["locid"], 0xFFFFFFFF)
        # Bytes A6 B0 occupy lanes 0..1 of word 1; the NUL is lane 2.
        self.assertEqual(conds[1]["amount"], 0xB0A6)
        self.assertEqual(conds[1]["locid"], 0xFFFFFF)

    def test_two_players_emit_two_independent_chains_in_caller_order(self) -> None:
        chains = _conds(_build(["A", "B"], [2, 0]))
        self.assertEqual(len(chains), 2)
        self.assertEqual(len(chains[0]), 2)
        self.assertEqual(len(chains[1]), 2)
        self.assertEqual(chains[0][0][0]["player"], player_name_epd(2))
        self.assertEqual(chains[1][0][0]["player"], player_name_epd(0))
        self.assertEqual(chains[0][0][0]["amount"], 0x41)
        self.assertEqual(chains[1][1][0]["amount"], 0x42)


class MatchNamesValidationTest(unittest.TestCase):
    def _code(self, fn: Any) -> str:
        with self.assertRaises(EudslError) as caught:
            fn()
        return caught.exception.code

    def test_names_container_and_contents(self) -> None:
        self.assertEqual(self._code(lambda: _build([], [0])), "EUDSL-BOUNDS")
        self.assertEqual(
            self._code(lambda: _build("Armo", [0])),  # type: ignore[arg-type]
            "EUDSL-TYPE",
        )
        self.assertEqual(
            self._code(lambda: _build(["Armo", 3], [0])),  # type: ignore[list-item]
            "EUDSL-TYPE",
        )
        self.assertEqual(
            self._code(lambda: _build(["Armo", "Armo"], [0])), "EUDSL-BOUNDS"
        )
        # A tuple is the other accepted ordered container.
        _build(("Armo",), [0])  # type: ignore[arg-type]

    def test_name_length_bound(self) -> None:
        self.assertEqual(self._code(lambda: _build(["A" * 36], [0])), "EUDSL-BOUNDS")
        _build(["A" * 35], [0])  # exactly 35 bytes is accepted

    def test_players_validation(self) -> None:
        self.assertEqual(self._code(lambda: _build(["A"], [])), "EUDSL-BOUNDS")
        self.assertEqual(self._code(lambda: _build(["A"], [0, 0])), "EUDSL-BOUNDS")
        self.assertEqual(
            self._code(lambda: _build(["A"], {0, 1})),  # type: ignore[arg-type]
            "EUDSL-TYPE",
        )
        self.assertEqual(self._code(lambda: _build(["A"], [True])), "EUDSL-TYPE")
        self.assertEqual(self._code(lambda: _build(["A"], [12])), "EUDSL-RANGE")

    def test_on_match_must_be_callable(self) -> None:
        self.assertEqual(
            self._code(
                lambda: build_module(
                    lambda: match_names(["A"], None, [0])  # type: ignore[arg-type]
                )
            ),
            "EUDSL-TYPE",
        )

    def test_outside_build_module_is_scope_error(self) -> None:
        self.assertEqual(
            self._code(lambda: match_names(["A"], _callback, [0])),
            "EUDSL-SCOPE",
        )

    def test_nul_and_lone_surrogate_rejected_by_encode_utf8(self) -> None:
        self.assertEqual(self._code(lambda: _build(["A\x00B"], [0])), "EUDSL-TYPE")
        self.assertEqual(self._code(lambda: _build(["\ud800"], [0])), "EUDSL-TYPE")


if __name__ == "__main__":
    unittest.main()
