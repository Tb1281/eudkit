/**
 * `TriggerStruct` — allocated struct whose fields live in individual `SetDeathsX`
 * action slots (`0..63`) of a `TriggerPage` (`8-EPD` stride, hardware per-action mask).
 */

import { ACT_COUNT, ACT_STRIDE_EPDS } from "./trigger.ts";
import { LazyBinding } from "./lazy-binding.ts";
import { type VarOperand, VarImpl, allocVarSlot, ensurePageFit } from "./var.ts";
import { EudslError, requirePlainInt, warnLazyInBuilder } from "./view.ts";

export interface FieldSpec {
  readonly mask: number;
  readonly shift: number;
}

export type StructSlotWidth =
  | "byte"
  | "word"
  | "dword"
  | FieldSpec
  | { mask: number; shift?: number }
  | number;

export function dword(): FieldSpec {
  return Object.freeze({ mask: 0xffffffff, shift: 0 });
}

export function word(shift = 0): FieldSpec {
  const m = ((0x0000ffff << shift) >>> 0) || 0x0000ffff;
  return Object.freeze({ mask: m, shift });
}

export function byte(shift = 0): FieldSpec {
  const m = ((0x000000ff << shift) >>> 0) || 0x000000ff;
  return Object.freeze({ mask: m, shift });
}

export function bit(width: number, shift = 0): FieldSpec {
  if (width < 1 || width > 32) {
    throw new EudslError("EUDSL-BOUNDS", `bit width must be in 1..32, got ${width}`);
  }
  const raw = width === 32 ? 0xffffffff : ((1 << width) - 1) >>> 0;
  const m = (raw << shift) >>> 0;
  return Object.freeze({ mask: m, shift });
}

export function masked(mask: number, shift = 0): FieldSpec {
  const m = ((mask >>> 0) << shift) >>> 0;
  return Object.freeze({ mask: m, shift });
}

const WIDTH_MASK: Record<string, number> = {
  byte: 0x000000ff,
  word: 0x0000ffff,
  dword: 0xffffffff,
};

function resolveFieldSpec(fspec: StructSlotWidth): { mask: number; shift: number } {
  if (typeof fspec === "string") {
    const m = WIDTH_MASK[fspec];
    if (m === undefined) {
      throw new EudslError("EUDSL-BOUNDS", `Unknown field width type: '${fspec}'`);
    }
    return { mask: m, shift: 0 };
  }
  if (typeof fspec === "number") {
    return { mask: fspec >>> 0, shift: 0 };
  }
  if (typeof fspec === "object" && fspec !== null && "mask" in fspec) {
    return { mask: fspec.mask >>> 0, shift: (fspec.shift ?? 0) >>> 0 };
  }
  throw new EudslError("EUDSL-BOUNDS", `Invalid field spec: ${JSON.stringify(fspec)}`);
}

export interface TriggerStructField {
  readonly name: string;
  readonly actionSlot: number;
  readonly epdOffset: number;
  readonly mask: number;
  readonly shift: number;
}

export interface TriggerStructSchema<K extends string = string> {
  readonly name: string;
  readonly fieldCount: number;
  readonly fieldNames: readonly K[];
  readonly fields: Readonly<Record<K, TriggerStructField>>;
  (inits?: Partial<Record<K, VarOperand>> | TriggerStructInstance<K>): TriggerStructInstance<K>;
  new (inits?: Partial<Record<K, VarOperand>> | TriggerStructInstance<K>): TriggerStructInstance<K>;
  instantiate(inits?: Partial<Record<K, VarOperand>> | TriggerStructInstance<K>): TriggerStructInstance<K>;
  create(inits?: Partial<Record<K, VarOperand>> | TriggerStructInstance<K>): TriggerStructInstance<K>;
}

/**
 * A `TriggerStructSchema` with its field-name parameter erased.
 *
 * API positions that accept any struct schema (array elements, queue
 * elements) must not use `TriggerStructSchema<string>`: a concrete
 * `TriggerStructSchema<"hp" | "mp">` is intentionally NOT assignable to
 * it, because instantiating the latter could produce an instance that
 * lacks the concrete fields. This structural form only keeps what such
 * APIs actually use, so every concrete schema is assignable.
 */
export interface AnyTriggerStructSchema {
  readonly name: string;
  readonly fieldCount: number;
  readonly fieldNames: readonly string[];
  instantiate(inits?: any): AnyTriggerStructInstance;
}

/**
 * A `TriggerStructInstance` with its field-name parameter erased. Used
 * where an API accepts any struct instance (for example
 * `FixedArray.set` on a struct array) without knowing the concrete
 * field names. `inits`/`src` stay `any` because the concrete instance
 * narrows them to its own field names.
 */
export interface AnyTriggerStructInstance {
  readonly _fieldNames: readonly string[];
  readonly pageIndex: number;
  readonly slotIndex: number;
  readonly fieldCount: number;
  readonly fields: readonly VarImpl[];
  set(src: any): void;
  copyFrom(src: any): void;
  copy_from(src: any): void;
}

/** The concrete struct instance type produced by schema type `S`. */
export type TriggerStructInstanceOf<S> =
  S extends TriggerStructSchema<infer K> ? TriggerStructInstance<K> : never;

export type TriggerStructInstance<K extends string = string> = {
  readonly [P in K]: VarImpl;
} & {
  readonly _schema: TriggerStructSchema<K>;
  readonly _fieldNames: readonly K[];
  readonly pageIndex: number;
  readonly slotIndex: number;
  readonly fieldCount: number;
  readonly fields: readonly VarImpl[];
  set(src: TriggerStructInstance<K> | Partial<Record<K, VarOperand>>): void;
  copyFrom(src: TriggerStructInstance<K>): void;
  copy_from(src: TriggerStructInstance<K>): void;
};

export function TriggerStruct<K extends string = string>(
  nameOrSpec: string | Record<K, StructSlotWidth>,
  maybeSpec?: Record<K, StructSlotWidth>,
): TriggerStructSchema<K> {
  let name = "TriggerStruct";
  let spec: Record<K, StructSlotWidth>;
  if (typeof nameOrSpec === "string") {
    name = nameOrSpec;
    spec = maybeSpec ?? ({} as Record<K, StructSlotWidth>);
  } else {
    spec = nameOrSpec;
  }

  const entries = Object.entries(spec) as Array<[K, StructSlotWidth]>;
  if (entries.length === 0 || entries.length > ACT_COUNT) {
    throw new EudslError(
      "EUDSL-BOUNDS",
      `TriggerStruct(${name}): field count must be in 1..${ACT_COUNT}, got ${entries.length}`,
    );
  }

  const fields = {} as Record<K, TriggerStructField>;
  const fieldNames: K[] = [];
  entries.forEach(([fname, fspec], idx) => {
    const { mask, shift } = resolveFieldSpec(fspec);
    fields[fname] = Object.freeze({
      name: fname,
      actionSlot: idx,
      epdOffset: idx * ACT_STRIDE_EPDS,
      mask,
      shift,
    });
    fieldNames.push(fname);
  });

  function instantiate(
    inits?: Partial<Record<K, VarOperand>> | TriggerStructInstance<K>,
  ): TriggerStructInstance<K> {
    // Contiguous allocation without splitting an instance across pages.
    // Page slots, not raw variable ids, decide the fit: non-page variables
    // consume ids but no page slots.
    ensurePageFit(entries.length);

    const fieldVars: Record<string, VarImpl> = {};
    const fieldVarsList: VarImpl[] = [];

    // Allocate all K slots contiguously
    for (const fname of fieldNames) {
      const fmeta = fields[fname];
      let initConst = 0;
      if (inits && typeof inits === "object" && fname in inits) {
        const val = (inits as Record<string, any>)[fname];
        if (typeof val === "number") {
          initConst = (requirePlainInt(val, "struct field init") & fmeta.mask) >>> 0;
        }
      }
      const vid = allocVarSlot(initConst, fmeta.mask, "page");
      const varObj = new VarImpl(initConst, vid, fmeta.mask, "page");
      fieldVars[fname] = varObj;
      fieldVarsList.push(varObj);
    }

    const firstVar = fieldVarsList[0];
    const instanceObj = {
      _schema: schema,
      _fieldNames: Object.freeze([...fieldNames]),
      pageIndex: firstVar.pageIndex,
      slotIndex: firstVar.slotIndex,
      fieldCount: entries.length,
      fields: Object.freeze([...fieldVarsList]),
      copyFrom(src: TriggerStructInstance<K>): void {
        for (const fname of fieldNames) {
          fieldVars[fname].set((src as any)[fname]);
        }
      },
      copy_from(src: TriggerStructInstance<K>): void {
        this.copyFrom(src);
      },
      set(src: TriggerStructInstance<K> | Partial<Record<K, VarOperand>>): void {
        if (src && typeof src === "object" && "_schema" in src) {
          this.copyFrom(src as TriggerStructInstance<K>);
        } else if (src && typeof src === "object") {
          for (const [k, v] of Object.entries(src)) {
            if (k in fieldVars) {
              fieldVars[k].set(v as VarOperand);
            } else {
              throw new EudslError(
                "EUDSL-UNKNOWN-FIELD",
                `Unknown field '${k}' on TriggerStruct '${name}'`,
              );
            }
          }
        } else {
          throw new TypeError(`Cannot set TriggerStruct from ${typeof src}`);
        }
      },
    };

    // If inits was a struct instance or has non-constant values, apply them now
    if (inits && typeof inits === "object") {
      if ("_schema" in inits) {
        instanceObj.copyFrom(inits as TriggerStructInstance<K>);
      } else {
        for (const fname of fieldNames) {
          if (fname in inits) {
            const v = (inits as Record<string, any>)[fname];
            if (typeof v !== "number") {
              fieldVars[fname].set(v);
            }
          }
        }
      }
    }

    return new Proxy(instanceObj as unknown as TriggerStructInstance<K>, {
      get(target, prop, receiver) {
        if (typeof prop === "string") {
          if (prop in fieldVars) {
            return fieldVars[prop];
          }
          if (Reflect.has(target, prop)) {
            return Reflect.get(target, prop, receiver);
          }
          if (prop === "then" || prop === "toJSON" || typeof prop === "symbol") {
            return undefined;
          }
          throw new EudslError(
            "EUDSL-UNKNOWN-FIELD",
            `Unknown field '${prop}' on TriggerStruct '${name}'`,
          );
        }
        return Reflect.get(target, prop, receiver);
      },
      set(target, prop, value, receiver) {
        if (typeof prop === "string") {
          if (prop in fieldVars) {
            fieldVars[prop].set(value as VarOperand);
            return true;
          }
          if (Reflect.has(target, prop)) {
            return Reflect.set(target, prop, value, receiver);
          }
          throw new EudslError(
            "EUDSL-UNKNOWN-FIELD",
            `Unknown field '${prop}' on TriggerStruct '${name}'`,
          );
        }
        return Reflect.set(target, prop, value, receiver);
      },
      has(target, prop) {
        if (typeof prop === "string" && prop in fieldVars) {
          return true;
        }
        return Reflect.has(target, prop);
      },
    });
  }

  const schemaFn = function (inits?: any) {
    return instantiate(inits);
  };
  Object.defineProperty(schemaFn, "name", {
    value: name,
    configurable: true,
  });
  Object.assign(schemaFn, {
    fieldCount: entries.length,
    fieldNames: Object.freeze([...fieldNames]),
    fields: Object.freeze(fields),
    instantiate,
    create: instantiate,
  });
  const schema = schemaFn as unknown as TriggerStructSchema<K>;

  return Object.freeze(schema);
}

/**
 * Declare a lazy `TriggerStruct` instance (globals P2-02): a handle
 * constructible anywhere (no builder needed) that resolves to a fully
 * eager binding on first resolving use inside `buildModule`, one binding
 * per builder. The schema itself is pure and needs no lazy form.
 *
 * The handle is a resolving Proxy (NOT a subclass: TS instances are
 * already proxies). Field names delegate to the binding's field vars;
 * `pageIndex`/`slotIndex`/`fields`/`set`/`copyFrom`/`copy_from` delegate
 * to the binding; `_schema`/`_fieldNames`/`fieldCount` are pure reads
 * that never resolve; `then`/`toJSON`/symbols return `undefined` WITHOUT
 * resolving (the eager proxy's observable behavior for those probes),
 * because resolving there would bind on mere `await`/`JSON.stringify`,
 * possibly outside a builder.
 *
 * Only constant-number inits are accepted, validated eagerly at the call
 * (design D4): a non-number init (another Var, an Expr, a struct
 * instance) throws `EUDSL-TYPE` here, because a deferred `copyFrom`
 * would be scope-shifted by a conditional first use, exactly like a
 * deferred `.set()`. Unknown field names throw `EUDSL-UNKNOWN-FIELD`
 * eagerly. Number masking per field spec happens at binding creation,
 * inside the eager `instantiate` call with the stored inits — no
 * separate masking logic in the handle. Binding creation is atomic: one
 * full eager `instantiate` (page fit plus ALL K field vars together), so
 * a partially-created binding is never observable. A never-used handle
 * emits nothing (D5). This factory is the only documented path to a
 * deferred struct handle.
 */
export function lazyStruct<K extends string = string>(
  schema: TriggerStructSchema<K>,
  inits?: Partial<Record<K, number>>,
): TriggerStructInstance<K> {
  warnLazyInBuilder();
  const fieldNames = [...schema.fieldNames] as K[];
  const fieldSet = new Set<string>(fieldNames as string[]);
  const storedInits: Record<string, number> = {};
  if (inits !== undefined && inits !== null) {
    if (typeof inits === "object" && "_schema" in inits) {
      throw new EudslError(
        "EUDSL-TYPE",
        "lazyStruct init must be plain numbers; struct instance init is not supported (a deferred copyFrom would be scope-shifted)",
      );
    }
    if (typeof inits !== "object") {
      throw new EudslError("EUDSL-TYPE", "lazyStruct inits must be an object");
    }
    for (const [k, v] of Object.entries(inits)) {
      if (!fieldSet.has(k)) {
        throw new EudslError(
          "EUDSL-UNKNOWN-FIELD",
          `Unknown field '${k}' on TriggerStruct '${schema.name}'`,
        );
      }
      if (typeof v !== "number") {
        throw new EudslError(
          "EUDSL-TYPE",
          `lazyStruct init for field '${k}' must be a constant number`,
        );
      }
      storedInits[k] = v;
    }
  }

  // Per-handle binding state (D3) + single-record post-build fallback
  // (P1-01 rule, ADR 0005): with an active builder, a missing binding is
  // created eagerly under that builder; with no active builder, no
  // allocation ever happens — a handle resolved under exactly one builder
  // returns its cached record, one resolved under several throws as
  // ambiguous, and a never-resolved handle throws `EUDSL-SCOPE`.
  const state = new LazyBinding<TriggerStructInstance<K>>("struct");

  function resolve(): TriggerStructInstance<K> {
    return state.resolve(() =>
      schema.instantiate(storedInits as Partial<Record<K, VarOperand>>),
    );
  }

  const BINDING_KEYS = new Set([
    "pageIndex",
    "slotIndex",
    "fields",
    "set",
    "copyFrom",
    "copy_from",
  ]);
  const target = {
    _schema: schema,
    _fieldNames: Object.freeze([...fieldNames]),
    fieldCount: fieldNames.length,
  };

  return new Proxy(target as unknown as TriggerStructInstance<K>, {
    get(t, prop, receiver) {
      if (typeof prop === "string") {
        if (fieldSet.has(prop)) {
          return (resolve() as any)[prop];
        }
        if (Reflect.has(t, prop)) {
          return Reflect.get(t, prop, receiver);
        }
        if (prop === "then" || prop === "toJSON") {
          return undefined;
        }
        if (BINDING_KEYS.has(prop)) {
          const binding = resolve() as any;
          const value = binding[prop];
          return typeof value === "function" ? value.bind(binding) : value;
        }
        throw new EudslError(
          "EUDSL-UNKNOWN-FIELD",
          `Unknown field '${prop}' on TriggerStruct '${schema.name}'`,
        );
      }
      // Symbols never resolve: return undefined without creating a binding.
      return undefined;
    },
    set(t, prop, value, receiver) {
      if (typeof prop === "string") {
        if (fieldSet.has(prop) || BINDING_KEYS.has(prop)) {
          return Reflect.set(resolve() as any, prop, value);
        }
        if (Reflect.has(t, prop)) {
          return Reflect.set(t, prop, value, receiver);
        }
        throw new EudslError(
          "EUDSL-UNKNOWN-FIELD",
          `Unknown field '${prop}' on TriggerStruct '${schema.name}'`,
        );
      }
      return Reflect.set(t, prop, value, receiver);
    },
    has(t, prop) {
      if (typeof prop === "string") {
        if (fieldSet.has(prop) || BINDING_KEYS.has(prop)) {
          return true;
        }
        return Reflect.has(t, prop);
      }
      return Reflect.has(t, prop);
    },
  });
}

export const defineTriggerStruct = TriggerStruct;
export const defineStruct = TriggerStruct;
export type TriggerStruct<K extends string = string> = TriggerStructSchema<K>;
