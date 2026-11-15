import test from "node:test";
import assert from "node:assert/strict";
import { hexByteGeometry } from "../string.ts";

test("hexByteGeometry calculates dword delta, byte lane, and byte mask", () => {
  // Lane 0, char 0 -> offset 0
  const g0 = hexByteGeometry(0, 0);
  assert.equal(g0.dwordDelta, 0);
  assert.equal(g0.byteInDword, 0);
  assert.equal(g0.byteMask, 0xff);

  // Lane 1, char 3 -> offset 4
  const g1 = hexByteGeometry(1, 3);
  assert.equal(g1.dwordDelta, 1);
  assert.equal(g1.byteInDword, 0);
  assert.equal(g1.byteMask, 0xff);

  // Lane 2, char 5 -> offset 7
  const g2 = hexByteGeometry(2, 5);
  assert.equal(g2.dwordDelta, 1);
  assert.equal(g2.byteInDword, 3);
  assert.equal(g2.byteMask, 0xff000000);
});
