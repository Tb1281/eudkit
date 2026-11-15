//! Generate the stock and eudkit-extension LSP vocabulary from
//! `schema/stock-rows.json`.
//!
//! `build.rs` includes this module and writes the result into `OUT_DIR`; the
//! integration tests include it too so the loud-failure paths are covered
//! without spawning a nested build.
//!
//! Generation rules:
//! - The 22 stock conditions, 57 stock actions, and 13 eudkit extension rows
//!   are read in spec order. The `records` section is not trigger vocabulary,
//!   so `UnitProperty` is not generated.
//! - `name` is the row id and `detail` is `stock condition N` / `stock action
//!   N` / `eudkit extension`.
//! - `doc` combines the emitted CHK shape (type, default flags, ordered
//!   parameters) with the row's spec doc. Rows whose prose needs a caveat the
//!   spec does not carry use [`CURATED_DOC_OVERLAY`]; an overlay key that no
//!   longer names a generated spec row fails the build, so a rename cannot
//!   silently drop curated prose.
//! - A missing or unparseable spec fails the build; there is no fallback to
//!   a stale hand table (same rule as the layout member tables).

use std::fs;
use std::path::Path;

use serde_json::Value;

/// Curated doc suffixes for rows whose editor prose needs more than the spec.
/// Every key must name a generated (stock or extension) spec row.
pub const CURATED_DOC_OVERLAY: &[(&str, &str)] = &[
    (
        "DisplayText",
        "The text is shown to CurrentPlayer (`0x6509B0`), not to AllPlayers. The compiled payload runs with CurrentPlayer reset to 0 (Player 1), so text for the local human must set CurrentPlayer to the user player id (`0x512684`) or come from a classic trigger owned by that player.",
    ),
    (
        "DisplayTextIndex",
        "Like `DisplayText`, it targets CurrentPlayer (`0x6509B0`); the compiled payload starts with CurrentPlayer = 0 (Player 1), so text for the local human must set CurrentPlayer to the user player id (`0x512684`).",
    ),
];

#[derive(Debug, Clone)]
pub struct SpecParam {
    pub name: String,
    pub default: Option<u64>,
    pub target_form: bool,
}

#[derive(Debug, Clone)]
pub struct SpecRow {
    pub id: String,
    pub section: String,
    pub chk_type: Option<u64>,
    pub default_flags: Option<u64>,
    pub doc: String,
    pub params: Vec<SpecParam>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct GeneratedSymbol {
    pub name: String,
    pub detail: String,
    pub doc: String,
}

/// Read and shape the spec. Errors are returned as strings so `build.rs` can
/// panic with a single loud message.
pub fn load_rows(path: &Path) -> Result<Vec<SpecRow>, String> {
    let text = fs::read_to_string(path)
        .map_err(|error| format!("cannot read spec {}: {error}", path.display()))?;
    let spec: Value = serde_json::from_str(&text)
        .map_err(|error| format!("cannot parse spec {}: {error}", path.display()))?;
    let format = spec["format"].as_u64().unwrap_or(0);
    if format != 1 {
        return Err(format!(
            "spec {} has unsupported format {format} (expected 1)",
            path.display()
        ));
    }

    let mut rows = Vec::new();
    for section in ["conditions", "actions", "extensions"] {
        let section_rows = spec["sections"][section]
            .as_array()
            .ok_or_else(|| format!("spec {} has no `sections.{section}` array", path.display()))?;
        for row in section_rows {
            rows.push(parse_row(section, row, path)?);
        }
    }
    Ok(rows)
}

fn parse_row(section: &str, row: &Value, path: &Path) -> Result<SpecRow, String> {
    let id = row["id"]
        .as_str()
        .ok_or_else(|| format!("spec {} has a {section} row without an id", path.display()))?
        .to_string();
    let mut params = Vec::new();
    for param in row["params"]
        .as_array()
        .map(|params| params.as_slice())
        .unwrap_or(&[])
    {
        let name = param["name"].as_str().ok_or_else(|| {
            format!(
                "spec {} row {id} has a parameter without a name",
                path.display()
            )
        })?;
        params.push(SpecParam {
            name: name.to_string(),
            default: param["default"].as_u64(),
            target_form: param.get("targetForm").is_some(),
        });
    }
    Ok(SpecRow {
        id,
        section: section.to_string(),
        chk_type: row["chkType"].as_u64(),
        default_flags: row["defaultFlags"].as_u64(),
        doc: row["doc"].as_str().unwrap_or("").to_string(),
        params,
    })
}

/// Render the generated symbols using the checked-in curated overlay.
pub fn render_symbols(rows: &[SpecRow]) -> Result<Vec<GeneratedSymbol>, String> {
    generate_with_overlay(rows, CURATED_DOC_OVERLAY)
}

/// Render the generated symbols with an explicit overlay. Used by tests to
/// prove that a stale key fails loudly.
pub fn generate_with_overlay(
    rows: &[SpecRow],
    overlay: &[(&str, &str)],
) -> Result<Vec<GeneratedSymbol>, String> {
    for (key, _) in overlay {
        if !rows.iter().any(|row| row.id == *key) {
            return Err(format!(
                "doc overlay key {key} is not a stock or extension spec row"
            ));
        }
    }
    Ok(rows
        .iter()
        .map(|row| {
            let detail = match row.section.as_str() {
                "conditions" => format!("stock condition {}", row.chk_type.unwrap_or(0)),
                "actions" => format!("stock action {}", row.chk_type.unwrap_or(0)),
                _ => "eudkit extension".to_string(),
            };
            let mut doc = generated_doc(row);
            if let Some((_, extra)) = overlay.iter().find(|(key, _)| *key == row.id) {
                doc.push(' ');
                doc.push_str(extra);
            }
            GeneratedSymbol {
                name: row.id.clone(),
                detail,
                doc,
            }
        })
        .collect())
}

fn generated_doc(row: &SpecRow) -> String {
    let params = if row.params.is_empty() {
        "no parameters".to_string()
    } else {
        row.params
            .iter()
            .map(|param| match param.default {
                Some(value) => format!("{} = {}", param.name, format_default(value)),
                None => param.name.clone(),
            })
            .collect::<Vec<_>>()
            .join(", ")
    };
    let spec_doc = row.doc.trim();
    let mut doc = match row.section.as_str() {
        "conditions" => format!(
            "Stock condition {} helper (`{params}`). Emits CHK condition type {} with the stock field layout; condition flags stay 0. {spec_doc}",
            row.chk_type.unwrap_or(0),
            row.chk_type.unwrap_or(0),
        ),
        "actions" => format!(
            "Stock action {} helper (`{params}`). Emits CHK action type {} with default flags {}. {spec_doc}",
            row.chk_type.unwrap_or(0),
            row.chk_type.unwrap_or(0),
            row.default_flags.unwrap_or(0),
        ),
        _ => format!("eudkit extension helper (`{params}`), not a StarEdit stock row. {spec_doc}"),
    };
    if row.section != "extensions" {
        let targets: Vec<&str> = row
            .params
            .iter()
            .filter(|param| param.target_form)
            .map(|param| param.name.as_str())
            .collect();
        if targets.is_empty() {
            doc.push_str(" Constants only.");
        } else {
            doc.push_str(&format!(
                " Stock constants only, except {} also accept eudkit extension field-reference/pointer targets that relocate those CHK fields.",
                targets
                    .iter()
                    .map(|name| format!("`{name}`"))
                    .collect::<Vec<_>>()
                    .join(" and ")
            ));
        }
    }
    doc
}

fn format_default(value: u64) -> String {
    if value == u64::from(u32::MAX) {
        "0xffffffff".to_string()
    } else {
        value.to_string()
    }
}

/// Render the `STOCK_VOCABULARY` const that `lib.rs` includes and appends to
/// the hand-written core vocabulary.
pub fn render_vocabulary(rows: &[SpecRow]) -> Result<String, String> {
    let mut out = String::new();
    out.push_str(
        "// Generated from schema/stock-rows.json by eudkit-lsp/build.rs; do not edit.\n\
         pub const STOCK_VOCABULARY: &[SymbolDoc] = &[\n",
    );
    for symbol in render_symbols(rows)? {
        out.push_str(&format!(
            "    SymbolDoc {{ name: {:?}, detail: {:?}, doc: {:?} }},\n",
            symbol.name, symbol.detail, symbol.doc
        ));
    }
    out.push_str("];\n");
    Ok(out)
}

/// Load the spec at `path` and render the generated vocabulary source.
pub fn generate(path: &Path) -> Result<String, String> {
    render_vocabulary(&load_rows(path)?)
}
