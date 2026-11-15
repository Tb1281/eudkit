/**
 * 01. Hello World — the smallest playable eudkit TypeScript module.
 *
 * Run these commands from the repository root:
 *
 *   ./target/debug/eudkit synth examples/quickstart/ts/01_hello_world.ts --compact
 *   ./target/debug/eudkit compile examples/quickstart/ts/01_hello_world.ts \
 *       --basemap tests/outputmap/basemap/basemap_silent.scx \
 *       -o /tmp/01_hello_world-ts.scx
 *
 * Inside this repository examples import the raw DSL sources by relative path,
 * from "../../../ts/index.ts". An installed consumer (the published npm
 * package) writes `import { ... } from "eudkit"` instead; the module body is
 * otherwise identical.
 *
 * `DisplayText` shows one chat line to the local player. Wrapping it in
 * `once([], ...)` makes it display on the first trigger loop only instead of
 * every loop. Every source passed to `eudkit` must export exactly one compiled
 * module as `module`.
 */

import { DisplayText, buildModule, once } from "../../../ts/index.ts";

export const module = buildModule(() => {
  once([], () => {
    DisplayText("Hello from eudkit!");
  });
});
