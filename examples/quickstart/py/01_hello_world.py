"""01. Hello World — the smallest playable eudkit Python module.

Run these commands from the repository root:

    ./target/debug/eudkit synth examples/quickstart/py/01_hello_world.py --compact
    ./target/debug/eudkit compile examples/quickstart/py/01_hello_world.py \
        --basemap tests/outputmap/basemap/basemap_silent.scx \
        -o /tmp/01_hello_world-py.scx

Inside this repository the DSL package is named ``py``, so examples use
``from py import ...``. An installed consumer (``pip install -e ./py``) writes
``from eudkit import ...`` instead; the source is otherwise identical.

``DisplayText`` shows one chat line to the local player. Wrapping it in
``once([], ...)`` makes it display on the first trigger loop only instead of
every loop. Every source passed to ``eudkit`` must expose exactly one compiled
module as the module-scope name ``module``.
"""

from py import DisplayText, build_module, once


def prog() -> None:
    once([], lambda: DisplayText("Hello from eudkit!"))


module = build_module(prog)
