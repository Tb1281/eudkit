"""Shared deferred-binding state machine for lazy declarations (ADR 0005 §7).

Every lazy handle resolves to a fully eager per-builder binding on first
resolving use inside ``build_module``. Only the ``instantiate`` thunk varies
between the eight lazy types; everything else is this state machine:

- the eager fast path (handled by :meth:`LazyBinding.of` returning ``None``),
- the active-builder lookup,
- the cached binding for that builder,
- binding storage, resolution count, and first-binding capture,
- the post-build zero/one/many ``EUDSL-SCOPE`` policy.

A handle stores its state in ``_deferred`` (an explicit initialisation; eager
handles keep ``False`` or, for eager struct instances, no attribute at all).
Callers keep their ``_resolve`` method name and delegate here, so the public
surface is unchanged.
"""

from __future__ import annotations

from typing import Any, Callable, Generic, TypeVar
from weakref import WeakKeyDictionary

from .trigger import _get_active_module_builder
from .view import EudslError

T = TypeVar("T")


class LazyBinding(Generic[T]):
    """Per-handle resolution state for one lazy declaration.

    ``type_name`` names the handle in ``EUDSL-SCOPE`` diagnostics (``"Var"``,
    ``"struct"``, ``"FixedArray"``, ...). ``entry`` is the builder entry point
    spelled in that diagnostic; Python's legacy messages say ``buildModule``
    for ``Var``/``struct`` and ``build_module`` for the other six, so it is a
    parameter rather than a shared literal.
    """

    def __init__(self, type_name: str, *, entry: str = "buildModule") -> None:
        self._type_name = type_name
        self._entry = entry
        # Bindings are weak-keyed by builder state so a finished builder (and
        # its binding) can be collected once the module is built;
        # ``_resolved_count`` is monotonic, so post-build ambiguity stays loud
        # even after a builder was collected.
        self._bindings: WeakKeyDictionary = WeakKeyDictionary()
        self._resolved_count = 0
        self._first_binding: T | None = None

    @classmethod
    def of(cls, owner: Any) -> "LazyBinding[Any] | None":
        """The binding state of ``owner``, or ``None`` for an eager handle.

        Eager handles either carry a false ``_deferred`` flag or (for eager
        struct instances) no ``_deferred`` attribute at all, so this probe
        must use ``__dict__.get`` and never trigger ``__getattr__``.
        """
        state = owner.__dict__.get("_deferred", False)
        return state if isinstance(state, cls) else None

    def resolve(self, instantiate: Callable[[], T]) -> T:
        """Resolve to the active builder's binding.

        With an active builder, a missing binding is created by ``instantiate``
        under that builder (separate bindings per builder, D3). With no active
        builder, no allocation ever happens: a handle resolved under exactly
        one builder returns its cached record, one resolved under several
        raises as ambiguous, and a never-resolved handle raises
        ``EUDSL-SCOPE``.
        """
        builder = _get_active_module_builder()
        if builder is not None:
            cached = self._bindings.get(builder)
            if cached is not None:
                return cached
            binding = instantiate()
            self._bindings[builder] = binding
            self._resolved_count += 1
            if self._resolved_count == 1:
                self._first_binding = binding
            return binding
        if self._resolved_count == 0:
            raise EudslError(
                "EUDSL-SCOPE",
                f"deferred {self._type_name} must be used inside {self._entry}()",
            )
        if self._resolved_count == 1:
            assert self._first_binding is not None
            return self._first_binding
        raise EudslError(
            "EUDSL-SCOPE",
            f"deferred {self._type_name} was resolved under "
            f"{self._resolved_count} builders; post-build read is ambiguous",
        )
