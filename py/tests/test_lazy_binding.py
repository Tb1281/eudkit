"""State-machine tests for the shared ``LazyBinding`` helper (ADR 0005 §7).

``LazyBinding`` owns the per-handle deferred-binding machine every lazy
declaration uses: eager fast path, active-builder lookup, cached binding,
``instantiate`` thunk, binding storage, resolution count, first-binding
capture, and the post-build zero/one/many policy.

These tests exercise the helper at its module seam plus the exact
``EUDSL-SCOPE`` text every lazy type keeps. They do not reach into the
lazy types' private binding dictionaries.
"""

from __future__ import annotations

import unittest
from typing import Any, Callable

from py import Var, build_module, lazyVar
from py.array import lazy_fixed_array
from py.lazy_binding import LazyBinding
from py.queue import lazy_deque, lazy_queue, lazy_stack, lazy_unit_group
from py.string import lazy_fixed_string
from py.struct import TriggerStruct, dword, lazy_struct
from py.view import EudslError

Point = TriggerStruct("Point", {"x": dword(), "y": dword()})


class _Owner:
    """A handle-shaped object: only deferred handles carry binding state."""


def _attach(owner: _Owner, type_name: str = "Var", entry: str = "buildModule") -> LazyBinding[Any]:
    state: LazyBinding[Any] = LazyBinding(type_name, entry=entry)
    owner._deferred = state
    return state


def _scope_error(call: Callable[[], Any]) -> EudslError:
    try:
        call()
    except EudslError as exc:
        return exc
    raise AssertionError("expected EUDSL-SCOPE")


def _never() -> Any:
    raise AssertionError("post-build read must not instantiate a binding")


class LazyBindingTest(unittest.TestCase):
    # ------------------------------------------------------------------
    # Eager fast path / missing attribute
    # ------------------------------------------------------------------

    def test_eager_handle_has_no_binding_state(self) -> None:
        self.assertIsNone(LazyBinding.of(_Owner()))
        explicit_false = _Owner()
        explicit_false._deferred = False
        self.assertIsNone(LazyBinding.of(explicit_false))

    def test_missing_deferred_attribute_on_eager_struct(self) -> None:
        # An eager TriggerStructInstance never initialises `_deferred`; its
        # `__dict__.get("_deferred", False)` probes must keep returning False
        # and `_resolve` must return the eager instance itself.
        captured: list[Any] = []

        def body() -> None:
            inst = Point.instantiate()
            captured.append(inst)
            self.assertNotIn("_deferred", inst.__dict__)
            self.assertIs(inst._resolve(), inst)
            self.assertEqual(inst.x.id, 0)
            self.assertEqual(inst.y.id, 1)

        build_module(body)
        self.assertIs(captured[0]._resolve(), captured[0])

    def test_eager_var_resolves_to_itself(self) -> None:
        captured: list[Var] = []

        def body() -> None:
            eager = Var(5)
            captured.append(eager)
            self.assertIs(eager._resolve(), eager)

        build_module(body)
        self.assertIs(captured[0]._resolve(), captured[0])
        self.assertEqual(captured[0].id, 0)

    # ------------------------------------------------------------------
    # Active builder, cache, first-binding capture
    # ------------------------------------------------------------------

    def test_builder_active_creates_binding_once_and_caches(self) -> None:
        owner = _Owner()
        state = _attach(owner)
        created: list[object] = []

        def instantiate() -> object:
            created.append(object())
            return created[-1]

        resolved: list[object] = []

        def body() -> None:
            resolved.append(state.resolve(instantiate))
            resolved.append(state.resolve(instantiate))

        build_module(body)
        self.assertEqual(len(created), 1)
        self.assertIs(resolved[0], resolved[1])
        self.assertIs(resolved[0], created[0])

    def test_single_builder_captures_first_binding_for_post_build_read(self) -> None:
        owner = _Owner()
        state = _attach(owner)
        created: list[object] = []

        def body() -> None:
            state.resolve(lambda: created.append(object()) or created[-1])

        build_module(body)
        self.assertEqual(len(created), 1)
        self.assertIs(state.resolve(_never), created[0])

    def test_two_builders_keep_separate_bindings(self) -> None:
        owner = _Owner()
        state = _attach(owner)
        bindings: list[object] = []

        def body() -> None:
            bindings.append(state.resolve(lambda: object()))

        build_module(body)
        build_module(body)
        self.assertEqual(len(bindings), 2)
        self.assertIsNot(bindings[0], bindings[1])
        # Two records: post-build reads stay ambiguous.
        error = _scope_error(lambda: state.resolve(_never))
        self.assertEqual(
            str(error),
            "EUDSL-SCOPE: deferred Var was resolved under 2 builders; "
            "post-build read is ambiguous",
        )

    # ------------------------------------------------------------------
    # Zero / one / many post-build policy
    # ------------------------------------------------------------------

    def test_zero_builders_raises_scope_error(self) -> None:
        error = _scope_error(lambda: _attach(_Owner()).resolve(_never))
        self.assertEqual(
            str(error),
            "EUDSL-SCOPE: deferred Var must be used inside buildModule()",
        )

    def test_entry_point_spelling_is_parameterised(self) -> None:
        owner = _Owner()
        state = _attach(owner, "FixedArray", "build_module")
        error = _scope_error(lambda: state.resolve(_never))
        self.assertEqual(
            str(error),
            "EUDSL-SCOPE: deferred FixedArray must be used inside build_module()",
        )

    def test_scope_messages_are_exact_for_all_lazy_types(self) -> None:
        cases: list[tuple[str, Callable[[], Any]]] = [
            (
                "deferred Var must be used inside buildModule()",
                lambda: lazyVar(0).id,
            ),
            (
                "deferred struct must be used inside buildModule()",
                lambda: lazy_struct(Point).pageIndex,
            ),
            (
                "deferred FixedArray must be used inside build_module()",
                lambda: lazy_fixed_array("dword", 2).vars,
            ),
            (
                "deferred Queue must be used inside build_module()",
                lambda: lazy_queue("dword", 2).length,
            ),
            (
                "deferred Deque must be used inside build_module()",
                lambda: lazy_deque("word", 2).length,
            ),
            (
                "deferred Stack must be used inside build_module()",
                lambda: lazy_stack(2).pos,
            ),
            (
                "deferred UnitGroup must be used inside build_module()",
                lambda: lazy_unit_group(2, autoPrune=False).length,
            ),
            (
                "deferred FixedString must be used inside build_module()",
                lambda: lazy_fixed_string(4).message_index,
            ),
        ]
        for message, call in cases:
            with self.subTest(message=message):
                error = _scope_error(call)
                self.assertEqual(str(error), f"EUDSL-SCOPE: {message}")


if __name__ == "__main__":
    unittest.main()
