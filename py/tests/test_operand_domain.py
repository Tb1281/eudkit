"""Ticket #39: close the operand-domain surface (comparisons, inits, arrays, CP).

Ticket #36 closed ``Var`` writes/comparisons and field *writes*. Three
families of the same defect were still open on py:

* **field comparisons coerced** — ``hp.eq(1.5)`` folded as ``eq(1)``,
  ``hp.eq("5")`` as ``eq(5)``, ``hp.eq(True)`` as ``eq(1)``; only the values
  ``int()`` happens to reject (``"abc"``, ``None``, NaN, inf) were loud, and
  with a raw ``ValueError``/``TypeError``/``OverflowError``, not ``EUDSL-TYPE``;
* **the CP path queued an act before the guard** — ``act_set``/``act_add``/
  ``act_sub`` prepared the ``CurrentPlayer`` target first, so a rejected
  operand left a stray CP move in the module;
* **inits and const arrays coerced** — ``S(v=1.5)`` wrote 1 and
  ``FixedArray.const([1.5])`` built ``[1]``.

Every new rejection below is a raw ``ValueError``/``TypeError``/
``OverflowError`` or a silent coercion on `ba98fb0`; the accepted-operand
controls and the two labelled *decision pin* rows pass on both sides by
design (they pin the legal domain and the carve-outs the guard must not
narrow).

Ticket #45 closes the last ``int()`` coercion on a memory-reaching path —
field augmented assignment — and the attribute form that never worked:

* ``f = CUnit(1000).hp; f += 1.5`` built ``SetDeathsX`` ``Add 1`` (253
  chars), ``f -= "5"`` built ``Subtract 5``, and ``f += float("nan")`` was a
  raw ``ValueError`` — the ``int()`` sat in ``__iadd__``/``__isub__`` ahead of
  the guarded ``act_add``/``act_sub``;
* ``u.hp += 1`` (a *valid* operand) queued the act and then raised
  ``EUDSL-TYPE`` from the attribute write-back into
  ``MemViewInstance.__setattr__`` (253 chars of act against a 39-char
  control) — so the spelling ADR 0002 advertises never worked on a
  ``MemViewInstance``.

The rows below are red on that tip and green after, except the labelled
controls: the same-field write-back rows that ``TriggerStructInstance``
already honours (``u.hp = u.hp`` is a *decision*, pinned on purpose) and the
two coverage pins (``TriggerFieldRef``'s six write bodies, the six
``MemFieldHandle`` dunders — already guarded code).

Case names are kept identical to the TypeScript sibling file
(`ts/tests/test_operand_domain.ts`) so the pair is diffable.

Ticket #49 closes the **death-table seam** — the last place a non-integer could
reach a lane without a word of complaint:

* **two shared resolve functions coerced** — `_resolve_epd_dest` /
  `_resolve_value_or_ptr` ended in `int(x) & 0xFFFFFFFF`, so
  `Deaths(1002, Exactly, 1.5)` built `amount: 1`, `Deaths(1.5, Exactly, 1)`
  built `player: 1`, and `"abc"` / `NaN` died with a raw
  `ValueError`/`TypeError`. Every death-table and raw-memory constructor
  (`Deaths`/`DeathsX`/`SetDeaths`/`SetDeathsX`, `Memory*`, `SetMemory*`)
  funnels through them;
* **the record builders masked without validating** — `_build_cond` /
  `_build_act` did `comparison & 0xFF` / `modifier & 0xFF`, so
  `Deaths(1002, 1.5, 1)` and `SetDeaths(1002, 1.5, 1)` were raw `TypeError`s
  and so was `SetDeathsX(…, mask=1.5)` — the 9 condition and 12 action integer
  fields now validate first and name the field;
* **`MemCond` forwarded its two DSL parameters unguarded** —
  `MemCond(1002, "==", 1.5)` built `amount: 1`, `MemCond(1002, 1.5, 1)` was a
  raw `TypeError` from `& 0xFF`, and `MemCond(1002, "==", 1, 1.5)` from
  `& 0xFFFFFFFF`.

The relocation forms are the legal runtime domain and are pinned as controls:
`Deaths(1002, Exactly, Var(0))` (mode 4), `Deaths(Var(0), Exactly, 1)` (mode 1),
`SetDeaths(t.act(0).value, SetTo, 1)` and `SetNextPtr(t0, t0)`. Also folded in
here is cycle-15 review should-fix **S2**: `CUnit(Var(0)); u.hp = u.hp` raised
`EUDSL-COND-TRUTHIFY` because `Var.__eq__` returns a `DeferredCond` and the
write-back guard's `and` chain truth-tested it. There is no TS twin (TS has no
`__setattr__`), so the two S2 rows below are py-only and the TS file carries a
comment where they would be.
"""

from __future__ import annotations

import json
import unittest
from typing import Any, Callable

from py import (
    Add,
    CUnit,
    Deaths,
    DeathsX,
    Exactly,
    FixedArray,
    FixedString,
    MemCond,
    Memory,
    MemoryX,
    SetDeaths,
    SetDeathsX,
    SetMemory,
    SetMemoryX,
    SetNextPtr,
    SetStringDword,
    SetTo,
    Trigger,
    TriggerStruct,
    Var,
    as_readonly,
    build_module,
    dword,
    memcpyEPD,
    memsetEPD,
    when,
    with_cp,
)
from py.view import EudslError

# Non-plain-int operands. `True` is an `int` subclass in Python, which is
# exactly the coercion the guard exists to stop.
#
# The labels are the TS sibling's (`ts/tests/test_operand_domain.ts:70-78`),
# with the one-per-language rows called out: TS spells the "nothing here" rows
# `undefined` and `null`, py has a single `None`; and the `integral-float` row
# is py-only (ADR 0019 §4 — `Number.isInteger(1.0)` is true on TS, so TS
# accepts `1.0` and keeps it in its accepted list instead). `str` stays `str`
# (not `string`) so the label is byte-identical to the TS row; renaming one
# side only would stop the pair diffing row by row.
_BAD_VALUES = (
    ("str", "abc"),
    ("none", None),
    ("bool", True),
    ("float", 1.5),
    ("nan", float("nan")),
    ("inf", float("inf")),
    # N1: `int("5")` == 5 — the operand that was silently wrong on both
    # frontends before cycle 13 and had no regression test.
    ("numeric-string", "5"),
    # S2, per-frontend row (T1 §4): py rejects the integral float `1.0`
    # (`isinstance(1.0, int)` is False, and py is strict about it by
    # decision); TS accepts it. The matrices differ on this row only.
    ("integral-float", 1.0),
)

# Operands that are `int`/`float` instances, i.e. the ones a dunder forwards
# to the guarded comparison body instead of returning `NotImplemented`.
_NUMERIC_VALUES = tuple(
    (case, value)
    for case, value in _BAD_VALUES
    if isinstance(value, (int, float))
)

# `MemCond`'s comparison takes a *name* (`"=="`, ...) as well as a constant, so
# the two string rows are not operands of the numeric branch — they are pinned
# as `EUDSL-CMP` by the control row instead. Same derivation as the TS sibling.
_NON_STRING_VALUES = tuple(
    (case, value) for case, value in _BAD_VALUES if not isinstance(value, str)
)

_OPS = ("eq", "ne", "ge", "le", "gt", "lt")
_WRITES = ("set", "iadd", "isub", "act_set", "act_add", "act_sub")

# Stable message fragments (T1 §1 records the table). A regression that drops
# the operand name from a message must fail here.
_PLAIN_INT = "must be a plain integer"
_VAR_OPERAND = "integer constant, a Var, or an Expr"

# The `act_*` forms validate through `_require_plain_int` ("... must be a
# plain integer"); the in-place forms delegate to the `Var.set`/`iadd`/`isub`
# operand guard ("... must be an integer constant, a Var, or an Expr").
_WRITE_MESSAGES = {
    "set": _VAR_OPERAND,
    "iadd": _VAR_OPERAND,
    "isub": _VAR_OPERAND,
    "act_set": _PLAIN_INT,
    "act_add": _PLAIN_INT,
    "act_sub": _PLAIN_INT,
}

_DOMAIN_FIELDS = TriggerStruct("OperandDomainFields", {"v": dword()})


def _augmented(field: Any, symbol: str, value: Any) -> Any:
    """Apply the literal ``+=`` / ``-=`` spelling to a *handle*.

    ``field`` is a parameter so the augmented assignment rebinds a local
    instead of tripping Python's closure rules: a ``+=`` on a name that is
    assigned anywhere in the enclosing function makes that name local, and the
    read half then raises ``UnboundLocalError``.

    This exercises ``__iadd__``/``__isub__`` only. The attribute form
    (``u.hp += 1``, whose write-back goes through
    ``MemViewInstance.__setattr__``) must be spelled literally at the call
    site, so it is not routed through this helper.
    """
    if symbol == "+=":
        field += value
        return field
    field -= value
    return field


class OperandDomainTest(unittest.TestCase):
    def _assert_eudsl_type(
        self, call: Callable[[], Any], expected: str = _PLAIN_INT
    ) -> None:
        with self.assertRaises(EudslError) as ctx:
            call()
        self.assertEqual(ctx.exception.code, "EUDSL-TYPE")
        self.assertIn(expected, str(ctx.exception))

    def _assert_plain_int_error(
        self, call: Callable[[], Any], surface: str
    ) -> None:
        """#49: the code, the stable domain substring, and the operand surface.

        Not whole-string equality — the two frontends render the *value*
        differently (`got a float` vs `got 1.5`, ADR 0019 §1), so the pair pins
        the code plus the two fragments that must not drift.
        """
        with self.assertRaises(EudslError) as ctx:
            call()
        message = str(ctx.exception)
        self.assertEqual(ctx.exception.code, "EUDSL-TYPE")
        self.assertIn(_PLAIN_INT, message)
        self.assertIn(surface, message)

    # -- field writes -------------------------------------------------------

    def test_mem_field_write_rejects_bad_operand(self) -> None:
        field = CUnit(1000).hp
        for case, value in _BAD_VALUES:
            for method in _WRITES:
                with self.subTest(case=case, method=method):
                    self._assert_eudsl_type(
                        lambda m=method, v=value: getattr(field, m)(v)
                    )

    def test_trigger_field_write_rejects_bad_operand(self) -> None:
        for case, value in _BAD_VALUES:
            for method in _WRITES:
                def prog(method: str = method, value: Any = value) -> None:
                    s = _DOMAIN_FIELDS(v=0)
                    getattr(s.v, method)(value)

                with self.subTest(case=case, method=method):
                    self._assert_eudsl_type(
                        lambda p=prog: build_module(p), _WRITE_MESSAGES[method]
                    )

    def test_trigger_field_ref_write_rejects_bad_operand(self) -> None:
        """Coverage pin (N4) — green before and after.

        ``TriggerFieldRef``'s six write bodies already route through
        ``_require_plain_int``; the sibling above covers a struct-field
        ``Var``, not the raw field reference off a bound condition slot
        (``py/view.py:355-377``). That reference is ``__index__``-able, which
        is exactly why the guard must not be widened to ``__index__`` (N2,
        ADR 0019 §5).
        """
        for case, value in _BAD_VALUES:
            for method in _WRITES:
                def prog(method: str = method, value: Any = value) -> None:
                    hit = Var(0)
                    t = Trigger(conds=[], acts=[hit.act_set(1)])
                    getattr(t.cond(0).player, method)(value)

                with self.subTest(case=case, method=method):
                    self._assert_eudsl_type(lambda p=prog: build_module(p))

    # -- field augmented assignment (#45) -----------------------------------

    def test_field_augmented_assign_rejects_bad_operand(self) -> None:
        """HEAD: ``+=``/``-=`` coerce the operand (both handle surfaces).

        Measured on the current tip: ``f = CUnit(1000).hp; f += 1.5`` builds
        ``SetDeathsX`` ``Add 1`` (253 chars), ``f -= "5"`` builds
        ``Subtract 5``, ``f += True`` builds ``Add 1``, a real
        ``TriggerFieldRef`` builds ``Add 1`` / ``Subtract 1`` (641 chars), and
        ``"abc"`` / ``None`` / NaN / inf die with a raw
        ``ValueError``/``TypeError``/``OverflowError`` from the ``int()`` in
        ``__iadd__``/``__isub__``. Both surfaces × both spellings ×
        ``_BAD_VALUES`` must be ``EUDSL-TYPE``.
        """
        field = CUnit(1000).hp
        for case, value in _BAD_VALUES:
            for symbol in ("+=", "-="):
                with self.subTest(surface="mem", case=case, op=symbol):
                    self._assert_eudsl_type(
                        lambda f=field, s=symbol, v=value: _augmented(f, s, v)
                    )

        for case, value in _BAD_VALUES:
            for symbol in ("+=", "-="):
                def prog(symbol: str = symbol, value: Any = value) -> None:
                    hit = Var(0)
                    t = Trigger(conds=[], acts=[hit.act_set(1)])
                    _augmented(t.cond(0).player, symbol, value)

                with self.subTest(surface="trigger", case=case, op=symbol):
                    self._assert_eudsl_type(lambda p=prog: build_module(p))

    def test_attribute_augmented_assign_queues_no_act(self) -> None:
        """HEAD: the act is queued *before* the write-back raises.

        ``u.hp += 1.5`` builds 253 chars against a 39-char control
        (``u = CUnit(1000)``) — the ``Add 1`` the ``int()`` produced stays in
        the module even though the call raised. After the fix the failed call
        contributes nothing, so the two modules are byte-identical (string
        equality, not just equal length: the stray act must be *gone*, not
        merely reshaped).
        """
        def build(value: Any) -> str:
            def prog() -> None:
                u = CUnit(1000)
                try:
                    u.hp += value
                except EudslError as exc:
                    self.assertEqual(exc.code, "EUDSL-TYPE")
                    self.assertIn(_PLAIN_INT, str(exc))

            return build_module(prog).to_json()

        def control() -> str:
            def prog() -> None:
                u = CUnit(1000)

            return build_module(prog).to_json()

        expected = control()
        for case, value in (("float", 1.5), ("numeric-string", "5")):
            with self.subTest(case=case):
                self.assertEqual(build(value), expected)

    def test_attribute_augmented_assign_emits_one_act(self) -> None:
        """HEAD: raises ``EUDSL-TYPE`` from the attribute write-back.

        ``__iadd__`` queues the act and returns ``self``; Python then assigns
        that handle back onto the attribute, and ``__setattr__`` treats it as a
        value. After the fix ``u.hp += 1`` builds exactly the HIR of
        ``u.hp.act_add(1)`` (253 chars, one act, ``modifier`` 8 = Add,
        ``dest`` 1002) and ``-=`` matches ``act_sub``.
        """
        def augmented(symbol: str) -> str:
            def prog() -> None:
                u = CUnit(1000)
                if symbol == "+=":
                    u.hp += 1
                else:
                    u.hp -= 1

            return build_module(prog).to_json()

        def explicit(method: str) -> str:
            def prog() -> None:
                u = CUnit(1000)
                getattr(u.hp, method)(1)

            return build_module(prog).to_json()

        for symbol, method, modifier in (
            ("+=", "act_add", 8),  # Add
            ("-=", "act_sub", 9),  # Subtract
        ):
            with self.subTest(op=symbol):
                self.assertEqual(augmented(symbol), explicit(method))
                stmts = json.loads(augmented(symbol))["module"]["stmts"]
                acts = [a for stmt in stmts for a in stmt.get("acts", [])]
                self.assertEqual(len(acts), 1)
                self.assertEqual(acts[0]["modifier"], modifier)
                self.assertEqual(acts[0]["dest"], 1002)

    def test_attribute_same_field_assignment_is_noop(self) -> None:
        """HEAD: raises ``EUDSL-TYPE`` — a handle is not a value.

        After: a no-op by decision. It is the rule
        ``TriggerStructInstance.__setattr__`` already applies (``t.v = t.v``
        emits nothing), and it is the only way ``u.hp += 1`` can work, since
        the augmented assignment ends in a write-back of the handle
        ``__iadd__`` returned. ``__getattr__`` allocates a fresh handle per
        access, so the check compares ``schema``/``spec``/``baseEpd``, not
        identity.
        """
        def assign_same() -> str:
            def prog() -> None:
                u = CUnit(1000)
                u.hp = u.hp

            return build_module(prog).to_json()

        def control() -> str:
            def prog() -> None:
                u = CUnit(1000)

            return build_module(prog).to_json()

        self.assertEqual(assign_same(), control())

    def test_attribute_assignment_still_rejects_other_fields(self) -> None:
        """Control — green before and after: only the *same* field is a no-op."""
        def assign_other_field() -> None:
            u = CUnit(1000)
            u.hp = u.shield

        def assign_float() -> None:
            u = CUnit(1000)
            u.hp = 1.5

        self._assert_eudsl_type(lambda: build_module(assign_other_field))
        self._assert_eudsl_type(lambda: build_module(assign_float))

    # -- field comparisons --------------------------------------------------

    def test_mem_field_comparison_rejects_bad_operand(self) -> None:
        """HEAD: 4/8 rows raw errors, 4/8 rows build (``eq(1)``/``eq(5)``)."""
        field = CUnit(1000).hp
        for case, value in _BAD_VALUES:
            for op in _OPS:
                with self.subTest(case=case, op=op):
                    self._assert_eudsl_type(
                        lambda o=op, v=value: getattr(field, o)(v)
                    )

    def test_trigger_field_comparison_rejects_bad_operand(self) -> None:
        """HEAD: 4/8 rows raw errors, 4/8 rows build (``eq(1)``/``eq(5)``)."""
        for case, value in _BAD_VALUES:
            for op in _OPS:
                def prog(op: str = op, value: Any = value) -> None:
                    hit = Var(0)
                    t = Trigger(conds=[], acts=[hit.act_set(1)])
                    getattr(t.cond(0).player, op)(value)

                with self.subTest(case=case, op=op):
                    self._assert_eudsl_type(lambda p=prog: build_module(p))

    # -- field dunders ------------------------------------------------------

    def test_field_dunder_rejects_non_plain_int(self) -> None:
        """HEAD: ``hp == 1.5`` builds ``eq(1)``; ``>= "abc"`` is a raw error.

        ``TriggerFieldRef.__eq__`` now routes ``str`` (and ``bool`` / ``float``)
        through the guarded ``eq`` body, so ``player == "abc"`` is
        ``EUDSL-TYPE`` (it was ``False`` on HEAD). ``None`` and other unrelated
        objects keep the ``NotImplemented`` protocol: the ``== None`` idiom
        stays ``False`` rather than becoming an error.
        """
        hp = CUnit(1000).hp
        mem_rows = (
            ("==", lambda field, v: field == v),
            ("!=", lambda field, v: field != v),
            # N5: the three ordering dunders were missing from this matrix;
            # they delegate unconditionally, so every row is already green on
            # HEAD — a coverage pin, not a new guard.
            ("<", lambda field, v: field < v),
            ("<=", lambda field, v: field <= v),
            (">", lambda field, v: field > v),
            (">=", lambda field, v: field >= v),
        )
        for case, value in _BAD_VALUES:
            for symbol, row in mem_rows:
                with self.subTest(surface="mem", case=case, op=symbol):
                    self._assert_eudsl_type(lambda r=row, v=value: r(hp, v))

        def trig(row: Callable[[Any, Any], Any], value: Any) -> Callable[[], None]:
            def prog() -> None:
                hit = Var(0)
                t = Trigger(conds=[], acts=[hit.act_set(1)])
                row(t.cond(0).player, value)

            return prog

        for case, value in _NUMERIC_VALUES:
            for symbol, row in (("==", lambda f, v: f == v), (">=", lambda f, v: f >= v)):
                with self.subTest(surface="trigger", case=case, op=symbol):
                    self._assert_eudsl_type(lambda p=trig(row, value): build_module(p))

        with self.subTest(surface="trigger", case="str", op="=="):
            self._assert_eudsl_type(
                lambda p=trig(lambda f, v: f == v, "abc"): build_module(p)
            )

        with self.subTest(surface="trigger", case="str", op=">="):
            self._assert_eudsl_type(
                lambda p=trig(lambda f, v: f >= v, "abc"): build_module(p)
            )

        def carve_outs() -> None:
            hit = Var(0)
            t = Trigger(conds=[], acts=[hit.act_set(1)])
            player = t.cond(0).player
            self.assertIs(player == None, False)  # noqa: E711

        build_module(carve_outs)

    # -- Var writes ---------------------------------------------------------

    def test_var_write_rejects_bad_operand(self) -> None:
        for case, value in _BAD_VALUES:
            for method in _WRITES:
                def prog(method: str = method, value: Any = value) -> None:
                    v = Var(0)
                    getattr(v, method)(value)

                with self.subTest(case=case, method=method):
                    self._assert_eudsl_type(
                        lambda p=prog: build_module(p), _WRITE_MESSAGES[method]
                    )

    # -- comparisons --------------------------------------------------------

    def test_comparison_rejects_bad_operand(self) -> None:
        for case, value in _BAD_VALUES:
            for op in _OPS:
                def prog(op: str = op, value: Any = value) -> None:
                    v = Var(0)
                    getattr(v, op)(value)

                with self.subTest(case=case, op=op):
                    self._assert_eudsl_type(
                        lambda p=prog: build_module(p), _VAR_OPERAND
                    )

    # -- inits --------------------------------------------------------------

    def test_var_init_rejects_bad_operand(self) -> None:
        """*Decision pin* — green before and after (via ``Var.set``)."""
        for case, value in _BAD_VALUES:
            def prog(value: Any = value) -> None:
                Var(value)

            with self.subTest(case=case):
                self._assert_eudsl_type(lambda p=prog: build_module(p), _VAR_OPERAND)

    def test_struct_init_rejects_bad_operand(self) -> None:
        """HEAD: ``S(v=1.5)``/``S(v=True)``/``S(v=1.0)`` write 1; NaN/inf raw."""
        for case, value in _BAD_VALUES:
            def prog(value: Any = value) -> None:
                _DOMAIN_FIELDS(v=value)

            with self.subTest(case=case):
                self._assert_eudsl_type(lambda p=prog: build_module(p))

    def test_struct_init_accepts_runtime_operands(self) -> None:
        """Control: ``Var``/``Expr``/``ReadonlyVar`` inits keep the runtime path."""
        def prog() -> None:
            w = Var(0)
            _DOMAIN_FIELDS(v=Var(0))
            _DOMAIN_FIELDS(v=w.add(1))
            _DOMAIN_FIELDS(v=as_readonly(w))

        build_module(prog)

    # -- const arrays -------------------------------------------------------

    def test_const_array_literal_rejects_bad_operand(self) -> None:
        """HEAD: ``[1.5]``/``["5"]``/``[True]``/``[1.0]`` build ``[1]``/``[5]``."""
        for case, value in _BAD_VALUES:
            with self.subTest(case=case):
                self._assert_eudsl_type(lambda v=value: FixedArray.const([v]))

    def test_const_array_comparison_rejects_bad_operand(self) -> None:
        """HEAD: every row is a raw ``TypeError`` from ``rhs & 0xFFFFFFFF``."""
        arr = FixedArray.const([0, 1])
        for case, value in _BAD_VALUES:
            for op in _OPS:
                with self.subTest(case=case, op=op):
                    self._assert_eudsl_type(
                        lambda o=op, v=value: getattr(arr.get(0), o)(v)
                    )

    # -- eagerness ----------------------------------------------------------

    def test_cp_path_error_queues_no_act(self) -> None:
        """HEAD: with_error is 978 chars vs 710 for the control (stray CP move).

        The failed call must contribute nothing to the HIR: after the
        ``act_*`` reorder the two modules are byte-identical (710 == 710).
        One row per write method; the shape is identical for all three.

        N6c: the ``710`` is pinned literally because it is the *same fixture*
        as the TS sibling (``ts/tests/test_operand_domain.ts:390`` asserts
        ``failed.length === 710``), so the two suites pin one number, not two.
        """
        def build(method: str, mode: str) -> str:
            def prog() -> None:
                if mode == "error":
                    with with_cp(7000) as cp:
                        try:
                            getattr(cp.view(CUnit).hp, method)("abc")
                        except EudslError as exc:
                            self.assertEqual(exc.code, "EUDSL-TYPE")
                            self.assertIn(_PLAIN_INT, str(exc))
                else:
                    with with_cp(7000):
                        pass
                hit = Var(0)
                when([hit.eq(0)], lambda: hit.iadd(1))

            return build_module(prog).to_json()

        for method in ("set", "iadd", "isub"):
            with self.subTest(method=method):
                failed = build(method, "error")
                self.assertEqual(len(failed), 710)
                self.assertEqual(failed, build(method, "control"))

    def test_field_comparison_is_eager(self) -> None:
        """HEAD: the raise is either absent (coercion) or a raw ``int()`` error."""
        self._assert_eudsl_type(lambda: CUnit(1000).hp.eq(1.5))

        def prog() -> None:
            hit = Var(0)
            t = Trigger(conds=[], acts=[hit.act_set(1)])
            # The error must escape this call, not the enclosing build.
            self._assert_eudsl_type(lambda: t.cond(0).player.ge(1.5))

        build_module(prog)

    # -- accepted operands (pins the legal domain; passes on HEAD too) ------

    def test_accepted_operands_build(self) -> None:
        def prog() -> None:
            v = Var(0)
            w = Var(0)
            for const in (0, -1, 0xFF, 0x100000000):
                v.set(const)
                v.iadd(const)
                v.isub(const)
            v.set(w)
            v.iadd(w)
            v.isub(w)
            v.eq(w)
            v.eq(v.add(1))
            v.eq(as_readonly(w))
            # Field comparisons and const arrays keep the legal domain
            # (ADR 0017 §2: the guards reject types, never magnitudes).
            for const in (0, -1, 0x100000000, -0x80000000):
                CUnit(1000).hp.eq(const)
                CUnit(1000).hp.ge(const)
                FixedArray.const([const, 0]).get(0).eq(const)
            FixedArray.const([0, 1]).get(0).ge(1)

        build_module(prog)

    # -- canonical operand materializer, narrowed adapters (FE4) ------------

    def test_operand_materializer_domains_match_their_adapters(self) -> None:
        """FE4: one materializer kernel, three deliberately narrowed adapters.

        `py.var.to_operand` / `to_var` own the "constant or handle -> Var"
        step. The callers differ only in the domain they admit:

        * `var`: plain int, `Var`, `ReadonlyVar`, `Expr` (unchanged);
        * `string`: `Var`, `Expr`, `ReadonlyVar` (a runtime position/content
          value, never a bare int);
        * `blockio`: `Var`, `Expr` — a `ReadonlyVar` is outside the block
          operand domain and keeps the existing `EUDSL-TYPE` message.

        The legal rows must build; the `ReadonlyVar` block rows must raise
        eagerly, naming the surface and the game-time-value domain.
        """

        def prog() -> None:
            v = Var(0)
            ro = as_readonly(v)

            # var: the full operand domain, including plain constants.
            for rhs in (1, v, ro, v.add(1)):
                v.set(rhs)
                v.iadd(rhs)
                v.isub(rhs)
                v.eq(rhs)

            # string: runtime position accepts Var, Expr, and ReadonlyVar.
            s = FixedString(8)
            s.writeFrom(v, "a")
            s.writeFrom(v.add(1), "b")
            s.writeFrom(ro, "c")

            # blockio: Var and Expr are legal block operands.
            memsetEPD(3000, v.add(1), v.add(1))
            memcpyEPD(4000, 3000, v.add(1))
            memsetEPD(3000, v, 2)

            block_rows = (
                ("memcpyEPD", "dstEPD", lambda: memcpyEPD(ro, 3000, 2)),
                ("memcpyEPD", "srcEPD", lambda: memcpyEPD(3000, ro, 2)),
                ("memcpyEPD", "count", lambda: memcpyEPD(3000, 3000, ro)),
                ("memsetEPD", "dstEPD", lambda: memsetEPD(ro, 1, 2)),
                ("memsetEPD", "value", lambda: memsetEPD(3000, ro, 2)),
                ("memsetEPD", "count", lambda: memsetEPD(3000, 1, ro)),
            )
            for what, surface, call in block_rows:
                with self.subTest(what=what, surface=surface):
                    with self.assertRaises(EudslError) as ctx:
                        call()
                    message = str(ctx.exception)
                    self.assertEqual(ctx.exception.code, "EUDSL-TYPE")
                    self.assertIn(f"{what}(): {surface}", message)
                    self.assertIn("game-time value", message)

        build_module(prog)

    # -- bad leaf inside an Expr -------------------------------------------

    def test_bad_expr_leaf_rejects(self) -> None:
        def set_prog() -> None:
            v = Var(0)
            v.set(v.add("abc"))

        def eq_prog() -> None:
            v = Var(0)
            hit = Var(0)
            when([v.eq(v.add("abc"))], lambda: hit.iadd(1))

        self._assert_eudsl_type(
            lambda: build_module(set_prog), "Unsupported Var operand"
        )
        self._assert_eudsl_type(
            lambda: build_module(eq_prog), "Unsupported Var operand"
        )

    # -- death table & MemCond (#49) ---------------------------------------

    def test_memcond_amount_rejects_bad_operand(self) -> None:
        """HEAD: 1.5/`"5"`/`True` build `amount: 1`/`5`/`1`; the rest are raw."""
        for case, value in _BAD_VALUES:
            with self.subTest(case=case):
                self._assert_plain_int_error(
                    lambda v=value: MemCond(1002, "==", v), "memory value"
                )

    def test_memcond_epd_addr_rejects_bad_operand(self) -> None:
        """HEAD: `MemCond(1.5, …)` builds `player: 1`; `"abc"` is a raw error."""
        for case, value in _BAD_VALUES:
            with self.subTest(case=case):
                self._assert_plain_int_error(
                    lambda v=value: MemCond(v, "==", 1), "memory destination"
                )

    def test_memcond_size_or_mask_rejects_bad_operand(self) -> None:
        """HEAD: every row is a raw ``TypeError`` from ``& 0xFFFFFFFF``."""
        for case, value in _BAD_VALUES:
            with self.subTest(case=case):
                self._assert_plain_int_error(
                    lambda v=value: MemCond(1002, "==", 1, v),
                    "MemCond size_or_mask",
                )

    def test_memcond_comparison_rejects_bad_operand(self) -> None:
        """HEAD: every numeric row is a raw ``TypeError`` from ``& 0xFF``.

        Only the *numeric* branch is guarded — a comparison *name* is the
        string branch, and an unknown name stays `EUDSL-CMP` (control below).
        """
        for case, value in _NON_STRING_VALUES:
            with self.subTest(case=case):
                self._assert_plain_int_error(
                    lambda v=value: MemCond(1002, v, 1), "MemCond comparison"
                )

        for name in ("abc", "5"):
            with self.subTest(control=name):
                with self.assertRaises(EudslError) as ctx:
                    MemCond(1002, name, 1)
                self.assertEqual(ctx.exception.code, "EUDSL-CMP")
                self.assertIn(f"Unknown comparison: {name}", str(ctx.exception))

    def test_deaths_amount_rejects_bad_operand(self) -> None:
        """HEAD: 1.5/`"5"`/`True` build `amount: 1`/`5`/`1`; the rest are raw."""
        for case, value in _BAD_VALUES:
            with self.subTest(case=case):
                self._assert_plain_int_error(
                    lambda v=value: Deaths(1002, Exactly, v), "memory value"
                )

    def test_setdeaths_value_rejects_bad_operand(self) -> None:
        for case, value in _BAD_VALUES:
            with self.subTest(case=case):
                self._assert_plain_int_error(
                    lambda v=value: SetDeaths(1002, SetTo, v), "memory value"
                )

    def test_memory_records_reject_bad_operand(self) -> None:
        """Chokepoint proof — three of the nine generated memory helpers.

        `Memory`/`SetMemory`/`SetMemoryX` reach the same two resolve functions
        as `Deaths`, so one guard covers the family; enumerating all nine would
        pin the stock-row generator, not the guard.
        """
        rows = (
            ("Memory", lambda v: Memory(3000, Exactly, v)),
            ("SetMemory", lambda v: SetMemory(3000, SetTo, v)),
            ("SetMemoryX", lambda v: SetMemoryX(3000, SetTo, v, 0xFFFF)),
        )
        for case, value in _BAD_VALUES:
            for name, call in rows:
                with self.subTest(record=name, case=case):
                    self._assert_plain_int_error(
                        lambda c=call, v=value: c(v), "memory value"
                    )

    def test_deaths_epd_dest_rejects_bad_operand(self) -> None:
        """HEAD: `Deaths(1.5, …)` builds `player: 1`; `"abc"` is a raw error."""
        for case, value in _BAD_VALUES:
            with self.subTest(case=case):
                self._assert_plain_int_error(
                    lambda v=value: Deaths(v, Exactly, 1), "memory destination"
                )

    def test_setdeaths_epd_dest_rejects_bad_operand(self) -> None:
        for case, value in _BAD_VALUES:
            with self.subTest(case=case):
                self._assert_plain_int_error(
                    lambda v=value: SetDeaths(v, SetTo, 1), "memory destination"
                )

    def test_deaths_comparison_field_rejects_bad_operand(self) -> None:
        """HEAD: every row is a raw ``TypeError`` from ``& 0xFF``."""
        for case, value in _NON_STRING_VALUES:
            with self.subTest(case=case):
                self._assert_plain_int_error(
                    lambda v=value: Deaths(1002, v, 1), "condition field comparison"
                )

    def test_setdeaths_modifier_field_rejects_bad_operand(self) -> None:
        for case, value in _NON_STRING_VALUES:
            with self.subTest(case=case):
                self._assert_plain_int_error(
                    lambda v=value: SetDeaths(1002, v, 1), "action field modifier"
                )

    def test_record_unit_field_rejects_bad_operand(self) -> None:
        """HEAD: every row is a raw ``TypeError`` from ``& 0xFFFF``."""
        rows = (
            ("Deaths", lambda v: Deaths(1002, Exactly, 1, v), "condition field unit"),
            ("SetDeaths", lambda v: SetDeaths(1002, SetTo, 1, v), "action field unit"),
        )
        for case, value in _BAD_VALUES:
            for name, call, surface in rows:
                with self.subTest(record=name, case=case):
                    self._assert_plain_int_error(
                        lambda c=call, v=value: c(v), surface
                    )

    def test_record_mask_field_rejects_bad_operand(self) -> None:
        """HEAD: every row is a raw ``TypeError`` from ``& 0xFFFFFFFF``."""
        for case, value in _BAD_VALUES:
            with self.subTest(case=case):
                self._assert_plain_int_error(
                    lambda v=value: SetDeathsX(1002, Add, 1, 0, v),
                    "action field mask",
                )

    def test_setstringdword_fields_reject_bad_operand(self) -> None:
        """cycle 17: the hand-written row masked before the guard.

        HEAD: `value=1.5` builds `value: 1`, `mask=1.5` builds
        `mask: 1, eudx: 17235` (a different act classification), and
        `modifier=True` builds `modifier: 1`; the rest are raw `TypeError`s.
        `index`/`byte_offset` stay `EUDSL-BOUNDS` (a different family).
        """
        rows = (
            ("SetStringDword value", lambda v: SetStringDword(0, 0, v)),
            ("SetStringDword mask", lambda v: SetStringDword(0, 0, 1, v)),
            (
                "SetStringDword modifier",
                lambda v: SetStringDword(0, 0, 1, 0xFFFFFFFF, v),
            ),
        )
        for case, value in _BAD_VALUES:
            for surface, call in rows:
                with self.subTest(field=surface, case=case):
                    self._assert_plain_int_error(
                        lambda c=call, v=value: c(v), surface
                    )

    def test_memory_addr_rejects_bad_operand(self) -> None:
        """cycle 17: `EPD()` folded the operand into the address-0 lane.

        HEAD: `Memory(True, …)` and every TS row build silently (wrong-lane
        `player: 4293515047`) because the guard ran *after* `EPD()`; the rest
        are raw `TypeError`s. Only parameter 1 (`addr`) is under test here.
        """
        rows = (
            ("Memory", lambda v: Memory(v, Exactly, 1)),
            ("MemoryX", lambda v: MemoryX(v, Exactly, 1, 0xFFFF)),
            ("SetMemory", lambda v: SetMemory(v, SetTo, 1)),
            ("SetMemoryX", lambda v: SetMemoryX(v, SetTo, 1, 0xFFFF)),
        )
        for case, value in _BAD_VALUES:
            for name, call in rows:
                with self.subTest(record=name, case=case):
                    self._assert_plain_int_error(
                        lambda c=call, v=value: c(v), "memory address"
                    )

    def test_setnextptr_src_rejects_bad_operand(self) -> None:
        """cycle 17: `.next` was dereferenced before any type check.

        HEAD: every row is a raw `AttributeError: 'float' object has no
        attribute 'next'`; TS names `undefined` (the failed lookup) and only
        raises `TypeError` for `null`/`undefined`. A plain integer is *also*
        rejected (it has no `.next`) — the legal forms are a `Trigger` and a
        `TriggerFieldRef`.
        """
        def prog() -> None:
            t = Trigger(conds=[], acts=[])
            for case, value in _BAD_VALUES:
                with self.subTest(case=case):
                    self._assert_plain_int_error(
                        lambda v=value: SetNextPtr(v, t), "src"
                    )

        build_module(prog)

    def test_string_and_memory_operand_controls_are_unchanged(self) -> None:
        """Control — green before and after: guard, not a semantic change.

        The records are pinned **literally** (fields and `relocs`) so "no HIR
        movement for legal programs" is the proof the three cycle-17 guards
        only narrowed the operand domain. Byte-identical to `51c6e2e`.
        """
        def prog() -> None:
            self.assertEqual(Memory(0x58A364, Exactly, 1)["player"], 0)
            self.assertEqual(SetMemory(0x58A364, SetTo, 1)["dest"], 0)
            self.assertEqual(
                dict(SetStringDword(0, 0, 1, 0xFFFFFFFF, SetTo)),
                {
                    "mask": 0,
                    "strid": 0,
                    "wavid": 0,
                    "time": 0,
                    "dest": 0,
                    "value": 1,
                    "unit": 0,
                    "acttype": 45,
                    "modifier": SetTo,
                    "flags": 20,
                    "padding": 0,
                    "eudx": 0,
                    "relocs": [
                        {
                            "field_offset": 16,
                            "target_string": 0,
                            "target_offset": 0,
                            "mode": 1,
                        }
                    ],
                },
            )
            t = Trigger(conds=[], acts=[])
            self.assertEqual(
                dict(SetNextPtr(t, t)),
                {
                    "mask": 0,
                    "strid": 0,
                    "wavid": 0,
                    "time": 0,
                    "dest": 0,
                    "value": 0,
                    "unit": 0,
                    "acttype": 45,
                    "modifier": SetTo,
                    "flags": 20,
                    "padding": 0,
                    "eudx": 0,
                    "relocs": [
                        {
                            "field_offset": 16,
                            "target_trigger": 0,
                            "target_offset": 4,
                            "mode": 1,
                        },
                        {
                            "field_offset": 20,
                            "target_trigger": 0,
                            "target_offset": 0,
                            "mode": 4,
                        },
                    ],
                },
            )

        build_module(prog)

    def test_death_table_runtime_operands_build(self) -> None:
        """Control — green before and after: the relocation domain stays legal.

        `Var.value` is a `TriggerFieldRef`, so the resolve functions consume it
        *before* the plain-int guard. The expected records are pinned literally,
        fields **and** `relocs` (mode 4 for a value, mode 1 for a destination),
        so a later "simplification" of the guard cannot narrow the runtime
        domain silently. Byte-identical to `5eb941f`.
        """
        def prog() -> None:
            t = Trigger(conds=[], acts=[])
            var = Var(0)
            self.assertEqual(
                dict(Deaths(1002, Exactly, var)),
                {
                    "locid": 0,
                    "player": 1002,
                    "amount": 0,
                    "unit": 0,
                    "comparison": Exactly,
                    "condtype": 15,
                    "restype": 0,
                    "flags": 0,
                    "eudx": 0,
                    "relocs": [
                        {
                            "field_offset": 8,
                            "target_var": 0,
                            "target_offset": 20,
                            "mode": 4,
                        }
                    ],
                },
            )
            self.assertEqual(
                dict(Deaths(var, Exactly, 1)),
                {
                    "locid": 0,
                    "player": 0,
                    "amount": 1,
                    "unit": 0,
                    "comparison": Exactly,
                    "condtype": 15,
                    "restype": 0,
                    "flags": 0,
                    "eudx": 0,
                    "relocs": [
                        {
                            "field_offset": 4,
                            "target_var": 0,
                            "target_offset": 20,
                            "mode": 1,
                        }
                    ],
                },
            )
            self.assertEqual(
                dict(SetDeaths(1002, SetTo, var)),
                {
                    "mask": 0,
                    "strid": 0,
                    "wavid": 0,
                    "time": 0,
                    "dest": 1002,
                    "value": 0,
                    "unit": 0,
                    "acttype": 45,
                    "modifier": SetTo,
                    "flags": 20,
                    "padding": 0,
                    "eudx": 0,
                    "relocs": [
                        {
                            "field_offset": 20,
                            "target_var": 0,
                            "target_offset": 20,
                            "mode": 4,
                        }
                    ],
                },
            )
            self.assertEqual(
                dict(SetDeaths(t.act(0).value, SetTo, 1)),
                {
                    "mask": 0,
                    "strid": 0,
                    "wavid": 0,
                    "time": 0,
                    "dest": 0,
                    "value": 1,
                    "unit": 0,
                    "acttype": 45,
                    "modifier": SetTo,
                    "flags": 20,
                    "padding": 0,
                    "eudx": 0,
                    "relocs": [
                        {
                            "field_offset": 16,
                            "target_trigger": 0,
                            "target_offset": 348,
                            "mode": 1,
                        }
                    ],
                },
            )
            SetNextPtr(t, t)

        build_module(prog)

    # -- S2: same-field write-back on a Var-based view (py only) ------------

    def test_attribute_same_field_assignment_with_var_base_is_noop(self) -> None:
        """S2 (#44 × #45): the write-back guard truth-tested a condition.

        `Var.__eq__` returns a `DeferredCond`, so the old
        `and value.baseEpd == field.baseEpd` chain evaluated a *runtime*
        condition while building and raised `EUDSL-COND-TRUTHIFY` — pointing at
        the wrong fix. After: `CUnit(Var(0)); u.hp = u.hp` is the
        `CUnit(Var(0))` control (66 chars). The identity `is` arm still covers
        a plain-int base; the value arm compares two plain integers instead of
        letting a `Var` comparison decide.
        """
        def assign_same() -> str:
            def prog() -> None:
                u = CUnit(Var(0))
                u.hp = u.hp

            return build_module(prog).to_json()

        def control() -> str:
            def prog() -> None:
                u = CUnit(Var(0))

            return build_module(prog).to_json()

        expected = control()
        self.assertEqual(len(expected), 66)
        self.assertEqual(assign_same(), expected)

    def test_attribute_assignment_with_var_base_rejects_other_fields(self) -> None:
        """Control — green before and after: only the *same* field is a no-op."""
        def assign_other_field() -> None:
            u = CUnit(Var(0))
            u.hp = u.shield

        def assign_float() -> None:
            u = CUnit(Var(0))
            u.hp = 1.5

        def add_one() -> None:
            u = CUnit(Var(0))
            u.hp += 1

        def add_one_constant_base() -> str:
            def prog() -> None:
                u = CUnit(1000)
                u.hp += 1

            return build_module(prog).to_json()

        self._assert_eudsl_type(lambda: build_module(assign_other_field))
        self._assert_eudsl_type(lambda: build_module(assign_float))
        with self.assertRaises(EudslError) as ctx:
            build_module(add_one)
        self.assertEqual(ctx.exception.code, "EUDSL-TARGET")
        self.assertIn("with_cp", str(ctx.exception))
        self.assertEqual(len(add_one_constant_base()), 253)


if __name__ == "__main__":
    unittest.main()
