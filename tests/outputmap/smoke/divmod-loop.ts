/**
 * Smoke map 2: re-entrant `divmod` in `whileLoop` (#122) & raw `Trigger` `ne` gate (#66).
 *
 * Mirrors `divmod-loop.py` 1:1.
 */

import { FixedString, Trigger, Var, buildModule, divmod, every, once, when, whileLoop } from "../../../ts/index.ts";

export const module = buildModule(() => {
  const frame = Var(0);
  const lastQsum = Var(0);
  const lastRsum = Var(0);
  const failCount = Var(0);
  const constQ = Var(0);
  const constR = Var(0);
  const mulCheck = Var(0);
  const neHits = Var(0);

  once(() => {
    const a = Var(1234);
    const b = Var(567);
    mulCheck.set(a.mul(b));

    const gateV = Var(0);
    for (const sample of [499, 500, 501]) {
      gateV.set(sample);
      Trigger({ conds: [gateV.ne(500)], acts: [neHits.act_add(1)] });
    }
  });

  const k = Var(0);
  const num = Var(0);
  const den = Var(7);
  const qsum = Var(0);
  const rsum = Var(0);

  every(6, () => {
    frame.iadd(1);
    const [q, r] = divmod(frame, 10);
    constQ.set(q);
    constR.set(r);

    k.set(0);
    qsum.set(0);
    rsum.set(0);

    whileLoop(k.lt(5), () => {
      num.set(1000000);
      num.iadd(k);
      const [vq, vr] = divmod(num, den);
      qsum.iadd(vq);
      rsum.iadd(vr);
      k.iadd(1);
    });
    lastQsum.set(qsum);
    lastRsum.set(rsum);

    when(qsum.ne(714285), () => failCount.iadd(1));
    when(rsum.ne(15), () => failCount.iadd(1));
  });

  const line1 = new FixedString(64);
  const line2 = new FixedString(64);
  const line3 = new FixedString(64);
  const verdict = new FixedString(64);

  every(6, () => {
    line1.clear();
    line1.write("DIVMOD QSUM={:\x1a>6d}/714285 RSUM={:\x1a>2d}/15 FAIL={:\x1a>3d}", lastQsum, lastRsum, failCount);
    line1.displayAt(1);

    line2.clear();
    line2.write("FRAME={:\x1a>5d} /10 -> Q={:\x1a>4d} R={:\x1a>1d}", frame, constQ, constR);
    line2.displayAt(2);

    line3.clear();
    line3.write("MUL={:\x1a>6d}/699678 NE_GATE={:\x1a>1d}/2", mulCheck, neHits);
    line3.displayAt(3);

    verdict.clear();
    when(
      [failCount.eq(0), mulCheck.eq(699678), neHits.eq(2), lastQsum.eq(714285)],
      () => verdict.write("STATUS: PASS"),
      () => verdict.write("STATUS: FAIL"),
    );
    verdict.displayAt(4);
  });
});
