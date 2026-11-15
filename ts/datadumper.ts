/**
 * The data dumper: a core helper port of eudplib's `_add_datadumper` /
 * `_f_datadumper` (`eudlib/utilf/datadumper.py`) and euddraft's
 * `plugins/dataDumper.py`. Python mirror: `py/datadumper.py`.
 *
 * The helper injects a raw byte payload (an external file such as a
 * `stat_txt.tbl` or a `RequireData` block) into the compiled module and makes
 * the game consume it. The payload lives in a module data blob (issue #99's
 * `Db` / `LirItem::Blob`); the helper only decides how the target memory
 * learns about it:
 *
 * - **default** (no flag): `SetMemory(outOffset, SetTo, db)` — every target
 *   dword receives the blob's payload address.
 * - **`copy`**: the target dword holds the *destination's byte address* (the
 *   game's own pointer, e.g. the `stat_txt.tbl` pointer). The helper reads that
 *   dword at run time and converts it to an EPD (eudplib's `f_epdread_epd`),
 *   then copies the payload there with `memcpyEPD` (eudplib's
 *   `f_repmovsd_epd`), `(len + 3) / 4` dwords. This is the mode eudplib's own
 *   test uses: a `RequireData` file is copied to the address stored at
 *   `0x58D740`.
 * - **`unpatchable`**: eudplib patches the target through `f_dwpatch_epd`,
 *   which also records the previous value on a patch stack so `f_unpatchall`
 *   can restore it. eudkit has neither build-time memory patching nor a patch
 *   stack — every write it emits is already a run-time action — so the mode
 *   emits the same `SetMemory` write as the default mode. The two flags stay
 *   mutually exclusive, as in the source (`assert "copy" not in flags`).
 *
 * The source splits registration (`_add_datadumper`) from emission
 * (`_f_datadumper`, run from `onPluginStart`) because euddraft plugins collect
 * settings first. eudkit has no plugin system, so one `dataDumper` call does
 * both, and the emission is wrapped in `once`: like the source's
 * `onPluginStart` it runs on the first trigger loop and then self-disables.
 *
 * ## Deviations from the source
 *
 * - **One blob per call.** The source's default and `unpatchable` branches
 *   call `Db(inputData)` inside the `outOffsets` loop, allocating one copy of
 *   the payload per target. The port allocates one blob and points every
 *   target at it: the bytes are identical and read-only, so the only
 *   difference is payload size (`len` bytes instead of
 *   `len * outOffsets.length`).
 * - **The payload is padded to a whole dword.** `copy` moves `(len + 3) / 4`
 *   dwords, so the last dword's tail bytes are part of the write. eudplib's
 *   unpadded `Db` reads whatever payload item follows; the port pads the blob
 *   with zeros so the tail is deterministic. (A dword-aligned payload is
 *   unchanged.)
 * - **An empty payload or an empty offset list is a no-op.** The source skips
 *   an empty offset list (`continue`) and would point at a zero-byte blob for
 *   an empty payload; the port returns `undefined` for the former and rejects
 *   the latter with `EUDSL-RANGE`.
 *
 * ## Cost
 *
 * The default and `unpatchable` modes are one `SetMemory` act per target and
 * one `once` gate. `copy` costs one *EPD read* per target (eudplib's
 * `f_epdread_epd`: one accumulator reset, one bit test per bit from 2 up, and
 * the `EPD_BASE` subtraction) on top of the `memcpyEPD` loop (a check-first
 * `while` with one copy trigger per dword). The payload itself is the dominant
 * part of the map-size cost.
 *
 * The helper must run inside {@link buildModule} (creating the blob is the
 * scope gate, exactly as for `Db`).
 */

import { Db, SetMemory, SetTo } from "./hir.ts";
import { memcpyEPD } from "./blockio.ts";
import { once } from "./temporal.ts";
import {
  EPD,
  EudslError,
  describeValue as describe,
  readEpdPointer,
  requirePlainInt,
} from "./view.ts";

/** Copy the payload to the byte address stored in each `outOffset` (read at
 * run time; eudplib's `f_epdread_epd` converts it to an EPD). */
export const FLAG_COPY = "copy";
/**
 * Write the payload address with the source's patch routine. eudkit has no
 * patch stack, so this is the same write as the default mode.
 */
export const FLAG_UNPATCHABLE = "unpatchable";

/** A `dataDumper` mode flag; at most one of the two, never both. */
export type DataDumperFlag = typeof FLAG_COPY | typeof FLAG_UNPATCHABLE;

const FLAGS: ReadonlySet<string> = new Set([FLAG_COPY, FLAG_UNPATCHABLE]);

function requirePayload(inputData: unknown): Uint8Array {
  if (!(inputData instanceof Uint8Array)) {
    throw new EudslError(
      "EUDSL-TYPE",
      `dataDumper(): inputData must be bytes, got ${describe(inputData)}`,
    );
  }
  if (inputData.length === 0) {
    throw new EudslError(
      "EUDSL-RANGE",
      "dataDumper(): inputData must be a non-empty byte string",
    );
  }
  return inputData;
}

function requireOutOffset(value: unknown): number {
  const offset = requirePlainInt(value, "dataDumper(): outOffset");
  if (offset <= 0 || offset > 0xfffffffc || offset % 4 !== 0) {
    throw new EudslError(
      "EUDSL-BOUNDS",
      `dataDumper(): outOffset must be a positive 4-byte-aligned address, got ${offset}`,
    );
  }
  return offset;
}

function requireOutOffsets(value: unknown): number[] {
  if (!Array.isArray(value)) {
    throw new EudslError(
      "EUDSL-TYPE",
      `dataDumper(): outOffsets must be an array of addresses, got ${describe(value)}`,
    );
  }
  return value.map((offset) => requireOutOffset(offset));
}

function requireFlags(value: unknown): ReadonlySet<string> {
  if (typeof value === "string" || value instanceof Uint8Array || !value) {
    throw new EudslError(
      "EUDSL-TYPE",
      `dataDumper(): flags must be a collection of flag names, got ${describe(value)}`,
    );
  }
  const flags = new Set<string>();
  for (const flag of value as Iterable<unknown>) {
    if (typeof flag !== "string" || !FLAGS.has(flag)) {
      throw new EudslError(
        "EUDSL-RANGE",
        `dataDumper(): unknown flag ${describe(flag)}; expected "copy" and/or "unpatchable"`,
      );
    }
    flags.add(flag);
  }
  if (flags.has(FLAG_COPY) && flags.has(FLAG_UNPATCHABLE)) {
    throw new EudslError(
      "EUDSL-BOUNDS",
      `dataDumper(): cannot apply both "copy" and "unpatchable"`,
    );
  }
  return flags;
}

/**
 * Inject `inputData` and point every `outOffset` at it.
 *
 * `outOffsets` are 4-byte-aligned memory addresses (byte addresses, not EPDs).
 * With the `copy` flag each target dword holds the destination's byte address
 * and is read at run time; without it the payload address is written into each
 * target. `unpatchable` is accepted for source parity and writes the same
 * address as the default mode (eudkit has no patch stack; see the module
 * docstring). The two flags are mutually exclusive.
 *
 * The body runs on the first trigger loop (eudplib's `onPluginStart`).
 * Returns the payload blob, or `undefined` when `outOffsets` is empty.
 */
export function dataDumper(
  inputData: Uint8Array,
  outOffsets: readonly number[],
  flags: Iterable<DataDumperFlag | string> = [],
): Db | undefined {
  const payload = requirePayload(inputData);
  const offsets = requireOutOffsets(outOffsets);
  const flagSet = requireFlags(flags);
  if (offsets.length === 0) return undefined;

  const dwords = Math.ceil(payload.length / 4);
  // Pad the tail dword so the `copy` mode's last rep-movsd dword reads
  // in-blob zeros instead of the next payload item (see the docstring).
  const padded = new Uint8Array(dwords * 4);
  padded.set(payload);
  const blob = new Db(padded);

  if (flagSet.has(FLAG_COPY)) {
    once(() => {
      for (const offset of offsets) {
        const destination = readEpdPointer(EPD(offset));
        memcpyEPD(destination, blob, dwords);
      }
    });
    return blob;
  }

  // Default and `unpatchable`: the source writes the payload address into
  // each target and keeps the location for `f_unpatchall`. eudkit never
  // patches memory at build time, so the plain run-time write is the whole
  // observable behaviour.
  once(() => {
    for (const offset of offsets) {
      SetMemory(offset, SetTo, blob);
    }
  });
  return blob;
}
