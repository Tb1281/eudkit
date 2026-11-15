/**
 * Explicit-entry contract (ADR 0012): the root source exports exactly one
 * compiled module as `module`; the CLI reads that export. The old implicit
 * `lastModule` capture hook must not return to the public API.
 */
import assert from "node:assert/strict";
import test from "node:test";

import * as eudkit from "../index.ts";

test("entry contract: lastModule is not part of the public API", () => {
  assert.equal("lastModule" in eudkit, false);
});
