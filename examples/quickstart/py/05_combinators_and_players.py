"""05. Combinators and Players — `cond_all`/`cond_any`/`cond_not`, `Player`,
`PVariable`, `for_each_player`, and `display_text_all`.

Run these commands from the repository root:

  ./target/debug/eudkit synth examples/quickstart/py/05_combinators_and_players.py --compact
  ./target/debug/eudkit compile examples/quickstart/py/05_combinators_and_players.py \
      --basemap tests/outputmap/basemap/basemap_silent.scx \
      -o /tmp/05_combinators_and_players-py.scx

What to notice:

- `cond_all`, `cond_any`, and `cond_not` compose arbitrary StarCraft
  conditions (AND/OR/NOT) and lower into flag preludes automatically,
  including >16-condition lists via auto-chunking.
- `Player(0)` exposes typed per-player fields (`minerals`, `gas`,
  `kill_score`, etc.) without raw address math, and `PVariable(init)`
  allocates an 8-slot per-player variable array.
- `for_each_player(body)` walks active players (`player_exists(p) == 1` for
  players `0..7` at runtime, or an unrolled compile-time `players=[...]`
  subset) **without modifying `CurrentPlayer`** (`0x6509B0`).
- `display_text_all` binds `CurrentPlayer` temporarily to `user_player()`
  (local human id, including observers) and restores the previous
  `CurrentPlayer` right after.

Installed consumers import `from eudkit import ...`; inside this repository
the same names come from `py`.
"""

from py import (
    FixedString,
    PVariable,
    Player,
    Var,
    build_module,
    cond_all,
    cond_any,
    cond_not,
    display_text_all,
    for_each_player,
    once,
    when,
)


def prog() -> None:
    ticks = Var(0)
    bonus = PVariable(10)
    p1 = Player(0)
    buf = FixedString(64)

    # Boolean combinators lower OR/NOT through trigger flags while AND flattens.
    ready = cond_all(
        cond_any(ticks.eq(0), ticks.ge(5)),
        cond_not(p1.gas.ge(9999)),
    )

    def on_ready() -> None:
        once(lambda: display_text_all("Combinators & Players ready!"))

        # Runtime player loop (0..7, activeOnly=True): CurrentPlayer stays untouched.
        def add_bonus(player: Var) -> None:
            current = bonus.get(player)
            current.iadd(1)
            bonus.set(player, current)

        for_each_player(add_bonus)

        buf.write("P1 bonus={:d}", bonus.get(0))
        buf.display_at(1)

    when([ready], on_ready)
    ticks.iadd(1)


module = build_module(prog)
