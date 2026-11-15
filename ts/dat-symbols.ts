/**
 * Identifier-safe entry names for the StarCraft DAT tables.
 *
 * Shared by the runtime (`ts/dat.ts`) and the checked-in code generator
 * (`ts/scripts/generate-dat.ts`) so `UnitType.Marine` in an editor and
 * `unitsDat.symbols.Marine` at runtime can never drift.
 */

/** Convert a schema display name to a PascalCase identifier. */
export function datSymbolIdentifier(name: string): string {
  const words = name.split(/[^0-9A-Za-z]+/).filter((word) => word.length > 0);
  let id = words.map((word) => word[0].toUpperCase() + word.slice(1)).join("");
  if (id.length === 0) {
    id = "_";
  }
  if (/^[0-9]/.test(id)) {
    id = `_${id}`;
  }
  return id;
}

const RACE_PREFIX = /^(?:Terran|Zerg|Protoss) /;

/**
 * Build the identifier -> entry mapping from a schema's display names.
 *
 * Exact names map to a PascalCase identifier. A race-prefixed name also gets
 * the prefix-stripped identifier (`Terran Marine` -> `Marine`) when that
 * alias is unambiguous: it must not collide with a base identifier or with
 * another stripped name from a different race.
 */
export function datSymbolsFor(
  names: Readonly<Record<string, number>>,
): Readonly<Record<string, number>> {
  const symbols = new Map<string, number>();
  for (const [name, index] of Object.entries(names)) {
    const id = datSymbolIdentifier(name);
    const existing = symbols.get(id);
    if (existing !== undefined && existing !== index) {
      throw new Error(
        `DAT symbol collision: '${name}' and another name both map to '${id}'`,
      );
    }
    symbols.set(id, index);
  }

  const aliasCandidates = new Map<string, Set<string>>();
  for (const name of Object.keys(names)) {
    const stripped = name.replace(RACE_PREFIX, "");
    if (stripped === name) {
      continue;
    }
    const id = datSymbolIdentifier(stripped);
    if (symbols.has(id)) {
      continue;
    }
    let candidates = aliasCandidates.get(id);
    if (!candidates) {
      candidates = new Set<string>();
      aliasCandidates.set(id, candidates);
    }
    candidates.add(name);
  }
  for (const [id, candidateNames] of aliasCandidates) {
    if (candidateNames.size !== 1) {
      continue;
    }
    const [name] = candidateNames;
    symbols.set(id, names[name]);
  }
  return Object.freeze(Object.fromEntries(symbols));
}
