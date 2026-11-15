"""Smoke map 2: re-entrant `divmod` in `while_loop` (#122) & raw `Trigger` `ne` gate (#66).

Continuously re-runs the #122 `while_loop` `divmod(1000000 + k, 7)` reproducer every
6 frames so any cross-iteration or cross-frame stale state turns FAIL immediately.
"""

from py import (
    Add,
    FixedString,
    Trigger,
    Var,
    build_module,
    divmod,
    every,
    once,
    when,
    while_loop,
)


def prog() -> None:
    frame = Var(0)
    last_qsum = Var(0)
    last_rsum = Var(0)
    fail_count = Var(0)
    const_q = Var(0)
    const_r = Var(0)
    mul_check = Var(0)
    ne_hits = Var(0)

    def run_checks() -> None:
        a = Var(1234)
        b = Var(567)
        mul_check.set(a.mul(b))  # 699678

        gate_v = Var(0)
        for sample in (499, 500, 501):
            gate_v.set(sample)
            Trigger(conds=[gate_v.ne(500)], acts=[ne_hits.act_add(1)])

    once(run_checks)

    k = Var(0)
    num = Var(0)
    den = Var(7)
    qsum = Var(0)
    rsum = Var(0)

    def loop_step() -> None:
        frame.iadd(1)
        q, r = divmod(frame, 10)
        const_q.set(q)
        const_r.set(r)

        k.set(0)
        qsum.set(0)
        rsum.set(0)

        def body() -> None:
            num.set(1000000)
            num.iadd(k)
            vq, vr = divmod(num, den)
            qsum.iadd(vq)
            rsum.iadd(vr)
            k.iadd(1)

        while_loop(k.lt(5), body)
        last_qsum.set(qsum)
        last_rsum.set(rsum)

        def check_mismatch() -> None:
            when(qsum.ne(714285), lambda: fail_count.iadd(1))
            when(rsum.ne(15), lambda: fail_count.iadd(1))

        check_mismatch()

    every(6, loop_step)

    line1 = FixedString(64)
    line2 = FixedString(64)
    line3 = FixedString(64)
    verdict = FixedString(64)

    def render_hud() -> None:
        line1.clear()
        line1.write("DIVMOD QSUM={:\x1a>6d}/714285 RSUM={:\x1a>2d}/15 FAIL={:\x1a>3d}", last_qsum, last_rsum, fail_count)
        line1.displayAt(1)

        line2.clear()
        line2.write("FRAME={:\x1a>5d} /10 -> Q={:\x1a>4d} R={:\x1a>1d}", frame, const_q, const_r)
        line2.displayAt(2)

        line3.clear()
        line3.write("MUL={:\x1a>6d}/699678 NE_GATE={:\x1a>1d}/2", mul_check, ne_hits)
        line3.displayAt(3)

        verdict.clear()
        when(
            [fail_count.eq(0), mul_check.eq(699678), ne_hits.eq(2), last_qsum.eq(714285)],
            lambda: verdict.write("STATUS: PASS"),
            lambda: verdict.write("STATUS: FAIL"),
        )
        verdict.displayAt(4)

    every(6, render_hud)


module = build_module(prog)
