"""03. Control Flow and Time — ``when``/``otherwise``, ``once``, ``every``,
``after``, and ``dispatch``.

Run these commands from the repository root:

    ./target/debug/eudkit synth examples/quickstart/py/03_control_flow_and_temporal.py --compact
    ./target/debug/eudkit compile examples/quickstart/py/03_control_flow_and_temporal.py \
        --basemap tests/outputmap/basemap/basemap_silent.scx \
        -o /tmp/03_control_flow_and_temporal-py.scx

Why can't I write ``if`` or ``while``?

``phase.eq(0)`` is a *runtime condition*: it describes StarCraft memory that
only exists once the map is running. Python ``if``/``while`` execute at build
time, before a game exists, so they cannot branch on it; eudkit rejects the
attempt with ``EUDSL-COND-TRUTHIFY`` (ADR 0020). Use the temporal forms:

* ``when(conds, then, otherwise)`` — every loop while ``conds`` hold; the third
  argument is the runtime else arm (``otherwise``). (In context-manager style,
  ``with when(conds):`` returns an object with a separate ``.otherwise()``
  method; that is a different mechanism from the third positional argument.)
* ``once(conds, body)`` — the first loop ``conds`` hold, then self-disable.
* ``every(n, body)`` — every ``n`` trigger loops.
* ``after(n, body)`` — arm an ``n``-loop countdown, then fire once.
* ``dispatch(value, cases, fallback)`` — one-of-N integer choice with
  compile-time keys (a runtime ``when`` chain, never build-time branching).

Installed consumers (``pip install -e ./py``) import ``from eudkit import ...``;
inside this repository the same names come from ``py``.
"""

from py import FixedString, Var, after, build_module, dispatch, every, once, when


def prog() -> None:
    phase = Var(0)
    score = Var(0)
    banner = FixedString(32)
    status = FixedString(64)

    # once(): a boot banner on the first trigger loop only.
    def show_banner() -> None:
        banner.write("BOOT PHASE=0")
        banner.display()

    once([phase.eq(0)], show_banner)

    # when(..., otherwise): runtime if/else on a game-time Var.
    when(
        [phase.eq(0)],
        lambda: score.set(10),
        lambda: score.set(20),
    )

    # every(): advance only while still in phase 0.
    def advance() -> None:
        when([phase.eq(0)], lambda: phase.set(1))

    every(4, advance)

    # after(): a one-shot delayed transition to phase 2.
    after(9, lambda: phase.set(2))

    # dispatch(): choose a HUD line from the runtime phase value.
    def on_phase_0() -> None:
        status.write("PHASE=0 SCORE={:\x1a>3d}", score)

    def on_phase_1() -> None:
        status.write("PHASE=1 SCORE={:\x1a>3d}", score)

    def on_fallback() -> None:
        status.write("PHASE=2 SCORE={:\x1a>3d}", score)

    dispatch(phase, [(0, on_phase_0), (1, on_phase_1)], on_fallback)
    status.display_at(1)


module = build_module(prog)
