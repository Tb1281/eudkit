"""Ticket #93: decimal parser parity, ASCII-only widths, and documented parse shape."""

from __future__ import annotations

import json
import subprocess
import unittest
from pathlib import Path

from py import EudslError
from py.string import FormatSpec, parse_format


REPO_ROOT = Path(__file__).resolve().parent.parent.parent

PARITY_FORMATS = [
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
]


def _outcome(fmt: str) -> dict:
    try:
        parts, placeholders = parse_format(fmt, "test")
    except EudslError as err:
        return {"ok": False, "code": err.code}

    normalized = []
    for kind, payload in parts:
        if kind == "lit":
            normalized.append({"kind": "lit", "text": payload})
        else:
            normalized.append(
                {
                    "kind": "arg",
                    "spec": {
                        "fill": payload.fill,
                        "align": payload.align,
                        "width": payload.width,
                        "radix": payload.radix,
                    },
                }
            )
    return {"ok": True, "placeholders": placeholders, "parts": normalized}


class Ticket93DecimalParserParityTest(unittest.TestCase):
    def test_bare_brace_is_decimal_and_explicit_hex_is_hex(self):
        cases = [
            ("{}", FormatSpec(fill=None, align=None, width=None, radix="dec")),
            ("{:d}", FormatSpec(fill=None, align=None, width=None, radix="dec")),
            ("{:x}", FormatSpec(fill=None, align=None, width=None, radix="hex")),
            ("{:08x}", FormatSpec(fill="0", align=">", width=8, radix="hex")),
        ]

        for fmt, expected in cases:
            parts, placeholders = parse_format(fmt, "test")
            self.assertEqual(placeholders, 1, f"placeholder count for {fmt!r}")
            self.assertEqual(len(parts), 1, f"part count for {fmt!r}")
            self.assertEqual(parts[0], ("arg", expected), f"spec for {fmt!r}")

    def test_documented_parse_shape(self):
        # Python returns ``(parts, placeholders)`` with ``("lit", text)`` and
        # ``("arg", FormatSpec)`` tuple parts. TypeScript's parseFormat returns
        # ``{ parts, placeholders }`` with tagged objects; that container
        # difference is intentional and asserted in ts/tests/test_ticket93.ts.
        result = parse_format("a{}b{{c}}{:04d}", "test")
        self.assertIsInstance(result, tuple)
        self.assertEqual(len(result), 2)
        self.assertEqual(
            result,
            (
                [
                    ("lit", "a"),
                    (
                        "arg",
                        FormatSpec(
                            fill=None, align=None, width=None, radix="dec"
                        ),
                    ),
                    ("lit", "b{c}"),
                    ("arg", FormatSpec(fill="0", align=">", width=4, radix="dec")),
                ],
                2,
            ),
        )

    def test_unicode_digits_are_rejected(self):
        cases = [
            ("{:٤d}", "EUDSL-FORMAT"),
            ("{:४d}", "EUDSL-FORMAT"),
            ("{:４d}", "EUDSL-FORMAT"),
            ("{:٠4d}", "EUDSL-FORMAT"),
            ("{:4٤d}", "EUDSL-FORMAT"),
            ("{:٤}", "EUDSL-FORMAT"),
            ("{:٤>4d}", "EUDSL-ASCII"),
        ]

        for fmt, code in cases:
            with self.assertRaises(EudslError, msg=f"Expected {code} for {fmt!r}") as ctx:
                parse_format(fmt, "test")
            self.assertEqual(ctx.exception.code, code, f"Wrong code for {fmt!r}")

    def test_invalid_specs_reject_with_eudsl_format(self):
        bad_specs = [
            "{:12d}",
            "{:0d}",
            "{:0}",
            "{:4x}",
            "{:q}",
            "{:d4}",
            "{",
            "}",
            "{:4d",
        ]

        for fmt in bad_specs:
            with self.assertRaises(
                EudslError, msg=f"Expected EUDSL-FORMAT for {fmt!r}"
            ) as ctx:
                parse_format(fmt, "test")
            self.assertEqual(ctx.exception.code, "EUDSL-FORMAT")

    def test_ts_and_py_parse_outcomes_match(self):
        ts_code = f"""
import {{ EudslError, parseFormat }} from "./ts/index.ts";

const FORMATS = {json.dumps(PARITY_FORMATS)};

function normalizeSpec(spec) {{
  return {{
    fill: spec.fill ?? null,
    align: spec.align ?? null,
    width: spec.width ?? null,
    radix: spec.radix,
  }};
}}

function outcome(fmt) {{
  try {{
    const {{ parts, placeholders }} = parseFormat(fmt, "test");
    return {{
      ok: true,
      placeholders,
      parts: parts.map((part) =>
        part.kind === "lit"
          ? {{ kind: "lit", text: part.text }}
          : {{ kind: "arg", spec: normalizeSpec(part.spec) }}
      ),
    }};
  }} catch (err) {{
    if (err instanceof EudslError) {{
      return {{ ok: false, code: err.code }};
    }}
    throw err;
  }}
}}

process.stdout.write(JSON.stringify(FORMATS.map(outcome)));
"""
        out = subprocess.check_output(
            [
                "node",
                "--experimental-strip-types",
                "--no-warnings",
                "--input-type=module",
                "-e",
                ts_code,
            ],
            cwd=REPO_ROOT,
            text=True,
        )
        ts_outcomes = json.loads(out)
        py_outcomes = [_outcome(fmt) for fmt in PARITY_FORMATS]
        self.assertEqual(ts_outcomes, py_outcomes)


if __name__ == "__main__":
    unittest.main()
