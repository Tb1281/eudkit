//! `TBL.save_tbl` port (`src/eudkit/core/mapdata/tblformat.py`).
//!
//! [`save_tbl`] is the pure serializer: given the final `_datatb` order
//! and the per-string-id `_dataindextb`, it emits the STR section bytes.
//!
//! The subtle part is rebuilding `(_datatb, _dataindextb)` from LIR v0.2's
//! `module.strings`, which only carries the **per-string-id** ordered list
//! (base64). Python's `TBL` does not keep first-appearance order: when the
//! template map was loaded, every empty (or location/switch-culled) string
//! slot was queued for **recycling** (`TBL._emptystring`), and each new
//! string added via `AddString`/`GetStringIndex` during the build takes
//! over the next queued slot id and is inserted into `_datatb`
//! immediately *before* the next non-empty template string after that
//! slot (its "anchor"). Strings added via `ForceAddString` (e.g.
//! `StringBuffer` contents) always append instead — they never recycle.
//!
//! [`TemplateStrings::parse`] ports the template half of
//! `TBL.load_tbl_with_chk` (run by `init_stringmaps` at `LoadMap` time):
//! the load-order `_datatb`, the per-id `_dataindextb`, and the recycle
//! queue. [`save_tbl_from_ordered`] then replays the build's net effect,
//! which is fully determined by the template state plus the final per-id
//! list:
//!
//! - A template slot `k` was recycled iff the final id-`k` string differs
//!   from its load-time content. Recycled strings are replayed in
//!   ascending slot order, each inserted before its anchor's current
//!   `_datatb` position (same-anchor strings keep ascending slot order).
//! - Ids past the template count were appended during the build, in id
//!   order (duplicates proxy the earlier entry, as `AddString` does).
//!
//! Interleaving of build-time `AddString`/`ForceAddString` calls does not
//! affect the outcome: recycles only ever insert before template-string
//! anchors, and appends always land past them, so the final `_datatb`
//! order and every `_dataindextb` value are replay-order independent.
//!
//! Tier-2 normalization (user-approved Q7(b): the pure path is Tier-2,
//! i.e. semantic equivalence + documented normalization):
//! `ForceAddString` duplicates are deduplicated through `stringmap`,
//! while Python's `TBL.ForceAddString` always appends a fresh `_datatb`
//! entry (its `_stringmap` update is commented out in `tblformat.py`).
//! Every string id still resolves to byte-identical content under both
//! schemes — verified on the strfunc build, 1030/1030 ids with 0
//! differing bytes (F44) — so only the `_datatb` packing differs (fewer
//! entries, shifted offsets). This is a documented normalization, not a
//! miscompile: Python's append-everything is implementation inertia, not
//! intended semantics ("refine and improve, don't deify").
//!
//! `DecodeUnitNameAs` re-encoding is not ported either (process-global
//! Python state, never set on the blank-template path, and not captured
//! in LIR).

use std::collections::{HashMap, HashSet};
use std::path::Path;

use crate::chk::Chk;
use crate::layout::{
    checked_records, MrgnEntry, FORC_NAME_OFFSETS, MRGN_RECORD_SIZE, SPRP_MIN_LEN,
    SPRP_NAME_OFFSETS, STR_LOAD_WIDTH, STR_SAVE_WIDTH, SWNM_COUNT,
    SWNM_SECTION_SIZE, SWNM_STRID_SIZE, UNIX_COUNT, UNIX_NAME_OFFSET,
    UNIX_NAME_STRIDE,
};
use crate::MapTailError;

// Test-only per-thread counter of [`TemplateStrings::parse`] calls.
//
// A thread-local counter keeps a test's "before/after" delta its own
// when the rest of the suite parses other templates in parallel. It is
// compiled out of non-test builds, so production pays nothing.
#[cfg(test)]
thread_local! {
    static TEMPLATE_PARSE_COUNT: std::cell::Cell<usize> = const { std::cell::Cell::new(0) };
}

/// Number of [`TemplateStrings::parse`] calls on this thread (tests only).
#[cfg(test)]
pub(crate) fn template_parse_count() -> usize {
    TEMPLATE_PARSE_COUNT.with(std::cell::Cell::get)
}

/// One recycled-slot record: the 0-based template string id whose content
/// was empty at load, plus the anchor — the next non-empty template
/// string after it (`None` when none follows; the anchor is then
/// `_first_extended_string`).
#[derive(Debug, Clone)]
struct EmptySlot {
    slot: u32,
    anchor: Option<Vec<u8>>,
}

/// Template-side TBL load state: the persistent fields of
/// `TBL.load_tbl_with_chk` after `init_stringmaps`.
#[derive(Debug, Clone)]
pub struct TemplateStrings {
    /// Unique strings in load (first-appearance) order.
    datatb: Vec<Vec<u8>>,
    /// Per template string id (0-based) → `datatb` index.
    dataindextb: Vec<u32>,
    /// String bytes → representative 0-based string id.
    stringmap: HashMap<Vec<u8>, u32>,
    /// Recycle queue in ascending slot-id order.
    empty: Vec<EmptySlot>,
    /// First unique string ever added (always a template string).
    first_extended: Vec<u8>,
    /// Template string count (ids are dense 1..=count).
    count: usize,
}

fn u16le(b: &[u8], off: usize) -> u16 {
    u16::from_le_bytes([b[off], b[off + 1]])
}

fn u32le(b: &[u8], off: usize) -> u32 {
    u32::from_le_bytes([b[off], b[off + 1], b[off + 2], b[off + 3]])
}

/// NUL-terminated string at `off`; `None` when `off` is out of range
/// (Python's "invalid string offset" → the id is skipped). A missing
/// terminator reads to the end of the section, like the guarded scan in
/// `load_tbl_with_chk`.
fn cstr_at(content: &[u8], off: usize) -> Option<&[u8]> {
    if off >= content.len() {
        return None;
    }
    let mut end = off;
    while end < content.len() && content[end] != 0 {
        end += 1;
    }
    Some(&content[off..end])
}

/// Like [`cstr_at`] but loud on a bad offset or a missing terminator:
/// mirrors the *unguarded* next-string scan in `load_tbl_with_chk`,
/// which lets `IndexError` propagate.
fn cstr_at_strict<'a>(
    section: &'static str,
    content: &'a [u8],
    off: usize,
) -> Result<&'a [u8], MapTailError> {
    let s = cstr_at(content, off)
        .ok_or_else(|| MapTailError::TruncatedSection(section))?;
    if off + s.len() >= content.len() {
        // Ran off the end without a NUL terminator.
        return Err(MapTailError::TruncatedSection(section));
    }
    Ok(s)
}

impl TemplateStrings {
    /// Port of the persistent state built by `TBL.load_tbl_with_chk`
    /// (via `init_stringmaps` at `LoadMap` time). Reads the pristine
    /// template sections: `STRx` (falling back to `STR `, with
    /// `load_entry` 4 vs 2 exactly like `init_stringmaps`), `MRGN`,
    /// `SWNM`, `UNIx`, `SPRP`, `FORC`.
    ///
    /// Must run before `_fix_map_data`: the TBL load reads `UNIx`
    /// (unit-name string ids), which `_fix_map_data` rewrites.
    pub fn parse(chkt: &Chk) -> Result<Self, MapTailError> {
        #[cfg(test)]
        TEMPLATE_PARSE_COUNT.with(|c| c.set(c.get() + 1));
        let (str_section, str_bytes, load_entry): (&'static str, &[u8], usize) =
            match chkt.getsection(b"STRx") {
                Some(b) => ("STRx", b, STR_SAVE_WIDTH),
                None => match chkt.getsection(b"STR ") {
                    Some(b) => ("STR ", b, STR_LOAD_WIDTH),
                    None => {
                        return Err(MapTailError::MissingSection("STRx/STR "));
                    }
                },
            };
        let read_entry = |off: usize| -> Result<usize, MapTailError> {
            if off + load_entry > str_bytes.len() {
                return Err(MapTailError::TruncatedSection(str_section));
            }
            Ok(if load_entry == STR_SAVE_WIDTH {
                u32le(str_bytes, off) as usize
            } else {
                u16le(str_bytes, off) as usize
            })
        };

        // --- removed_str: location/switch names culled at load ---
        let mut referenced = HashSet::new();
        if let Some(mrgn) = chkt.getsection(b"MRGN") {
            // Python: no try/except around MRGN (KeyError → skip).
            // A trailing partial record is loud, not silently
            // dropped (see the probe-pinned test below).
            let mrgn = checked_records(Some(mrgn), "MRGN", MRGN_RECORD_SIZE)?;
            for entry in MrgnEntry::entries(mrgn) {
                let strid = entry.strid();
                if strid != 0 {
                    referenced.insert(strid as usize);
                }
            }
        }
        let mut sw_referenced = HashSet::new();
        if let Some(swnm) = chkt.getsection(b"SWNM") {
            if swnm.len() < SWNM_SECTION_SIZE {
                return Err(MapTailError::TruncatedSection("SWNM"));
            }
            for i in 0..SWNM_COUNT {
                let strid = u32le(swnm, SWNM_STRID_SIZE * i);
                if strid != 0 {
                    sw_referenced.insert(strid as usize);
                }
            }
        }
        let unix = chkt
            .getsection(b"UNIx")
            .ok_or(MapTailError::MissingSection("UNIx"))?;
        if unix.len() < UNIX_NAME_OFFSET + UNIX_COUNT * UNIX_NAME_STRIDE {
            return Err(MapTailError::TruncatedSection("UNIx"));
        }
        let mut reserved: HashSet<usize> = HashSet::new();
        for i in 0..UNIX_COUNT {
            let strid = u16le(unix, UNIX_NAME_OFFSET + i * UNIX_NAME_STRIDE);
            if strid != 0 {
                reserved.insert(strid as usize);
            }
        }
        let sprp = chkt
            .getsection(b"SPRP")
            .ok_or(MapTailError::MissingSection("SPRP"))?;
        if sprp.len() < SPRP_MIN_LEN {
            return Err(MapTailError::TruncatedSection("SPRP"));
        }
        for &off in &SPRP_NAME_OFFSETS {
            let strid = u16le(sprp, off);
            if strid != 0 {
                reserved.insert(strid as usize);
            }
        }
        let forc = chkt
            .getsection(b"FORC")
            .ok_or(MapTailError::MissingSection("FORC"))?;
        // DELIBERATE tightening: the old bound was 14, but the loop
        // reads name offsets 8, 10, 12, 14, needing byte 15 — a
        // 14-byte FORC was a latent panic. Now loud TruncatedSection.
        if forc.len() < FORC_NAME_OFFSETS[3] + 2 {
            return Err(MapTailError::TruncatedSection("FORC"));
        }
        for &off in &FORC_NAME_OFFSETS {
            let strid = u16le(forc, off);
            if strid != 0 {
                reserved.insert(strid as usize);
            }
        }
        let removed: HashSet<usize> = referenced
            .union(&sw_referenced)
            .copied()
            .filter(|id| !reserved.contains(id))
            .collect();

        // --- load_tbl_with_chk main loop ---
        let stringcount = read_entry(0)?;
        let mut datatb: Vec<Vec<u8>> = Vec::new();
        let mut dataindextb: Vec<u32> = Vec::new();
        let mut stringmap: HashMap<Vec<u8>, u32> = HashMap::new();
        let mut empty: Vec<EmptySlot> = Vec::new();
        let mut first_extended: Option<Vec<u8>> = None;

        // load-time AddString (recycle branch unreachable: not _loaded).
        let add_string = |string: Vec<u8>,
                              datatb: &mut Vec<Vec<u8>>,
                              dataindextb: &mut Vec<u32>,
                              stringmap: &mut HashMap<Vec<u8>, u32>,
                              first_extended: &mut Option<Vec<u8>>| {
            let stringindex = dataindextb.len() as u32;
            if let Some(&repr) = stringmap.get(&string) {
                let dataindex = dataindextb[repr as usize];
                dataindextb.push(dataindex);
            } else {
                if first_extended.is_none() {
                    *first_extended = Some(string.clone());
                }
                let dataindex = datatb.len() as u32;
                datatb.push(string.clone());
                dataindextb.push(dataindex);
                stringmap.insert(string, stringindex);
            }
        };

        for i in 1..=stringcount {
            let stringoffset = read_entry(i * load_entry)?;
            let Some(raw) = cstr_at(str_bytes, stringoffset) else {
                // Invalid string offset: Python `continue`s, skipping the
                // id entirely (no AddString, no empty record).
                continue;
            };
            let string = raw.to_vec();

            if string.is_empty() || removed.contains(&i) {
                // Queue this slot for recycling: find the next non-empty,
                // non-removed string after it.
                let mut pushed = false;
                for j in (i + 1)..=stringcount {
                    if removed.contains(&j) {
                        continue;
                    }
                    let nextoffset = read_entry(j * load_entry)?;
                    let next = cstr_at_strict(str_section, str_bytes, nextoffset)?;
                    if !next.is_empty() {
                        empty.push(EmptySlot {
                            slot: (i - 1) as u32,
                            anchor: Some(next.to_vec()),
                        });
                        pushed = true;
                        break;
                    }
                }
                if !pushed {
                    empty.push(EmptySlot {
                        slot: (i - 1) as u32,
                        anchor: None,
                    });
                }
            }

            // `if i in removed_str: string = b""` — after the empty scan,
            // before AddString. (unitmap/locmap/swmap recording skipped:
            // it does not affect save_tbl bytes.)
            let string = if removed.contains(&i) {
                Vec::new()
            } else {
                string
            };
            add_string(
                string,
                &mut datatb,
                &mut dataindextb,
                &mut stringmap,
                &mut first_extended,
            );
        }

        let first_extended = first_extended.ok_or_else(|| {
            MapTailError::TemplateTbl("template string table is empty".into())
        })?;
        Ok(TemplateStrings {
            count: dataindextb.len(),
            datatb,
            dataindextb,
            stringmap,
            empty,
            first_extended,
        })
    }

    /// Number of template string ids (dense 1..=count).
    pub fn count(&self) -> usize {
        self.count
    }

    /// String bytes for a 1-based template string id, as loaded
    /// (empty for recycled/removed slots; `None` for out-of-range ids).
    pub fn string_by_id(&self, id: usize) -> Option<&[u8]> {
        let idx = self.dataindextb.get(id.checked_sub(1)?)?;
        Some(self.datatb[*idx as usize].as_slice())
    }

    /// Serialize the template's own load state as STRx (`save_entry=4`):
    /// the v0.1 string-less-LIR fallback. Mirrors Python's
    /// `load_entry=2` → `save_entry=4` conversion (`init_stringmaps`
    /// parses with the section's native entry width, `save_tbl` always
    /// writes width 4). Copying the raw section bytes instead would
    /// mislabel a `STR ` (u16 offsets) section as `STRx` (u32 offsets) —
    /// a silently corrupt string table (F43).
    pub fn save_as_strx(&self) -> Vec<u8> {
        save_tbl(&self.datatb, &self.dataindextb)
    }
}

/// Pure port of `TBL.save_tbl`.
///
/// - `datatb`: unique strings in `_datatb` order (exact bytes).
/// - `dataindextb`: per string id (0-based) → `datatb` index.
/// - Entry width is 4 (`save_entry=4` pin).
pub fn save_tbl(datatb: &[Vec<u8>], dataindextb: &[u32]) -> Vec<u8> {
    fn roundup_by_4(n: usize) -> usize {
        n.next_multiple_of(4)
    }

    let size = 4usize;
    let mut stroffset: Vec<u32> = Vec::with_capacity(datatb.len());
    let mut outindex = roundup_by_4(size * dataindextb.len() + size);
    for s in datatb {
        stroffset.push(outindex as u32);
        outindex += roundup_by_4(s.len() + 1);
    }

    let mut out = Vec::new();
    // String count.
    out.extend_from_slice(&(dataindextb.len() as u32).to_le_bytes());
    // String offsets.
    for &dataidx in dataindextb {
        out.extend_from_slice(&stroffset[dataidx as usize].to_le_bytes());
    }
    // Pad the offset table to a 4-byte multiple.
    let tablesize = size * (dataindextb.len() + 1);
    out.extend(std::iter::repeat(0u8).take(roundup_by_4(tablesize) - tablesize));
    // String data, NUL-terminated and 4-padded.
    for s in datatb {
        out.extend_from_slice(s);
        out.extend(std::iter::repeat(0u8).take(roundup_by_4(s.len() + 1) - s.len()));
    }
    out
}

/// A built string table: the serialized STR section bytes plus the
/// per-string-id section offsets of that same build. The offsets are
/// derived from the same (`datatb`, `dataindextb`) pair the bytes were
/// serialized from — never from a second serialization — so the bytes
/// and the offsets cannot disagree about a string's address.
///
/// The ordered path always carries offsets (`Some`); the fallback path
/// (`save_as_strx`, string-less LIR) carries none: no ordered list
/// exists there, and verification is skipped when `str_addrs` is empty,
/// so fallback offsets would be meaningless, not merely uncomputed.
#[derive(Debug, Clone)]
pub struct BuiltStringTable {
    /// Serialized STR section bytes (always width 4, `save_entry=4`).
    pub bytes: Vec<u8>,
    /// Per-string-id section offsets, indexed by (game id - 1), or
    /// `None` for the fallback path.
    pub offsets: Option<Vec<u32>>,
}

impl BuiltStringTable {
    /// Build the table from a per-string-id ordered list and the
    /// template load state: one replay, one serialization, offsets
    /// from that same build.
    pub fn from_ordered(
        ordered: &[Vec<u8>],
        template: &TemplateStrings,
    ) -> Result<Self, MapTailError> {
        let (bytes, offsets) = build_ordered(ordered, template)?;
        Ok(BuiltStringTable {
            bytes,
            offsets: Some(offsets),
        })
    }

    /// Build the table for string-less LIR: re-serialize the template's
    /// own table as STRx. No ordered list exists, so no offsets.
    pub fn fallback(template: &TemplateStrings) -> Self {
        BuiltStringTable {
            bytes: template.save_as_strx(),
            offsets: None,
        }
    }
}

/// Per-`datatb`-entry section offsets for a table with `id_count` ids:
/// the header occupies `roundup4(4 * id_count + 4)` bytes, then each
/// entry's NUL-terminated, 4-padded bytes in `datatb` order. Shared by
/// [`save_tbl`] and [`per_id_offsets`] so the serialized header and
/// the reported offsets use one arithmetic.
fn entry_offsets(datatb: &[Vec<u8>], id_count: usize) -> Vec<u32> {
    fn roundup_by_4(n: usize) -> usize {
        n.next_multiple_of(4)
    }

    let mut stroffset: Vec<u32> = Vec::with_capacity(datatb.len());
    let mut outindex = roundup_by_4(4 * id_count + 4);
    for s in datatb {
        stroffset.push(outindex as u32);
        outindex += roundup_by_4(s.len() + 1);
    }
    stroffset
}

/// Per-string-id section offsets for a (`datatb`, `dataindextb`) pair:
/// each id's offset is its `datatb` entry's offset.
fn per_id_offsets(datatb: &[Vec<u8>], dataindextb: &[u32]) -> Vec<u32> {
    let stroffset = entry_offsets(datatb, dataindextb.len());
    dataindextb
        .iter()
        .map(|&dataidx| stroffset[dataidx as usize])
        .collect()
}

/// Rebuild `(_datatb, _dataindextb)` from the per-string-id ordered list
/// (LIR v0.2 `module.strings`, base64-decoded) and the template load
/// state, replaying the build's net string-table effect (see the module
/// docs for the recycling rule), then serialize with [`save_tbl`].
/// Returns the bytes plus the per-string-id offsets of that same build.
fn build_ordered(
    ordered: &[Vec<u8>],
    template: &TemplateStrings,
) -> Result<(Vec<u8>, Vec<u32>), MapTailError> {
    let tcount = template.count;
    if ordered.len() < tcount {
        return Err(MapTailError::TemplateTbl(format!(
            "LIR strings ({} ids) shorter than template table ({tcount} ids); \
             hint: the HIR string table was not seeded for this basemap — \
             regenerate the HIR through eudc (with --basemap) so MapInfo \
             is passed to the frontend and the lowering",
            ordered.len()
        )));
    }

    let mut datatb = template.datatb.clone();
    let mut dataindextb = template.dataindextb.clone();
    let mut stringmap = template.stringmap.clone();
    let slot_anchor: HashMap<u32, Option<&Vec<u8>>> = template
        .empty
        .iter()
        .map(|e| (e.slot, e.anchor.as_ref()))
        .collect();

    // Recycled slots, in ascending id order: the final id-k string
    // differs from its load-time content exactly when slot k was reused.
    for k in 0..tcount {
        let load_content = &datatb[dataindextb[k] as usize];
        if ordered[k] == *load_content {
            continue;
        }
        let s = ordered[k].clone();
        let entry_anchor =
            slot_anchor
                .get(&(k as u32))
                .ok_or_else(|| {
                    MapTailError::TemplateTbl(format!(
                        "string id {} changed but was not a recyclable template slot",
                        k + 1
                    ))
                })?;
        // Bare queue entries anchor at the first extended string.
        let anchor: &[u8] = match entry_anchor {
            Some(v) => v.as_slice(),
            None => template.first_extended.as_slice(),
        };
        let pos = datatb
            .iter()
            .position(|x| x.as_slice() == anchor)
            .ok_or_else(|| {
                MapTailError::TemplateTbl(
                    "recycle anchor missing from template strings".into(),
                )
            })?;
        // Port of the AddString recycle branch: insert before the anchor,
        // bump shifted indices, take over the slot id.
        datatb.insert(pos, s.clone());
        for v in dataindextb.iter_mut() {
            if *v >= pos as u32 {
                *v += 1;
            }
        }
        dataindextb[k] = pos as u32;
        stringmap.insert(s, k as u32);
    }

    // Ids past the template count were appended during the build, in id
    // order; duplicates proxy the earlier entry (AddString semantics).
    // ForceAddString appends are indistinguishable here: Python would
    // store a duplicate `_datatb` entry, but every id resolves to the
    // same bytes either way (Tier-2 normalization, see module docs).
    for (k, s) in ordered.iter().enumerate().skip(tcount) {
        if let Some(&repr) = stringmap.get(s) {
            dataindextb.push(dataindextb[repr as usize]);
        } else {
            stringmap.insert(s.clone(), k as u32);
            dataindextb.push(datatb.len() as u32);
            datatb.push(s.clone());
        }
    }

    let bytes = save_tbl(&datatb, &dataindextb);
    let offsets = per_id_offsets(&datatb, &dataindextb);
    Ok((bytes, offsets))
}

/// Rebuild and serialize the ordered string table: thin wrapper over
/// [`BuiltStringTable::from_ordered`] returning only the bytes.
pub fn save_tbl_from_ordered(
    ordered: &[Vec<u8>],
    template: &TemplateStrings,
) -> Result<Vec<u8>, MapTailError> {
    Ok(BuiltStringTable::from_ordered(ordered, template)?.bytes)
}

/// Per-string-id STR-section offsets for an ordered string list, via
/// the exact replay assembly uses ([`BuiltStringTable::from_ordered`]).
///
/// Shared by the eudc driver (which computes the table lowering
/// resolves `StrAddr` against) and by `assemble_map` (which compares
/// it against the recorded table and fails loud on any mismatch).
/// The offsets come from the same build the bytes come from — rather
/// than a second serialization read back, and rather than duplicating
/// `save_tbl`'s arithmetic — so the two computations are identical by
/// construction. The returned vector is indexed by (game id - 1).
pub fn string_section_offsets(
    ordered: &[Vec<u8>],
    template: &TemplateStrings,
) -> Result<Vec<u32>, MapTailError> {
    let table = BuiltStringTable::from_ordered(ordered, template)?;
    table.offsets.ok_or_else(|| {
        MapTailError::TemplateTbl(
            "ordered string-table build carries no offsets".into(),
        )
    })
}

/// Compute section-relative offsets for a complete ordered LIR string list.
///
/// `strings_b64` must be the exact list the final LIR carries (template
/// strings first when a basemap is used, then module/display strings).
/// The STR header size depends on the total id count, so this must run
/// after all strings are known; `assemble_map` recomputes and verifies.
///
/// This one-shot wrapper opens a temporary [`crate::basemap::BasemapContext`];
/// a driver that also needs MapInfo should keep one context and call
/// [`crate::basemap::BasemapContext::str_offsets_for_full_strings`] so
/// the template string table is parsed only once per compile.
pub fn str_offsets_for_full_strings(
    basemap: &Path,
    strings_b64: &[String],
) -> Result<Vec<u32>, MapTailError> {
    crate::basemap::BasemapContext::open(basemap)?.str_offsets_for_full_strings(strings_b64)
}

/// Driver-side offset computation for one basemap: parse the template
/// CHK, rebuild the ordered list the lowering will produce from the
/// template strings plus the HIR module strings past the template
/// count (exactly `StringTableState::configure`'s construction:
/// template verbatim ++ appended HIR tail), and return the per-id
/// offsets via [`string_section_offsets`].
///
/// `template_b64` is the MapInfo template list (base64 raw bytes, one
/// entry per template id); `hir_strings` is the HIR module string
/// table (empty = template-only mode). When `hir_strings` is shorter
/// than the template but non-empty, the ordered list stays
/// template-only — lowering's witness check then fails loud on its
/// own; the driver never invents offsets for a mismatched table.
pub fn str_offsets_for_basemap(
    basemap: &Path,
    template_b64: &[String],
    hir_strings: &[String],
) -> Result<Vec<u32>, MapTailError> {
    let ctx = crate::basemap::BasemapContext::open(basemap)?;
    let tpl = ctx.template_strings();
    let tcount = tpl.count();
    let mut ordered: Vec<Vec<u8>> = Vec::with_capacity(template_b64.len());
    for (i, entry) in template_b64.iter().enumerate() {
        ordered.push(crate::decode_b64(entry).ok_or_else(|| {
            MapTailError::BadStrings(format!(
                "template string {i} is not valid base64"
            ))
        })?);
    }
    if hir_strings.len() >= tcount {
        ordered.extend(
            hir_strings[tcount..]
                .iter()
                .map(|s| s.as_bytes().to_vec()),
        );
    }
    string_section_offsets(&ordered, tpl)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::layout::{UNIX_NAME_OFFSET, UNIX_SECTION_LEN};

    #[test]
    fn save_tbl_small() {
        // datatb = ["a", "bc"], ids: 0->"a", 1->"bc", 2->"a" (proxy).
        let datatb = vec![b"a".to_vec(), b"bc".to_vec()];
        let dataindextb = vec![0u32, 1, 0];
        let out = save_tbl(&datatb, &dataindextb);
        // count=3, offsets: outindex starts at roundup(4*3+4)=16;
        // "a"@16, "bc"@20; table = 4*(3+1)=16 bytes, no pad.
        let mut want = Vec::new();
        want.extend_from_slice(&3u32.to_le_bytes());
        want.extend_from_slice(&16u32.to_le_bytes());
        want.extend_from_slice(&20u32.to_le_bytes());
        want.extend_from_slice(&16u32.to_le_bytes());
        want.extend_from_slice(b"a\0\0\0");
        want.extend_from_slice(b"bc\0\0");
        assert_eq!(out, want);
    }

    /// Build a synthetic template CHK the way `load_tbl_with_chk` sees
    /// it: 6 strings, ids 1..6 = ["aa", "", "bb", "cc", "", ""],
    /// load_entry=2 (`STR `), no MRGN/SWNM names, no reserved ids.
    fn synthetic_template() -> Chk {
        // Offsets (u16): strings start at roundup(2*(6+1)) = 14.
        // "aa"@14, ""@18, "bb"@20, "cc"@24, ""@28, ""@28 (dup).
        let mut strsec = Vec::new();
        strsec.extend_from_slice(&6u16.to_le_bytes());
        for off in [14u16, 18, 20, 24, 28, 28] {
            strsec.extend_from_slice(&off.to_le_bytes());
        }
        assert_eq!(strsec.len(), 14);
        strsec.extend_from_slice(b"aa\0\0"); // 14..18
        strsec.extend_from_slice(b"\0\0"); // 18..20
        strsec.extend_from_slice(b"bb\0\0"); // 20..24
        strsec.extend_from_slice(b"cc\0\0"); // 24..28
        strsec.extend_from_slice(b"\0\0"); // 28..30
        let mut chkt = Chk::new();
        chkt.setsection(*b"STR ", strsec);
        chkt.setsection(*b"UNIx", vec![0u8; UNIX_SECTION_LEN]);
        chkt.setsection(*b"SPRP", vec![0u8; 4]);
        chkt.setsection(*b"FORC", vec![0u8; 16]);
        chkt
    }

    #[test]
    fn mrgn_partial_record_is_loud_at_parse() {
        // Probe-pinned: before the tightening, a 38-byte MRGN (one
        // full entry + 18 bytes of a second entry whose strid bytes
        // sit at +16/+17 of the partial entry) parsed SUCCESSFULLY,
        // the partial location was silently dropped, and its strid
        // never entered the referenced set. The tightening makes the
        // malformed section loud instead.
        let mut chkt = synthetic_template();
        let mut mrgn = vec![0u8; 38];
        mrgn[36..38].copy_from_slice(&3u16.to_le_bytes());
        chkt.setsection(*b"MRGN", mrgn);
        assert!(matches!(
            TemplateStrings::parse(&chkt),
            Err(MapTailError::TruncatedSection("MRGN"))
        ));
    }

    #[test]
    fn swnm_short_section_is_loud_at_parse() {
        let mut chkt = synthetic_template();
        chkt.setsection(*b"SWNM", vec![0u8; 1023]);
        assert!(matches!(
            TemplateStrings::parse(&chkt),
            Err(MapTailError::TruncatedSection("SWNM"))
        ));
    }

    #[test]
    fn forc_14_byte_section_is_loud_at_parse() {
        // DELIBERATE tightening (port-09): the old <14 acceptance is
        // a latent panic — the loop then reads name offsets 8, 10,
        // 12, 14, needing byte 15. A 14-byte FORC must fail loud.
        // Valid programs are unaffected (real FORC is >= 16).
        let mut chkt = synthetic_template();
        chkt.setsection(*b"FORC", vec![0u8; 14]);
        assert!(matches!(
            TemplateStrings::parse(&chkt),
            Err(MapTailError::TruncatedSection("FORC"))
        ));
    }

    #[test]
    fn template_parse_recycle_queue() {
        let chkt = synthetic_template();
        let tpl = TemplateStrings::parse(&chkt).unwrap();
        assert_eq!(tpl.count(), 6);
        // Load order datatb: ["aa", "", "bb", "cc"].
        assert_eq!(
            tpl.datatb,
            vec![
                b"aa".to_vec(),
                b"".to_vec(),
                b"bb".to_vec(),
                b"cc".to_vec()
            ]
        );
        // dataindextb: [0, 1, 2, 3, 1, 1].
        assert_eq!(tpl.dataindextb, vec![0, 1, 2, 3, 1, 1]);
        // Empty slots: id 2 (0-based 1) → next non-empty "bb";
        // ids 5,6 (0-based 4,5) → no later non-empty → bare.
        assert_eq!(tpl.empty.len(), 3);
        assert_eq!(tpl.empty[0].slot, 1);
        assert_eq!(tpl.empty[0].anchor, Some(b"bb".to_vec()));
        assert_eq!(tpl.empty[1].slot, 4);
        assert_eq!(tpl.empty[1].anchor, None);
        assert_eq!(tpl.empty[2].slot, 5);
        assert_eq!(tpl.empty[2].anchor, None);
        assert_eq!(tpl.first_extended, b"aa".to_vec());
    }

    #[test]
    fn save_tbl_from_ordered_recycles_before_anchor() {
        // Build adds "NEW1" (recycles slot id 2 → before "bb") and "NEW2"
        // (recycles slot id 5 → bare → before first_extended "aa"), plus
        // a ForceAddString-style append "TAIL".
        let chkt = synthetic_template();
        let tpl = TemplateStrings::parse(&chkt).unwrap();
        let ordered = vec![
            b"aa".to_vec(),
            b"NEW1".to_vec(),
            b"bb".to_vec(),
            b"cc".to_vec(),
            b"NEW2".to_vec(),
            b"".to_vec(),
            b"TAIL".to_vec(),
        ];
        let out = save_tbl_from_ordered(&ordered, &tpl).unwrap();
        // Expected _datatb: "NEW1" before "bb"; "NEW2" before "aa"
        // (bare → first_extended anchor); "TAIL" appended.
        // Load datatb was [aa, "", bb, cc].
        let datatb = vec![
            b"NEW2".to_vec(),
            b"aa".to_vec(),
            b"".to_vec(),
            b"NEW1".to_vec(),
            b"bb".to_vec(),
            b"cc".to_vec(),
            b"TAIL".to_vec(),
        ];
        let dataindextb = vec![1u32, 3, 4, 5, 0, 2, 6];
        assert_eq!(out, save_tbl(&datatb, &dataindextb));
    }

    #[test]
    fn save_tbl_from_ordered_no_build_changes() {
        // No recycled slots, no appends: the table round-trips the
        // template load order exactly.
        let chkt = synthetic_template();
        let tpl = TemplateStrings::parse(&chkt).unwrap();
        let ordered = vec![
            b"aa".to_vec(),
            b"".to_vec(),
            b"bb".to_vec(),
            b"cc".to_vec(),
            b"".to_vec(),
            b"".to_vec(),
        ];
        let out = save_tbl_from_ordered(&ordered, &tpl).unwrap();
        assert_eq!(
            out,
            save_tbl(
                &vec![
                    b"aa".to_vec(),
                    b"".to_vec(),
                    b"bb".to_vec(),
                    b"cc".to_vec()
                ],
                &[0, 1, 2, 3, 1, 1],
            )
        );
    }

    /// Parse a STRx section (u32 offsets) into per-id (1-based) strings.
    fn parse_strx_ids(section: &[u8]) -> Vec<Vec<u8>> {
        let n = u32::from_le_bytes(section[0..4].try_into().unwrap()) as usize;
        assert!(n < 1_000_000, "STRx string count garbage: {n}");
        let mut out = Vec::with_capacity(n);
        for i in 0..n {
            let o = 4 + 4 * i;
            let off = u32::from_le_bytes(section[o..o + 4].try_into().unwrap()) as usize;
            let end = section[off..]
                .iter()
                .position(|&b| b == 0)
                .unwrap_or_else(|| panic!("STRx id {}: unterminated", i + 1));
            out.push(section[off..off + end].to_vec());
        }
        out
    }

    #[test]
    fn save_tbl_from_ordered_forceaddstring_duplicates_resolve_identically() {
        // Equivalence check (F44): Python's `TBL.ForceAddString` always
        // appends a fresh `_datatb` entry (e.g. three `StringBuffer(1023)`
        // calls → three identical entries), while the replay deduplicates
        // through `stringmap`. This pins the Tier-2 normalization contract:
        // every string id must resolve to byte-identical content under both
        // schemes — only the `_datatb` packing may differ. Verified on the
        // real strfunc build too (1030/1030 ids, 0 differing bytes).
        let chkt = synthetic_template();
        let tpl = TemplateStrings::parse(&chkt).unwrap();
        // Mimic StringBuffer: the same bytes ForceAddString'd three times
        // as separate ids, plus a duplicate pair, plus a recycled slot.
        let dup1023 = vec![b'\r'; 1023];
        let dup218 = vec![b'\r'; 218];
        let ordered = vec![
            b"aa".to_vec(),
            b"NEW1".to_vec(), // recycles slot id 2
            b"bb".to_vec(),
            b"cc".to_vec(),
            b"".to_vec(),
            b"".to_vec(),
            dup1023.clone(), // ForceAddString #1
            dup1023.clone(), // ForceAddString #2 (duplicate)
            dup1023.clone(), // ForceAddString #3 (duplicate)
            dup218.clone(),  // ForceAddString #4
            dup218.clone(),  // ForceAddString #5 (duplicate)
            b"dpdkfah!".to_vec(),
            b"dpdkfah!".to_vec(), // duplicate
        ];
        let out = save_tbl_from_ordered(&ordered, &tpl).unwrap();
        let got = parse_strx_ids(&out);
        assert_eq!(got.len(), ordered.len());
        for (i, (g, want)) in got.iter().zip(ordered.iter()).enumerate() {
            assert_eq!(g, want, "id {} must resolve to its assigned bytes", i + 1);
        }
    }

    #[test]
    fn save_tbl_from_ordered_short_lir_is_loud() {
        let chkt = synthetic_template();
        let tpl = TemplateStrings::parse(&chkt).unwrap();
        let ordered = vec![b"aa".to_vec()];
        let err = save_tbl_from_ordered(&ordered, &tpl)
            .expect_err("short LIR string table must fail loud");
        let msg = format!("{err}");
        assert!(
            matches!(err, MapTailError::TemplateTbl(_)),
            "unexpected error: {msg}"
        );
        // The error must carry the MapInfo seeding hint.
        assert!(msg.contains("not seeded for this basemap"), "{msg}");
        assert!(msg.contains("MapInfo"), "{msg}");
        assert!(msg.contains("regenerate the HIR through eudc"), "{msg}");
    }

    /// Load the real blank-template scenario.chk through stormlib, the
    /// same way `assemble_map` does.
    fn real_template_chk() -> Chk {
        let path = concat!(
            env!("CARGO_MANIFEST_DIR"),
            "/../../tests/outputmap/basemap/basemap_wireframe.scx"
        );
        let mut ar = stormlib::Archive::open(
            std::path::Path::new(path),
            stormlib::OpenArchiveFlags::empty(),
        )
        .unwrap();
        let raw = ar.open_file("staredit\\scenario.chk").unwrap().read_all().unwrap();
        drop(ar);
        let mut chkt = Chk::new();
        assert!(chkt.loadchk(&raw));
        chkt
    }

    #[test]
    fn template_parse_real_basemap() {
        // Regression: TemplateStrings::parse on the real blank template
        // must reproduce the load state Python's load_tbl_with_chk
        // builds (verified against eudplib's LoadMap): 1024 ids, 7
        // unique strings, 1018 queued empty slots starting with
        // (slot 2 → "Force 1").
        let chkt = real_template_chk();
        let tpl = TemplateStrings::parse(&chkt).unwrap();
        assert_eq!(tpl.count(), 1024);
        assert_eq!(tpl.dataindextb.len(), 1024);
        assert_eq!(
            tpl.datatb,
            vec![
                b"Untitled Scenario".to_vec(),
                b"Destroy all enemy buildings.".to_vec(),
                b"".to_vec(),
                b"Force 1".to_vec(),
                b"Force 2".to_vec(),
                b"Force 3".to_vec(),
                b"Force 4".to_vec(),
            ]
        );
        assert_eq!(tpl.empty.len(), 1018);
        assert_eq!(tpl.empty[0].slot, 2);
        assert_eq!(tpl.empty[0].anchor, Some(b"Force 1".to_vec()));
        assert_eq!(tpl.empty[1017].slot, 1023);
        assert_eq!(tpl.empty[1017].anchor, None);
        assert_eq!(tpl.first_extended, b"Untitled Scenario".to_vec());
    }

    #[test]
    fn template_parse_strx_preferred() {
        // STRx present → load_entry=4; STR ignored.
        let mut chkt = synthetic_template();
        let mut strx = Vec::new();
        strx.extend_from_slice(&1u32.to_le_bytes());
        strx.extend_from_slice(&8u32.to_le_bytes());
        strx.extend_from_slice(b"Z\0\0\0");
        chkt.setsection(*b"STRx", strx);
        let tpl = TemplateStrings::parse(&chkt).unwrap();
        assert_eq!(tpl.count(), 1);
        assert_eq!(tpl.datatb, vec![b"Z".to_vec()]);
    }

    #[test]
    fn string_section_offsets_recycle_and_duplicate_append() {
        // Same ordered list as save_tbl_from_ordered_recycles_before_anchor:
        // "NEW1" recycles slot id 2 (before "bb"), "NEW2" recycles slot
        // id 5 (bare → before first_extended "aa"), "TAIL" appends.
        // Hand-computed from datatb [NEW2, aa, "", NEW1, bb, cc, TAIL]
        // and dataindextb [1, 3, 4, 5, 0, 2, 6] with a 32-byte header.
        let chkt = synthetic_template();
        let tpl = TemplateStrings::parse(&chkt).unwrap();
        let ordered = vec![
            b"aa".to_vec(),
            b"NEW1".to_vec(),
            b"bb".to_vec(),
            b"cc".to_vec(),
            b"NEW2".to_vec(),
            b"".to_vec(),
            b"TAIL".to_vec(),
        ];
        let offsets = string_section_offsets(&ordered, &tpl).unwrap();
        assert_eq!(offsets, vec![40, 48, 56, 60, 32, 44, 64]);
        // And identical to save_tbl's own stroffset table, parsed back
        // from the serialized bytes (the spec's identity requirement).
        let bytes = save_tbl_from_ordered(&ordered, &tpl).unwrap();
        let n = u32le(&bytes, 0) as usize;
        let from_bytes: Vec<u32> =
            (0..n).map(|i| u32le(&bytes, 4 + 4 * i)).collect();
        assert_eq!(offsets, from_bytes);

        // Duplicate-append case: appended "TAIL" twice proxies the
        // first entry (AddString dedup), so the second id shares the
        // first's offset and the table grows by one entry only.
        let ordered_dup = vec![
            b"aa".to_vec(),
            b"".to_vec(),
            b"bb".to_vec(),
            b"cc".to_vec(),
            b"".to_vec(),
            b"".to_vec(),
            b"TAIL".to_vec(),
            b"TAIL".to_vec(),
        ];
        assert_eq!(
            string_section_offsets(&ordered_dup, &tpl).unwrap(),
            vec![36, 40, 44, 48, 40, 40, 52, 52]
        );
    }

    #[test]
    fn built_table_from_ordered_matches_save_tbl_and_offsets() {
        // One build, one answer: the bytes equal the old
        // save_tbl_from_ordered serialization and the offsets equal
        // the old string_section_offsets computation, from the same
        // build — never from a second serialization.
        let chkt = synthetic_template();
        let tpl = TemplateStrings::parse(&chkt).unwrap();
        let ordered = vec![
            b"aa".to_vec(),
            b"NEW1".to_vec(),
            b"bb".to_vec(),
            b"cc".to_vec(),
            b"NEW2".to_vec(),
            b"".to_vec(),
            b"TAIL".to_vec(),
        ];
        let built = BuiltStringTable::from_ordered(&ordered, &tpl).unwrap();
        assert_eq!(
            built.bytes,
            save_tbl_from_ordered(&ordered, &tpl).unwrap()
        );
        assert_eq!(
            built.offsets.as_ref().unwrap(),
            &string_section_offsets(&ordered, &tpl).unwrap()
        );
    }

    #[test]
    fn built_table_fallback_carries_no_offsets() {
        // String-less LIR: re-serialize the template's own table as
        // STRx, and carry no offsets — no ordered list exists there,
        // so fallback offsets would be meaningless, not uncomputed.
        let chkt = synthetic_template();
        let tpl = TemplateStrings::parse(&chkt).unwrap();
        let built = BuiltStringTable::fallback(&tpl);
        assert_eq!(built.bytes, tpl.save_as_strx());
        assert!(built.offsets.is_none());
    }

    /// Three-string template for the divergence test: ids 1..3 =
    /// ["aa", "", "bb"], load_entry=2 (`STR `), no MRGN/SWNM names
    /// except what the test itself sets, no reserved ids.
    fn three_string_template() -> Chk {
        // Offsets (u16): strings start at roundup(2*(3+1)) = 8.
        // "aa"@8, ""@12, "bb"@14.
        let mut strsec = Vec::new();
        strsec.extend_from_slice(&3u16.to_le_bytes());
        for off in [8u16, 12, 14] {
            strsec.extend_from_slice(&off.to_le_bytes());
        }
        assert_eq!(strsec.len(), 8);
        strsec.extend_from_slice(b"aa\0\0"); // 8..12
        strsec.extend_from_slice(b"\0\0"); // 12..14
        strsec.extend_from_slice(b"bb\0\0"); // 14..18
        let mut chkt = Chk::new();
        chkt.setsection(*b"STR ", strsec);
        chkt.setsection(*b"UNIx", vec![0u8; UNIX_SECTION_LEN]);
        chkt.setsection(*b"SPRP", vec![0u8; 4]);
        chkt.setsection(*b"FORC", vec![0u8; 16]);
        chkt
    }

    #[test]
    fn template_parse_after_fix_map_data_diverges_and_verification_is_blind() {
        // Ordering pin for assemble_map (DISCRIMINATING — whole-program
        // tests would miss it): the TBL parse must run on the pristine
        // template, before `fix_map_data`. The fix rewrites UNIx, and
        // the parse reads UNIx reserved unit-name ids at
        // UNIX_NAME_OFFSET + 2i (228 flag bytes + first six setting
        // groups, 4+2+1+2+2+2=13 per entry: 228 * 14 = 3192) — the
        // fix's seventh setting
        // group writes exactly there, so a parse taken after the fix
        // sees a different load state. Fixture: id 3 ("bb") is the
        // LAST non-empty string, a UNIx reserved name, and also an
        // MRGN location name. Before the fix it is reserved (not
        // removed); after the fix its UNIx slot is zeroed, so it
        // becomes removed, and the recycle anchors change: slot id 2's
        // anchor goes from "bb" to the bare first-extended anchor
        // "aa", and "bb" itself is re-inserted before "aa" instead
        // of staying in place. The final table therefore differs —
        // while a verification recomputed from the WRONG parse agrees
        // with the wrong table, proving verification cannot catch
        // this reorder. The ordering is pinned by position (parse
        // before fix in assemble_map), not by verification.
        let mut chkt = three_string_template();
        // MRGN: one 20-byte location record naming string id 3 ("bb").
        let mut mrgn = vec![0u8; 20];
        mrgn[16..18].copy_from_slice(&3u16.to_le_bytes());
        chkt.setsection(*b"MRGN", mrgn);
        // UNIx: flag byte 0 set (fix_stats writes for unit 0) and
        // reserved name id 3 at UNIX_NAME_OFFSET (parse reads it; fix zeroes it).
        let mut unix = vec![0u8; UNIX_SECTION_LEN];
        unix[0] = 1;
        unix[UNIX_NAME_OFFSET..UNIX_NAME_OFFSET + 2].copy_from_slice(&3u16.to_le_bytes());
        chkt.setsection(*b"UNIx", unix);
        // Sections fix_map_data requires.
        chkt.setsection(*b"UNIT", vec![]);
        chkt.setsection(*b"UPGx", vec![0u8; 794]);
        chkt.setsection(*b"TECx", vec![0u8; 396]);
        chkt.setsection(*b"MTXM", vec![]);
        // Build recycles slot id 2 with "NEW1"; id 3 stays "bb".
        let ordered = vec![b"aa".to_vec(), b"NEW1".to_vec(), b"bb".to_vec()];
        let before = TemplateStrings::parse(&chkt).unwrap();
        let table_before =
            BuiltStringTable::from_ordered(&ordered, &before).unwrap();
        crate::fixmapdata::fix_map_data(&mut chkt).unwrap();
        // The fix actually ran and zeroed the reserved name slot.
        let unix_after = chkt.getsection(b"UNIx").unwrap();
        assert_eq!(u16le(unix_after, UNIX_NAME_OFFSET), 0);
        let after = TemplateStrings::parse(&chkt).unwrap();
        let table_after =
            BuiltStringTable::from_ordered(&ordered, &after).unwrap();
        // The reorder changes the emitted table: before, "NEW1" lands
        // before "bb"; after, both "NEW1" and "bb" land before "aa".
        assert_ne!(table_before.bytes, table_after.bytes);
        assert_ne!(table_before.offsets, table_after.offsets);
        // ...but verification recomputed from the wrong parse agrees
        // with the wrong table, so it would pass a wrong map.
        assert_eq!(
            string_section_offsets(&ordered, &after).unwrap(),
            table_after.offsets.unwrap()
        );
    }
}
