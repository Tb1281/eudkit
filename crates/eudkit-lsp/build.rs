//! Generate the CUnit/CSprite member tables from the schema layouts.
//!
//! The DSL (`ts/view.ts` `loadLayout`) already reads
//! `schema/layouts/cunit.json` and `csprite.json` as its source of truth;
//! the LSP used to hand-copy a small subset of those fields, and the copy
//! drifted (it suggested `player`, `positionX`, and `positionY` on CUnit,
//! all rejected by the DSL). Generating the tables from the same files
//! makes that drift class impossible for MemView fields: every size>0
//! schema field appears, in offset order, with its schema aliases.
//!
//! Build coupling, decided by the candidate 7 design: this script reads the
//! schema through `CARGO_WORKSPACE_DIR` and FAILS LOUDLY if a schema file
//! is missing or unparseable. It never falls back to a stale hand table,
//! and the generated file is not checked in (a checked-in generated file
//! could drift exactly like the hand table did).
//!
//! Generation rules:
//! - Only size>0 fields are generated; size-0 `UnsupportedMember` fields
//!   (e.g. CUnit `orderQueueHead`) are not members the DSL can read.
//! - Labels are the canonical field name, every schema alias, and the DSL's
//!   common domain aliases (`ts/view.ts` `buildSchemaIndex`: `hp` ->
//!   `hitPoints`/`hit_points`, `shield` -> `shields`, `order` ->
//!   `orderID`/`order_id`, `owner` -> `playerID`/`player_id`, `posX` ->
//!   `pos_x`, `posY` -> `pos_y`), deduplicated by exact label. A label
//!   whose normalized form (underscores stripped, lowercased — the DSL's
//!   `normalizeName`) maps to two different canonical fields fails the
//!   build, since the DSL would resolve it ambiguously.
//! - `detail` comes from the field size (`byte`/`word`/`dword`), or from
//!   the pointer target (`pointer<CUnit>`). Any other size fails the build:
//!   the DSL's `FieldWidth` is 1 | 2 | 4, so an unexpected size means the
//!   schema changed shape, not that a new width appeared.
//! - `doc` comes from the curated overlay below for common fields, and
//!   from a generated default otherwise. An overlay key that is not a
//!   canonical field of its layout fails the build (a stale overlay key
//!   would otherwise silently stop applying).

use std::collections::{BTreeMap, HashMap};
use std::env;
use std::fs;
use std::path::PathBuf;

#[path = "build/stock_vocabulary.rs"]
mod stock_vocabulary;

/// Locate the workspace root: `CARGO_WORKSPACE_DIR` when it points at a
/// directory holding the schema, otherwise the nearest ancestor of the
/// crate manifest that does. Both probes look for the real schema file,
/// never for a stale copy; if neither finds it, the build fails loudly.
fn workspace_root() -> PathBuf {
    if let Ok(dir) = env::var("CARGO_WORKSPACE_DIR") {
        let dir = PathBuf::from(dir);
        if dir.join("schema/layouts/cunit.json").exists() {
            return dir;
        }
    }
    let mut dir = PathBuf::from(env::var("CARGO_MANIFEST_DIR").expect("CARGO_MANIFEST_DIR is set"));
    loop {
        if dir.join("schema/layouts/cunit.json").exists() {
            return dir;
        }
        if !dir.pop() {
            break;
        }
    }
    panic!(
        "eudkit-lsp build.rs: could not locate schema/layouts/cunit.json from CARGO_WORKSPACE_DIR or any ancestor of CARGO_MANIFEST_DIR; the schema files must be present to build"
    );
}

fn main() {
    let workspace = workspace_root();
    let layouts = [
        ("CUnit", "cunit", CUNIT_DOCS),
        ("CSprite", "csprite", CSPRITE_DOCS),
    ];
    let mut generated = String::new();
    for (layout, file, docs) in layouts {
        let path = workspace
            .join("schema")
            .join("layouts")
            .join(format!("{file}.json"));
        println!("cargo::rerun-if-changed={}", path.display());
        let text = fs::read_to_string(&path).unwrap_or_else(|e| {
            panic!("eudkit-lsp build.rs: cannot read schema {path}: {e}", path = path.display())
        });
        let schema: serde_json::Value = serde_json::from_str(&text).unwrap_or_else(|e| {
            panic!("eudkit-lsp build.rs: cannot parse schema {path}: {e}", path = path.display())
        });
        let fields = schema["fields"]
            .as_object()
            .unwrap_or_else(|| panic!("eudkit-lsp build.rs: schema {file}.json has no fields object"));
        generated.push_str(&generate_table(layout, fields, docs));
    }
    let out = PathBuf::from(env::var("OUT_DIR").expect("OUT_DIR is set")).join("generated.rs");
    fs::write(&out, generated).unwrap_or_else(|e| {
        panic!("eudkit-lsp build.rs: cannot write {}: {e}", out.display())
    });

    // Stock and extension helper vocabulary lives in the stock row spec, the
    // same artifact the DSL goldens are frozen from. A missing or unparseable
    // spec, or a curated doc-overlay key that no longer names a spec row,
    // fails the build instead of falling back to a stale hand table.
    let spec = workspace.join("schema").join("stock-rows.json");
    println!("cargo::rerun-if-changed={}", spec.display());
    let vocabulary = stock_vocabulary::generate(&spec).unwrap_or_else(|error| {
        panic!("eudkit-lsp build.rs: {error}");
    });
    let out = PathBuf::from(env::var("OUT_DIR").expect("OUT_DIR is set"))
        .join("stock_vocabulary.rs");
    fs::write(&out, vocabulary).unwrap_or_else(|e| {
        panic!("eudkit-lsp build.rs: cannot write {}: {e}", out.display())
    });
}

/// Curated docs for common fields; everything else gets a generated default.
const CUNIT_DOCS: &[(&str, &str)] = &[
    ("hp", "CUnit hit points."),
    ("prev", "Previous unit in the unit list."),
    ("next", "Next unit in the unit list."),
    ("sprite", "Linked sprite."),
    ("owner", "Owning player."),
    ("order", "Current order id."),
    ("shield", "CUnit shield points."),
    ("posX", "Current X position."),
    ("posY", "Current Y position."),
    ("moveTarget", "Move target unit."),
    ("moveTargetX", "Move target X position."),
    ("moveTargetY", "Move target Y position."),
    ("unitID", "Unit type id."),
    ("statusFlags", "Unit status flags."),
];

const CSPRITE_DOCS: &[(&str, &str)] = &[
    ("sprite", "Sprite type id."),
    ("player", "Owning player."),
    ("selectionIndex", "Selection index."),
    ("visibilityFlags", "Visibility flags."),
    ("elevationLevel", "Elevation level."),
];

/// DSL common domain aliases from `ts/view.ts` `buildSchemaIndex`.
const COMMON_ALIASES: &[(&str, &[&str])] = &[
    ("hp", &["hitPoints", "hit_points"]),
    ("shield", &["shields"]),
    ("order", &["orderID", "order_id"]),
    ("owner", &["playerID", "player_id"]),
    ("posX", &["pos_x"]),
    ("posY", &["pos_y"]),
];

fn normalize(name: &str) -> String {
    name.chars().filter(|c| *c != '_').collect::<String>().to_ascii_lowercase()
}

fn generate_table(
    layout: &str,
    fields: &serde_json::Map<String, serde_json::Value>,
    docs: &[(&str, &str)],
) -> String {
    let overlay: HashMap<&str, &str> = docs.iter().copied().collect();
    for key in overlay.keys() {
        let Some(spec) = fields.get(*key) else {
            panic!("eudkit-lsp build.rs: doc overlay key {key} is not a field of {layout}");
        };
        if spec["size"].as_u64().unwrap_or(0) == 0 {
            panic!("eudkit-lsp build.rs: doc overlay key {key} is a size-0 field of {layout}");
        }
    }
    // Canonical fields with size > 0, in offset order (name breaks ties
    // for overlapping fields such as moveTargetPos/moveTargetX at 16).
    let mut ordered: Vec<(&str, &serde_json::Value)> = fields
        .iter()
        .filter(|(_, spec)| spec["size"].as_u64().unwrap_or(0) > 0)
        .map(|(name, spec)| (name.as_str(), spec))
        .collect();
    ordered.sort_by_key(|(name, spec)| (spec["offset"].as_u64().unwrap_or(0), (*name).to_string()));

    let mut out = String::new();
    out.push_str(&format!(
        "pub const {layout}_GENERATED: &[MemberDoc] = &[\n",
        layout = layout.to_ascii_uppercase()
    ));
    // Normalized label -> canonical field, for the ambiguity check.
    let mut seen: BTreeMap<String, String> = BTreeMap::new();
    let mut emit = |out: &mut String, label: &str, canonical: &str, spec: &serde_json::Value| {
        let norm = normalize(label);
        if let Some(prev) = seen.get(&norm) {
            if prev != canonical {
                panic!(
                    "eudkit-lsp build.rs: label {label} normalizes to the same name as {prev} but belongs to {canonical} in {layout}"
                );
            }
        }
        seen.insert(norm, canonical.to_string());
        let size = spec["size"].as_u64().unwrap_or(0);
        let detail = match spec["pointer"].as_str() {
            Some(target) => format!("pointer<{target}>"),
            None => match size {
                1 => "byte".to_string(),
                2 => "word".to_string(),
                4 => "dword".to_string(),
                other => panic!("eudkit-lsp build.rs: unexpected field size {other} for {canonical} in {layout}"),
            },
        };
        let doc = match overlay.get(canonical) {
            Some(doc) => (*doc).to_string(),
            None => match spec["pointer"].as_str() {
                Some(target) => format!(
                    "Pointer to {target} at offset {}.",
                    spec["offset"].as_u64().unwrap_or(0)
                ),
                None => format!(
                    "{layout} field {canonical} at offset {} ({}).",
                    spec["offset"].as_u64().unwrap_or(0),
                    detail
                ),
            },
        };
        out.push_str(&format!(
            "    MemberDoc {{ name: {label:?}, detail: {detail:?}, doc: {doc:?} }},\n"
        ));
    };
    for (name, spec) in &ordered {
        emit(&mut out, name, name, spec);
        if let Some(aliases) = spec["aliases"].as_array() {
            for alias in aliases {
                if let Some(alias) = alias.as_str() {
                    emit(&mut out, alias, name, spec);
                }
            }
        }
        for (canonical, aliases) in COMMON_ALIASES {
            if *canonical == *name {
                for alias in *aliases {
                    // Skip labels already emitted as canonical or schema
                    // alias names; the normalized-ambiguity check above
                    // still runs for the ones that are emitted.
                    let already = *name == *alias
                        || spec["aliases"]
                            .as_array()
                            .is_some_and(|a| a.iter().any(|x| x.as_str() == Some(alias)));
                    if !already {
                        emit(&mut out, alias, name, spec);
                    }
                }
            }
        }
    }
    out.push_str("];\n");
    out
}
