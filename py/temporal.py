"""Temporal and structured control flow primitives (`when`, `once`, `until`,
`on_rising`, `on_falling`, `every`, `after`, `while_loop`, `func`, `dispatch`).
Supports both callback style and context-manager style (`with when(...):`).
"""

from __future__ import annotations

from typing import Any, Callable, Sequence

import inspect

from .conditions import (
    PlanContext,
    build_plan,
    condAll,
    condAny,
    condNot,
    cond_all,
    cond_any,
    cond_not,
    emit_plan_leaves_first,
    expand_cond_list,
    validate_conds,
)
from .hir import Never, SetTo
from .trigger import _get_active_module_builder
from .var import Expr, ReadonlyVar, Var, _emit_var_transfer
from .view import EudslError, _describe_value, _is_plain_int


def _raw_conds(conds: Any) -> list[Any]:
    if isinstance(conds, (list, tuple)):
        return list(conds)
    if conds is not None:
        return [conds]
    return []


def _reject_boolean_conds(raw: Sequence[Any]) -> None:
    """Reject a bare boolean in an already-flattened cond list.

    Shared by ``_normalize_conds`` and ``_WhileCM`` so every control form
    that consumes conds (``when``, ``once``, ``until``, ``while_loop``,
    ``on_rising``, ``on_falling``) fails eagerly with ``EUDSL-TYPE`` instead
    of emitting HIR the Rust parser cannot assemble.
    """
    for c in raw:
        if isinstance(c, bool):
            raise EudslError(
                "EUDSL-TYPE",
                f"condition list must not contain a boolean; got {_describe_value(c)}",
            )


def _normalize_conds(conds: Any) -> list[dict[str, Any]]:
    raw = _raw_conds(conds)
    _reject_boolean_conds(raw)
    return expand_cond_list(raw, PlanContext())


def _validate_count(n: Any, who: str) -> int:
    if isinstance(n, bool) or not isinstance(n, int):
        raise EudslError(
            "EUDSL-TYPE",
            f"{who}: count must be a build-time integer constant, got {n!r}",
        )
    if n <= 0:
        raise EudslError(
            "EUDSL-RANGE",
            f"{who}: count must be a positive integer, got {n}",
        )
    return n


class _WhenCM:
    def __init__(
        self,
        conds: Any,
        else_body: Callable[[], Any] | None = None,
    ) -> None:
        # Validate the else arm before normalising conds, matching TS: a
        # cond that expands to a gate prelude must not emit anything into a
        # live builder on a call that is about to fail.
        if else_body is not None and not callable(else_body):
            raise EudslError("EUDSL-TYPE", "when() else_body must be callable")
        # Bool is checked BEFORE _normalize_conds (and before int
        # handling: bool is an int subclass). True maps to 0 conds,
        # NOT an Always cond. False marks the then block dropped: no
        # then node is queued on exit, but a non-empty else_body is
        # queued unconditionally as when([], else_body). The scope is
        # still pushed/popped so builder state stays balanced and the
        # scope error still fires outside buildModule. Cond lists are
        # normalised eagerly, so a boolean item raises EUDSL-TYPE.
        if conds is False:
            self.dropped = True
            self.conds: list[dict[str, Any]] = []
        elif conds is True:
            self.dropped = False
            self.conds = []
        else:
            self.dropped = False
            self.conds = _normalize_conds(conds)
        self.else_body = else_body
        self.builder = _get_active_module_builder()

    def __enter__(self) -> _WhenCM:
        if self.builder is None:
            raise EudslError(
                "EUDSL-SCOPE", "when() must be called inside buildModule()"
            )
        self.builder.push_scope()
        return self

    def otherwise(self, body: Callable[[], Any]) -> _WhenCM:
        if not callable(body):
            raise EudslError(
                "EUDSL-TYPE", "when().otherwise() requires a callable"
            )
        if self.else_body is not None:
            raise EudslError(
                "EUDSL-TYPE", "when() else_body is already set"
            )
        self.else_body = body
        return self

    else_ = otherwise

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        entries = self.builder.pop_scope()
        if self.dropped:
            if self.else_body is not None:
                else_stmts = self._flush_else()
                if else_stmts:
                    self.builder.queue_stmt({
                        "kind": "when",
                        "conds": [],
                        "body": else_stmts,
                    })
            return
        stmts = self.builder.flush_entries(entries)
        node: dict[str, Any] = {
            "kind": "when",
            "conds": self.conds,
            "body": stmts,
        }
        if self.else_body is not None and self.conds:
            else_stmts = self._flush_else()
            if else_stmts:
                node["else_body"] = else_stmts
        self.builder.queue_stmt(node)

    def _flush_else(self) -> list[dict[str, Any]]:
        self.builder.push_scope()
        try:
            self.else_body()
        finally:
            entries = self.builder.pop_scope()
        return self.builder.flush_entries(entries)


def when(
    conds: Any,
    body: Callable[[], Any] | None = None,
    else_body: Callable[[], Any] | None = None,
) -> _WhenCM | None:
    """Execute `body` on every trigger loop in which `conds` hold.
    Supports both callback style `when(conds, body)` and context-manager style `with when(conds):`.
    Body-only form `when(body)` is unconditional (0 conds); `when(True, body)`
    is identical to it; `when(False, body)` drops the block entirely (the
    body is never executed and no node is queued).

    The optional third argument is a runtime else arm; it runs iff
    ``NOT(all conds)``. With the context-manager form call
    ``w.otherwise(fn)`` / ``w.else_(fn)`` inside the ``with`` suite instead.
    ``when(False, body, else_body)`` queues ``else_body`` unconditionally,
    while ``when(True, body, else_body)`` / ``when([], body, else_body)``
    never run the else arm.
    """
    if body is None:
        if callable(conds):
            # Body-only form: a single callable argument can only be a
            # body (conds are dicts, never callables).
            if else_body is not None:
                raise EudslError(
                    "EUDSL-TYPE",
                    "when() else_body requires a body; use .otherwise() "
                    "inside the with suite",
                )
            with _WhenCM(True):
                conds()
            return None
        if else_body is not None:
            raise EudslError(
                "EUDSL-TYPE",
                "when() else_body requires a body; use .otherwise() "
                "inside the with suite",
            )
        return _WhenCM(conds)
    cm = _WhenCM(conds, else_body)
    if cm.dropped:
        # Entering preserves the scope error outside buildModule and
        # keeps builder state balanced; the body is never executed.
        # __exit__ still queues the else arm unconditionally when set.
        with cm:
            pass
        return None
    with cm:
        body()
    return None


_DISPATCH_ELSE_CHAIN_MAX = 9


def _dispatch_validate(
    value: Any,
    cases: Any,
    fallback: Callable[[], Any] | None,
) -> tuple[Any, Any, list[tuple[int, Callable[[], Any]]], Callable[[], Any] | None]:
    """Validate a ``dispatch`` call in the documented deterministic order.

    Scope first (no arm or fallback body may run outside ``buildModule``), then
    the value domain, the cases container, the fallback callable, the non-empty
    bound, and finally each ``(key, body)`` pair in the caller's order. Keys
    normalise to their unsigned 32-bit value; duplicates are ``EUDSL-BOUNDS``.
    """
    builder = _get_active_module_builder()
    if builder is None:
        raise EudslError(
            "EUDSL-SCOPE", "dispatch() must be called inside buildModule()"
        )
    if not (
        _is_plain_int(value) or isinstance(value, (Var, Expr, ReadonlyVar))
    ):
        raise EudslError(
            "EUDSL-TYPE",
            "dispatch(): value must be a Var, Expr, ReadonlyVar, or plain "
            f"integer, got {_describe_value(value)}",
        )
    if not isinstance(cases, (list, tuple)):
        raise EudslError(
            "EUDSL-TYPE",
            "dispatch(): cases must be a list/tuple of (key, body) pairs",
        )
    if fallback is not None and not callable(fallback):
        raise EudslError("EUDSL-TYPE", "dispatch(): fallback must be callable")
    if not cases:
        raise EudslError(
            "EUDSL-BOUNDS",
            "dispatch(): cases must contain at least one (key, body) pair",
        )
    normalized: list[tuple[int, Callable[[], Any]]] = []
    seen: set[int] = set()
    for item in cases:
        if not isinstance(item, (list, tuple)) or len(item) != 2:
            raise EudslError(
                "EUDSL-TYPE", "dispatch(): each case must be a (key, body) pair"
            )
        key, body = item
        if isinstance(key, (Var, Expr, ReadonlyVar)):
            raise EudslError(
                "EUDSL-TYPE",
                "dispatch(): runtime keys are not supported; keys must be "
                "compile-time plain integers",
            )
        if not _is_plain_int(key):
            raise EudslError(
                "EUDSL-TYPE",
                "dispatch(): case keys must be plain integers, got "
                f"{_describe_value(key)}",
            )
        if not callable(body):
            raise EudslError(
                "EUDSL-TYPE",
                f"dispatch(): body for key {key!r} must be callable",
            )
        # Python ints are arbitrary precision; mask to match TS's unsigned `>>> 0`.
        norm_key = int(key) & 0xFFFFFFFF
        if norm_key in seen:
            raise EudslError(
                "EUDSL-BOUNDS",
                f"dispatch(): duplicate key 0x{norm_key:08X} after 32-bit "
                "normalization",
            )
        seen.add(norm_key)
        normalized.append((norm_key, body))
    return builder, value, normalized, fallback


def _dispatch_capture(
    builder: Any, body: Callable[[], Any]
) -> list[dict[str, Any]]:
    """Run one zero-argument arm body in a fresh scope and flush its stmts."""
    builder.push_scope()
    try:
        body()
    finally:
        entries = builder.pop_scope()
    return builder.flush_entries(entries)


def _dispatch_when(
    value: Any,
    key: int,
    body: list[dict[str, Any]],
    else_stmts: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build one ``when`` node; omit ``else_body`` when the arm is empty."""
    node: dict[str, Any] = {
        "kind": "when",
        "conds": [value.eq(key)],
        "body": body,
    }
    if else_stmts:
        node["else_body"] = else_stmts
    return node


def dispatch(
    value: Any,
    cases: Sequence[tuple[Any, Callable[[], Any]]],
    fallback: Callable[[], Any] | None = None,
) -> None:
    """Lower a one-of-N integer choice to the existing HIR.

    ``value`` is a plain integer, ``Var``, ``Expr``, or ``ReadonlyVar``. A plain
    integer is folded at build time: only the selected arm, the fallback, or
    nothing is emitted, with no ``when`` node. An ``Expr`` is materialized once
    into a fresh ``Var``; a ``Var``/``ReadonlyVar`` is used directly.

    ``cases`` is a non-empty ordered list of ``(key, body)`` pairs with
    compile-time plain-integer keys, normalised to 32 bits and unique after
    normalisation. Bodies are zero-argument builders. ``fallback`` (named, not
    ``default``) is an optional zero-argument builder; a miss without one is a
    documented no-op.

    Runtime ``N <= 9`` lowers to a nested ``when(..., else_body)`` chain built
    from the last case backwards, so the first matching key wins. Runtime
    ``N >= 10`` lowers to a balanced ``HirWhen`` tree (``ge(mid)`` internal
    nodes, ``eq(key)`` leaves); with a fallback, the same fallback statements
    are attached to every leaf's ``else_body`` because only one leaf is
    reachable. No new HIR node and no dense jump table are involved.
    """
    builder, value, normalized, fallback = _dispatch_validate(
        value, cases, fallback
    )

    if _is_plain_int(value):
        target = int(value) & 0xFFFFFFFF
        selected = next(
            (body for key, body in normalized if key == target), fallback
        )
        if selected is not None:
            for stmt in _dispatch_capture(builder, selected):
                builder.queue_stmt(stmt)
        return

    if isinstance(value, Expr):
        value = Var(value)

    bodies = {
        key: _dispatch_capture(builder, body) for key, body in normalized
    }
    fallback_stmts = (
        _dispatch_capture(builder, fallback) if fallback is not None else []
    )
    count = len(normalized)
    if count <= _DISPATCH_ELSE_CHAIN_MAX:
        node: dict[str, Any] | None = None
        for index in range(count - 1, -1, -1):
            key, _body = normalized[index]
            else_stmts = (
                fallback_stmts
                if index == count - 1
                else ([node] if node is not None else None)
            )
            node = _dispatch_when(value, key, bodies[key], else_stmts)
        assert node is not None
        builder.queue_stmt(node)
        return

    keys = sorted(key for key, _body in normalized)

    def build_tree(subkeys: list[int]) -> dict[str, Any]:
        if len(subkeys) == 1:
            key = subkeys[0]
            return _dispatch_when(value, key, bodies[key], fallback_stmts)
        mid = len(subkeys) // 2
        key = subkeys[mid]
        right = build_tree(subkeys[mid:])
        left = build_tree(subkeys[:mid])
        return {
            "kind": "when",
            "conds": [value.ge(key)],
            "body": [right],
            "else_body": [left],
        }

    builder.queue_stmt(build_tree(keys))


class _OnceCM:
    def __init__(self, conds: Any) -> None:
        if conds is False:
            self.dropped = True
            self.conds: list[dict[str, Any]] = []
        elif conds is True:
            self.dropped = False
            self.conds = []
        else:
            self.dropped = False
            self.conds = _normalize_conds(conds)
        self.builder = _get_active_module_builder()

    def __enter__(self) -> _OnceCM:
        if self.builder is None:
            raise EudslError(
                "EUDSL-SCOPE", "once() must be called inside buildModule()"
            )
        self.builder.push_scope()
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        entries = self.builder.pop_scope()
        if self.dropped:
            return
        stmts = self.builder.flush_entries(entries)
        self.builder.queue_stmt({
            "kind": "once",
            "conds": self.conds,
            "body": stmts,
        })


def once(conds: Any, body: Callable[[], Any] | None = None) -> _OnceCM | None:
    """Execute `body` on the first trigger loop in which `conds` hold and permanently
    self-disable afterwards (0T executed on subsequent loops).
    Body-only form `once(body)` runs once unconditionally; `once(True, body)`
    is identical to it; `once(False, body)` drops the block entirely.
    """
    if body is None:
        if callable(conds):
            with _OnceCM(True):
                conds()
            return None
        return _OnceCM(conds)
    cm = _OnceCM(conds)
    if cm.dropped:
        with cm:
            pass
        return None
    with cm:
        body()
    return None


class _UntilCM:
    """C11-02: emits a `HirUntil` node instead of desugaring. The latch
    variable is still allocated on entry, before the body runs, in
    exactly the position the old desugar allocated `active`, and is
    carried on the node as `latch_var`; the opt temporal pass desugars
    the node to the old 2-`when` latch shape (exactly, for gated
    `until`; only the reset trip-when for an unconditional gate, whose
    body never runs) using that id
    verbatim, so the node is the single home for the until desugar."""

    def __init__(self, conds: Any) -> None:
        # Bool is checked with `is` BEFORE _normalize_conds (and
        # before int handling: bool is an int subclass). False maps
        # to a Never cond: the trip-when never fires, so the latch
        # stays 1 and the body-when runs every loop — exactly the
        # decided until(False) semantics, identical to until(Never(),
        # body). Dropping False from the list would instead give the
        # until(True) shape (latch trips immediately, body never
        # runs), which is why False is NOT dropped.
        self.builder = _get_active_module_builder()
        if conds is False:
            self.conds = [Never()]
        elif conds is True:
            self.conds = []
        else:
            raw = _raw_conds(conds)
            _reject_boolean_conds(raw)
            # Validate eagerly so `until(lambda: None)`, a float, or a bool
            # item fails in __init__ with EUDSL-TYPE, before any latch or flag
            # variable exists (matching TS until()).
            validate_conds(raw)
            if self.builder is not None:
                # Resolve/expand before __enter__ allocates the latch, so the
                # flag/temp var ids match the eager pre-ADR ordering.
                self.conds = expand_cond_list(raw, PlanContext(self.builder))
            else:
                # Detached use still validates eagerly; __enter__ reports the
                # EUDSL-SCOPE error (non-prelude conds included).
                self.conds = []
        self.active: Any = None

    def __enter__(self) -> _UntilCM:
        if self.builder is None:
            raise EudslError(
                "EUDSL-SCOPE", "until() must be called inside buildModule()"
            )
        self.active = Var(1)
        self.builder.push_scope()
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        entries = self.builder.pop_scope()
        stmts = self.builder.flush_entries(entries)
        self.builder.queue_stmt(
            {
                "kind": "until",
                "conds": self.conds,
                "body": stmts,
                "latch_var": self.active.id,
            }
        )


def until(conds: Any, body: Callable[[], Any] | None = None) -> _UntilCM | None:
    """Run `body` while `conds` are false; permanently disable once `conds` hold.
    Conds stay REQUIRED (no body-only overload): `until(True, body)` is 0
    conds, so the latch trips immediately and the body never runs (opt
    emits only the reset trip-when for an unconditional gate — exclusive
    semantics, issue #72; a flat leaf-act body no longer rides along in
    a merged trip+body trigger);
    `until(False, ...)` maps to a Never cond, so the trip-when never
    fires, the latch stays 1, and the body runs every loop."""
    if body is None:
        return _UntilCM(conds)
    with _UntilCM(conds):
        body()
    return None


class _OnRisingCM:
    # C11-03: emits a HirEdge node instead of desugaring. The two state
    # variables are still allocated here, in the same order as the old
    # desugar allocated them, so their declarations (and therefore their
    # StoragePlan ranks) are identical to the baseline; the opt desugar
    # expands the node using these reserved ids.
    def __init__(self, conds: Any) -> None:
        self.conds = _normalize_conds(conds)
        self.builder = _get_active_module_builder()
        self.curr: Any = None
        self.prev: Any = None

    def __enter__(self) -> _OnRisingCM:
        if self.builder is None:
            raise EudslError(
                "EUDSL-SCOPE", "onRising() must be called inside buildModule()"
            )
        self.curr = Var(0)
        self.prev = Var(0)
        self.builder.push_scope()
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        entries = self.builder.pop_scope()
        stmts = self.builder.flush_entries(entries)
        self.builder.queue_stmt({
            "kind": "edge",
            "direction": "rising",
            "conds": self.conds,
            "body": stmts,
            "curr_var": self.curr.id,
            "prev_var": self.prev.id,
        })


def on_rising(conds: Any, body: Callable[[], Any] | None = None) -> _OnRisingCM | None:
    """Run `body` on the trigger loop where `conds` transition false -> true."""
    if body is None:
        return _OnRisingCM(conds)
    with _OnRisingCM(conds):
        body()
    return None


onRising = on_rising


class _OnFallingCM:
    # C11-03: emits a HirEdge node (direction "falling") instead of
    # desugaring; state variables are reserved as in _OnRisingCM.
    def __init__(self, conds: Any) -> None:
        self.conds = _normalize_conds(conds)
        self.builder = _get_active_module_builder()
        self.curr: Any = None
        self.prev: Any = None

    def __enter__(self) -> _OnFallingCM:
        if self.builder is None:
            raise EudslError(
                "EUDSL-SCOPE", "onFalling() must be called inside buildModule()"
            )
        self.curr = Var(0)
        self.prev = Var(0)
        self.builder.push_scope()
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        entries = self.builder.pop_scope()
        stmts = self.builder.flush_entries(entries)
        self.builder.queue_stmt({
            "kind": "edge",
            "direction": "falling",
            "conds": self.conds,
            "body": stmts,
            "curr_var": self.curr.id,
            "prev_var": self.prev.id,
        })


def on_falling(conds: Any, body: Callable[[], Any] | None = None) -> _OnFallingCM | None:
    """Run `body` on the trigger loop where `conds` transition true -> false."""
    if body is None:
        return _OnFallingCM(conds)
    with _OnFallingCM(conds):
        body()
    return None


onFalling = on_falling


class _EveryCM:
    """C11-04: emits a `HirEvery` node instead of desugaring. The counter
    variable is allocated at `__enter__` time, in the old order (before
    the body runs, exactly where the old desugar allocated it), and
    carried on the node as `counter_var`; opt desugars using that id
    verbatim and never mints var ids."""

    def __init__(self, n: int) -> None:
        self.count = _validate_count(n, "every")
        self.cnt: Any = None
        self.builder: Any = None

    def __enter__(self) -> _EveryCM:
        self.builder = _get_active_module_builder()
        if self.builder is None:
            raise EudslError(
                "EUDSL-SCOPE",
                "Control flow constructs must be called inside build_module()",
            )
        self.cnt = Var(0)
        self.builder.push_scope()
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        entries = self.builder.pop_scope()
        stmts = self.builder.flush_entries(entries)
        self.builder.queue_stmt({
            "kind": "every",
            "n": self.count,
            "counter_var": self.cnt.id,
            "body": stmts,
        })


def every(n: int, body: Callable[[], Any] | None = None) -> _EveryCM | None:
    """Run `body` every `n` trigger loops."""
    if body is None:
        return _EveryCM(n)
    with _EveryCM(n):
        body()
    return None


class _AfterCM:
    """C11-04: emits a `HirAfter` node instead of desugaring. The counter
    is allocated at `__enter__` time like `every`'s, and opt desugars to
    exactly today's 3-`when` arm/decrement/fire shape."""

    def __init__(self, n: int) -> None:
        self.count = _validate_count(n, "after")
        self.cnt: Any = None
        self.builder: Any = None

    def __enter__(self) -> _AfterCM:
        self.builder = _get_active_module_builder()
        if self.builder is None:
            raise EudslError(
                "EUDSL-SCOPE",
                "Control flow constructs must be called inside build_module()",
            )
        self.cnt = Var(0)
        self.builder.push_scope()
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        entries = self.builder.pop_scope()
        stmts = self.builder.flush_entries(entries)
        self.builder.queue_stmt({
            "kind": "after",
            "n": self.count,
            "counter_var": self.cnt.id,
            "body": stmts,
        })


def after(n: int, body: Callable[[], Any] | None = None) -> _AfterCM | None:
    """Delayed one-shot: arms an `n`-loop countdown when reached and fires once at 0."""
    if body is None:
        return _AfterCM(n)
    with _AfterCM(n):
        body()
    return None


class _WhileCM:
    def __init__(self, conds: Any) -> None:
        self.raw_conds = _raw_conds(conds)   # intent, not YET acts
        _reject_boolean_conds(self.raw_conds)
        self.conds: list[dict[str, Any]] = []
        self.plan: Any = None
        self.ctx: PlanContext | None = None
        self.builder = _get_active_module_builder()

    def __enter__(self) -> _WhileCM:
        if self.builder is None:
            raise EudslError(
                "EUDSL-SCOPE", "while_loop() must be called inside buildModule()"
            )
        # Entry: acts before the while gate -> first check reads live values.
        # One context owns every flag the plan allocates, so the tail can
        # re-emit the same preludes against the same flags.
        self.ctx = PlanContext(self.builder)
        self.plan = build_plan(self.raw_conds, self.ctx)
        # Top-level literal leaves (deferred temp transfers) first, then the
        # gate preludes, preserving master's emission order on mixed lists.
        self.conds = emit_plan_leaves_first(self.plan, self.ctx)
        self.builder.push_scope()
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        # Tail: re-emit the same plan with the same context. Deferred leaves
        # rebuild into their existing temps first; every gate flag is then
        # refreshed from live memory as the last body statements, so the
        # restore act loops back to a gate it just recomputed.
        assert self.ctx is not None and self.plan is not None
        emit_plan_leaves_first(self.plan, self.ctx)
        entries = self.builder.pop_scope()
        stmts = self.builder.flush_entries(entries)
        self.builder.queue_stmt({
            "kind": "while",
            "conds": self.conds,
            "body": stmts,
        })


def while_loop(conds: Any, body: Callable[[], Any] | None = None) -> _WhileCM | None:
    """Structured intra-frame loop repeating `body` as long as `conds` hold."""
    if body is None:
        return _WhileCM(conds)
    with _WhileCM(conds):
        body()
    return None


whileLoop = while_loop


class _FuncHandle:
    """Callable handle for a subroutine declared with :func:`func`.

    Calling it queues a ``call`` statement at the current scope: arguments are
    bound positionally, the chain jumps into the one shared body, and the
    result is copied into a fresh variable (``None`` for side-effecting
    subroutines). A call site therefore costs a short fixed sequence, not a
    copy of the body.
    """

    def __init__(
        self, builder: Any, func_id: int, arity: int, has_result: bool
    ) -> None:
        self._builder = builder
        self._func_id = func_id
        self._arity = arity
        self._has_result = has_result

    @property
    def func_id(self) -> int:
        return self._func_id

    def __call__(self, *args: Any) -> Var | None:
        if len(args) != self._arity:
            raise EudslError(
                "EUDSL-BOUNDS",
                f"func call expects {self._arity} argument(s), got {len(args)}",
            )
        encoded: list[dict[str, Any]] = []
        for arg in args:
            if isinstance(arg, Var):
                encoded.append({"kind": "var", "id": arg.id})
            elif _is_plain_int(arg):
                encoded.append({"kind": "const", "value": int(arg) & 0xFFFFFFFF})
            else:
                raise EudslError(
                    "EUDSL-TYPE",
                    "func arguments must be a Var or a constant integer, "
                    f"got {_describe_value(arg)}",
                )
        dst = Var(0) if self._has_result else None
        self._builder.queue_call(
            self._func_id, encoded, dst.id if dst is not None else None
        )
        return dst

    def __repr__(self) -> str:
        return f"<func #{self._func_id}>"


def func(body: Callable[..., Any]) -> _FuncHandle:
    """Declare a reusable subroutine and return its call handle.

    The body runs exactly once, at definition time, with one ``Var`` per
    positional parameter; its statements are captured as a module-level
    ``func`` and emitted once no matter how many sites call it. To return a
    value, ``return`` a ``Var``; the call then yields a fresh ``Var`` holding a
    copy of that result (``return None`` for a side-effecting subroutine).

    Subroutines cannot recurse (directly or indirectly): the return path is a
    single pointer patched per call site, so a nested self-call would lose the
    outer return address. Cycles are rejected at HIR validation.

    Examples::

        read_dword = func(lambda addr: read(addr))

        def bump(counter):
            counter.iadd(1)

        bump_fn = func(bump)
        bump_fn(self.ticks)
    """
    builder = _get_active_module_builder()
    if builder is None or not hasattr(builder, "capture_entries"):
        raise EudslError("EUDSL-SCOPE", "func() must be called inside buildModule()")
    try:
        signature = inspect.signature(body)
    except (TypeError, ValueError):
        raise EudslError(
            "EUDSL-TYPE", "func(): body must be a function with a readable signature"
        )
    params: list[Any] = []
    for param in signature.parameters.values():
        if param.kind in (param.VAR_POSITIONAL, param.VAR_KEYWORD):
            raise EudslError(
                "EUDSL-TYPE", "func(): variadic parameters are not supported"
            )
        if param.kind == param.KEYWORD_ONLY:
            raise EudslError(
                "EUDSL-TYPE",
                f"func(): keyword-only parameter {param.name!r} is not supported",
            )
        params.append(param)
    param_vars = [Var(0) for _ in params]
    captured: dict[str, int] = {}

    def run_body() -> None:
        result = body(*param_vars)
        if result is None:
            return
        if not isinstance(result, Var):
            raise EudslError(
                "EUDSL-TYPE",
                "func(): body must return a Var or None, "
                f"got {_describe_value(result)}",
            )
        # The returned var *is* the result slot: the call site copies it into
        # a fresh var, so a second copy here would only add a value move (a
        # 64-act TriggerPage firing) per executed call.
        captured["id"] = result.id

    stmts = builder.capture_entries(run_body)
    func_id = builder.alloc_func_id()
    node: dict[str, Any] = {
        "id": int(func_id),
        "params": [v.id for v in param_vars],
        "body": stmts,
    }
    result_id = captured.get("id")
    if result_id is not None:
        node["result"] = result_id
    builder.queue_func(node)
    return _FuncHandle(builder, func_id, len(param_vars), result_id is not None)


Func = func

__all__ = [
    "Func",
    "after",
    "condAll",
    "condAny",
    "condNot",
    "cond_all",
    "cond_any",
    "cond_not",
    "dispatch",
    "every",
    "func",
    "onFalling",
    "onRising",
    "on_falling",
    "on_rising",
    "once",
    "until",
    "when",
    "whileLoop",
    "while_loop",
]
