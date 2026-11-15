/**
 * Fixed-capacity, STR-backed `FixedString` text buffer.
 *
 * `FixedString(size)` reserves a marker entry in the module string table and
 * allocates a page-pinned scalar holding its late-bound byte address. `size` is
 * the maximum content length: content occupies offsets `0..size-1`, and exactly
 * one byte at offset `size` is reserved for the NUL terminator. Writes that
 * cannot fit fail at build time with `EUDSL-BOUNDS`; a runtime `writeFrom`
 * position is clamped to `size` and every content byte is guarded at runtime.
 * Writes are dword-only: literal runs pack into constant `SetDeaths` dwords,
 * dynamic bytes patch a scratch pointer and select the byte lane at runtime.
 *
 * `write` always starts at byte offset `0`; `append` continues from `lastpos`;
 * `writeFrom` starts at an explicit constant or runtime offset. Static runs
 * defer their trailing NUL so adjacent writes pack into dwords; the terminator
 * is emitted at display/print, scope boundaries, and module end. Dynamic
 * writes still terminate immediately. The terminator never advances `lastpos`
 * past the content.
 *
 * Scope boundaries align partial tails to the next dword with `0x1A` padding
 * (zero-width in SC:R, verified by the committed smoke map). Alignment at a
 * scope boundary is never a per-append decision: it lets the following
 * dynamic bytes bake their byte lane into the emitted mask instead of
 * selecting it with a four-way runtime branch. Padding is used only when the
 * padded tail plus its NUL still fits in the declared capacity; otherwise the
 * boundary falls back to the unaligned lane. A runtime `writeFrom` position
 * makes the lane unknowable, so that string also stays unaligned until the
 * next absolute write.
 *
 * Fixed-width hex placeholders fold directly at their compile-time byte lane,
 * with mask-limited first/last dword writes when the lane is non-zero, so a
 * static run needs no mid-string padding, no runtime position shift, and no
 * action-field patching. Fixed-width decimal placeholders use the same direct
 * strategy: the static target writes each digit byte into its compile-time
 * dword with a byte-masked `SetStringDword` action, so the fixed-width
 * decimal path has no scratch trigger, no `emitVarStore` field patching, and
 * no runtime `pos.shr(2)`. The shared format-run layout walk charges
 * literals, fixed-width hex, `spec.width` per fixed-width decimal
 * placeholder, and the widest variable-width decimal output; a run that would
 * overrun `size` fails with `EUDSL-BOUNDS` before emitting a write.
 *
 * Format strings use bare `{}` / `{:d}` for variable-width decimal and
 * `{:x}` / `{:08x}` for 8-digit lowercase hex. Explicit widths render
 * fixed-width decimal (for example `{:04d}`). `{{` / `}}` escape literal
 * braces. Format mistakes are loud `EUDSL-FORMAT` errors at build time
 * (ADR 0014).
 */

import {
  Add,
  AddrReloc,
  AtLeast,
  Deaths,
  DeathsX,
  DisplayTextIndex,
  Exactly,
  Memory,
  MemoryX,
  SetDeaths,
  SetDeathsX,
  SetMemory,
  SetStringDword,
  SetTo,
  Subtract,
  TriggerFieldReloc,
  emitVarStore,
  type HirAct,
} from "./hir.ts";
import { CP_ADDR } from "./cp.ts";
import { LazyBinding } from "./lazy-binding.ts";
import {
  Trigger,
  _registerTrigger,
  _getActiveModuleBuilder,
} from "./trigger.ts";
import { VarImpl, Expr, allocString, allocStringVar, getScopeSerial, toVar, type ReadonlyVar } from "./var.ts";
import { Func, when } from "./temporal.ts";
import {
  CURRENT_PLAYER,
  EPD_CURRENT_PLAYER,
  EudslError,
  EudslWarning,
  describeValue as describeShared,
  emitWarning,
  onWarning,
  type WarningListener,
  warnLazyInBuilder,
} from "./view.ts";

export { emitWarning, onWarning, type WarningListener };

const MAX_SIZE = 65535;
const DISPLAY_LINE_ADDR = 0x640b58;
const DWORD_BYTES = 4;
const TERMINATOR_BYTES = 1;
const HEX_CONTENT_BYTES = 8;

/** Zero-width alignment pad byte, verified in StarCraft: Remastered. */
export const STRING_PAD_BYTE = 0x1a;

/**
 * Render an operand exactly as `FixedString` always has.
 *
 * `null` renders as `"null"` here (the pre-dedupe rendering); every other
 * operand delegates to the canonical `view` describer. FE0 folded this helper
 * into `describeValue`, whose shared form renders `null` as `a object`.
 */
function describeArg(value: unknown): string {
  if (value === null) return "null";
  return describeShared(value);
}

/**
 * Encode literal content as canonical UTF-8 bytes (RFC 3629).
 *
 * `FixedString(size)` capacity is a byte capacity (ADR 0003), so string
 * content is lowered to its UTF-8 bytes here. Iterating the string yields code
 * points, so a valid UTF-16 surrogate pair is one scalar value and emits four
 * bytes. A lone surrogate is rejected **before** any `TextEncoder` call:
 * `TextEncoder` silently substitutes U+FFFD instead of failing, and an
 * embedded NUL would terminate the `STRx` entry. Both are `EUDSL-TYPE`.
 */
export function encodeUtf8(text: string, what: string): number[] {
  const out: number[] = [];
  for (const ch of text) {
    const cp = ch.codePointAt(0) ?? 0;
    if (cp === 0) {
      throw new EudslError(
        "EUDSL-TYPE",
        `${what}: embedded NUL (U+0000) cannot be stored in a FixedString`,
      );
    }
    if (cp >= 0xd800 && cp <= 0xdfff) {
      throw new EudslError(
        "EUDSL-TYPE",
        `${what}: lone UTF-16 surrogate U+${cp
          .toString(16)
          .toUpperCase()
          .padStart(4, "0")} is not valid UTF-8`,
      );
    }
    if (cp <= 0x7f) {
      out.push(cp);
    } else if (cp <= 0x7ff) {
      out.push(0xc0 | (cp >> 6));
      out.push(0x80 | (cp & 0x3f));
    } else if (cp <= 0xffff) {
      out.push(0xe0 | (cp >> 12));
      out.push(0x80 | ((cp >> 6) & 0x3f));
      out.push(0x80 | (cp & 0x3f));
    } else {
      out.push(0xf0 | (cp >> 18));
      out.push(0x80 | ((cp >> 12) & 0x3f));
      out.push(0x80 | ((cp >> 6) & 0x3f));
      out.push(0x80 | (cp & 0x3f));
    }
  }
  return out;
}

/**
 * Encode one ASCII byte for the format-spec `fill` (ADR 0014). Fill is
 * repeated as a single byte, so a multi-byte character cannot be a fill;
 * literal content uses `encodeUtf8` instead.
 */
function encodeAsciiByte(ch: string, what: string): number {
  const cp = ch.codePointAt(0) ?? 0;
  if (cp > 0x7f) {
    throw new EudslError(
      "EUDSL-ASCII",
      `${what}: format fill must be a single ASCII byte; got U+${cp
        .toString(16)
        .toUpperCase()
        .padStart(4, "0")} in ${JSON.stringify(ch)}`,
    );
  }
  return cp;
}

function hexBytes(value: number): number[] {
  const u = value >>> 0;
  const out: number[] = [];
  for (let i = 7; i >= 0; i--) {
    const nib = (u >>> (4 * i)) & 0xf;
    out.push(nib < 10 ? 0x30 + nib : 0x61 + (nib - 10));
  }
  return out;
}

function decBytes(value: number, width?: number, fill?: string): number[] {
  const u = value >>> 0;
  let s = `${width !== undefined && width < 10 ? u % 10 ** width : u}`;
  if (width !== undefined && s.length < width) {
    const pad = fill ?? " ";
    s = pad.repeat(width - s.length) + s;
  }
  return encodeUtf8(s, "FixedString {:d}");
}

function hasPlaceholder(text: string): boolean {
  let i = 0;
  while (i < text.length) {
    const c = text[i];
    if (c === "{") {
      if (text[i + 1] === "{") {
        i += 2;
      } else {
        return true;
      }
    } else if (c === "}") {
      if (text[i + 1] === "}") {
        i += 2;
      } else {
        return true;
      }
    } else {
      i++;
    }
  }
  return false;
}

export interface FormatSpec {
  readonly fill?: string;
  readonly align?: ">";
  readonly width?: number;
  readonly radix: "dec" | "hex";
}

export type FormatPart =
  | { readonly kind: "lit"; readonly text: string }
  | { readonly kind: "arg"; readonly spec: FormatSpec; readonly radix: "dec" | "hex" };

function parseSpec(spec: string, what: string): FormatSpec {
  if (spec === "") {
    return { fill: undefined, align: undefined, width: undefined, radix: "dec" };
  }
  let fill: string | undefined;
  let align: ">" | undefined;
  let rem = spec;

  const isAlign = (c: string): boolean =>
    c === ">" || c === "<" || c === "^" || c === "=";

  if (spec.length >= 2 && isAlign(spec[1])) {
    const a = spec[1];
    if (a === "<") {
      throw new EudslError(
        "EUDSL-FORMAT",
        `${what}: malformed format string: left alignment '<' is not supported in ${JSON.stringify(spec)}`,
      );
    }
    if (a !== ">") {
      throw new EudslError(
        "EUDSL-FORMAT",
        `${what}: malformed format string: alignment ${JSON.stringify(a)} is not supported in ${JSON.stringify(spec)}`,
      );
    }
    encodeAsciiByte(spec[0], what);
    fill = spec[0];
    align = ">";
    rem = spec.slice(2);
  } else if (isAlign(spec[0])) {
    const a = spec[0];
    if (a === "<") {
      throw new EudslError(
        "EUDSL-FORMAT",
        `${what}: malformed format string: left alignment '<' is not supported in ${JSON.stringify(spec)}`,
      );
    }
    if (a !== ">") {
      throw new EudslError(
        "EUDSL-FORMAT",
        `${what}: malformed format string: alignment ${JSON.stringify(a)} is not supported in ${JSON.stringify(spec)}`,
      );
    }
    fill = " ";
    align = ">";
    rem = spec.slice(1);
  } else {
    for (let j = 0; j < spec.length; j++) {
      if (isAlign(spec[j])) {
        throw new EudslError(
          "EUDSL-FORMAT",
          `${what}: malformed format string: misplaced alignment ${JSON.stringify(spec[j])} in ${JSON.stringify(spec)}`,
        );
      }
    }
  }

  if (align === undefined) {
    if (rem.startsWith("0")) {
      fill = "0";
      align = ">";
      rem = rem.slice(1);
    } else if (rem.length > 0 && rem[0] >= "1" && rem[0] <= "9") {
      fill = " ";
      align = ">";
    }
  }

  if (align !== undefined) {
    if (rem.length === 0 || rem[0] < "1" || rem[0] > "9") {
      throw new EudslError(
        "EUDSL-FORMAT",
        `${what}: malformed format string: missing positive width after align/fill in ${JSON.stringify(spec)}`,
      );
    }
  }

  let width: number | undefined;
  if (rem.length > 0 && rem[0] >= "0" && rem[0] <= "9") {
    let digitStr = "";
    let k = 0;
    while (k < rem.length && rem[k] >= "0" && rem[k] <= "9") {
      digitStr += rem[k];
      k++;
    }
    rem = rem.slice(k);
    if (digitStr.startsWith("0")) {
      throw new EudslError(
        "EUDSL-FORMAT",
        `${what}: malformed format string: invalid width ${JSON.stringify(digitStr)} in ${JSON.stringify(spec)}`,
      );
    }
    width = parseInt(digitStr, 10);
    if (width < 1 || width > 10) {
      throw new EudslError(
        "EUDSL-FORMAT",
        `${what}: malformed format string: width must be between 1 and 10, got ${width} in ${JSON.stringify(spec)}`,
      );
    }
  }

  let radix: "dec" | "hex";
  if (rem === "" || rem === "d") {
    radix = "dec";
  } else if (rem === "x") {
    radix = "hex";
  } else {
    throw new EudslError(
      "EUDSL-FORMAT",
      `${what}: malformed format string: unknown format spec type ${JSON.stringify(rem)} in ${JSON.stringify(spec)}`,
    );
  }

  if (radix === "hex" && width !== undefined && width !== 8) {
    throw new EudslError(
      "EUDSL-FORMAT",
      `${what}: malformed format string: hex format spec requires width 8, got ${width} in ${JSON.stringify(spec)}`,
    );
  }

  return { fill, align, width, radix };
}

/**
 * Parse a `FixedString` format string into literal runs and argument
 * placeholders.
 *
 * Returns `{ parts, placeholders }`. Each part is either
 * `{ kind: "lit", text }` or `{ kind: "arg", spec, radix }`. Python's
 * `parse_format` returns `(parts, placeholders)` with `("lit", text)` /
 * `("arg", FormatSpec)` tuple parts instead. The container difference is
 * intentional: TypeScript uses an object return while Python uses its
 * idiomatic tuple return. `FormatSpec` fields and parsing semantics are
 * identical in both frontends.
 */
export function parseFormat(
  format: string,
  what: string,
): { parts: FormatPart[]; placeholders: number } {
  const parts: FormatPart[] = [];
  let placeholders = 0;
  let lit = "";
  const flush = (): void => {
    if (lit.length > 0) {
      parts.push({ kind: "lit", text: lit });
      lit = "";
    }
  };
  let i = 0;
  while (i < format.length) {
    const c = format[i];
    if (c === "{") {
      const next = format[i + 1] ?? "";
      if (next === "{") {
        lit += "{";
        i += 2;
      } else if (next === "}") {
        flush();
        parts.push({
          kind: "arg",
          spec: { fill: undefined, align: undefined, width: undefined, radix: "dec" },
          radix: "dec",
        });
        placeholders++;
        i += 2;
      } else if (next === ":") {
        const close = format.indexOf("}", i + 2);
        if (close < 0) {
          throw new EudslError(
            "EUDSL-FORMAT",
            `${what}: malformed format string ${JSON.stringify(
              format,
            )}: unterminated format spec (use '{:x}', '{:d}', or '{{' for a literal brace)`,
          );
        }
        const spec = format.slice(i + 2, close);
        const parsedSpec = parseSpec(spec, what);
        flush();
        parts.push({ kind: "arg", spec: parsedSpec, radix: parsedSpec.radix });
        placeholders++;
        i = close + 1;
      } else {
        throw new EudslError(
          "EUDSL-FORMAT",
          `${what}: malformed format string ${JSON.stringify(
            format,
          )}: lone '{' (use '{{' for a literal brace)`,
        );
      }
    } else if (c === "}") {
      const next = format[i + 1] ?? "";
      if (next === "}") {
        lit += "}";
        i += 2;
      } else {
        throw new EudslError(
          "EUDSL-FORMAT",
          `${what}: malformed format string ${JSON.stringify(
            format,
          )}: lone '}' (use '}}' for a literal brace)`,
        );
      }
    } else {
      lit += c;
      i++;
    }
  }
  flush();

  let bareDecimalCount = 0;
  for (const part of parts) {
    if (part.kind === "arg" && part.spec.radix === "dec" && part.spec.width === undefined) {
      bareDecimalCount++;
    }
  }
  if (bareDecimalCount >= 2) {
    emitWarning(
      "EUDSL-WARN-PERF",
      `${what}: format string interpolates 2 or more bare decimal placeholders; fixed-width decimal specs (e.g. '{:\\x1a>4d}') reduce static trigger overhead from ~688T to ~51T static per variable`,
    );
  }

  return { parts, placeholders };
}

type ContentArg = string | number | VarImpl | Expr | ReadonlyVar;

interface ExpandedArg {
  readonly kind: "lit" | "game";
  readonly bytes?: number[];
  readonly radix: "hex" | "dec";
  readonly spec: FormatSpec;
  readonly value?: VarImpl | Expr;
}

function isRuntimeValue(value: ContentArg): value is VarImpl | Expr {
  return value instanceof VarImpl || value instanceof Expr;
}

/**
 * Materialize a string operand; the domain is
 * `Var | Expr | ReadonlyVar`.
 *
 * The runtime guards (`isRuntimeValue` at the content/position edges) narrow
 * first; `ReadonlyVar` is a `VarImpl` at run time, so this delegates the
 * shared "constant or handle -> Var" step to the canonical kernel in `var`.
 */
function materialize(value: VarImpl | Expr): VarImpl {
  return toVar(value);
}

const DECIMAL_POWERS = [
  1_000_000_000, 100_000_000, 10_000_000, 1_000_000, 100_000, 10_000, 1_000,
  100, 10, 1,
] as const;
const DECIMAL_CONTENT_BYTES = DECIMAL_POWERS.length;

/**
 * Shared variable-width decimal kernel, one body per build and capacity.
 *
 * Inline, a bare `{}` is ~688T of static triggers (10 digit ladders plus the
 * conditional byte appends). Declaring the loop once and calling it makes K
 * sites cost O(K + M) static, with each extra site paying only the argument
 * transfers and the call/result sequence.
 *
 * The body is emitted against parameter variables for the position and buffer
 * base, and it returns the advanced position. The capacity stays a compile-time
 * constant, so one body serves every `{}` site on strings of the same size and
 * the per-digit `pos < size` guard folds to a direct condition (a runtime
 * `limit` would force a var/var comparison: two page-firing transfers per
 * digit, which regressed executed cost). Distinct sizes get distinct bodies.
 */
type DecimalKernel = ((...args: readonly (VarImpl | number)[]) => VarImpl) & {
  readonly funcId: number;
};

const decimalKernels = new WeakMap<object, Map<number, DecimalKernel>>();

interface RunLayout {
  /** Offset the run ends at once every item is laid out. */
  readonly end: number;
}

/**
 * Shared layout walk for one format run.
 *
 * `emitArgs` uses the emission loop's own item order and `checkCapacity`
 * consumes `end` for the capacity bound, so the checked bound cannot drift
 * from what the run writes. Fixed-width decimal placeholders are charged by
 * their declared `spec.width`; variable-width decimal charges the widest
 * output it can produce.
 */
function layoutRun(
  expanded: ExpandedArg[],
  start: number,
  staticKnown: boolean,
): RunLayout {
  let pos = start;
  let staticPosKnown = staticKnown;
  for (const item of expanded) {
    if (item.kind === "lit") {
      pos += item.bytes?.length ?? 0;
      continue;
    }
    if (item.radix === "hex" && staticPosKnown) {
      // Fixed-width hex folds at its compile-time lane, so every following
      // literal and placeholder in this run stays statically knowable.
      pos += HEX_CONTENT_BYTES;
      continue;
    }
    if (item.radix === "dec" && item.spec.width !== undefined && staticPosKnown) {
      // Fixed-width decimal writes each column directly at its compile-time
      // lane, so later items keep their static offsets too.
      pos += item.spec.width;
      continue;
    }
    pos += item.radix === "hex"
      ? HEX_CONTENT_BYTES
      : (item.spec.width ?? DECIMAL_CONTENT_BYTES);
    staticPosKnown = false;
  }
  return { end: pos };
}

export interface HexByteGeometry {
  readonly dwordDelta: number;
  readonly byteInDword: number;
  readonly byteMask: number;
}

export function hexByteGeometry(lane: number, charIdx: number): HexByteGeometry {
  const dwordDelta = (lane + charIdx) >> 2;
  const byteInDword = (lane + charIdx) & 3;
  const byteMask = (0xff << (8 * byteInDword)) >>> 0;
  return { dwordDelta, byteInDword, byteMask };
}

interface HexBit {
  readonly bit: number;
  readonly nibWeight: number;
  readonly charIndex: number;
}

/**
 * Create the eight nibble accumulators and the 32 value-bit triggers shared by
 * the direct-dword and dynamic-byte hex paths.
 *
 * `digitAction`, when supplied, is emitted before the nibble update in the same
 * bit-test trigger. The direct path uses it to add the ASCII digit weight
 * straight to the target string dword.
 */
function accumulateHexNibbles(
  value: VarImpl,
  digitAction?: (bit: HexBit) => HirAct,
): VarImpl[] {
  const nibbles: VarImpl[] = [];
  for (let j = 0; j < HEX_CONTENT_BYTES; j++) {
    const nib = new VarImpl(0);
    nib.set(0);
    nibbles.push(nib);
  }

  for (let b = 0; b < 32; b++) {
    const nibIdx = (b / 4) | 0;
    const nibWeight = (1 << (b % 4)) >>> 0;
    const charIndex = HEX_CONTENT_BYTES - 1 - nibIdx;
    const acts: HirAct[] = [];
    if (digitAction) {
      acts.push(digitAction({ bit: b, nibWeight, charIndex }));
    }
    acts.push(
      SetDeathsX(nibbles[nibIdx].value, Add, nibWeight, 0, 0xffffffff),
    );
    new Trigger({
      conds: [DeathsX(value.value, AtLeast, 1, 0, (1 << b) >>> 0)],
      acts,
    });
  }
  return nibbles;
}

/**
 * Write eight lowercase hex digits directly into a string buffer using
 * compile-time dword offsets and byte-lane masks.
 *
 * The destination is a late-bound `target_string` reloc, so lowering resolves
 * the string address itself: the fixed path needs no runtime `pos.shr(2)` and
 * no scratch-trigger `dest`/`value` patching. Each bit-test trigger adds the
 * ASCII digit weight straight to its target byte; the nibble variables exist
 * only to decide the `>= 10` lowercase adjustment.
 *
 * With `lane != 0` the eight bytes span three dwords. The first and last
 * writes are mask-limited to the bytes the placeholder owns, so adjacent
 * content keeps its bytes.
 */
function emitPackedHex(
  index: number,
  value: VarImpl,
  dwordOffset: number,
  lane: number,
): void {
  const lastDwordDelta = (lane + HEX_CONTENT_BYTES - 1) >> 2;
  for (let d = 0; d <= lastDwordDelta; d++) {
    let mask = 0;
    for (let byte = 0; byte < DWORD_BYTES; byte++) {
      const charIdx = d * DWORD_BYTES + byte - lane;
      if (charIdx >= 0 && charIdx < HEX_CONTENT_BYTES) {
        mask = (mask | (0xff << (8 * byte))) >>> 0;
      }
    }
    SetStringDword(index, (dwordOffset + d) * DWORD_BYTES, 0x30303030, mask);
  }

  const nibbles = accumulateHexNibbles(value, ({ nibWeight, charIndex }) => {
    const { dwordDelta, byteInDword, byteMask } = hexByteGeometry(lane, charIndex);
    const digitWeight = (nibWeight << (8 * byteInDword)) >>> 0;
    return SetStringDword(
      index,
      (dwordOffset + dwordDelta) * DWORD_BYTES,
      digitWeight,
      byteMask,
      Add,
    );
  });

  for (let charIdx = 0; charIdx < HEX_CONTENT_BYTES; charIdx++) {
    const nibIdx = HEX_CONTENT_BYTES - 1 - charIdx;
    const { dwordDelta, byteInDword, byteMask } = hexByteGeometry(lane, charIdx);
    const adjust = (0x27 << (8 * byteInDword)) >>> 0;
    const nib = nibbles[nibIdx];
    when(nib.ge(10), () => {
      SetStringDword(
        index,
        (dwordOffset + dwordDelta) * DWORD_BYTES,
        adjust,
        byteMask,
        Add,
      );
    });
  }
}

function checkFixedStringSize(size: number): void {
  if (!Number.isInteger(size) || size <= 0) {
    throw new EudslError(
      "EUDSL-BOUNDS",
      `FixedString(): size must be a positive integer (bytes), got ${describeArg(size)}`,
    );
  }
  if (size > MAX_SIZE) {
    throw new EudslError(
      "EUDSL-BOUNDS",
      `FixedString(): size ${size} exceeds the build-time cap ${MAX_SIZE}`,
    );
  }
}

function shiftByte(value: number, lane: number | null): number {
  const byte = value & 0xff;
  return lane === null ? byte : (byte << (8 * lane)) >>> 0;
}

/**
 * Destination strategy for the fixed-width decimal digit loop.
 *
 * `begin` creates the per-digit accumulator (or writes its initial byte),
 * `add`/`assign` mutate it, and `end` commits a dynamically appended byte.
 * The static target owns a compile-time dword/lane and writes masked
 * `SetStringDword` actions straight into the string buffer; the dynamic
 * target keeps a scratch `Var` for the runtime-position fallback.
 */
interface FixedDecimalTarget {
  laneFor(k: number): number | null;
  begin(k: number, lane: number | null, base: number): VarImpl | null;
  add(handle: VarImpl | null, k: number, lane: number | null, amount: number): HirAct;
  end(handle: VarImpl | null, k: number, lane: number | null): void;
}

/** Write digit bytes at their compile-time dword and byte lane. */
class StaticDecimalTarget implements FixedDecimalTarget {
  private readonly stringIndex: number;
  private readonly startPos: number;

  constructor(stringIndex: number, startPos: number) {
    this.stringIndex = stringIndex;
    this.startPos = startPos;
  }

  laneFor(k: number): number {
    return (this.startPos + k) & 3;
  }

  private write(k: number, lane: number, value: number, modifier: number): HirAct {
    const dwordOffset = (this.startPos + k) >> 2;
    const mask = (0xff << (8 * lane)) >>> 0;
    return SetStringDword(
      this.stringIndex,
      dwordOffset * DWORD_BYTES,
      shiftByte(value, lane),
      mask,
      modifier,
    );
  }

  begin(k: number, lane: number | null, base: number): null {
    this.write(k, lane!, base, SetTo);
    return null;
  }

  add(handle: VarImpl | null, k: number, lane: number | null, amount: number): HirAct {
    return this.write(k, lane!, amount, Add);
  }

  end(handle: VarImpl | null, k: number, lane: number | null): void {}
}

/** Accumulate a digit in a scratch `Var` and append it at runtime. */
class DynamicDecimalTarget implements FixedDecimalTarget {
  private readonly append: (digit: VarImpl) => void;

  constructor(append: (digit: VarImpl) => void) {
    this.append = append;
  }

  laneFor(k: number): null {
    return null;
  }

  begin(k: number, lane: number | null, base: number): VarImpl {
    const digit = new VarImpl(0);
    digit.set(shiftByte(base, lane));
    return digit;
  }

  add(handle: VarImpl | null, k: number, lane: number | null, amount: number): HirAct {
    return SetDeaths(handle!.value, Add, shiftByte(amount, lane), 0);
  }

  end(handle: VarImpl | null, k: number, lane: number | null): void {
    this.append(handle!);
  }
}

export class FixedString {
  readonly size: number;

  private readonly index: number;
  private baseVar: VarImpl | null = null;
  private posVar: VarImpl | null = null;
  private lineSaveVar: VarImpl | null = null;

  private pending: number[] = [];
  private staticPos: number | null = 0;
  // Conservative next-content offset when the runtime position is known at
  // build time; null once a runtime `writeFrom` position makes it unknowable.
  private boundPos: number | null = 0;
  // Byte lane of the next content position when it is provable at build time.
  // Null means a runtime write or a conditional tail made the lane unknowable.
  private alignedLane: number | null = 0;
  private flushingBoundary = false;
  private staticSerial: number;
  private needsTerminator = false;
  private emitting = false;
  // CP cursor (issue #87): true while the CurrentPlayer cursor holds
  // `base + (pos >> 2)` for the byte writer.
  private cpValid = false;

  constructor(size: number) {
    checkFixedStringSize(size);
    this.size = size;
    const builder = _getActiveModuleBuilder();
    if (!builder || typeof builder.allocString !== "function") {
      throw new EudslError(
        "EUDSL-SCOPE",
        "FixedString() must be constructed inside buildModule()",
      );
    }
    const tag = typeof builder.getNextStringIndex === "function"
      ? builder.getNextStringIndex()
      : 0;
    const dummy = `${" ".repeat(size + TERMINATOR_BYTES)}\x7fEUDString#${tag}\x7f`;
    this.index = allocString(dummy);
    this.staticSerial = getScopeSerial();
    if (typeof builder.registerFlusher === "function") {
      builder.registerFlusher((align) => this.flushAtScopeBoundary(align));
    }
  }

  get messageIndex(): number {
    return this.index;
  }

  write(...args: ContentArg[]): void {
    this.startAt(0);
    this.emitting = true;
    this.emitArgs(args, "write()");
    this.emitting = false;
    this.finish();
  }

  append(...args: ContentArg[]): void {
    if (this.staticPos === null || getScopeSerial() !== this.staticSerial) {
      this.startDynamic();
    }
    this.emitting = true;
    this.emitArgs(args, "append()");
    this.emitting = false;
    this.finish();
  }

  writeFrom(
    pos: number | VarImpl | Expr | ReadonlyVar,
    ...args: ContentArg[]
  ): void {
    if (typeof pos === "number") {
      if (!Number.isInteger(pos) || pos < 0 || pos > this.size) {
        throw new EudslError(
          "EUDSL-BOUNDS",
          `FixedString.writeFrom(): pos must be an integer in 0..${this.size}, got ${pos}`,
        );
      }
      this.flushTail();
      this.startAt(pos);
    } else {
      if (!isRuntimeValue(pos)) {
        throw new EudslError(
          "EUDSL-TYPE",
          `FixedString.writeFrom(): pos must be an integer or game-time value, got ${describeArg(
            pos,
          )}`,
        );
      }
      this.startDynamic(materialize(pos));
    }
    this.emitting = true;
    this.emitArgs(args, "writeFrom()");
    this.emitting = false;
    this.finish();
  }

  write_from(
    pos: number | VarImpl | Expr | ReadonlyVar,
    ...args: ContentArg[]
  ): void {
    this.writeFrom(pos, ...args);
  }

  print(...args: ContentArg[]): void {
    if (args.length > 0) {
      this.write(...args);
    }
    this.display();
  }

  printAt(
    line: number | VarImpl | Expr | ReadonlyVar,
    ...args: ContentArg[]
  ): void {
    if (args.length > 0) {
      this.write(...args);
    }
    this.displayAt(line);
  }

  print_at(
    line: number | VarImpl | Expr | ReadonlyVar,
    ...args: ContentArg[]
  ): void {
    this.printAt(line, ...args);
  }

  display(): void {
    this.flushTail();
    DisplayTextIndex(this.index);
  }

  displayAt(line: number | VarImpl | Expr | ReadonlyVar): void {
    this.flushTail();
    if (typeof line === "number") {
      if (!Number.isInteger(line) || line < 0 || line > 10) {
        throw new EudslError(
          "EUDSL-BOUNDS",
          `FixedString.displayAt(): line must be 0-10, got ${line}`,
        );
      }
    } else if (!isRuntimeValue(line)) {
      throw new EudslError(
        "EUDSL-TYPE",
        `FixedString.displayAt(): line must be an integer 0-10 or a game-time value, got ${describeArg(
          line,
        )}`,
      );
    }

    // eudplib semantics:
    // 1. Read current 0x640b58 (bits 8, 4, 2, 1) into saved
    // 2. Add line to 0x640b58 (modulo 11)
    // 3. DisplayText
    // 4. Restore 0x640b58 = saved
    const saved = this.ensureLineSave();
    saved.set(0);
    for (const bit of [8, 4, 2, 1] as const) {
      when(MemoryX(DISPLAY_LINE_ADDR, AtLeast, 1, bit), () => {
        saved.iadd(bit);
      });
    }

    if (typeof line === "number") {
      if (line !== 0) {
        SetMemory(DISPLAY_LINE_ADDR, Add, line);
        when(Memory(DISPLAY_LINE_ADDR, AtLeast, 11), () => {
          SetMemory(DISPLAY_LINE_ADDR, Subtract, 11);
        });
      }
    } else {
      emitVarStore(materialize(line).id, AddrReloc(DISPLAY_LINE_ADDR), Add);
      when(Memory(DISPLAY_LINE_ADDR, AtLeast, 11), () => {
        SetMemory(DISPLAY_LINE_ADDR, Subtract, 11);
      });
    }

    this.display();
    emitVarStore(saved.id, AddrReloc(DISPLAY_LINE_ADDR), SetTo);
  }

  display_at(line: number | VarImpl | Expr | ReadonlyVar): void {
    this.displayAt(line);
  }

  clear(): void {
    this.pending.length = 0;
    this.needsTerminator = false;
    const clearBytes = Math.min(
      this.size + TERMINATOR_BYTES,
      DWORD_BYTES,
    );
    const mask = (0xffffffff >>> (8 * (DWORD_BYTES - clearBytes))) >>> 0;
    SetStringDword(this.index, 0, 0, mask);
    this.staticPos = 0;
    this.boundPos = 0;
    this.alignedLane = 0;
    this.cpValid = false;
    this.staticSerial = getScopeSerial();
    if (this.posVar !== null) {
      this.posVar.set(0);
    }
  }

  // -------------------------------------------------------------------------
  // Position tracking
  // -------------------------------------------------------------------------

  private startAt(pos: number): void {
    this.pending.length = 0;
    this.needsTerminator = false;
    this.staticPos = pos;
    this.boundPos = pos;
    this.alignedLane = pos & 3;
    this.staticSerial = getScopeSerial();
    this.cpValid = false;
    if (this.posVar !== null) {
      this.posVar.set(pos);
    }
  }

  private startDynamic(pos?: VarImpl): void {
    this.flushTail();
    // `startDynamic` always writes `target` itself below, so it must not
    // install the static position twice when the var is created here.
    const target = this.ensurePos(false);
    if (pos !== undefined) {
      if (pos.id !== target.id) {
        target.set(pos);
      }
      // A runtime position cannot be proven at build time. Clamp it once, then
      // guard every content byte against `size` while it is emitted. Its lane
      // is unknown too, so keep the four-way lane selection. Clear the static
      // position *before* the clamp's scope flush so that flush cannot mark
      // the string as statically aligned again.
      this.boundPos = null;
      this.alignedLane = null;
      this.staticPos = null;
      when(target.gt(this.size), () => {
        target.set(this.size);
      });
    } else if (this.staticPos !== null) {
      // `append()` after a scope boundary continues from the content end
      // written so far. The position variable survives across trigger loops, so
      // re-install it at runtime every frame; a one-time initial value would
      // let the writes walk past the terminator on the second frame.
      target.set(this.staticPos);
    }
    this.staticPos = null;
    // Every branch above may rewrite the runtime position var.
    this.cpValid = false;
    this.staticSerial = getScopeSerial();
  }

  private ensureBase(): VarImpl {
    if (this.baseVar === null) {
      const id = allocStringVar(this.index, 0);
      this.baseVar = new VarImpl(0, id, undefined, "page");
    }
    return this.baseVar;
  }

  private ensurePos(install = true): VarImpl {
    if (this.posVar === null) {
      if (this.staticPos === null) {
        throw new EudslError(
          "EUDSL-SCOPE",
          "FixedString internal error: runtime position requested without a known position",
        );
      }
      this.posVar = new VarImpl(this.staticPos);
      if (install) {
        // The constructor initializer runs once at module load. Re-install the
        // content end at this emission point so a dynamic run that created the
        // position var still starts from `lastpos` on every trigger loop.
        this.posVar.set(this.staticPos);
      }
    }
    return this.posVar;
  }

  private ensureLineSave(): VarImpl {
    if (this.lineSaveVar === null) {
      this.lineSaveVar = new VarImpl(0);
    }
    return this.lineSaveVar;
  }

  private finish(): void {
    this.staticSerial = getScopeSerial();
    if (this.staticPos !== null) {
      this.needsTerminator = true;
      this.alignedLane = (this.staticPos + this.pending.length) & 3;
      if (this.posVar !== null) {
        this.posVar.set(this.staticPos + this.pending.length);
      }
    } else {
      this.appendDynamicByte(new VarImpl(0), false, undefined, 0);
    }
    if (this.cpValid) {
      // CP cursor (issue #87): leave CurrentPlayer at the payload resting
      // value 0 (ADR 0004 resets curpl once per loop; py/camera.py restores
      // 0).
      SetDeaths(EPD_CURRENT_PLAYER, SetTo, 0, 0);
      this.cpValid = false;
    }
  }

  private flushAtScopeBoundary(align = true): void {
    if (this.emitting) {
      this.flushContent();
      return;
    }
    if (!align) {
      this.flushTail();
      return;
    }
    if (this.staticPos === null) {
      this.flushDynamicBoundary();
      return;
    }

    const end = this.staticPos + this.pending.length;
    const pad = (DWORD_BYTES - (end % DWORD_BYTES)) % DWORD_BYTES;
    const fits = end + pad <= this.size;

    // Nothing deferred: only rewrite the terminator when the lane needs
    // normalizing, and never touch a tail this boundary cannot fit.
    if (this.pending.length === 0 && !this.needsTerminator) {
      if ((end & 3) === 0) {
        this.alignedLane = 0;
        return;
      }
      if (!fits) {
        this.alignedLane = null;
        return;
      }
    } else if (!fits) {
      // Keep the content inside this scope, but give up on alignment.
      this.flushTail();
      this.alignedLane = null;
      return;
    }

    for (let i = 0; i < pad; i++) {
      this.pending.push(STRING_PAD_BYTE);
    }
    this.pending.push(0);
    this.flushPending(true);
    this.staticPos = end + pad;
    // Padding consumes content offsets, so the capacity bound moves with it.
    if (this.boundPos !== null) {
      this.boundPos = Math.max(this.boundPos, this.staticPos);
    }
    this.needsTerminator = false;
    this.alignedLane = 0;
    if (this.posVar !== null) {
      this.posVar.set(this.staticPos);
    }
  }

  private flushDynamicBoundary(): void {
    if (this.flushingBoundary) return;
    const lane = this.alignedLane;
    if (lane === null || lane === 0) return;
    const pad = (DWORD_BYTES - lane) % DWORD_BYTES;
    if (this.boundPos === null || this.boundPos + pad > this.size) {
      this.alignedLane = null;
      return;
    }
    this.flushingBoundary = true;
    try {
      for (let i = 0; i < pad; i++) {
        this.appendDynamicByte(
          new VarImpl(STRING_PAD_BYTE),
          true,
          (lane + i) & 3,
        );
      }
      this.appendDynamicByte(new VarImpl(0), false, 0);
      this.alignedLane = 0;
    } finally {
      this.flushingBoundary = false;
    }
  }

  /** Lane of the next content byte, including deferred static bytes. */
  private nextContentLane(): number | null {
    if (this.staticPos !== null) {
      return (this.staticPos + this.pending.length) & 3;
    }
    return this.alignedLane;
  }

  private flushContent(): void {
    if (this.staticPos !== null) {
      this.flushPending(true);
      this.alignedLane = this.staticPos & 3;
    }
  }

  private flushTail(): void {
    if (this.staticPos === null) return;
    if (this.pending.length === 0 && !this.needsTerminator) {
      this.alignedLane = this.staticPos & 3;
      return;
    }
    // Display and module-end tails stay unaligned: no run follows them that
    // could benefit from padding.
    if (this.needsTerminator) {
      const end = this.staticPos + this.pending.length;
      this.pending.push(0);
      this.flushPending(true);
      this.staticPos = end;
      this.needsTerminator = false;
    } else {
      this.flushPending(true);
    }
    this.alignedLane = this.staticPos & 3;
  }

  // -------------------------------------------------------------------------
  // Argument expansion and rendering
  // -------------------------------------------------------------------------

  private emitArgs(args: ContentArg[], what: string): void {
    let expanded: ExpandedArg[];
    const first = args[0];
    if (typeof first === "string" && hasPlaceholder(first)) {
      const { parts, placeholders } = parseFormat(first, `FixedString.${what}`);
      const values = args.slice(1);
      if (placeholders !== values.length) {
        throw new EudslError(
          "EUDSL-FORMAT",
          `FixedString.${what}: format string has ${placeholders} placeholder(s) but ${values.length} arg(s) were given`,
        );
      }
      let vi = 0;
      expanded = parts.map((part) => {
        if (part.kind === "lit") {
          return {
            kind: "lit",
            radix: "hex",
            spec: { radix: "hex" },
            bytes: encodeUtf8(part.text, `FixedString.${what}`),
          };
        }
        return this.classifyArg(values[vi++], part.spec, what);
      });
    } else {
      expanded = args.map((arg) => this.classifyArg(arg, "hex", what));
    }

    // One layout walk drives the capacity bound for the same item order the
    // emission loop below writes, so the two cannot drift.
    const staticKnown = this.staticPos !== null;
    const start = this.staticPos !== null
      ? this.staticPos + this.pending.length
      : this.boundPos;
    if (start !== null) {
      this.checkCapacity(layoutRun(expanded, start, staticKnown), start, what);
    }

    for (const item of expanded) {
      if (item.kind === "lit") {
        this.appendStaticBytes(item.bytes ?? []);
      } else if (item.radix === "hex" && this.staticPos !== null) {
        this.flushPending(true);
        emitPackedHex(
          this.index,
          materialize(item.value as VarImpl | Expr),
          this.staticPos >> 2,
          this.staticPos & 3,
        );
        this.staticPos += HEX_CONTENT_BYTES;
        if (this.boundPos !== null) {
          this.boundPos += HEX_CONTENT_BYTES;
        }
        if (this.posVar !== null) {
          this.posVar.set(this.staticPos);
        }
        this.alignedLane = this.staticPos & 3;
      } else if (item.radix === "dec" && item.spec.width !== undefined && this.staticPos !== null) {
        this.flushPending(true);
        this.emitFixedDecimalValue(materialize(item.value as VarImpl | Expr), item.spec);
        this.staticPos += item.spec.width;
        if (this.boundPos !== null) {
          this.boundPos += item.spec.width;
        }
        if (this.posVar !== null) {
          this.posVar.set(this.staticPos);
        }
        this.alignedLane = this.staticPos & 3;
      } else {
        this.flushPending(true);
        this.ensurePos();
        if (item.radix === "hex") {
          this.emitHexValue(materialize(item.value as VarImpl | Expr));
        } else {
          this.emitDecimalValue(materialize(item.value as VarImpl | Expr), item.spec);
        }
      }
    }
  }

  private checkCapacity(layout: RunLayout, start: number, what: string): void {
    const contentLength = layout.end - start;
    if (layout.end > this.size) {
      throw new EudslError(
        "EUDSL-BOUNDS",
        `FixedString.${what}: ${contentLength} content byte(s) at offset ${start} would end at ${layout.end}, past the ${this.size}-byte capacity`,
      );
    }
  }

  private classifyArg(
    arg: ContentArg,
    spec: FormatSpec | "hex" | "dec",
    what: string,
  ): ExpandedArg {
    const normSpec: FormatSpec = typeof spec === "string" ? { radix: spec } : spec;
    if (typeof arg === "string") {
      return {
        kind: "lit",
        radix: normSpec.radix,
        spec: normSpec,
        bytes: encodeUtf8(arg, `FixedString.${what}`),
      };
    }
    if (typeof arg === "number") {
      if (!Number.isInteger(arg)) {
        throw new EudslError(
          "EUDSL-TYPE",
          `FixedString.${what}: expected an integer, string, or game-time value, got ${arg}`,
        );
      }
      return {
        kind: "lit",
        radix: normSpec.radix,
        spec: normSpec,
        bytes:
          normSpec.radix === "hex"
            ? hexBytes(arg)
            : decBytes(arg, normSpec.width, normSpec.fill),
      };
    }
    if (isRuntimeValue(arg)) {
      return { kind: "game", radix: normSpec.radix, spec: normSpec, value: arg };
    }
    throw new EudslError(
      "EUDSL-TYPE",
      `FixedString.${what}: expected a string, integer, or game-time value, got ${describeArg(
        arg,
      )}`,
    );
  }

  /**
   * Shared fixed-width decimal digit loop.
   *
   * `target` owns the destination: the static target writes each digit byte
   * straight into the buffer's compile-time dword with a byte mask, while the
   * dynamic target accumulates a scratch `Var` and appends it through the
   * runtime-position fallback. `target.laneFor(k)` returns the compile-time
   * lane when it is known, or `null` otherwise.
   */
  private emitFixedDecimalCore(
    value: VarImpl,
    spec: FormatSpec,
    target: FixedDecimalTarget,
  ): void {
    const work = new VarImpl(0);
    work.set(value);
    const width = spec.width ?? DECIMAL_CONTENT_BYTES;
    if (width < 10) {
      work.imod(10 ** width);
    }

    const fillByte = spec.fill !== undefined ? spec.fill.charCodeAt(0) : 0x20;
    const suppressZeros = spec.fill !== "0";
    let started: VarImpl | null = null;
    if (suppressZeros) {
      started = new VarImpl(0);
      started.set(0);
    }

    const asciiZero = 0x30;
    const fillDelta = (asciiZero - fillByte) & 0xff;

    for (let k = 0; k < width; k++) {
      const lane = target.laneFor(k);
      const power = 10 ** (width - 1 - k);
      const isUnits = k === width - 1;
      const weights = power === 1_000_000_000 ? [4, 2, 1] : [8, 4, 2, 1];

      if (!suppressZeros || isUnits) {
        const digit = target.begin(k, lane, asciiZero);
        for (const w of weights) {
          const testVal = (w * power) >>> 0;
          new Trigger({
            conds: [Deaths(work.value, AtLeast, testVal, 0)],
            acts: [
              SetDeaths(work.value, Subtract, testVal, 0),
              target.add(digit, k, lane, w),
            ],
          });
        }
        target.end(digit, k, lane);
      } else {
        const digit = target.begin(k, lane, fillByte);
        // Work still holds this digit's full value here: detecting the first
        // non-zero digit before the weight tests subtract from it removes the
        // per-digit digit-var comparison.
        new Trigger({
          conds: [
            Deaths(started!.value, Exactly, 0, 0),
            Deaths(work.value, AtLeast, power, 0),
          ],
          acts: [SetDeaths(started!.value, SetTo, 1, 0)],
        });
        // Digits after the first non-zero one (and that digit itself, now that
        // `started` is set) turn the fill byte into ASCII '0'; the weight
        // tests below add the digit's value to it.
        new Trigger({
          conds: [Deaths(started!.value, Exactly, 1, 0)],
          acts: [target.add(digit, k, lane, fillDelta)],
        });
        for (const w of weights) {
          const testVal = (w * power) >>> 0;
          new Trigger({
            conds: [Deaths(work.value, AtLeast, testVal, 0)],
            acts: [
              SetDeaths(work.value, Subtract, testVal, 0),
              target.add(digit, k, lane, w),
            ],
          });
        }
        target.end(digit, k, lane);
      }
    }
  }

  private emitFixedDecimalValue(value: VarImpl, spec: FormatSpec): void {
    const startPos = this.staticPos!;
    this.emitFixedDecimalCore(
      value,
      spec,
      new StaticDecimalTarget(this.index, startPos),
    );
  }

  private emitHexValue(value: VarImpl): void {
    const startLane = this.nextContentLane();
    const nibbles = accumulateHexNibbles(value);

    for (let charIdx = 0; charIdx < HEX_CONTENT_BYTES; charIdx++) {
      const nibIdx = HEX_CONTENT_BYTES - 1 - charIdx;
      const nib = nibbles[nibIdx];

      const ascii = new VarImpl(0);
      ascii.set(nib);
      ascii.iadd(0x30);

      when(nib.ge(10), () => {
        ascii.iadd(0x27);
      });

      this.appendDynamicByte(
        ascii,
        true,
        startLane === null
          ? null
          : (startLane + charIdx) & 3,
      );
    }
  }

  private emitDecimalValue(value: VarImpl, spec?: FormatSpec): void {
    if (spec !== undefined && spec.width !== undefined) {
      this.emitFixedDecimalCore(
        value,
        spec,
        new DynamicDecimalTarget((digit) => this.appendDynamicByte(digit, true, null)),
      );
      return;
    }

    // Variable-width decimal: one shared kernel per module and capacity (see
    // `sharedDecimalKernel`). A bare `{}` is ~688T static inline, so sharing
    // it makes K sites cost O(K + M) instead of O(K * M).
    const pos = this.ensurePos();
    const base = this.ensureBase();
    // Seed the byte-writer cursor here, at the call site, where the
    // build-time position may still be known (a constant dword index needs no
    // `pos >> 2` bit ladder). The body assumes a live cursor.
    if (!this.cpValid) {
      const start = this.staticPos;
      this.seedCp(pos, base, start === null ? null : start >> 2);
    }
    const kernel = this.sharedDecimalKernel();
    const newPos = kernel(value, pos, base);
    // The kernel returns the advanced position. Its body keeps the
    // CurrentPlayer cursor in lockstep (`base + (pos >> 2)`, one advance per
    // lane wrap), and it ran against this string's own base, so the caller
    // can keep borrowing the cursor: a following literal or digit run writes
    // through it without re-deriving `pos >> 2`. Only the build-time position
    // becomes unknowable (the width is runtime dependent).
    pos.set(newPos);
    this.cpValid = true;
    this.staticPos = null;
    this.boundPos = null;
    this.alignedLane = null;
  }

  /**
   * Emit the variable-width decimal digit loop at the current state.
   *
   * Shared by the one-per-capacity kernel body: its position and base arrive
   * as parameters, so one body serves every `{}` site of that size.
   */
  private emitDecimalBody(value: VarImpl): void {
    if (!this.cpValid) {
      const start = this.staticPos;
      this.seedCp(
        this.ensurePos(),
        this.ensureBase(),
        start === null ? null : start >> 2,
      );
    }
    const work = new VarImpl(0);
    work.set(value);
    const started = new VarImpl(0);
    started.set(0);
    for (const power of DECIMAL_POWERS) {
      const digit = new VarImpl(0);
      digit.set(0);
      const weights = power === 1_000_000_000 ? [4, 2, 1] : [8, 4, 2, 1];
      for (const w of weights) {
        const testVal = (w * power) >>> 0;
        new Trigger({
          conds: [Deaths(work.value, AtLeast, testVal, 0)],
          acts: [
            SetDeaths(work.value, Subtract, testVal, 0),
            SetDeaths(digit.value, Add, w, 0),
          ],
        });
      }

      when(started.eq(1), () => {
        this.appendDecimalDigit(digit);
      });
      if (power === 1) {
        when(started.eq(0), () => {
          started.set(1);
          this.appendDecimalDigit(digit);
        });
      } else {
        when([started.eq(0), digit.ge(1)], () => {
          started.set(1);
          this.appendDecimalDigit(digit);
        });
      }
    }
  }

  /**
   * Return this build's shared variable-width decimal writer, declaring it on
   * first use.
   *
   * The body runs against parameter variables instead of this string's
   * cursor: `pos` (byte position) and `base` (STR buffer base) arrive per
   * call, and the body returns the advanced position. The capacity stays this
   * declaring string's compile-time `size`, so the digit guards fold to
   * direct conditions; call sites of a different size get their own body.
   * The caller seeds the CurrentPlayer cursor before jumping in.
   */
  private sharedDecimalKernel(): DecimalKernel {
    const builder = _getActiveModuleBuilder();
    if (builder === null) {
      throw new EudslError(
        "EUDSL-SCOPE",
        "a variable-width decimal write must be inside buildModule()",
      );
    }
    let kernels = decimalKernels.get(builder);
    if (kernels === undefined) {
      kernels = new Map<number, DecimalKernel>();
      decimalKernels.set(builder, kernels);
    }
    const cached = kernels.get(this.size);
    if (cached !== undefined) return cached;

    const saved = {
      posVar: this.posVar,
      baseVar: this.baseVar,
      staticPos: this.staticPos,
      boundPos: this.boundPos,
      alignedLane: this.alignedLane,
      cpValid: this.cpValid,
      pending: this.pending,
    };
    this.pending = [];
    let kernel: DecimalKernel;
    try {
      const handle = Func((value: VarImpl, pos: VarImpl, base: VarImpl): VarImpl => {
        this.posVar = pos;
        this.baseVar = base;
        this.staticPos = null;
        this.boundPos = null;
        this.alignedLane = null;
        // The call site seeded the cursor; the body must not re-derive it.
        this.cpValid = true;
        this.emitDecimalBody(value);
        return pos;
      }) as unknown as DecimalKernel;
      kernel = handle;
    } finally {
      this.posVar = saved.posVar;
      this.baseVar = saved.baseVar;
      this.staticPos = saved.staticPos;
      this.boundPos = saved.boundPos;
      this.alignedLane = saved.alignedLane;
      this.cpValid = saved.cpValid;
      this.pending = saved.pending;
    }
    kernels.set(this.size, kernel);
    return kernel;
  }

  private appendDecimalDigit(digit: VarImpl): void {
    // CP cursor (issue #87): ASCII '0' is a compile-time constant; the digit
    // value is added as four constant bit adds, so no `ascii` var and no value
    // shift ladder is emitted.
    this.appendDynamicByte(digit, true, null, 0x30, [1, 2, 4, 8]);
  }

  private appendStaticBytes(bytes: number[]): void {
    if (bytes.length === 0) return;
    if (this.staticPos !== null) {
      this.pending.push(...bytes);
      if (this.boundPos !== null) {
        this.boundPos += bytes.length;
      }
      return;
    }
    if (
      this.alignedLane === 0 &&
      this.boundPos !== null &&
      bytes.length >= DWORD_BYTES
    ) {
      this.appendAlignedDwords(bytes);
      return;
    }
    for (const byte of bytes) {
      this.appendDynamicByte(new VarImpl(byte), true, this.alignedLane);
    }
  }

  private appendAlignedDwords(bytes: number[]): void {
    this.flushPending(true);
    // The dword writes below move `pos` outside the CP-cursor writer, so any
    // borrowed CurrentPlayer cursor is stale from here on.
    this.cpValid = false;
    const pos = this.ensurePos();
    const base = this.ensureBase();
    const dwordIndex = new VarImpl(0);
    dwordIndex.set(pos.shr(2));
    const destBase = new VarImpl(0);
    destBase.set(base);
    destBase.iadd(dwordIndex);

    let consumed = 0;
    let chunk = 0;
    while (bytes.length - consumed >= DWORD_BYTES) {
      let value = 0;
      for (let i = 0; i < DWORD_BYTES; i++) {
        value |= bytes[consumed + i] << (8 * i);
      }
      const dest = new VarImpl(0);
      dest.set(destBase);
      if (chunk > 0) dest.iadd(chunk);
      const ptr = new Trigger({
        conds: [],
        acts: [SetDeaths(0, SetTo, value >>> 0, 0)],
        preserved: true,
        register: false,
      });
      emitVarStore(
        dest.id,
        TriggerFieldReloc(ptr.id, ptr.act(0).dest.byteOffset),
        SetTo,
      );
      _registerTrigger(ptr);
      consumed += DWORD_BYTES;
      chunk += 1;
    }
    pos.iadd(chunk * DWORD_BYTES);
    if (this.boundPos !== null) {
      this.boundPos += chunk * DWORD_BYTES;
    }

    const rem = bytes.length - consumed;
    if (rem > 0) {
      let value = 0;
      for (let i = 0; i < rem; i++) {
        value |= bytes[consumed + i] << (8 * i);
      }
      const dest = new VarImpl(0);
      dest.set(destBase);
      if (chunk > 0) dest.iadd(chunk);
      const mask = (0xffffffff >>> (8 * (DWORD_BYTES - rem))) >>> 0;
      const ptr = new Trigger({
        conds: [],
        acts: [SetDeathsX(0, SetTo, value >>> 0, 0, mask)],
        preserved: true,
        register: false,
      });
      emitVarStore(
        dest.id,
        TriggerFieldReloc(ptr.id, ptr.act(0).dest.byteOffset),
        SetTo,
      );
      _registerTrigger(ptr);
      pos.iadd(rem);
      if (this.boundPos !== null) {
        this.boundPos += rem;
      }
      this.alignedLane = rem & 3;
    } else {
      this.alignedLane = 0;
    }
    this.staticPos = null;
  }

  private flushPending(writePartial: boolean): void {
    if (this.pending.length === 0) return;
    if (this.staticPos === null) {
      throw new EudslError(
        "EUDSL-SCOPE",
        "FixedString internal error: pending bytes without a static position",
      );
    }
    const start = this.staticPos;
    let pos = start;
    let consumed = 0;
    while (consumed < this.pending.length) {
      const aligned = pos & ~3;
      const subp = pos & 3;
      const fit = 4 - subp;
      const n = Math.min(fit, this.pending.length - consumed);
      if (!writePartial && n < fit) break;
      let dword = 0;
      for (let i = 0; i < n; i++) {
        dword |= this.pending[consumed + i] << (8 * (subp + i));
      }
      const byteMask = (0xffffffff >>> (8 * (4 - n))) >>> 0;
      const mask = (byteMask << (8 * subp)) >>> 0;
      SetStringDword(this.index, aligned, dword >>> 0, mask);
      consumed += n;
      pos += n;
    }
    if (consumed > 0) {
      this.pending.splice(0, consumed);
      this.staticPos = pos;
      this.cpValid = false;
    }
  }

  /** CP cursor (issue #87): point CurrentPlayer at the dword holding the next
   * byte.
   *
   * The register holds a dword address, so it must be written by a var-store
   * (an action *value* field is a literal, not a variable reference). When the
   * content start is known at build time the dword index is a constant, so no
   * `pos >> 2` bit ladder is emitted.
   */
  private seedCp(pos: VarImpl, base: VarImpl, dwordIndex: number | null = null): void {
    const cpTarget = new VarImpl(0);
    cpTarget.set(base);
    if (dwordIndex === null) cpTarget.iadd(pos.shr(2));
    else if (dwordIndex !== 0) cpTarget.iadd(dwordIndex);
    emitVarStore(cpTarget.id, AddrReloc(CP_ADDR), SetTo);
    this.cpValid = true;
  }

  private appendDynamicByte(
    value: VarImpl,
    advance = true,
    lane?: number | null,
    constByte?: number,
    bits: readonly number[] = [],
  ): void {
    this.flushPending(true);
    const pos = this.ensurePos();
    const base = this.ensureBase();
    const knownLane = lane === undefined ? this.nextContentLane() : lane;

    if (knownLane !== null) {
      const writeByte = (): void => {
        const dwordIndex = new VarImpl(0);
        dwordIndex.set(pos.shr(2));
        const dest = new VarImpl(0);
        dest.set(base);
        dest.iadd(dwordIndex);

        const mask = (0xff << (8 * knownLane)) >>> 0;
        const ptr = new Trigger({
          conds: [],
          acts: [SetDeathsX(0, SetTo, 0, 0, mask)],
          preserved: true,
          register: false,
        });
        emitVarStore(
          dest.id,
          TriggerFieldReloc(ptr.id, ptr.act(0).dest.byteOffset),
          SetTo,
        );

        const shifted = new VarImpl(0);
        shifted.set(value);
        if (knownLane > 0) shifted.ishl(8 * knownLane);
        emitVarStore(
          shifted.id,
          TriggerFieldReloc(ptr.id, ptr.act(0).value.byteOffset),
          SetTo,
        );
        _registerTrigger(ptr);
      };

      if (advance) {
        when(pos.lt(this.size), () => {
          writeByte();
          pos.iadd(1);
          this.cpValid = false;
          if (this.boundPos !== null) {
            this.boundPos += 1;
          }
        });
        this.staticPos = null;
        this.alignedLane = (knownLane + 1) & 3;
      } else {
        // The terminator is allowed at the clamped position (which can equal
        // `size`), so it bypasses the content-byte guard.
        writeByte();
      }
      return;
    }

    // CP cursor (issue #87): CurrentPlayer-cursor byte-lane writer.
    //
    // The cursor holds `base + (pos >> 2)` (an EPD, so dword-granular). It is
    // advanced in the lane-3 branch when the byte lane wraps, so no per-byte
    // `pos.shr(2)` ladder is emitted. A format run seeds it once (`seedCp` in
    // `emitDecimalValue`); any other unknown-lane write (runtime `writeFrom`,
    // dynamic fixed-width decimal, dynamic static runs) seeds it on demand
    // with a runtime `pos >> 2`. Each lane writes a constant byte plus
    // constant bit adds (`constByte` / `bits`), so no per-lane value shift
    // ladder is emitted either: the ASCII digit is `0x30` plus the digit's
    // 1/2/4/8 bits, all compile-time constants.
    if (!this.cpValid) {
      const start = this.staticPos;
      this.seedCp(pos, base, start === null ? null : start >> 2);
    }

    const writeByte = (): void => {
      for (let k = 0; k < 4; k++) {
        const mask = (0xff << (8 * k)) >>> 0;
        // `pos & 3 === k` as one masked-equality condition: no lane variable
        // and no `pos.bitAnd` action.
        when(DeathsX(pos.value, Exactly, k, 0, 0x3), () => {
          if (constByte !== undefined) {
            SetDeathsX(
              CURRENT_PLAYER,
              SetTo,
              (constByte << (8 * k)) >>> 0,
              0,
              mask,
            );
            for (const bit of bits) {
              when(DeathsX(value.value, AtLeast, 1, 0, bit), () => {
                SetDeathsX(
                  CURRENT_PLAYER,
                  Add,
                  (bit << (8 * k)) >>> 0,
                  0,
                  mask,
                );
              });
            }
          } else {
            // Generic byte value: an 8-bit add ladder.
            SetDeathsX(CURRENT_PLAYER, SetTo, 0, 0, mask);
            for (const bit of [1, 2, 4, 8, 16, 32, 64, 128] as const) {
              when(DeathsX(value.value, AtLeast, 1, 0, bit), () => {
                SetDeathsX(
                  CURRENT_PLAYER,
                  Add,
                  (bit << (8 * k)) >>> 0,
                  0,
                  mask,
                );
              });
            }
          }
          if (k === 3) {
            // Dword-granular cursor: advance on lane wrap.
            SetDeaths(EPD_CURRENT_PLAYER, Add, 1, 0);
          }
        });
      }
    };

    if (advance) {
      when(pos.lt(this.size), () => {
        writeByte();
        pos.iadd(1);
        if (this.boundPos !== null) {
          this.boundPos += 1;
        }
      });
      this.staticPos = null;
    } else {
      // The terminator is allowed at the clamped position (which can equal
      // `size`), so it bypasses the content-byte guard.
      writeByte();
    }
    this.alignedLane = null;
  }
}

/**
 * A lazy `FixedString` handle (globals P4): constructible anywhere (no
 * builder needed) that resolves to a fully eager `FixedString` binding on
 * first resolving use inside `buildModule`, one binding per builder.
 *
 * ADR 0005 D8 selects a wrapper plus explicit method delegation for this
 * phase, not a subclass and not a deferred flag on `FixedString`. `size`
 * is a pure read; `messageIndex` and every method resolve. Binding
 * creation is one full eager `FixedString` construction (string index and
 * flusher registration in eager order), so flusher order follows binding
 * order (D9). A never-used handle emits nothing (D5).
 */
class LazyFixedString {
  readonly size: number;
  // The shared deferred-binding state machine owns the per-builder map,
  // resolution count, first-binding capture, and post-build policy.
  private readonly state = new LazyBinding<FixedString>("FixedString", "buildModule");

  constructor(size: number) {
    checkFixedStringSize(size);
    this.size = size;
  }

  private resolve(): FixedString {
    return this.state.resolve(() => new FixedString(this.size));
  }

  get messageIndex(): number {
    return this.resolve().messageIndex;
  }

  write(...args: ContentArg[]): void {
    this.resolve().write(...args);
  }

  append(...args: ContentArg[]): void {
    this.resolve().append(...args);
  }

  writeFrom(
    pos: number | VarImpl | Expr | ReadonlyVar,
    ...args: ContentArg[]
  ): void {
    this.resolve().writeFrom(pos, ...args);
  }

  write_from(
    pos: number | VarImpl | Expr | ReadonlyVar,
    ...args: ContentArg[]
  ): void {
    this.writeFrom(pos, ...args);
  }

  print(...args: ContentArg[]): void {
    this.resolve().print(...args);
  }

  printAt(
    line: number | VarImpl | Expr | ReadonlyVar,
    ...args: ContentArg[]
  ): void {
    this.resolve().printAt(line, ...args);
  }

  print_at(
    line: number | VarImpl | Expr | ReadonlyVar,
    ...args: ContentArg[]
  ): void {
    this.printAt(line, ...args);
  }

  display(): void {
    this.resolve().display();
  }

  displayAt(line: number | VarImpl | Expr | ReadonlyVar): void {
    this.resolve().displayAt(line);
  }

  display_at(line: number | VarImpl | Expr | ReadonlyVar): void {
    this.displayAt(line);
  }

  clear(): void {
    this.resolve().clear();
  }
}

/**
 * Declare a lazy `FixedString` (globals P4). Call shape mirrors
 * `new FixedString(size)`. Bounds are validated eagerly with the same
 * `EUDSL-BOUNDS` messages; no string-table entry, index, var, or flusher
 * is allocated until first resolving use. See {@link LazyFixedString}.
 * This factory is the documented entry point; the wrapper class stays
 * internal.
 */
export function lazyFixedString(size: number): LazyFixedString {
  warnLazyInBuilder();
  return new LazyFixedString(size);
}

