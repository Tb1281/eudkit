/**
 * Root export parity for `readEpd` / `readEpdPointer`.
 *
 * `ts/index.ts` re-exports `view.ts` wholesale, so both names are reachable
 * from the package root. Python's curated barrel must match: this test pins
 * that the barrel import is the *same function object* as the `view.ts`
 * import, and that a one-call program emits the Python fixture HIR.
 *
 * Mirror of `py/tests/test_view_exports.py`.
 */

import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import test from "node:test";

import {
  EPD,
  buildModule,
  readEpd,
  readEpdPointer,
} from "../index.ts";
import {
  readEpd as viewReadEpd,
  readEpdPointer as viewReadEpdPointer,
} from "../view.ts";
import { REPO_ROOT } from "./helpers.ts";

/** eudplib's own test reads the dword pointer at this address. */
const PTR = 0x58d740;

const PY_PARITY_SCRIPT = `
import sys
from py import EPD, build_module, read_epd, read_epd_pointer

PTR = 0x58D740

def prog():
    read_epd(EPD(PTR), 2, 0)
    read_epd_pointer(EPD(PTR))

sys.stdout.write(build_module(prog).to_json())
`;

test("root barrel re-exports the view functions as the same objects", () => {
  assert.equal(typeof readEpd, "function");
  assert.equal(typeof readEpdPointer, "function");
  assert.equal(readEpd, viewReadEpd);
  assert.equal(readEpdPointer, viewReadEpdPointer);
});

test("root imports build a Var and emit the Python fixture HIR", () => {
  const mod = buildModule(() => {
    readEpd(EPD(PTR), 2, 0);
    readEpdPointer(EPD(PTR));
  });
  assert.ok([...mod.vars].length > 0, "the reads must allocate a Var");

  const pyHir = execFileSync("python3", ["-c", PY_PARITY_SCRIPT], {
    cwd: REPO_ROOT,
    encoding: "utf8",
    env: { ...process.env, PYTHONPATH: "." },
  });
  assert.equal(mod.toJson(), pyHir);
});
