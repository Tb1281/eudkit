import { FixedString, buildModule, once } from "../../../ts/index.ts";

// ADR 0010 smoke map: build on plain `basemap.scx`, whose authored
// `Hello World!` classic trigger is enabled. The runner pair brackets the
// authored chain before the compiled payload, so the authored line must
// render before `RUNNER PAYLOAD`. Both lines use plain `DisplayText` (no
// `printAt`), so their vertical order on screen is execution order.
export const module = buildModule(() => {
  const line = new FixedString(32);
  once(() => {
    line.print("RUNNER PAYLOAD");
  });
});
