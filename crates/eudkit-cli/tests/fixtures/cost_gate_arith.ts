/**
 * Ticket #102 cost-gate fixture: the shared Var x Var `mul` kernel and the
 * single-diff gated `div` / `mod` ladder, one site each.
 *
 * This is a dedicated fixture rather than an extension of `cost_gate.ts` so
 * the long-frozen 320T ungated / 355T gated headline goldens stay
 * byte-identical; the two frontends must still synthesize and lower these
 * probes byte-identically.
 */

import { Var, buildModule } from "../../../../ts/index.ts";

export const module = buildModule(() => {
  const a = Var(0xdeadbeef);
  const b = Var(0xffff);
  const prod = Var(0);
  prod.set(a.mul(b));

  const num = Var(1000);
  const den = Var(7);
  const quot = Var(0);
  quot.set(num.div(den));
  const rem = Var(0);
  rem.set(num.mod(den));
});
