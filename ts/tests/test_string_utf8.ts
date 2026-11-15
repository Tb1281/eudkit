/**
 * UTF-8 string bytes for `FixedString` (T-UTF8, issue #1).
 *
 * `FixedString(size)` keeps a **byte** capacity (ADR 0003). Literal content is
 * lowered to canonical RFC 3629 UTF-8 bytes, so Korean, Japanese, Chinese,
 * emoji, and Latin text all work without changing the capacity model. An
 * embedded NUL (which would terminate the `STRx` entry) and a lone UTF-16
 * surrogate (which is never valid UTF-8) are rejected with `EUDSL-TYPE` before
 * any bytes are emitted. Format-spec `fill` remains one ASCII byte
 * (`EUDSL-ASCII`).
 */
import assert from "node:assert/strict";
import test from "node:test";

import { Always, EudslError, FixedString, buildModule, when } from "../index.ts";
import { encodeUtf8 } from "../string.ts";
import { rawStringBytes, runSim } from "./helpers.ts";

function assertCode(fn: () => unknown, code: string): EudslError {
  let caught: unknown;
  assert.throws(fn, (err: unknown) => {
    caught = err;
    return err instanceof EudslError && err.code === code;
  });
  return caught as EudslError;
}

function simBytes(prog: () => void, count: number): number[] {
  const mod = buildModule(prog);
  const report = runSim(mod.toJson());
  return rawStringBytes(report.external_epds, count);
}

test("encodeUtf8 parity byte tables", () => {
  const cases: Array<[string, number[]]> = [
    ["ABC", [0x41, 0x42, 0x43]],
    ["é", [0xc3, 0xa9]],
    ["한", [0xed, 0x95, 0x9c]],
    ["日本語", [0xe6, 0x97, 0xa5, 0xe6, 0x9c, 0xac, 0xe8, 0xaa, 0x9e]],
    ["🙂", [0xf0, 0x9f, 0x99, 0x82]],
    [
      "中文한글🙂",
      [
        0xe4, 0xb8, 0xad,
        0xe6, 0x96, 0x87,
        0xed, 0x95, 0x9c,
        0xea, 0xb8, 0x80,
        0xf0, 0x9f, 0x99, 0x82,
      ],
    ],
    ["a한🙂", [0x61, 0xed, 0x95, 0x9c, 0xf0, 0x9f, 0x99, 0x82]],
  ];
  for (const [text, want] of cases) {
    assert.deepEqual(encodeUtf8(text, "test"), want, `bytes for ${text}`);
  }
});

test("encodeUtf8 encoding-length boundaries", () => {
  assert.deepEqual(encodeUtf8("\x7f", "test"), [0x7f]);
  assert.deepEqual(encodeUtf8("\x80", "test"), [0xc2, 0x80]);
  assert.deepEqual(encodeUtf8("\u07ff", "test"), [0xdf, 0xbf]);
  assert.deepEqual(encodeUtf8("\u0800", "test"), [0xe0, 0xa0, 0x80]);
  assert.deepEqual(encodeUtf8("\uffff", "test"), [0xef, 0xbf, 0xbf]);
  assert.deepEqual(encodeUtf8("\u{10ffff}", "test"), [0xf4, 0x8f, 0xbf, 0xbf]);
});

test("a valid UTF-16 surrogate pair is one four-byte code point", () => {
  // Both spellings are the same scalar U+1F642 in a JS string.
  assert.deepEqual(encodeUtf8("\u{1f642}", "test"), [0xf0, 0x9f, 0x99, 0x82]);
  assert.deepEqual(encodeUtf8("\uD83D\uDE42", "test"), [0xf0, 0x9f, 0x99, 0x82]);
});

test("encodeUtf8 rejects an embedded NUL", () => {
  const err = assertCode(() => encodeUtf8("a\x00b", "FixedString.write()"), "EUDSL-TYPE");
  assert.match(err.message, /FixedString\.write\(\)/);
  assert.match(err.message, /U\+0000/);
});

test("encodeUtf8 rejects lone surrogates", () => {
  const lone = [
    String.fromCharCode(0xd800),
    String.fromCharCode(0xdfff),
    String.fromCharCode(0xdc00),
    `a${String.fromCharCode(0xd83d)}b`,
  ];
  for (const text of lone) {
    assertCode(() => encodeUtf8(text, "test"), "EUDSL-TYPE");
  }
});

test("a lone surrogate is rejected", () => {
  assertCode(
    () => encodeUtf8(String.fromCharCode(0xd800), "test"),
    "EUDSL-TYPE",
  );
});

test("FixedString rejects NUL and lone surrogates through write", () => {
  for (const text of ["\x00", String.fromCharCode(0xd800)]) {
    assertCode(
      () =>
        buildModule(() => {
          const s = new FixedString(8);
          s.write(text);
        }),
      "EUDSL-TYPE",
    );
  }
});

test("two-byte content needs two bytes of capacity", () => {
  assertCode(
    () =>
      buildModule(() => {
        const s = new FixedString(1);
        s.write("é");
      }),
    "EUDSL-BOUNDS",
  );

  const bytes = simBytes(() => {
    const s = new FixedString(2);
    s.write("é");
  }, 3);
  assert.deepEqual(bytes, [0xc3, 0xa9, 0x00], "C3 A9 at 0..1 and the NUL at 2");
});

test("two multibyte literals fit exactly by bytes", () => {
  const bytes = simBytes(() => {
    const s = new FixedString(4);
    s.write("é", "é");
  }, 5);
  assert.deepEqual(bytes, [0xc3, 0xa9, 0xc3, 0xa9, 0x00]);

  assertCode(
    () =>
      buildModule(() => {
        const s = new FixedString(3);
        s.write("é", "é");
      }),
    "EUDSL-BOUNDS",
  );
});

test("three-byte Hangul needs three bytes of capacity", () => {
  const bytes = simBytes(() => {
    const s = new FixedString(3);
    s.write("한");
  }, 4);
  assert.deepEqual(bytes, [0xed, 0x95, 0x9c, 0x00]);

  assertCode(
    () =>
      buildModule(() => {
        const s = new FixedString(2);
        s.write("한");
      }),
    "EUDSL-BOUNDS",
  );
});

test("Japanese text round-trips through FixedString", () => {
  const bytes = simBytes(() => {
    const s = new FixedString(9);
    s.write("日本語");
  }, 10);
  assert.deepEqual(bytes, [
    0xe6, 0x97, 0xa5,
    0xe6, 0x9c, 0xac,
    0xe8, 0xaa, 0x9e,
    0x00,
  ]);

  assertCode(
    () =>
      buildModule(() => {
        const s = new FixedString(8);
        s.write("日本語");
      }),
    "EUDSL-BOUNDS",
  );
});

test("mixed Han, Hangul, and emoji round-trip end to end", () => {
  const want = [
    0xe4, 0xb8, 0xad,
    0xe6, 0x96, 0x87,
    0xed, 0x95, 0x9c,
    0xea, 0xb8, 0x80,
    0xf0, 0x9f, 0x99, 0x82,
    0x00,
  ];
  const bytes = simBytes(() => {
    const s = new FixedString(16);
    s.write("中文한글🙂");
  }, 17);
  assert.deepEqual(bytes, want);

  assertCode(
    () =>
      buildModule(() => {
        const s = new FixedString(15);
        s.write("中文한글🙂");
      }),
    "EUDSL-BOUNDS",
  );
});

test("0x1A padding never splits a code point", () => {
  // "é" is 2 content bytes; a scope boundary pads to lane 0 with
  // (-2) % 4 = 2 copies of STRING_PAD_BYTE, never between C3 and A9.
  // (A module-end tail stays unaligned, so force a scope boundary.)
  const bytes = simBytes(() => {
    const s = new FixedString(8);
    s.write("é");
    when(Always(), () => {});
  }, 5);
  assert.deepEqual(
    bytes,
    [0xc3, 0xa9, 0x1a, 0x1a, 0x00],
    "pad bytes follow the complete UTF-8 sequence",
  );
});

test("a non-ASCII literal is not an ASCII error", () => {
  const bytes = simBytes(() => {
    const s = new FixedString(4);
    s.write("한");
    when(Always(), () => {});
  }, 5);
  assert.deepEqual(bytes, [0xed, 0x95, 0x9c, 0x1a, 0x00]);
});

test("a multibyte format fill is rejected with EUDSL-ASCII", () => {
  assertCode(
    () =>
      buildModule(() => {
        const s = new FixedString(16);
        s.write("{:한>4d}", 1);
      }),
    "EUDSL-ASCII",
  );
});

test("ASCII literals stay byte-identical", () => {
  const bytes = simBytes(() => {
    const s = new FixedString(8);
    s.write("ABC");
    when(Always(), () => {});
  }, 5);
  assert.deepEqual(bytes, [0x41, 0x42, 0x43, 0x1a, 0x00]);
});
