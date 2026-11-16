"""Private per-builder linear-congruential random state.

The state is one `Var` cached per active `buildModule` builder, so two modules
compiled in one process cannot share a seed. `srand` writes the seed by value;
`rand` is the HIWORD of one LCG draw and `dwrand` joins the HIWORDs of two
draws. `randomize` folds the game-tick dword into the current state.
"""

from __future__ import annotations

if __package__ in (None, ""):
    # `py/tests/__init__.py` puts `py/` on sys.path, so a bare `import random`
    # resolves this file as a top-level module. Rebind that name to the real
    # stdlib module instead of leaking the eudkit API into it.
    import importlib.util
    import sys
    import sysconfig
    from pathlib import Path

    _STDLIB_RANDOM = Path(sysconfig.get_path("stdlib")) / "random.py"
    _SPEC = importlib.util.spec_from_file_location("random", _STDLIB_RANDOM)
    assert _SPEC is not None and _SPEC.loader is not None
    _MODULE = importlib.util.module_from_spec(_SPEC)
    sys.modules["random"] = _MODULE
    _SPEC.loader.exec_module(_MODULE)
else:
    import weakref
    from typing import Any

    from .camera import GAME_TICK_ADDR
    from .hir import AtLeast, DeathsX
    from .temporal import when
    from .trigger import _get_active_module_builder
    from .var import Expr, ReadonlyVar, Var, divmod
    from .view import EPD, EudslError, _require_plain_int, read_epd

    #: Seed operand domain: the same set `Var.set` accepts.
    RandSeed = int | Var | ReadonlyVar | Expr

    #: LCG multiplier (`eudplib` `f_rand`).
    LCG_MULT: int = 1103515245
    #: LCG increment (`eudplib` `f_rand`).
    LCG_INC: int = 12345

    #: One private state `Var` per builder; never a module-level variable.
    _STATE_CACHE: "weakref.WeakKeyDictionary[Any, Var]" = weakref.WeakKeyDictionary()

    __all__ = [
        "RandSeed", "LCG_MULT", "LCG_INC",
        "srand", "get_seed", "getSeed", "randomize",
        "rand", "dwrand", "dwRand", "rand_range", "randRange",
    ]

    def _state() -> Var:
        """Return this builder's private LCG state, creating it on first use."""
        builder = _get_active_module_builder()
        if builder is None:
            raise EudslError(
                "EUDSL-SCOPE",
                "random state must be created inside buildModule()",
            )
        state = _STATE_CACHE.get(builder)
        if state is None:
            state = Var(0)
            _STATE_CACHE[builder] = state
        return state

    def _next_into(state: Var) -> None:
        """Advance ``state`` by one LCG step, modulo 2**32."""
        state.imul(LCG_MULT)
        state.iadd(LCG_INC)

    def _hiword_into(source: Var, dst: Var, downshift: int) -> None:
        """Add the source's bits 16..31 to ``dst``, shifted by ``downshift``."""
        for bit in range(16, 32):
            add = 1 << (bit - downshift)
            mask_bit = 1 << bit

            def take(add: int = add) -> None:
                dst.iadd(add)

            # A masked `AtLeast` is a bit test; a plain `ge(1 << bit)` would
            # also fire for every lower bit, so it cannot extract a HIWORD.
            when(DeathsX(source.value, AtLeast, mask_bit, 0, mask_bit), take)

    def srand(seed: RandSeed) -> None:
        """Write ``seed`` into this builder's private LCG state by value."""
        _state().set(seed)

    def get_seed() -> Var:
        """Return a fresh copy of the current LCG state without advancing it."""
        out = Var(0)
        out.set(_state())
        return out

    def randomize() -> None:
        """Fold the game-tick dword into the current state, then advance once."""
        state = _state()
        tick = read_epd(EPD(GAME_TICK_ADDR), 32, 0, 0xFFFFFFFF)
        state.iadd(tick)
        state.imul(LCG_MULT)
        state.iadd(LCG_INC)

    def rand() -> Var:
        """Advance the state once and return its high 16 bits as a fresh `Var`."""
        state = _state()
        _next_into(state)
        out = Var(0)
        out.set(0)
        _hiword_into(state, out, 16)
        return out

    def dwrand() -> Var:
        """Advance twice and join the two HIWORDs into one 32-bit fresh `Var`."""
        state = _state()
        _next_into(state)
        out = Var(0)
        out.set(0)
        _hiword_into(state, out, 0)
        _next_into(state)
        _hiword_into(state, out, 16)
        return out

    def rand_range(lo: int, hi: int) -> Var:
        """Return an inclusive ``[lo, hi]`` draw as a fresh `Var`.

        Bounds are build-time integers. ``span == 1`` folds to ``Var(lo)`` and
        consumes no draw; otherwise the result is ``lo + dwrand() % span``.
        """
        lo = _require_plain_int(lo, "rand_range(): lo")
        hi = _require_plain_int(hi, "rand_range(): hi")
        if lo < 0 or hi < lo or hi > 0xFFFFFFFF:
            raise EudslError(
                "EUDSL-RANGE",
                f"rand_range(): bounds must satisfy 0 <= lo <= hi <= 0xFFFFFFFF, "
                f"got {lo}..{hi}",
            )
        span = hi - lo + 1
        if span > 0xFFFFFFFF:
            raise EudslError(
                "EUDSL-RANGE",
                f"rand_range(): span {span} exceeds 0xFFFFFFFF",
            )
        if span == 1:
            return Var(lo)
        draw = dwrand()
        _quot, rem = divmod(draw, span)
        out = Var(lo)
        out.iadd(rem)
        return out

    getSeed = get_seed
    dwRand = dwrand
    randRange = rand_range
