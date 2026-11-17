"""02. Variables and Strings — runtime ``Var`` arithmetic plus a HUD line.

Run these commands from the repository root:

    ./target/debug/eudkit synth examples/quickstart/py/02_variables_and_strings.py --compact
    ./target/debug/eudkit compile examples/quickstart/py/02_variables_and_strings.py \
        --basemap tests/outputmap/basemap/basemap_silent.scx \
        -o /tmp/02_variables_and_strings-py.scx

What to notice:

* ``Var(0)`` is mutable game memory, not a Python integer. ``counter.iadd(1)``
  queues a trigger action that runs once per trigger loop in StarCraft.
* ``counter.mul(2)`` is an expression; wrapping it in ``Var(...)`` materializes
  the result once so it can be formatted and reused.
* ``FixedString.write`` renders game-time values into SC:R string memory.
  The ``\\x1a`` in a format spec is SC:R's zero-width alignment pad, so
  ``COUNT=   3`` appears without extra visible spaces.
* ``status.display_at(2)`` shifts the display cursor forward by 2 lines
  (mod 11) from its current position and then restores it; it does not write
  to absolute chat line 2. ``display()``
  appends to the next line instead.

Installed consumers (``pip install -e ./py``) import ``from eudkit import ...``;
inside this repository the same names come from ``py``.
"""

from py import FixedString, Var, build_module


def prog() -> None:
    counter = Var(0)
    status = FixedString(64)

    # Runtime arithmetic: each call emits trigger actions, not build-time math.
    counter.iadd(1)
    doubled = Var(counter.mul(2))
    counter.iadd(doubled)

    status.write("COUNT={:\x1a>4d} DOUBLE={:\x1a>4d}", counter, doubled)
    status.display_at(2)


module = build_module(prog)
