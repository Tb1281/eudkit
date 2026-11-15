import test from "node:test";
import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import * as path from "node:path";
import { fileURLToPath } from "node:url";

import { EudslError, parseFormat } from "../index.ts";
import type { FormatSpec } from "../index.ts";

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const repoRoot = path.resolve(__dirname, "../..");

const PARITY_FORMATS = [
  "hello {}",
  "{:d} {:x}",
  "{:x}",
  "{:08x}",
  "{:04d}",
  "{:\x1a>4d}",
  "{:٤d}",
  "{:४d}",
  "{:４d}",
  "{:٠4d}",
  "{:4٤d}",
  "{:٤>4d}",
  "{:12d}",
  "{:0d}",
  "{:0}",
  "{:4x}",
  "{:q}",
  "{",
  "}",
  "{:4d",
];

interface NormalizedSpec {
  fill: string | null;
  align: ">" | null;
  width: number | null;
  radix: "dec" | "hex";
}

type ParseOutcome =
  | {
    ok: true;
    placeholders: number;
    parts: Array<
      | { kind: "lit"; text: string }
      | { kind: "arg"; spec: NormalizedSpec }
    >;
  }
  | { ok: false; code: string };

function normalizeSpec(spec: FormatSpec): NormalizedSpec {
  return {
    fill: spec.fill ?? null,
    align: spec.align ?? null,
    width: spec.width ?? null,
    radix: spec.radix,
  };
}

function parseOutcome(format: string): ParseOutcome {
  try {
    const { parts, placeholders } = parseFormat(format, "test");
    return {
      ok: true,
      placeholders,
      parts: parts.map((part) =>
        part.kind === "lit"
          ? { kind: "lit" as const, text: part.text }
          : { kind: "arg" as const, spec: normalizeSpec(part.spec) }
      ),
    };
  } catch (err) {
    if (err instanceof EudslError) {
      return { ok: false, code: err.code };
    }
    throw err;
  }
}

test("Ticket 93: bare {} is decimal; {:x}/{:08x} are hex", () => {
  const cases: Array<[string, FormatSpec]> = [
    ["{}", { fill: undefined, align: undefined, width: undefined, radix: "dec" }],
    ["{:d}", { fill: undefined, align: undefined, width: undefined, radix: "dec" }],
    ["{:x}", { fill: undefined, align: undefined, width: undefined, radix: "hex" }],
    ["{:08x}", { fill: "0", align: ">", width: 8, radix: "hex" }],
  ];

  for (const [fmt, expected] of cases) {
    const { parts, placeholders } = parseFormat(fmt, "test");
    assert.equal(placeholders, 1, `placeholder count for ${fmt}`);
    assert.equal(parts.length, 1, `part count for ${fmt}`);
    const part = parts[0];
    assert(part.kind === "arg", `expected an argument part for ${fmt}`);
    assert.deepEqual(part.spec, expected, `spec for ${fmt}`);
  }
});

test("Ticket 93: documented parseFormat return shape", () => {
  // TS returns `{ parts, placeholders }` with tagged `{ kind: "lit", text }`
  // and `{ kind: "arg", spec, radix }` objects. Python's parse_format returns
  // `(parts, placeholders)` with tuple parts; that container difference is
  // intentional and asserted in py/tests/test_ticket93.py.
  const result = parseFormat("a{}b{{c}}{:04d}", "test");
  assert.deepEqual(Object.keys(result).sort(), ["parts", "placeholders"]);
  assert.equal(result.placeholders, 2);
  assert.deepEqual(result.parts, [
    { kind: "lit", text: "a" },
    {
      kind: "arg",
      spec: { fill: undefined, align: undefined, width: undefined, radix: "dec" },
      radix: "dec",
    },
    { kind: "lit", text: "b{c}" },
    {
      kind: "arg",
      spec: { fill: "0", align: ">", width: 4, radix: "dec" },
      radix: "dec",
    },
  ]);
});

test("Ticket 93: Unicode digits are rejected in format specs", () => {
  const cases: Array<[string, string]> = [
    ["{:٤d}", "EUDSL-FORMAT"],
    ["{:४d}", "EUDSL-FORMAT"],
    ["{:４d}", "EUDSL-FORMAT"],
    ["{:٠4d}", "EUDSL-FORMAT"],
    ["{:4٤d}", "EUDSL-FORMAT"],
    ["{:٤}", "EUDSL-FORMAT"],
    ["{:٤>4d}", "EUDSL-ASCII"],
  ];

  for (const [fmt, code] of cases) {
    assert.throws(
      () => parseFormat(fmt, "test"),
      (err: any) => err instanceof EudslError && err.code === code,
      `Expected ${code} for ${fmt}`,
    );
  }
});

test("Ticket 93: invalid specs reject with EUDSL-FORMAT", () => {
  const badSpecs = [
    "{:12d}",
    "{:0d}",
    "{:0}",
    "{:4x}",
    "{:q}",
    "{:d4}",
    "{",
    "}",
    "{:4d",
  ];

  for (const fmt of badSpecs) {
    assert.throws(
      () => parseFormat(fmt, "test"),
      (err: any) => err instanceof EudslError && err.code === "EUDSL-FORMAT",
      `Expected EUDSL-FORMAT for ${fmt}`,
    );
  }
});

test("Ticket 93: TS and Python parse outcomes match", () => {
  const pyCode = `
import json
from py import EudslError
from py.string import parse_format

FORMATS = ${JSON.stringify(PARITY_FORMATS)}

def outcome(fmt):
    try:
        parts, placeholders = parse_format(fmt, "test")
        normalized = []
        for kind, payload in parts:
            if kind == "lit":
                normalized.append({"kind": "lit", "text": payload})
            else:
                normalized.append({
                    "kind": "arg",
                    "spec": {
                        "fill": payload.fill,
                        "align": payload.align,
                        "width": payload.width,
                        "radix": payload.radix,
                    },
                })
        return {"ok": True, "placeholders": placeholders, "parts": normalized}
    except EudslError as err:
        return {"ok": False, "code": err.code}

print(json.dumps([outcome(fmt) for fmt in FORMATS]))
`;

  const out = execFileSync(
    "python3",
    ["-c", pyCode],
    {
      cwd: repoRoot,
      env: { ...process.env, PYTHONPATH: "." },
      encoding: "utf-8",
      maxBuffer: 10 * 1024 * 1024,
    },
  );
  const pyOutcomes = JSON.parse(out);
  const tsOutcomes = PARITY_FORMATS.map(parseOutcome);
  assert.deepEqual(tsOutcomes, pyOutcomes);
});
