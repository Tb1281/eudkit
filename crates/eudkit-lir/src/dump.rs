//! Textual human-readable dump for LIR 3.0 documents.

use std::fmt::Write as _;

use crate::{ActData, BlobItem, CondData, LirDocument, LirError, LirItem, TriggerItem};

/// Render a parsed [`LirDocument`] to its canonical human-readable `.lir` form.
pub fn render_lir(doc: &LirDocument) -> Result<String, LirError> {
    if !crate::version_supported(&doc.version) {
        return Err(LirError::BadVersion(doc.version.clone()));
    }
    let m = &doc.module;
    let mut out = String::new();
    writeln!(
        out,
        "LIR v{} items={} entry={} strings={}",
        doc.version,
        m.items.len(),
        m.entry,
        m.strings.len()
    )
    .unwrap();
    for (idx, item) in m.items.iter().enumerate() {
        let marker = if idx == m.entry { " [ENTRY]" } else { "" };
        match item {
            LirItem::Trigger(t) => render_trigger(&mut out, idx, marker, t),
            LirItem::Blob(b) => render_blob(&mut out, idx, marker, b),
        }
    }
    Ok(out)
}

fn render_trigger(out: &mut String, idx: usize, marker: &str, t: &TriggerItem) {
    let preserved = if t.preserved { "preserved" } else { "once" };
    writeln!(
        out,
        "[{idx}] trigger{marker} size={} {preserved} prev={} next={}",
        t.size,
        fmt_ptr(t.prev, t.prev_reloc),
        fmt_ptr(t.next, t.next_reloc),
    )
    .unwrap();
    for (ci, c) in t.conds.iter().enumerate() {
        writeln!(out, "  cond[{ci}]: {}", fmt_cond(c)).unwrap();
    }
    for (ai, a) in t.acts.iter().enumerate() {
        writeln!(out, "  act[{ai}]: {}", fmt_act(a)).unwrap();
    }
}

fn render_blob(out: &mut String, idx: usize, marker: &str, b: &BlobItem) {
    writeln!(
        out,
        "[{idx}] blob{marker} size={} relocs={}",
        b.size,
        b.relocs.len()
    )
    .unwrap();
    for r in &b.relocs {
        writeln!(
            out,
            "  reloc @+0x{:x} mode={} -> ({}, +0x{:x})",
            r.offset, r.mode, r.target_item, r.target_offset
        )
        .unwrap();
    }
}

fn fmt_ptr(target: Option<i64>, reloc: bool) -> String {
    match (target, reloc) {
        (Some(v), true) => format!("item({v})"),
        (Some(v), false) => format!("const(0x{v:x})"),
        (None, _) => "null".to_string(),
    }
}

fn fmt_cond(c: &CondData) -> String {
    let r = &c.cond;
    let mut parts = match r.condtype {
        15 if r.eudx == 0x4353 => vec![format!(
            "DeathsX(player={}, cmp={}, amount={}, unit={}, mask=0x{:x})",
            fmt_p(r.player, &c.relocs, 4),
            r.comparison,
            fmt_p(r.amount, &c.relocs, 8),
            r.unit,
            r.locid,
        )],
        15 => vec![format!(
            "Deaths(player={}, cmp={}, amount={}, unit={})",
            fmt_p(r.player, &c.relocs, 4),
            r.comparison,
            fmt_p(r.amount, &c.relocs, 8),
            r.unit,
        )],
        22 => vec!["Always()".to_string()],
        23 => vec!["Never()".to_string()],
        other => vec![format!(
            "Cond#{other}(loc={}, player={}, amount={}, unit={}, cmp={}, res={}, flags=0x{:x}, eudx=0x{:x})",
            fmt_p(r.locid, &c.relocs, 0),
            fmt_p(r.player, &c.relocs, 4),
            fmt_p(r.amount, &c.relocs, 8),
            r.unit,
            r.comparison,
            r.restype,
            r.flags,
            r.eudx,
        )],
    };
    if !c.relocs.is_empty() {
        for r in &c.relocs {
            parts.push(format!(
                "[reloc @+{}:m{}->({},+0x{:x})]",
                r.field_offset, r.mode, r.target_item, r.target_offset
            ));
        }
    }
    parts.join(" ")
}

fn fmt_act(a: &ActData) -> String {
    let r = &a.act;
    let mut parts = match r.acttype {
        45 if r.eudx == 0x4353 => vec![format!(
            "SetDeathsX(player={}, mod={}, number={}, unit={}, mask=0x{:x})",
            fmt_p(r.dest, &a.relocs, 16),
            r.modifier,
            fmt_p(r.value, &a.relocs, 20),
            r.unit,
            r.mask,
        )],
        45 => vec![format!(
            "SetDeaths(player={}, mod={}, number={}, unit={})",
            fmt_p(r.dest, &a.relocs, 16),
            r.modifier,
            fmt_p(r.value, &a.relocs, 20),
            r.unit,
        )],
        other => vec![format!(
            "Act#{other}(loc={}, str={}, wav={}, time={}, p1={}, p2={}, unit={}, amt={}, flags=0x{:x}, eudx=0x{:x})",
            fmt_p(r.mask, &a.relocs, 0),
            r.strid,
            r.wavid,
            r.time,
            fmt_p(r.dest, &a.relocs, 16),
            fmt_p(r.value, &a.relocs, 20),
            r.unit,
            r.modifier,
            r.flags,
            r.eudx,
        )],
    };
    if !a.relocs.is_empty() {
        for r in &a.relocs {
            parts.push(format!(
                "[reloc @+{}:m{}->({},+0x{:x})]",
                r.field_offset, r.mode, r.target_item, r.target_offset
            ));
        }
    }
    parts.join(" ")
}

fn fmt_p(val: u32, relocs: &[crate::RelocData], field_offset: usize) -> String {
    if let Some(r) = relocs.iter().find(|r| r.field_offset == field_offset) {
        let mode = match r.mode {
            1 => "epd",
            4 => "ptr",
            m => {
                return format!("reloc(m{m}:item{}+0x{:x})", r.target_item, r.target_offset);
            }
        };
        if val == 0 {
            format!("{mode}(item{}+0x{:x})", r.target_item, r.target_offset)
        } else {
            format!(
                "{mode}(item{}+0x{:x})+0x{val:x}",
                r.target_item, r.target_offset
            )
        }
    } else if val >= 0x1000 {
        format!("0x{val:x}")
    } else {
        format!("{val}")
    }
}
