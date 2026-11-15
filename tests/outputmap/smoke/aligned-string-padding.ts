import { FixedString, Var, buildModule, when } from "../../../ts/index.ts";

// SC:R smoke map for capacity-aware aligned FixedString writes.
//
// Line 1 is an unpadded 8-byte control. Line 2 writes the same 8 visible
// characters, but crosses two scope boundaries. The aligned prototype pads
// both boundaries with 0x1A bytes, so the raw second string is
// "AB\x1a\x1aCD\x1a\x1aEFGH".
//
// If 0x1A is zero-width in SC:R, both lines read "ABCDEFGH". If it renders as
// a glyph, moves the cursor, or truncates the line, the pad-byte assumption is
// false and aligned writes must not use 0x1A.
export const module = buildModule(() => {
  const yes = Var(1);

  const control = new FixedString(16);
  control.printAt(1, "ABCDEFGH");

  const padded = new FixedString(32);
  padded.write("AB");
  when(yes.eq(1), () => {
    padded.append("CD");
  });
  padded.append("EFGH");
  padded.displayAt(2);
});
