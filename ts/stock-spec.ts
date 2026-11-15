/**
 * Metadata contract for the generated stock emitter (`stock.generated.ts`).
 *
 * `schema/stock-rows.json` is the single source of truth for the stock and
 * eudkit-extension helper rows. The generator renders one `StockRowSpec` per
 * non-special row plus an explicit function declaration whose signature still
 * names the documented parameters and defaults.
 *
 * A row's `params` are in TypeScript signature order. Validation and emission
 * run in the canonical `buildCond`/`buildAct` field order below, which is the
 * order the frozen goldens pin for diagnostics; the field byte offsets come
 * from the HIR 3.1 layout and feed the relocation forms (`epd`,
 * `value-or-ptr`).
 */

/**
 * HIR `eudx` sentinel that tags a masked eudkit wire form (condition/action
 * 15 or 45 with a `mask` parameter). Shared by `hir.ts` (which re-exports it)
 * and the generator, so rendering never has to import the runtime emitter.
 */
export const EUDX_MAGIC = 0x4353;

/** How a helper parameter is checked before it reaches the emitter. */
export type StockValidator =
  | { readonly kind: "none" }
  | {
      readonly kind: "range";
      readonly label: string;
      readonly min: number;
      readonly max?: number;
    }
  | {
      readonly kind: "enum";
      readonly label: string;
      readonly values: readonly number[];
    }
  | {
      readonly kind: "count";
      readonly label: string;
      readonly min: number;
      readonly max: number;
      readonly all: number;
    };

/** How a validated parameter lands in the HIR record. */
export type StockEncoding =
  | "u8"
  | "u16"
  | "u32"
  | "u8-truncate"
  | "u16-truncate"
  | "u32-wrap"
  | "mask"
  | "epd"
  | "epd-from-address"
  | "value-or-ptr"
  | "ptr"
  | "string"
  | "string-index";

/** One ordered helper parameter. */
export interface StockParamSpec {
  /** HIR field (`buildCond`/`buildAct` init key) the parameter feeds. */
  readonly field: string;
  readonly encoding: StockEncoding;
  readonly validator: StockValidator;
}

/** One generated helper row. */
export interface StockRowSpec {
  readonly id: string;
  readonly kind: "condition" | "action";
  readonly chkType: number;
  /** Flags emitted when the row has no `flags` parameter. */
  readonly flags: number;
  /** `eudx` marker emitted by the masked wire forms. */
  readonly eudx?: number;
  /** Modifier emitted when the row has no `modifier` parameter. */
  readonly modifier?: number;
  /** Parameters in TypeScript signature order. */
  readonly params: readonly StockParamSpec[];
}

/** Canonical condition field order (validation and emission). */
export const COND_FIELD_ORDER = [
  "locid",
  "player",
  "amount",
  "unit",
  "comparison",
  "restype",
  "flags",
  "eudx",
] as const;

/** Canonical action field order (validation and emission). */
export const ACT_FIELD_ORDER = [
  "mask",
  "strid",
  "wavid",
  "time",
  "dest",
  "value",
  "unit",
  "modifier",
  "flags",
  "text",
  "wav",
  "uprp",
  "str_index",
  "relocs",
] as const;

/** Byte offsets inside a 20-byte HIR condition record. */
export const COND_FIELD_OFFSETS: Readonly<Record<string, number>> = {
  locid: 0,
  player: 4,
  amount: 8,
  unit: 12,
  comparison: 14,
  condtype: 15,
  restype: 16,
  flags: 17,
  eudx: 18,
};

/**
 * Byte offsets inside a 32-byte HIR action record. Mirrors the Rust
 * constants `eudkit_lir::DEST_FIELD_OFFSET` (16) and
 * `eudkit_lir::VALUE_FIELD_OFFSET` (20); there is no other name for the
 * value field (see ADR 0022).
 */
export const ACT_FIELD_OFFSETS: Readonly<Record<string, number>> = {
  mask: 0,
  strid: 4,
  wavid: 8,
  time: 12,
  dest: 16,
  value: 20,
  unit: 24,
  acttype: 26,
  modifier: 27,
  flags: 28,
  padding: 29,
  eudx: 30,
};
