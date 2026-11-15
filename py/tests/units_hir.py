"""Shared HIR inspection helpers for unit-list and free-list walk tests.

The walk suites (`test_units_list`, `test_capacity_t3`) pin the same loop
shape: one `while`, a CP-relative pointer reader, a `done` sentinel and
CurrentPlayer stores. These helpers keep the JSON navigation in one place.
"""

from __future__ import annotations

import json
from typing import Any

from py import UNIT_LIST_D, UNIT_LIST_S, UNIT_LIST_T, UNIT_LIST_U, build_module
from py.cp import CP_ADDR

#: Documented global head dwords for the four CUnit lists (ADR 0015).
UNIT_LIST_HEADS = {
    UNIT_LIST_U: 161843,
    UNIT_LIST_T: 161841,
    UNIT_LIST_S: 161844,
    UNIT_LIST_D: 161842,
}

#: Link word each list follows: `ptr + 1` for `next`, `ptr + 0` for `prev`.
UNIT_LIST_LINK_OFFSETS = {
    UNIT_LIST_U: 1,
    UNIT_LIST_T: 1,
    UNIT_LIST_S: 1,
    UNIT_LIST_D: 0,
}


def hir_module(prog) -> dict[str, Any]:
    """Build ``prog`` and return its serialized HIR module."""
    return json.loads(build_module(prog).to_json())["module"]


def walk_whiles(hir: dict[str, Any]) -> list[dict[str, Any]]:
    return [s for s in hir["stmts"] if s["kind"] == "while"]


def walk_while(hir: dict[str, Any]) -> dict[str, Any]:
    found = walk_whiles(hir)
    assert len(found) == 1, found
    return found[0]


def loop_ptr_id(stmt: dict[str, Any]) -> int:
    """The pointer var read by the loop's first (pool bound) condition."""
    return stmt["conds"][0]["relocs"][0]["target_var"]


def loop_done_id(stmt: dict[str, Any]) -> int:
    """The `done` sentinel read by the loop's third condition."""
    return stmt["conds"][2]["relocs"][0]["target_var"]


def cp_stores(stmts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        s
        for s in stmts
        if s["kind"] == "varstore" and s.get("dst_addr") == CP_ADDR
    ]


def var_init(hir: dict[str, Any], var_id: int) -> int:
    return next(v["init"] for v in hir["vars"] if v["id"] == var_id)


def acts_targeting(stmts: list[dict[str, Any]], var_id: int) -> list[dict[str, Any]]:
    return [
        stmt
        for stmt in stmts
        if stmt["kind"] == "act"
        and any(r.get("target_var") == var_id for r in stmt.get("relocs", []))
    ]
