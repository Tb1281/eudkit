/**
 * Read/write accessors for the StarCraft Brood War DAT tables.
 *
 * The canonical layouts live in `schema/layouts/dat-*.json`. DAT tables are
 * struct-of-arrays: every field owns an absolute base address, and entry `i`
 * lives at `base + i * stride`. Accessors are therefore constant-index only:
 * sub-dword fields need a compile-time shift/mask, so a runtime index would
 * require a branch per byte lane.
 *
 * `unitsDat[UnitType.TerranMarine].maxHp.set(40)` reuses the same
 * `MemFieldHandle` semantics as `MemView` (`set`/`iadd`/`isub`/`eq`/.../
 * `read`). Successful access never allocates storage; a field access is pure
 * address math, and the emitted action/condition is the same one `CUnit`
 * would produce at that absolute EPD.
 */

import fs from "node:fs";

import {
  EPD,
  EPD_BASE,
  EudslError,
  MemFieldHandle,
  defineMemView,
  describeValue,
  locateSchemaFile,
  type FieldWidth,
  type MemViewSchema,
} from "./view.ts";
import { EUDX_MAGIC, SetTo, type HirAct, type HirReloc } from "./hir.ts";
import { ACT_FIELD_OFFSETS } from "./stock-spec.ts";
import type { FixedString, lazyFixedString } from "./string.ts";
import { Trigger } from "./trigger.ts";
import type {
  FlingyEntry,
  FlingyFields,
  TechEntry,
  TechFields,
  TrgUnitEntry,
  TrgUnitFields,
  UpgradeEntry,
  UpgradeFields,
  WeaponEntry,
  WeaponFields,
} from "./dat.generated.ts";
import { datSymbolsFor } from "./dat-symbols.ts";

export { datSymbolIdentifier, datSymbolsFor } from "./dat-symbols.ts";

export type {
  FlingyEntry,
  FlingyFields,
  TechEntry,
  TechFields,
  TrgUnitEntry,
  TrgUnitFields,
  UpgradeEntry,
  UpgradeFields,
  WeaponEntry,
  WeaponFields,
} from "./dat.generated.ts";
export {
  FlingyType,
  TechType,
  UnitType,
  UpgradeType,
  WeaponType,
} from "./dat.generated.ts";

/** One field of a DAT table, as stored in `schema/layouts/dat-*.json`. */
export interface DatFieldSpec {
  readonly base: number;
  readonly size: FieldWidth;
  readonly stride: number;
  readonly aliases?: readonly string[];
}

/** Parsed DAT table metadata. */
export interface DatTableSchema<F extends string = string> {
  readonly table: string;
  readonly kind: "soa";
  readonly count: number;
  readonly fields: Readonly<Record<F, DatFieldSpec>>;
  readonly names: Readonly<Record<string, number>>;
}

const datMemViewCache = new WeakMap<DatTableSchema, MemViewSchema>();

function datMemView(table: DatTableSchema): MemViewSchema {
  let schema = datMemViewCache.get(table);
  if (!schema) {
    schema = defineMemView(table.table, 4, {});
    datMemViewCache.set(table, schema);
  }
  return schema;
}

/**
 * A field handle bound to one DAT entry. The absolute field address is folded
 * into the EPD and byte-lane shift at construction time.
 */
export class DatFieldHandle extends MemFieldHandle {
  readonly table: DatTableSchema;
  readonly index: number;
  readonly field: string;
  /** Absolute memory address of this field for this entry. */
  readonly address: number;

  constructor(
    table: DatTableSchema,
    field: string,
    spec: DatFieldSpec,
    index: number,
  ) {
    const address = spec.base + index * spec.stride;
    const epd = EPD(address);
    const offset = (((address - EPD_BASE) % 4) + 4) % 4;
    super(
      datMemView(table),
      { offset, size: spec.size, aliases: spec.aliases },
      epd,
      field,
    );
    this.table = table;
    this.index = index;
    this.field = field;
    this.address = address;
  }

  /**
   * Write `fs`'s 16-bit wire string id into this 16-bit field.
   *
   * The id is resolved at assembly as `template_count + messageIndex + 1`,
   * which the frontend cannot know. The field's lane mask and bit shift keep
   * the neighbouring entry's id intact.
   */
  setString(fs: FixedString | ReturnType<typeof lazyFixedString>): void {
    if (this.size !== 2) {
      throw new EudslError(
        "EUDSL-BOUNDS",
        `setString() requires a 16-bit field; '${this.field}' is ${this.size} bytes`,
      );
    }
    const index =
      fs === null || typeof fs !== "object"
        ? undefined
        : (fs as { messageIndex?: unknown }).messageIndex;
    if (typeof index !== "number" || !Number.isInteger(index)) {
      throw new EudslError(
        "EUDSL-TYPE",
        "setString() requires a FixedString with an integer messageIndex, " +
          `got ${describeValue(fs)}`,
      );
    }
    // `HirReloc.mode` is still typed `1 | 4` in trigger.ts; the HIR accepts
    // mode 6 (Rust validates the value), so widen only this literal.
    const reloc: HirReloc = {
      field_offset: ACT_FIELD_OFFSETS.value!,
      target_string: index,
      target_offset: this.shift,
      mode: 6 as unknown as HirReloc["mode"],
    };
    // Match `SetDeathsX(dest, SetTo, 0, 0, mask)` exactly, including the
    // EUDX marker that makes the mask meaningful at runtime.
    const act: HirAct = Object.freeze({
      mask: this.bitMask,
      strid: 0,
      wavid: 0,
      time: 0,
      dest: this.epd!,
      value: 0,
      unit: 0,
      acttype: 45,
      modifier: SetTo,
      flags: 20,
      padding: 0,
      eudx: EUDX_MAGIC,
      relocs: [reloc],
    });
    new Trigger({ conds: [], acts: [act], preserved: true });
  }

  // Python/TS parity alias, like `memcpyEPD` / `memcpy_epd` in view.ts.
  set_string = this.setString;
}

/**
 * One entry of a DAT table. Property access resolves a canonical field name,
 * schema alias (`verticalOffset`), or snake_case spelling (`max_hp`) to a
 * [`DatFieldHandle`].
 */
export type DatEntry<F extends string = string> = {
  readonly table: DatTableSchema<F>;
  readonly index: number;
  field(name: F | string): DatFieldHandle;
} & { readonly [K in F]: DatFieldHandle };

/**
 * A DAT table. Entry lookup is constant-index only; `at` raises
 * `EUDSL-TARGET` for a non-integer (for example a `Var`) index and
 * `EUDSL-BOUNDS` for an out-of-range one.
 */
export interface DatTable<F extends string = string, E extends DatEntry<F> = DatEntry<F>> {
  readonly table: string;
  readonly kind: "soa";
  readonly count: number;
  readonly fields: Readonly<Record<F, DatFieldSpec>>;
  readonly names: Readonly<Record<string, number>>;
  /** Identifier-safe names derived from the schema `names` table. */
  readonly symbols: Readonly<Record<string, number>>;
  at(index: number): E;
  byName(name: string): E;
  nameOf(index: number): string | undefined;
  [index: number]: E;
}

function normalizeDatName(name: string): string {
  return name.replace(/_/g, "").toLowerCase();
}

function fieldIndex(
  schema: DatTableSchema,
): Map<string, { name: string; spec: DatFieldSpec }> {
  const index = new Map<string, { name: string; spec: DatFieldSpec }>();
  for (const [name, spec] of Object.entries(schema.fields)) {
    const item = { name, spec };
    index.set(name, item);
    index.set(normalizeDatName(name), item);
    for (const alias of spec.aliases ?? []) {
      index.set(alias, item);
      index.set(normalizeDatName(alias), item);
    }
  }
  return index;
}

function loadSchema(fileName: string): DatTableSchema {
  const data = JSON.parse(fs.readFileSync(locateSchemaFile(fileName), "utf-8")) as {
    table?: unknown;
    kind?: unknown;
    count?: unknown;
    fields?: unknown;
    names?: unknown;
  };
  if (typeof data.table !== "string" || data.table.length === 0) {
    throw new Error(`DAT schema '${fileName}' has no table name`);
  }
  if (data.kind !== "soa") {
    throw new Error(`DAT schema '${fileName}' must have kind 'soa'`);
  }
  if (
    typeof data.count !== "number" ||
    !Number.isInteger(data.count) ||
    data.count <= 0
  ) {
    throw new Error(`DAT schema '${fileName}' has an invalid count`);
  }
  if (typeof data.fields !== "object" || data.fields === null) {
    throw new Error(`DAT schema '${fileName}' has no fields`);
  }
  if (typeof data.names !== "object" || data.names === null) {
    throw new Error(`DAT schema '${fileName}' has no names`);
  }

  const fields: Record<string, DatFieldSpec> = Object.create(null);
  for (const [name, raw] of Object.entries(data.fields as Record<string, any>)) {
    const base = raw?.base;
    const size = raw?.size;
    const stride = raw?.stride;
    if (
      typeof base !== "number" ||
      !Number.isInteger(base) ||
      base < 0 ||
      typeof stride !== "number" ||
      !Number.isInteger(stride) ||
      stride < 1 ||
      (size !== 1 && size !== 2 && size !== 4)
    ) {
      throw new Error(
        `DAT schema '${fileName}' field '${name}' must have base >= 0, stride >= 1, and size 1/2/4`,
      );
    }
    const aliases = Array.isArray(raw.aliases)
      ? raw.aliases.filter((a: unknown): a is string => typeof a === "string")
      : undefined;
    fields[name] = Object.freeze(aliases ? { base, size, stride, aliases: Object.freeze([...aliases]) } : { base, size, stride });
  }

  const names: Record<string, number> = Object.create(null);
  for (const [name, raw] of Object.entries(data.names as Record<string, unknown>)) {
    if (
      typeof raw !== "number" ||
      !Number.isInteger(raw) ||
      raw < 0 ||
      raw >= data.count
    ) {
      throw new Error(
        `DAT schema '${fileName}' name '${name}' must map to an entry in 0..${data.count - 1}`,
      );
    }
    names[name] = raw;
  }

  return Object.freeze({
    table: data.table,
    kind: "soa",
    count: data.count,
    fields: Object.freeze(fields),
    names: Object.freeze(names),
  });
}

class DatEntryImpl<F extends string> {
  readonly table: DatTableSchema<F>;
  readonly index: number;
  private readonly indexMap: Map<string, { name: string; spec: DatFieldSpec }>;

  constructor(
    table: DatTableSchema<F>,
    index: number,
    indexMap: Map<string, { name: string; spec: DatFieldSpec }>,
  ) {
    this.table = table;
    this.index = index;
    this.indexMap = indexMap;
    return new Proxy(this, {
      get(target, prop, receiver) {
        if (typeof prop === "symbol" || prop in target) {
          return Reflect.get(target, prop, receiver);
        }
        if (typeof prop === "string") {
          const resolved = target.indexMap.get(prop) ?? target.indexMap.get(normalizeDatName(prop));
          if (resolved) {
            return new DatFieldHandle(target.table, resolved.name, resolved.spec, target.index);
          }
          throw new EudslError(
            "EUDSL-BOUNDS",
            `Unknown field '${prop}' on DAT table '${target.table.table}'`,
          );
        }
        return Reflect.get(target, prop, receiver);
      },
    });
  }

  field(name: string): DatFieldHandle {
    const resolved = this.indexMap.get(name) ?? this.indexMap.get(normalizeDatName(name));
    if (!resolved) {
      throw new EudslError(
        "EUDSL-BOUNDS",
        `Unknown field '${name}' on DAT table '${this.table.table}'`,
      );
    }
    return new DatFieldHandle(this.table, resolved.name, resolved.spec, this.index);
  }
}

class DatTableImpl<F extends string, E extends DatEntry<F>> {
  readonly table: string;
  readonly kind = "soa";
  readonly count: number;
  readonly fields: Readonly<Record<F, DatFieldSpec>>;
  readonly names: Readonly<Record<string, number>>;
  readonly symbols: Readonly<Record<string, number>>;
  private readonly schema: DatTableSchema<F>;
  private readonly indexMap: Map<string, { name: string; spec: DatFieldSpec }>;
  private readonly firstNames = new Map<number, string>();

  constructor(schema: DatTableSchema<F>) {
    this.schema = schema;
    this.table = schema.table;
    this.count = schema.count;
    this.fields = schema.fields;
    this.names = schema.names;
    this.symbols = datSymbolsFor(schema.names);
    this.indexMap = fieldIndex(schema);
    for (const [name, index] of Object.entries(schema.names)) {
      if (!this.firstNames.has(index)) {
        this.firstNames.set(index, name);
      }
    }
    return new Proxy(this, {
      get(target, prop, receiver) {
        if (typeof prop === "symbol" || prop in target) {
          return Reflect.get(target, prop, receiver);
        }
        if (typeof prop === "string" && /^(?:0|[1-9][0-9]*)$/.test(prop)) {
          return target.at(Number(prop));
        }
        throw new EudslError(
          "EUDSL-BOUNDS",
          `Unknown property '${String(prop)}' on DAT table '${target.table}'`,
        );
      },
    });
  }

  at(index: number): E {
    if (typeof index !== "number" || !Number.isInteger(index)) {
      throw new EudslError(
        "EUDSL-TARGET",
        `DAT table '${this.table}' requires a compile-time constant integer index`,
      );
    }
    if (index < 0 || index >= this.count) {
      throw new EudslError(
        "EUDSL-BOUNDS",
        `DAT table '${this.table}' index ${index} is outside 0..${this.count - 1}`,
      );
    }
    return new DatEntryImpl(this.schema, index, this.indexMap) as unknown as E;
  }

  byName(name: string): E {
    if (typeof name !== "string") {
      throw new EudslError(
        "EUDSL-BOUNDS",
        `DAT table '${this.table}' name must be a string`,
      );
    }
    // `names` is a plain object, so a missing name must not fall through to
    // Object.prototype (`toString`, `constructor`, ...).
    const index = Object.hasOwn(this.names, name) ? this.names[name] : undefined;
    if (index === undefined) {
      throw new EudslError(
        "EUDSL-BOUNDS",
        `Unknown name '${name}' on DAT table '${this.table}'`,
      );
    }
    return this.at(index);
  }

  nameOf(index: number): string | undefined {
    return this.firstNames.get(index);
  }
}

/**
 * Load one `schema/layouts/<fileName>.json` DAT table.
 *
 * `fileName` is the schema basename, e.g. `dat-trgunit`.
 */
export function loadDatTable<F extends string = string, E extends DatEntry<F> = DatEntry<F>>(
  fileName: string,
): DatTable<F, E> {
  const schema = loadSchema(fileName) as DatTableSchema<F>;
  return new DatTableImpl<F, E>(schema) as unknown as DatTable<F, E>;
}

/** `flingy.dat` / `dat-flingy.json`. */
export const flingyDat = loadDatTable<FlingyFields, FlingyEntry>("dat-flingy");
/** `techdata.dat` / `dat-tech.json`. */
export const techDat = loadDatTable<TechFields, TechEntry>("dat-tech");
/** `units.dat` / `dat-trgunit.json`. */
export const unitsDat = loadDatTable<TrgUnitFields, TrgUnitEntry>("dat-trgunit");
/** `upgrades.dat` / `dat-upgrade.json`. */
export const upgradesDat = loadDatTable<UpgradeFields, UpgradeEntry>("dat-upgrade");
/** `weapons.dat` / `dat-weapon.json`. */
export const weaponsDat = loadDatTable<WeaponFields, WeaponEntry>("dat-weapon");
