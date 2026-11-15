"""Smoke map 1: runtime `when(..., else_body)` and multi-way `dispatch()`.

Tests Cycle 5/6 control flow in-game:
- 4-case else-chain `dispatch()` (`N <= 9`) on startup values `[0, 1, 2, 3, 99]`;
- 12-case balanced BST `dispatch()` (`N >= 10`) on startup values `[0, 5, 21, 144, 7]`;
- `when([x.eq(42)], then, else)` taking both THEN and ELSE arms;
- Live rolling counter `step` (0..15) every 12 frames with live `else` parity + BST `dispatch`.
"""

from py import FixedString, Var, build_module, dispatch, every, once, when


def prog() -> None:
    chain_sum = Var(0)
    bst_sum = Var(0)
    else_sum = Var(0)

    def chain_dispatch(v: Var) -> None:
        dispatch(
            v,
            [
                (0, lambda: chain_sum.iadd(10)),
                (1, lambda: chain_sum.iadd(20)),
                (2, lambda: chain_sum.iadd(30)),
                (3, lambda: chain_sum.iadd(40)),
            ],
            fallback=lambda: chain_sum.iadd(999),
        )

    def bst_dispatch(v: Var, dst: Var) -> None:
        dispatch(
            v,
            [
                (0, lambda: dst.iadd(100)),
                (1, lambda: dst.iadd(101)),
                (2, lambda: dst.iadd(102)),
                (3, lambda: dst.iadd(103)),
                (5, lambda: dst.iadd(105)),
                (8, lambda: dst.iadd(108)),
                (13, lambda: dst.iadd(113)),
                (21, lambda: dst.iadd(121)),
                (34, lambda: dst.iadd(134)),
                (55, lambda: dst.iadd(155)),
                (89, lambda: dst.iadd(189)),
                (144, lambda: dst.iadd(244)),
            ],
            fallback=lambda: dst.iadd(500),
        )

    def run_checks() -> None:
        probe = Var(0)
        for k in (0, 1, 2, 3, 99):
            probe.set(k)
            chain_dispatch(probe)

        for k in (0, 5, 21, 144, 7):
            probe.set(k)
            bst_dispatch(probe, bst_sum)

        # 42 -> +10; 7 -> +2 => expected 12
        probe.set(42)
        when([probe.eq(42)], lambda: else_sum.iadd(10), lambda: else_sum.iadd(100))
        probe.set(7)
        when([probe.eq(42)], lambda: else_sum.iadd(100), lambda: else_sum.iadd(2))

    once(run_checks)

    step = Var(0)
    parity = Var(0)
    branch_flag = Var(0)
    live_val = Var(0)

    def tick_step() -> None:
        step.iadd(1)
        when(step.ge(16), lambda: step.set(0))
        parity.set(step.bitAnd(1))
        when([parity.eq(0)], lambda: branch_flag.set(0), lambda: branch_flag.set(1))
        live_val.set(0)
        bst_dispatch(step, live_val)

    every(12, tick_step)

    line1 = FixedString(64)
    line2 = FixedString(64)
    line3 = FixedString(64)
    verdict = FixedString(64)

    def render_hud() -> None:
        line1.clear()
        line1.write("DISPATCH CHAIN={:\x1a>4d}/1099 BST={:\x1a>4d}/1070", chain_sum, bst_sum)
        line1.displayAt(1)

        line2.clear()
        line2.write("WHEN ELSE CHECK={:\x1a>2d}/12", else_sum)
        line2.displayAt(2)

        line3.clear()
        line3.write("LIVE STEP={:\x1a>2d} ODD={:\x1a>1d} BST_OUT={:\x1a>3d}", step, branch_flag, live_val)
        line3.displayAt(3)

        verdict.clear()
        when(
            [chain_sum.eq(1099), bst_sum.eq(1070), else_sum.eq(12)],
            lambda: verdict.write("STATUS: PASS"),
            lambda: verdict.write("STATUS: FAIL"),
        )
        verdict.displayAt(4)

    every(6, render_hud)


module = build_module(prog)
