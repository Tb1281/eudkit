"""Deterministic DSL fuzz parity: oracle + GatePlan vs native simulator.

The one-off ``/tmp/fuzz_dsl.py`` fuzzer proved that Python, TypeScript, and
``eudkit-compile sim`` agree on a large randomized matrix.  This module keeps
the useful part of that idea as a fast, self-contained unittest:

* a fixed ~36-case matrix spans every temporal form, raw ``Trigger``,
  ``condAll`` / ``condAny`` / ``condNot`` at depths 0..4, combinator arities
  0/1/2/3/17, and 0..130 actions;
* a pure-Python semantic oracle executes the same specification and a
  captured :class:`GatePlan` is evaluated against the oracle state each loop;
* both are compared with the native simulator for loop counts 1..5; and
* a sample of cases is rebuilt from equivalent TypeScript source so the two
  frontends must emit byte-identical HIR.

No runtime dependency on ``/tmp/fuzz_dsl.py``.
"""

from __future__ import annotations

import json
import random
import subprocess
import tempfile
import unittest
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

import py.conditions as PC
import py.temporal as temporal
import py.trigger as trigger_module
from py import Always, Never, Trigger, Var, build_module, condAll, condAny, condNot
from py.tests.helpers import EUDKIT_COMPILE, REPO_ROOT, run_sim
from py.temporal import (
    on_falling,
    on_rising,
    once,
    until,
    when,
    while_loop,
)
from py.var import DeferredCond

MASK32 = 0xFFFFFFFF
CONTROL_KINDS = {"when", "once", "until", "while", "rising", "falling", "raw"}
ACTION_KINDS = {"set", "iadd", "isub", "setv"}
COMPARISONS = ("eq", "ne", "ge", "gt", "le", "lt")
FORMS = ("when", "once", "until", "while", "rising", "falling", "raw")


# ---------------------------------------------------------------------------
# Specification model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Leaf:
    cid: int
    var: int
    op: str
    const: int


@dataclass(frozen=True)
class All:
    cid: int
    kids: tuple[Any, ...]


@dataclass(frozen=True)
class Any_:
    cid: int
    kids: tuple[Any, ...]


@dataclass(frozen=True)
class Not_:
    cid: int
    kid: Any


@dataclass(frozen=True)
class AlwaysC:
    cid: int


@dataclass(frozen=True)
class NeverC:
    cid: int


@dataclass
class Stmt:
    kind: str
    sid: int = -1
    conds: list[Any] = field(default_factory=list)
    body: list["Stmt"] = field(default_factory=list)
    else_body: list["Stmt"] | None = None
    value: int = 0
    src: int = 0
    preserved: bool = True
    # Filled while building the Python module; used by the oracle.
    latch: int | None = None
    curr: int | None = None
    prev: int | None = None
    fired: bool = False


@dataclass
class Case:
    index: int
    form: str
    seed: int
    depth: int
    arity: int
    acts: int
    var_inits: list[int]
    roots: list[Stmt]
    style: str
    nested: bool = False
    notes: list[str] = field(default_factory=list)
    user_ids: list[int] = field(default_factory=list)


class CondBuilder:
    """Deterministic condition-tree builder."""

    def __init__(self, seed: int, nvars: int) -> None:
        self.rng = random.Random(seed)
        self.nvars = nvars
        self._next = 0

    def _cid(self) -> int:
        self._next += 1
        return self._next - 1

    def leaf(self) -> Leaf:
        return Leaf(
            self._cid(),
            self.rng.randrange(self.nvars),
            self.rng.choice(COMPARISONS),
            self.rng.randint(0, 8),
        )

    def tree(self, depth: int, arity: int, style: str, top: bool = True) -> Any:
        cid = self._cid()
        if depth <= 0:
            return self.leaf()
        if style == "not":
            return Not_(cid, self.tree(depth - 1, arity, style, False))
        if style == "mix" and depth % 2 == 1:
            return Not_(cid, self.tree(depth - 1, arity, style, False))
        use_all = style != "any"
        if style == "mix":
            use_all = (depth // 2) % 2 == 0
        # Bound nested growth: only the top level honors the full arity.
        count = arity if top else (1 if depth == 1 else 2)
        kids = tuple(
            self.tree(depth - 1, arity, style, False) for _ in range(count)
        )
        return All(cid, kids) if use_all else Any_(cid, kids)


def _action(rng: random.Random, kind: str, pool: list[int]) -> Stmt:
    dst = rng.choice(pool)
    if kind == "setv":
        return Stmt("setv", conds=[dst], src=rng.choice(pool))
    return Stmt(kind, conds=[dst], value=rng.randint(0, 9))


def _actions(rng: random.Random, count: int, nvars: int, avoid: set[int]) -> list[Stmt]:
    pool = [index for index in range(nvars) if index not in avoid] or [0]
    return [
        _action(rng, rng.choice(["set", "set", "iadd", "isub", "setv"]), pool)
        for _ in range(count)
    ]


def _nested_stmt(rng: random.Random, nvars: int, avoid: set[int]) -> Stmt:
    """A small nested ``when`` so nesting is exercised without extra loops."""
    return Stmt(
        "when",
        conds=[Leaf(-1, rng.randrange(nvars), "ge", 0)],
        body=_actions(rng, 2, nvars, avoid),
    )


def _make_case(
    index: int,
    form: str,
    depth: int,
    arity: int,
    acts: int,
    style: str = "all",
    *,
    nested: bool = False,
    else_acts: int = 0,
    preserved: bool = True,
    raw_cond_count: int | None = None,
) -> Case:
    tag = sum((pos + 1) * ord(ch) for pos, ch in enumerate(form))
    rng = random.Random(10_000 + index * 7_919 + tag)
    nvars = 6
    inits = [rng.randint(0, 5) for _ in range(nvars)]
    builder = CondBuilder(10_000 + index, nvars)

    if form == "raw":
        cond_count = raw_cond_count
        if cond_count is None:
            cond_count = min(max(arity, 1), 16)
        conds = [builder.leaf() for _ in range(cond_count)]
    else:
        conds = [builder.tree(depth, arity, style)]

    avoid: set[int] = set()
    counter: int | None = None
    if form == "while":
        counter = nvars - 1
        inits[counter] = rng.randint(1, 3)
        # The loop counter is decremented exactly once per body execution,
        # so random actions must not overwrite it.
        avoid.add(counter)
        conds.insert(0, Leaf(-1, counter, "ge", 1))
    elif form in ("rising", "falling"):
        inits[0] = 1
        conds.insert(0, Leaf(-1, 0, "eq", 1))
    elif form == "until":
        inits[1] = 0
        conds.insert(0, Leaf(-1, 1, "eq", 1))

    body = _actions(rng, acts, nvars, avoid)
    if form == "while" and counter is not None:
        body = [Stmt("isub", conds=[counter], value=1)] + body
    elif form in ("rising", "falling"):
        mutation = Stmt("set", conds=[0], value=0)
        body = [mutation] + body[1:] if body else [mutation]
    elif form == "until":
        mutation = Stmt("set", conds=[1], value=1)
        body = [mutation] + body[1:] if body else [mutation]

    if nested:
        body.append(_nested_stmt(rng, nvars, avoid))

    if form == "raw":
        # Raw Trigger actions are declarative and cap at 64 slots.
        body = [
            Stmt("set", conds=[act.conds[0]], value=act.value)
            if act.kind == "setv"
            else act
            for act in body
        ][:64]

    else_body = None
    if form == "when" and else_acts:
        else_body = _actions(rng, else_acts, nvars, avoid)

    root = Stmt(
        form,
        conds=conds,
        body=body,
        else_body=else_body,
        preserved=preserved,
    )
    _assign_sids([root])
    return Case(
        index=index,
        form=form,
        seed=10_000 + index,
        depth=depth,
        arity=arity,
        acts=acts,
        var_inits=inits,
        roots=[root],
        style=style,
        nested=nested,
        notes=[f"depth={depth}", f"arity={arity}", f"acts={acts}"],
    )


def _assign_sids(stmts: Iterable[Stmt]) -> None:
    """Give every control statement a stable id for plan capture."""
    counter = 0
    stack = list(stmts)
    while stack:
        stmt = stack.pop()
        if stmt.kind in CONTROL_KINDS:
            stmt.sid = counter
            counter += 1
        stack.extend(stmt.body)
        if stmt.else_body:
            stack.extend(stmt.else_body)


def make_cases() -> list[Case]:
    """A fixed matrix covering forms, depths, arities, and action counts."""
    cases: list[Case] = []

    def add(form: str, depth: int, arity: int, acts: int, style: str = "all", **kw: Any) -> None:
        cases.append(_make_case(len(cases), form, depth, arity, acts, style, **kw))

    # One case per form; act counts span the fast and maximal bodies.
    add("when", 2, 2, 0)
    add("once", 2, 2, 1)
    add("until", 2, 2, 16)
    add("while", 2, 2, 63)
    add("rising", 2, 2, 64)
    add("falling", 2, 2, 65)
    add("raw", 2, 2, 64)

    # Depth sweep 0..4.
    add("when", 0, 1, 3)
    add("once", 1, 2, 8)
    add("until", 2, 3, 12)
    add("while", 3, 2, 17, nested=True)
    add("rising", 4, 2, 33, style="mix")
    add("falling", 0, 1, 34)
    add("raw", 1, 1, 50, raw_cond_count=4)

    # Arity sweep 0/1/2/3/17.
    add("when", 1, 0, 130)
    add("once", 1, 1, 2)
    add("until", 1, 2, 3)
    add("while", 1, 3, 4)
    add("rising", 1, 17, 5)
    add("falling", 1, 0, 6, style="any")
    add("raw", 1, 1, 7)

    # Combinator edges, including empty OR and a 16-condition raw Trigger.
    add("when", 2, 17, 9, style="any")
    add("once", 2, 17, 10)
    add("until", 3, 3, 11, style="mix")
    add("while", 1, 17, 130)
    add("rising", 4, 2, 20, style="not")
    add("falling", 4, 2, 21, style="mix")
    add("raw", 1, 1, 64, raw_cond_count=16)

    # Nested bodies and else arms.
    add("when", 2, 2, 22, nested=True, else_acts=4)
    add("once", 2, 2, 23, nested=True)
    add("until", 2, 2, 24, nested=True)
    add("while", 2, 2, 25, nested=True)
    add("rising", 2, 2, 26, nested=True)
    add("falling", 2, 2, 27, nested=True)

    # Empty condAny (Never) + large else, and a one-shot raw Trigger.
    add("when", 0, 0, 65, style="any", else_acts=65)
    add("raw", 1, 3, 5, preserved=False)
    return cases


# ---------------------------------------------------------------------------
# GatePlan capture
# ---------------------------------------------------------------------------


_ORIG_EMIT = PC.emit_plan
_ORIG_EMIT_LEAVES = PC.emit_plan_leaves_first
_ORIG_ALLOC_FLAG = PC.PlanContext.get_or_alloc_flag
_ORIG_EXPAND_CONDLIST = temporal.expand_cond_list
_ORIG_BUILD_PLAN = trigger_module.build_plan

_CASE_PLANS: dict[int, dict[str, Any]] = {}
_ACTIVE_SIDS: list[int] = []
_CURRENT_FLAGS: list[int] | None = None
_EMIT_DEPTH = 0


def _patched_alloc_flag(self: PC.PlanContext, node: Any) -> Any:
    existed = id(node) in self._flags
    flag = _ORIG_ALLOC_FLAG(self, node)
    if not existed and _CURRENT_FLAGS is not None:
        _CURRENT_FLAGS.append(flag.id)
    return flag


def _wrap_emit(orig: Any) -> Any:
    def wrapper(plan: Any, ctx: Any) -> Any:
        global _EMIT_DEPTH, _CURRENT_FLAGS
        # Internal module-finalisation triggers (raw ``Trigger`` instances
        # minted while flushing queued acts) run with no generated gate active.
        if _EMIT_DEPTH == 0 and not _ACTIVE_SIDS:
            return orig(plan, ctx)
        top = _EMIT_DEPTH == 0
        previous = _CURRENT_FLAGS
        if top:
            sid = _ACTIVE_SIDS[-1]
            record = _CASE_PLANS.setdefault(sid, {"plan": plan, "flags": []})
            _CURRENT_FLAGS = record["flags"]
        _EMIT_DEPTH += 1
        try:
            return orig(plan, ctx)
        finally:
            _EMIT_DEPTH -= 1
            if top:
                _CURRENT_FLAGS = previous

    return wrapper


def _install_instrumentation() -> None:
    PC.PlanContext.get_or_alloc_flag = _patched_alloc_flag
    PC.emit_plan = _wrap_emit(_ORIG_EMIT)
    PC.emit_plan_leaves_first = _wrap_emit(_ORIG_EMIT_LEAVES)
    # Modules that imported these names directly need the wrapped versions.
    temporal.expand_cond_list = PC.expand_cond_list
    temporal.emit_plan_leaves_first = PC.emit_plan_leaves_first
    trigger_module.emit_plan = PC.emit_plan
    trigger_module.build_plan = PC.build_plan


def _remove_instrumentation() -> None:
    PC.PlanContext.get_or_alloc_flag = _ORIG_ALLOC_FLAG
    PC.emit_plan = _ORIG_EMIT
    PC.emit_plan_leaves_first = _ORIG_EMIT_LEAVES
    temporal.expand_cond_list = _ORIG_EXPAND_CONDLIST
    temporal.emit_plan_leaves_first = _ORIG_EMIT_LEAVES
    trigger_module.emit_plan = _ORIG_EMIT
    trigger_module.build_plan = _ORIG_BUILD_PLAN


def _begin(sid: int) -> None:
    _ACTIVE_SIDS.append(sid)


def _end() -> None:
    _ACTIVE_SIDS.pop()


# ---------------------------------------------------------------------------
# Python module build
# ---------------------------------------------------------------------------


def _py_cond(cond: Any, pyvars: list[Var], memo: dict[int, Any]) -> Any:
    key = id(cond)
    if key in memo:
        return memo[key]
    if isinstance(cond, AlwaysC):
        out = Always()
    elif isinstance(cond, NeverC):
        out = Never()
    elif isinstance(cond, All):
        out = condAll(*(_py_cond(kid, pyvars, memo) for kid in cond.kids))
    elif isinstance(cond, Any_):
        out = condAny(*(_py_cond(kid, pyvars, memo) for kid in cond.kids))
    elif isinstance(cond, Not_):
        out = condNot(_py_cond(cond.kid, pyvars, memo))
    elif isinstance(cond, Leaf):
        out = getattr(pyvars[cond.var], cond.op)(cond.const)
    else:  # pragma: no cover - the generator only produces known node types
        raise AssertionError(type(cond))
    memo[key] = out
    return out


def _py_action(action: Stmt, pyvars: list[Var]) -> None:
    dst = pyvars[action.conds[0]]
    if action.kind == "set":
        dst.set(action.value)
    elif action.kind == "iadd":
        dst.iadd(action.value)
    elif action.kind == "isub":
        dst.isub(action.value)
    elif action.kind == "setv":
        dst.set(pyvars[action.src])
    else:  # pragma: no cover
        raise AssertionError(action.kind)


def _py_raw_action(action: Stmt, pyvars: list[Var]) -> dict[str, Any]:
    dst = pyvars[action.conds[0]]
    if action.kind == "set":
        return dst.act_set(action.value)
    if action.kind == "iadd":
        return dst.act_add(action.value)
    if action.kind == "isub":
        return dst.act_sub(action.value)
    raise AssertionError(action.kind)


def _py_stmt(stmt: Stmt, pyvars: list[Var], memo: dict[int, Any]) -> None:
    if stmt.kind in ACTION_KINDS:
        _py_action(stmt, pyvars)
        return

    conds = [_py_cond(cond, pyvars, memo) for cond in stmt.conds]
    if stmt.kind == "raw":
        acts = [_py_raw_action(action, pyvars) for action in stmt.body]
        _begin(stmt.sid)
        try:
            Trigger(conds=conds, acts=acts, preserved=stmt.preserved)
        finally:
            _end()
        return

    _begin(stmt.sid)
    try:
        if stmt.kind == "when":
            with when(conds) as builder:
                for child in stmt.body:
                    _py_stmt(child, pyvars, memo)
                if stmt.else_body is not None:
                    def _else() -> None:
                        for child in stmt.else_body or []:
                            _py_stmt(child, pyvars, memo)

                    builder.otherwise(_else)
        elif stmt.kind == "once":
            with once(conds):
                for child in stmt.body:
                    _py_stmt(child, pyvars, memo)
        elif stmt.kind == "until":
            with until(conds) as builder:
                stmt.latch = builder.active.id
                for child in stmt.body:
                    _py_stmt(child, pyvars, memo)
        elif stmt.kind == "while":
            with while_loop(conds):
                for child in stmt.body:
                    _py_stmt(child, pyvars, memo)
        elif stmt.kind == "rising":
            with on_rising(conds) as builder:
                stmt.curr = builder.curr.id
                stmt.prev = builder.prev.id
                for child in stmt.body:
                    _py_stmt(child, pyvars, memo)
        elif stmt.kind == "falling":
            with on_falling(conds) as builder:
                stmt.curr = builder.curr.id
                stmt.prev = builder.prev.id
                for child in stmt.body:
                    _py_stmt(child, pyvars, memo)
        else:  # pragma: no cover
            raise AssertionError(stmt.kind)
    finally:
        _end()


def build_py(case: Case) -> tuple[Any, str]:
    """Build ``case`` in-process and return ``(module, compact HIR JSON)``."""
    _CASE_PLANS.clear()
    _ACTIVE_SIDS.clear()

    def prog() -> None:
        pyvars = [Var(value) for value in case.var_inits]
        case.user_ids = [var.id for var in pyvars]
        memo: dict[int, Any] = {}
        for stmt in case.roots:
            _py_stmt(stmt, pyvars, memo)

    module = build_module(prog)
    return module, module.to_json()


# ---------------------------------------------------------------------------
# Semantic oracle and GatePlan evaluation
# ---------------------------------------------------------------------------


def _cmp_const(op: str, lhs: int, rhs: int) -> bool:
    if op == "eq":
        return lhs == rhs
    if op == "ne":
        return lhs != rhs
    if op == "ge":
        return lhs >= rhs
    if op == "gt":
        return lhs > rhs
    if op == "le":
        return lhs <= rhs
    if op == "lt":
        return lhs < rhs
    raise AssertionError(op)


def _eval_cond(cond: Any, state: dict[int, int]) -> bool:
    if isinstance(cond, AlwaysC):
        return True
    if isinstance(cond, NeverC):
        return False
    if isinstance(cond, All):
        return all([_eval_cond(kid, state) for kid in cond.kids])
    if isinstance(cond, Any_):
        return any([_eval_cond(kid, state) for kid in cond.kids])
    if isinstance(cond, Not_):
        return not _eval_cond(cond.kid, state)
    if isinstance(cond, Leaf):
        lhs = state.get(cond.var, 0) & MASK32
        return _cmp_const(cond.op, lhs, cond.const & MASK32)
    raise AssertionError(type(cond))


def _sat_sub(lhs: int, rhs: int) -> int:
    lhs &= MASK32
    rhs &= MASK32
    return (lhs - rhs) & MASK32 if lhs >= rhs else 0


def _deferred_truth(cond: DeferredCond, state: dict[int, int]) -> bool:
    """Model ``DeferredCond.materialize()`` and return its condition truth."""
    target = state.get(cond.target.id, 0) & MASK32
    rhs = cond.rhs
    operand = state.get(rhs.id, 0) & MASK32 if hasattr(rhs, "id") else int(rhs) & MASK32

    if cond.ne_const is not None:
        diff = cond._tmp
        assert diff is not None
        state[diff.id] = ((-cond.ne_const) & MASK32) + target & MASK32
        return state[diff.id] >= 1

    diff = cond._tmp
    assert diff is not None
    if cond.op in ("eq", "ne"):
        state[diff.id] = MASK32
        state[diff.id] = _sat_sub(state[diff.id], operand)
        state[diff.id] = (state[diff.id] + 1) & MASK32
        state[diff.id] = (state[diff.id] + target) & MASK32
        return state[diff.id] == 0 if cond.op == "eq" else state[diff.id] >= 1
    if cond.op in ("lt", "ge"):
        state[diff.id] = operand
        state[diff.id] = _sat_sub(state[diff.id], target)
        return state[diff.id] >= 1 if cond.op == "lt" else state[diff.id] == 0
    if cond.op in ("le", "gt"):
        state[diff.id] = target
        state[diff.id] = _sat_sub(state[diff.id], operand)
        return state[diff.id] == 0 if cond.op == "le" else state[diff.id] >= 1
    raise AssertionError(cond.op)


def _leaf_truth(node: Any, state: dict[int, int]) -> bool:
    if isinstance(node, DeferredCond):
        return _deferred_truth(node, state)
    if not isinstance(node, dict) or "condtype" not in node:
        raise AssertionError(f"unhandled plan leaf {node!r}")
    condtype = node["condtype"]
    if condtype == 22:
        return True
    if condtype == 23:
        return False
    if condtype not in (15, 16):
        raise AssertionError(f"unhandled condtype {condtype}")
    target = next(
        (
            reloc["target_var"]
            for reloc in node.get("relocs") or []
            if reloc.get("target_var") is not None
        ),
        None,
    )
    if target is None:
        raise AssertionError(f"leaf has no target var: {node!r}")
    value = state.get(int(target), 0) & MASK32
    amount = int(node["amount"]) & MASK32
    comparison = int(node["comparison"])
    if comparison == 0:
        return value >= amount
    if comparison == 1:
        return value <= amount
    if comparison == 10:
        return value == amount
    raise AssertionError(f"unhandled comparison {comparison}")


def _eval_plan(record: dict[str, Any], state: dict[int, int]) -> tuple[bool, list[str]]:
    flags = record["flags"]
    cursor = [0]
    errors: list[str] = []

    def take_flag() -> int:
        if cursor[0] >= len(flags):
            errors.append("plan wanted more flags than were allocated")
            return -1
        flag = flags[cursor[0]]
        cursor[0] += 1
        return flag

    def eval_node(node: Any) -> bool:
        if isinstance(node, PC.GatePlan):
            return all([eval_node(child) for child in node.nodes])
        if isinstance(node, PC.AndNode):
            return all([eval_node(child) for child in node.nodes])
        if isinstance(node, PC.OrNode):
            if not node.nodes:
                return False
            flag = take_flag()
            state[flag] = 0
            hit = False
            for branch in node.nodes:
                if eval_node(branch):
                    state[flag] = 1
                    hit = True
            return hit
        if isinstance(node, PC.NotOpaqueNode):
            flag = take_flag()
            state[flag] = 1
            if eval_node(node.inner):
                state[flag] = 0
            return state[flag] == 1
        if isinstance(node, PC.LitNode):
            return eval_node(node.cond)
        if isinstance(node, PC.NotNode):
            return not eval_node(node.inner)
        gate_plan = getattr(node, "gate_plan", None)
        if callable(gate_plan):
            plan = gate_plan()
            if plan is not None:
                return eval_node(plan)
        return _leaf_truth(node, state)

    truth = all([eval_node(child) for child in record["plan"].nodes])
    if cursor[0] != len(flags):
        errors.append(f"consumed {cursor[0]} of {len(flags)} flags")
    return truth, errors


def _exec_action(action: Stmt, state: dict[int, int]) -> None:
    dst = action.conds[0]
    if action.kind == "set":
        state[dst] = action.value & MASK32
    elif action.kind == "iadd":
        state[dst] = (state[dst] + action.value) & MASK32
    elif action.kind == "isub":
        state[dst] = (state[dst] - action.value) & MASK32
    elif action.kind == "setv":
        state[dst] = state[action.src] & MASK32
    else:  # pragma: no cover
        raise AssertionError(action.kind)


def _eval_gate(
    stmt: Stmt,
    state: dict[int, int],
    plans: dict[int, dict[str, Any]],
    mismatches: list[str],
) -> bool:
    ast_truth = all([_eval_cond(cond, state) for cond in stmt.conds])
    record = plans.get(stmt.sid)
    if record is not None:
        plan_truth, errors = _eval_plan(record, state)
        mismatches.extend(f"sid {stmt.sid} ({stmt.kind}): {error}" for error in errors)
        if plan_truth != ast_truth:
            mismatches.append(
                f"sid {stmt.sid} ({stmt.kind}): plan {plan_truth} != AST {ast_truth}"
            )
    return ast_truth


def _exec_stmts(
    stmts: list[Stmt],
    state: dict[int, int],
    plans: dict[int, dict[str, Any]],
    mismatches: list[str],
) -> None:
    for stmt in stmts:
        _exec_stmt(stmt, state, plans, mismatches)


def _exec_stmt(
    stmt: Stmt,
    state: dict[int, int],
    plans: dict[int, dict[str, Any]],
    mismatches: list[str],
) -> None:
    kind = stmt.kind
    if kind in ACTION_KINDS:
        _exec_action(stmt, state)
        return
    if kind == "raw":
        truth = _eval_gate(stmt, state, plans, mismatches)
        if truth and not stmt.fired:
            for action in stmt.body:
                _exec_action(action, state)
            if not stmt.preserved:
                stmt.fired = True
        return
    if kind == "when":
        if _eval_gate(stmt, state, plans, mismatches):
            _exec_stmts(stmt.body, state, plans, mismatches)
        elif stmt.else_body is not None and stmt.conds:
            _exec_stmts(stmt.else_body, state, plans, mismatches)
        return
    if kind == "once":
        truth = _eval_gate(stmt, state, plans, mismatches)
        if not stmt.fired and truth:
            _exec_stmts(stmt.body, state, plans, mismatches)
            stmt.fired = True
        return
    if kind == "until":
        truth = _eval_gate(stmt, state, plans, mismatches)
        if state.get(stmt.latch, 0) == 1:
            if truth:
                state[stmt.latch] = 0
            else:
                _exec_stmts(stmt.body, state, plans, mismatches)
        return
    if kind == "while":
        iterations = 0
        while _eval_gate(stmt, state, plans, mismatches):
            _exec_stmts(stmt.body, state, plans, mismatches)
            iterations += 1
            if iterations > 64:
                raise AssertionError(f"while sid {stmt.sid} did not terminate")
        return
    if kind in ("rising", "falling"):
        truth = _eval_gate(stmt, state, plans, mismatches)
        state[stmt.curr] = 1 if truth else 0
        if kind == "rising":
            fire = state[stmt.curr] == 1 and state.get(stmt.prev, 0) == 0
        else:
            fire = state[stmt.curr] == 0 and state.get(stmt.prev, 0) == 1
        if fire:
            _exec_stmts(stmt.body, state, plans, mismatches)
        state[stmt.prev] = state[stmt.curr]
        return
    raise AssertionError(kind)


def _reset_edges(stmts: list[Stmt]) -> None:
    for stmt in stmts:
        if stmt.kind not in CONTROL_KINDS:
            continue
        stmt.fired = False
        _reset_edges(stmt.body)
        if stmt.else_body:
            _reset_edges(stmt.else_body)


def run_oracle(
    case: Case,
    doc: dict[str, Any],
    plans: dict[int, dict[str, Any]],
    loops: int,
) -> tuple[list[int], list[str]]:
    state = {
        int(var["id"]): int(var["init"]) & MASK32
        for var in doc.get("module", {}).get("vars", [])
    }
    for index, var_id in enumerate(case.user_ids):
        want = case.var_inits[index] & MASK32
        if state.get(var_id) != want:
            raise AssertionError(
                f"var {var_id} init {state.get(var_id)} != declared {want}"
            )
    _reset_edges(case.roots)
    mismatches: list[str] = []
    for _ in range(loops):
        _exec_stmts(case.roots, state, plans, mismatches)
    ids = [int(var["id"]) for var in doc.get("module", {}).get("vars", [])]
    return [state.get(var_id, 0) & MASK32 for var_id in ids], mismatches


# ---------------------------------------------------------------------------
# TypeScript source generation
# ---------------------------------------------------------------------------


_TS_PRELUDE = """import {
  Var, Trigger, buildModule, Always, Never,
  when, once, until, whileLoop, onRising, onFalling,
  condAll, condAny, condNot,
} from "%s/ts/index.ts";
""" % REPO_ROOT


def _ts_cond_value(cond: Any, names: dict[int, str]) -> str:
    if isinstance(cond, AlwaysC):
        return "Always()"
    if isinstance(cond, NeverC):
        return "Never()"
    if isinstance(cond, All):
        return f"condAll({', '.join(names[id(kid)] for kid in cond.kids)})"
    if isinstance(cond, Any_):
        return f"condAny({', '.join(names[id(kid)] for kid in cond.kids)})"
    if isinstance(cond, Not_):
        return f"condNot({names[id(cond.kid)]})"
    if isinstance(cond, Leaf):
        return f"v{cond.var}.{cond.op}({cond.const})"
    raise AssertionError(type(cond))


def _ts_action(action: Stmt) -> str:
    if action.kind == "set":
        return f"v{action.conds[0]}.set({action.value});"
    if action.kind == "iadd":
        return f"v{action.conds[0]}.iadd({action.value});"
    if action.kind == "isub":
        return f"v{action.conds[0]}.isub({action.value});"
    if action.kind == "setv":
        return f"v{action.conds[0]}.set(v{action.src});"
    raise AssertionError(action.kind)


def _ts_raw_action(action: Stmt) -> str:
    if action.kind == "set":
        return f"v{action.conds[0]}.act_set({action.value})"
    if action.kind == "iadd":
        return f"v{action.conds[0]}.act_add({action.value})"
    if action.kind == "isub":
        return f"v{action.conds[0]}.act_sub({action.value})"
    raise AssertionError(action.kind)


def _ts_body(
    stmts: list[Stmt], indent: str, names: dict[int, str]
) -> list[str]:
    lines: list[str] = []
    for stmt in stmts:
        lines.extend(_ts_stmt(stmt, indent, names))
    return lines


def _ts_stmt(stmt: Stmt, indent: str, names: dict[int, str]) -> list[str]:
    kind = stmt.kind
    if kind in ACTION_KINDS:
        return [indent + _ts_action(stmt)]
    if kind == "raw":
        conds = ", ".join(names[id(cond)] for cond in stmt.conds)
        acts = ", ".join(_ts_raw_action(action) for action in stmt.body)
        preserved = "true" if stmt.preserved else "false"
        return [
            indent
            + f"new Trigger({{ conds: [{conds}], acts: [{acts}], "
            + f"preserved: {preserved} }});"
        ]
    control = {
        "when": "when",
        "once": "once",
        "until": "until",
        "while": "whileLoop",
        "rising": "onRising",
        "falling": "onFalling",
    }[kind]
    conds = ", ".join(names[id(cond)] for cond in stmt.conds)
    lines = [indent + f"{control}([{conds}], () => {{"]
    lines.extend(_ts_body(stmt.body, indent + "  ", names))
    if kind == "when" and stmt.else_body is not None:
        lines.append(indent + "}, () => {")
        lines.extend(_ts_body(stmt.else_body, indent + "  ", names))
    lines.append(indent + "});")
    return lines


def gen_ts_source(case: Case) -> str:
    names: dict[int, str] = {}
    emitted: set[int] = set()
    definition_lines: list[str] = []

    def define(cond: Any) -> None:
        if id(cond) in emitted:
            return
        if isinstance(cond, (All, Any_)):
            for kid in cond.kids:
                define(kid)
        elif isinstance(cond, Not_):
            define(cond.kid)
        name = f"c{id(cond)}"
        definition_lines.append(f"  const {name} = {_ts_cond_value(cond, names)};")
        names[id(cond)] = name
        emitted.add(id(cond))

    def define_in_stmts(stmts: list[Stmt]) -> None:
        for stmt in stmts:
            if stmt.kind not in CONTROL_KINDS:
                continue
            for cond in stmt.conds:
                define(cond)
            define_in_stmts(stmt.body)
            if stmt.else_body:
                define_in_stmts(stmt.else_body)

    define_in_stmts(case.roots)
    lines = [_TS_PRELUDE, "const module_ = buildModule(() => {"]
    for index, value in enumerate(case.var_inits):
        lines.append(f"  const v{index} = Var({value});")
    lines.extend(definition_lines)
    lines.extend(_ts_body(case.roots, "  ", names))
    lines.append("});")
    lines.append("console.log(module_.toJson());")
    return "\n".join(lines) + "\n"


def run_ts(case: Case, directory: Path) -> str:
    source = gen_ts_source(case)
    path = directory / f"case_{case.index}.ts"
    path.write_text(source, encoding="utf-8")
    proc = subprocess.run(
        ["node", "--experimental-strip-types", "--no-warnings", str(path)],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT / "ts",
    )
    if proc.returncode != 0:
        raise AssertionError(
            f"case {case.index}: TS build failed:\n{proc.stderr}\n--- source ---\n{source}"
        )
    return proc.stdout.strip().splitlines()[-1]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def _control_stmts(stmts: list[Stmt]) -> Iterable[Stmt]:
    for stmt in stmts:
        if stmt.kind not in CONTROL_KINDS:
            continue
        yield stmt
        yield from _control_stmts(stmt.body)
        if stmt.else_body:
            yield from _control_stmts(stmt.else_body)


def _cond_depth(cond: Any) -> int:
    if isinstance(cond, (All, Any_)):
        return 1 + max((_cond_depth(kid) for kid in cond.kids), default=0)
    if isinstance(cond, Not_):
        return 1 + _cond_depth(cond.kid)
    return 0


def _cond_kinds(cond: Any, out: set[str]) -> None:
    if isinstance(cond, All):
        out.add("condAll")
        for kid in cond.kids:
            _cond_kinds(kid, out)
    elif isinstance(cond, Any_):
        out.add("condAny")
        for kid in cond.kids:
            _cond_kinds(kid, out)
    elif isinstance(cond, Not_):
        out.add("condNot")
        _cond_kinds(cond.kid, out)


class TestDslFuzzParity(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        assert EUDKIT_COMPILE.exists(), f"missing binary: {EUDKIT_COMPILE}"
        _install_instrumentation()
        cls.cases = make_cases()

    @classmethod
    def tearDownClass(cls) -> None:
        _remove_instrumentation()

    def test_matrix_coverage(self) -> None:
        cases = self.cases
        self.assertGreaterEqual(len(cases), 35)
        self.assertEqual({case.form for case in cases}, set(FORMS))
        self.assertEqual({case.arity for case in cases}, {0, 1, 2, 3, 17})
        self.assertIn(0, {case.acts for case in cases})
        self.assertIn(130, {case.acts for case in cases})

        depths: set[int] = set()
        kinds: set[str] = set()
        for case in cases:
            for stmt in _control_stmts(case.roots):
                for cond in stmt.conds:
                    depths.add(_cond_depth(cond))
                    _cond_kinds(cond, kinds)
        self.assertEqual(depths, {0, 1, 2, 3, 4})
        self.assertEqual(kinds, {"condAll", "condAny", "condNot"})

    def test_oracle_gate_plans_and_sim_agree(self) -> None:
        for case in self.cases:
            with self.subTest(index=case.index, form=case.form):
                module, py_json = build_py(case)
                doc = json.loads(py_json)
                plans = {
                    sid: {"plan": record["plan"], "flags": list(record["flags"])}
                    for sid, record in _CASE_PLANS.items()
                }
                missing = [
                    stmt.sid
                    for stmt in _control_stmts(case.roots)
                    if stmt.sid not in plans
                ]
                self.assertEqual(missing, [], "gates without a captured GatePlan")

                for loops in range(1, 6):
                    expected, mismatches = run_oracle(case, doc, plans, loops)
                    self.assertEqual(
                        mismatches,
                        [],
                        f"case {case.index} loops {loops}: GatePlan mismatch",
                    )
                    report = run_sim(py_json, loops=loops)
                    self.assertEqual(
                        report["var_values"],
                        expected,
                        f"case {case.index} loops {loops}: sim vars != oracle",
                    )
                    self.assertEqual(
                        report.get("external_epds") or {},
                        {},
                        f"case {case.index} loops {loops}: unexpected external memory",
                    )

    def test_ts_py_hir_parity_on_sampled_cases(self) -> None:
        # Cover each temporal family, raw Triggers, and both edge directions.
        sampled = {0, 1, 3, 4, 5, 8, 35}
        with tempfile.TemporaryDirectory(prefix="eudkit-dsl-fuzz-ts-") as tmpdir:
            directory = Path(tmpdir)
            for case in self.cases:
                if case.index not in sampled:
                    continue
                with self.subTest(index=case.index, form=case.form):
                    module, py_json = build_py(case)
                    ts_json = run_ts(case, directory)
                    self.assertEqual(
                        json.loads(ts_json),
                        json.loads(py_json),
                        f"case {case.index}: TypeScript and Python HIR differ",
                    )


if __name__ == "__main__":
    unittest.main()
