"""Condition combinators and gate-plan lowering (ADR 0028).

``cond_all``, ``cond_any`` and ``cond_not`` build the same runtime conditions as
the plain cond lists they replace, but with explicit AND / OR / NOT intent.  A
``GatePlan`` is the resolved tree of those markers; ``expand_cond_list`` turns it
into the flat cond list a ``when``/``Trigger`` consumes, emitting any flag gate
prelude at the use site.

This module is deliberately a leaf: it imports nothing from the package at
module import time.  ``view``, ``var``, ``temporal`` and ``trigger`` all import
it, so every package helper is imported lazily inside the function that needs
it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

__all__ = [
    "AndNode",
    "GatePlan",
    "LitNode",
    "NotNode",
    "NotOpaqueNode",
    "OrNode",
    "build_plan",
    "complement",
    "condAll",
    "condAny",
    "condNot",
    "cond_all",
    "cond_any",
    "cond_not",
    "emit_plan",
    "emit_plan_leaves_first",
    "expand_cond_list",
    "plan_cond_count",
    "validate_cond",
    "validate_conds",
]

_COND_ALWAYS = 22
_COND_NEVER = 23
_MISSING = object()


def _type_error(message: str) -> Any:
    from .view import EudslError

    return EudslError("EUDSL-TYPE", message)


def _describe(value: object) -> str:
    from .view import _describe_value

    return _describe_value(value)


def _refuse_truthiness() -> None:
    from .view import _build_time_truthiness_error

    raise _build_time_truthiness_error("condition")


class _Node:
    """Base for every composite condition marker.

    A marker describes an in-game predicate; ``if``/``while``/``and``/``or``/
    ``not`` on one would pick a build-time branch instead of emitting runtime
    HIR (ADR 0020), so truthiness is always refused.
    """

    __slots__ = ()

    def __bool__(self) -> bool:
        _refuse_truthiness()
        raise AssertionError("unreachable")  # pragma: no cover


@dataclass(frozen=True)
class LitNode(_Node):
    """A leaf condition wrapped in the plan tree (used by ``DeferredNe``)."""

    cond: Any


@dataclass(frozen=True)
class AndNode(_Node):
    """All ``nodes`` must hold (splices into the consumer's cond list)."""

    nodes: tuple[Any, ...]


@dataclass(frozen=True)
class OrNode(_Node):
    """At least one of ``nodes`` must hold (lowers through a flag gate)."""

    nodes: tuple[Any, ...]


@dataclass(frozen=True)
class NotNode(_Node):
    """A pending negation produced by :func:`cond_not`.

    Kept as an identity-bearing node so ``cond_not(cond_not(x))`` can return
    ``x`` itself.  Plans resolve it through :func:`complement` before emission.
    """

    inner: Any


@dataclass(frozen=True)
class NotOpaqueNode(_Node):
    """The complement of an opaque cond, lowered through a flag gate."""

    inner: Any


@dataclass(frozen=True)
class GatePlan(_Node):
    """An ordered list of resolved condition roots."""

    nodes: tuple[Any, ...]


def _is_condition_marker(cond: object) -> bool:
    from .var import DeferredCond
    from .view import DeferredNe

    return isinstance(cond, (DeferredNe, DeferredCond))


def _is_plain_cond(cond: object) -> bool:
    if not isinstance(cond, Mapping) or "condtype" not in cond:
        return False
    condtype = cond.get("condtype")
    return isinstance(condtype, int) and not isinstance(condtype, bool)


def validate_cond(cond: Any) -> None:
    """Reject a non-condition before any flag is allocated or trigger emitted."""

    if isinstance(cond, GatePlan):
        for node in cond.nodes:
            validate_cond(node)
        return
    if isinstance(cond, LitNode):
        validate_cond(cond.cond)
        return
    if isinstance(cond, (NotNode, NotOpaqueNode)):
        validate_cond(cond.inner)
        return
    if isinstance(cond, (AndNode, OrNode)):
        for node in cond.nodes:
            validate_cond(node)
        return
    if _is_condition_marker(cond) or _is_plain_cond(cond):
        return
    raise _type_error(f"not a condition: {_describe(cond)}")


def validate_conds(conds: tuple[Any, ...] | list[Any]) -> None:
    for cond in conds:
        validate_cond(cond)


def cond_all(*conds: Any) -> Any:
    """Every condition in ``conds`` must hold (an empty list is vacuously true).

    Nested :class:`AndNode` children are flattened and a one-child result is
    the child itself (ADR 0028 §2).  An empty conjunction stays an empty
    ``AndNode`` so it splices to zero conditions.
    """

    validate_conds(conds)
    flat: list[Any] = []
    for cond in conds:
        if isinstance(cond, AndNode):
            flat.extend(cond.nodes)
        else:
            flat.append(cond)
    if len(flat) == 1:
        return flat[0]
    return AndNode(tuple(flat))


def cond_any(*conds: Any) -> Any:
    """At least one condition in ``conds`` must hold (empty means never).

    Nested :class:`OrNode` children share one flag, a one-child result is the
    child itself, and zero branches stay an empty ``OrNode`` that expands to
    ``Never()``.
    """

    validate_conds(conds)
    flat: list[Any] = []
    for cond in conds:
        if isinstance(cond, OrNode):
            flat.extend(cond.nodes)
        else:
            flat.append(cond)
    if len(flat) == 1:
        return flat[0]
    return OrNode(tuple(flat))


def cond_not(cond: Any = _MISSING, *extra: Any) -> Any:
    """Negate exactly one condition.

    Double negation is resolved immediately: ``cond_not(cond_not(x))`` returns
    ``x`` itself.  Every other form keeps a :class:`NotNode` so the plan builder
    can preserve that identity while still lowering leaves directly (for
    example ``cond_not(field.ne(k))`` becomes ``field.eq(k)`` at 1C / 0T).

    The parameter is named so ``cond_not(cond=x)`` also works.
    """

    if cond is _MISSING or extra:
        got = 0 if cond is _MISSING else 1 + len(extra)
        raise _type_error(
            f"cond_not() takes exactly 1 condition, got {got}"
        )
    validate_cond(cond)
    if isinstance(cond, NotNode):
        return cond.inner
    return NotNode(cond)


condAll = cond_all
condAny = cond_any
condNot = cond_not


def complement(cond: Any) -> Any:
    """Return the logical negation of ``cond`` without wrapping a flag when possible."""

    validate_cond(cond)
    if isinstance(cond, NotNode):
        return cond.inner
    if isinstance(cond, NotOpaqueNode):
        return cond.inner
    if isinstance(cond, LitNode):
        return complement(cond.cond)
    if isinstance(cond, AndNode):
        return cond_any(*(complement(node) for node in cond.nodes))
    if isinstance(cond, OrNode):
        return cond_all(*(complement(node) for node in cond.nodes))
    method = getattr(cond, "complement", None)
    if callable(method):
        return method()
    if isinstance(cond, Mapping):
        condtype = cond.get("condtype")
        if condtype == _COND_ALWAYS:
            from .hir import Never

            return Never()
        if condtype == _COND_NEVER:
            from .hir import Always

            return Always()
        return NotOpaqueNode(cond)
    raise _type_error(f"cannot complement {_describe(cond)}")


class PlanContext:
    """Per-use-site flag owner for OR / opaque-NOT gates.

    The same context is reused when ``while_loop`` re-emits its plan at the
    tail, so every gate keeps the flag allocated for its entry prelude.
    """

    def __init__(self, builder: Any = None) -> None:
        if builder is None:
            from .trigger import _get_active_module_builder

            builder = _get_active_module_builder()
        self.builder = builder
        self._flags: dict[int, Any] = {}

    def get_or_alloc_flag(self, node: Any) -> Any:
        flag = self._flags.get(id(node))
        if flag is not None:
            return flag
        if self.builder is None:
            from .view import EudslError

            raise EudslError(
                "EUDSL-SCOPE",
                "condition composites need an active buildModule() to allocate "
                "their gate flag",
            )
        from .var import Var

        flag = Var(0)
        self._flags[id(node)] = flag
        return flag


def _normalize(cond: Any, ctx: PlanContext) -> Any:
    """Resolve identity nodes and gate-shaped leaves once, before emission."""

    if isinstance(cond, GatePlan):
        return GatePlan(tuple(_normalize(node, ctx) for node in cond.nodes))
    if isinstance(cond, NotNode):
        return _normalize(complement(cond.inner), ctx)
    if isinstance(cond, LitNode):
        return LitNode(_normalize(cond.cond, ctx))
    if isinstance(cond, AndNode):
        return AndNode(tuple(_normalize(node, ctx) for node in cond.nodes))
    if isinstance(cond, OrNode):
        return OrNode(tuple(_normalize(node, ctx) for node in cond.nodes))
    if isinstance(cond, NotOpaqueNode):
        return NotOpaqueNode(_normalize(cond.inner, ctx))
    gate_plan = getattr(cond, "gate_plan", None)
    if callable(gate_plan) and ctx.builder is not None:
        plan = gate_plan()
        if plan is not None:
            return _normalize(plan, ctx)
    return cond


def build_plan(raw_conds: Any, ctx: PlanContext) -> GatePlan:
    """Validate every input, then resolve it into a stable plan tree.

    Validation runs over the whole input before any leaf gate plan is resolved,
    so a bad condition never leaves a partially allocated flag or prelude
    behind.
    """

    raw = list(raw_conds)
    validate_conds(raw)
    return GatePlan(tuple(_normalize(cond, ctx) for cond in raw))


def _emit_or_prelude(node: OrNode, ctx: PlanContext, flag: Any) -> None:
    from .temporal import when
    from .trigger import Trigger

    # Reset the accumulator before any branch reads it.
    Trigger(conds=[], acts=[flag.act_set(0)], preserved=True)
    for branch in node.nodes:
        branch_conds: list[Any] = []
        _emit_node(branch, ctx, branch_conds)
        when(branch_conds, lambda f=flag: f.set(1))


def _emit_not_opaque_prelude(
    node: NotOpaqueNode, ctx: PlanContext, flag: Any
) -> None:
    from .temporal import when
    from .trigger import Trigger

    # The flag starts true; the inner cond clears it.
    Trigger(conds=[], acts=[flag.act_set(1)], preserved=True)
    inner_conds: list[Any] = []
    _emit_node(node.inner, ctx, inner_conds)
    when(inner_conds, lambda f=flag: f.set(0))


def _emit_node(node: Any, ctx: PlanContext, out: list[Any]) -> None:
    if isinstance(node, GatePlan):
        for child in node.nodes:
            _emit_node(child, ctx, out)
        return
    if isinstance(node, AndNode):
        for child in node.nodes:
            _emit_node(child, ctx, out)
        return
    if isinstance(node, OrNode):
        if not node.nodes:
            from .hir import Never

            out.append(Never())
            return
        flag = ctx.get_or_alloc_flag(node)
        _emit_or_prelude(node, ctx, flag)
        out.append(flag.eq(1))
        return
    if isinstance(node, NotOpaqueNode):
        flag = ctx.get_or_alloc_flag(node)
        _emit_not_opaque_prelude(node, ctx, flag)
        out.append(flag.eq(1))
        return
    if isinstance(node, LitNode):
        _emit_node(node.cond, ctx, out)
        return

    gate_plan = getattr(node, "gate_plan", None)
    if callable(gate_plan) and ctx.builder is not None:
        plan = gate_plan()
        if plan is not None:
            _emit_node(_normalize(plan, ctx), ctx, out)
            return
    materialize = getattr(node, "materialize", None)
    if callable(materialize):
        out.append(materialize())
        return
    out.append(node)


def _node_cond_count(node: Any) -> int:
    if isinstance(node, (GatePlan, AndNode)):
        return sum(_node_cond_count(child) for child in node.nodes)
    return 1


def plan_cond_count(plan: GatePlan) -> int:
    """Count the condition slots ``plan`` occupies in its consumer.

    An ``AndNode`` splices its children; every other node (including an empty
    ``OrNode`` that becomes ``Never()`` and a flag gate) occupies one slot.
    Raw ``Trigger`` uses this for its 16-condition cap before any prelude.
    """

    return _node_cond_count(plan)


def _prepare_top_level(node: Any, ctx: PlanContext, slots: list[Any]) -> None:
    if isinstance(node, (GatePlan, AndNode)):
        for child in node.nodes:
            _prepare_top_level(child, ctx, slots)
        return
    if isinstance(node, (OrNode, NotOpaqueNode)):
        slots.append((True, node))
        return
    out: list[Any] = []
    _emit_node(node, ctx, out)
    slots.append((False, out))


def emit_plan_leaves_first(plan: GatePlan, ctx: PlanContext) -> list[Any]:
    """Emit ``plan`` with all top-level literal leaves materialized first.

    ``while_loop`` keeps the pre-ADR emission order on mixed lists such as
    ``[DeferredNe, DeferredCond]``: deferred temp transfers queue first, then
    the gate reset/set-whens.  The returned condition list keeps the caller's
    order; only the prelude acts move.
    """

    slots: list[Any] = []
    for node in plan.nodes:
        _prepare_top_level(node, ctx, slots)
    out: list[Any] = []
    for is_gate, value in slots:
        if is_gate:
            _emit_node(value, ctx, out)
        else:
            out.extend(value)
    return out


def emit_plan(plan: GatePlan, ctx: PlanContext) -> list[Any]:
    """Emit ``plan``'s preludes and return its flat cond list.

    Calling this twice with the same ``ctx`` re-emits every prelude against the
    flags allocated on the first pass; ``while_loop`` uses that for its tail
    refresh.
    """

    out: list[Any] = []
    for node in plan.nodes:
        _emit_node(node, ctx, out)
    return out


def expand_cond_list(raw_conds: Any, ctx: PlanContext) -> list[Any]:
    """Resolve, emit and flatten one condition list at its use site."""

    return emit_plan(build_plan(raw_conds, ctx), ctx)
