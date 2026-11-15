"""Ticket #102 cost-gate fixture: the shared Var x Var `mul` kernel and the
single-diff gated `div` / `mod` ladder, one site each.

This is a dedicated fixture rather than an extension of `cost_gate.py` so the
long-frozen 320T ungated / 355T gated headline goldens stay byte-identical; the
two frontends must still synthesize and lower these probes byte-identically.
"""

from py import Var, build_module


def prog():
    a = Var(0xDEADBEEF)
    b = Var(0xFFFF)
    prod = Var(0)
    prod.set(a.mul(b))

    num = Var(1000)
    den = Var(7)
    quot = Var(0)
    quot.set(num.div(den))
    rem = Var(0)
    rem.set(num.mod(den))


module = build_module(prog)
