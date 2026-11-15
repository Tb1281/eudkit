//! CHK section geometry for the map tail: named constants, typed
//! views for the sections the tail interprets, and length validators.
//!
//! Two trigger encodings live in one output map and must NOT be
//! merged:
//!
//! - Classic triggers are 2400 bytes (16 conditions x 20 B +
//!   64 actions x 32 B + a 4-byte flag dword + a 28-byte player
//!   mask). The flag dword sits at 2368 = 320 + 2048. Classic
//!   triggers are used for TEMPLATE triggers in [`crate::payload_init`].
//! - LIR `RawTrigger`s are 2408 bytes (prev 0, next 4, conds
//!   8..328, acts 328..2376, flags 2376, players 2380, current
//!   action 2407). They are used for COMPILED payload items via
//!   `lir2payload`, appended after the STR section.
//!
//! Both layouts and their encoders now live in one module,
//! [`eudkit_lir::layout`] (issue #101); the classic constants and
//! [`ClassicTrigger`] are re-exported below. The two encodings are
//! still never transcoded into each other: applying 2368 to a
//! `RawTrigger`, or 2376 to a classic trigger, corrupts a different
//! field (`CLASSIC_FLAG_BYTE_OFFSET` is not `FLAGS_BYTE_OFFSET`).
//!
//! Similarly, the template STR section uses width 2 (`STR `, u16
//! offsets) while the emitted section uses width 4 (`STRx`, u32
//! offsets; `save_tbl` always writes width 4). The two widths are
//! distinct named constants below; merging them into one `STR_WIDTH`
//! would reintroduce the F43 mislabeling bug.
//!
//! `Chk` itself stays a dumb ordered store: validators here take a
//! `&Chk` (or a section slice) and return loud errors, rather than
//! becoming methods on `Chk`.

use crate::chk::Chk;
use crate::MapTailError;

/// Sum of the first `n` entries of a settings-group table, as a
/// `const fn` so derived offsets are compile-time constants.
const fn sum_prefix(groups: &[usize], n: usize) -> usize {
    let mut sum = 0usize;
    let mut i = 0usize;
    while i < n {
        sum += groups[i];
        i += 1;
    }
    sum
}

/// Sum of a whole settings-group table.
const fn sum_all(groups: &[usize]) -> usize {
    sum_prefix(groups, groups.len())
}

// ---------------------------------------------------------------------------
// MRGN (locations)
// ---------------------------------------------------------------------------

/// One MRGN location record: 4 x u32 rect, u16 string id, u16 elevation.
pub const MRGN_RECORD_SIZE: usize = 20;
/// Byte offset of the string id inside one MRGN record.
pub const MRGN_STRID_OFFSET: usize = 16;
/// Byte offset of the elevation inside one MRGN record.
pub const MRGN_ELEVATION_OFFSET: usize = 18;
/// MRGN obfuscation input length (`mrgndata`): 5100 bytes.
pub const MRGN_OBFUSCATION_LEN: usize = 5100;

/// One MRGN location record, borrowed from a 20-byte record slice.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct MrgnEntry<'a> {
    bytes: &'a [u8],
}

impl<'a> MrgnEntry<'a> {
    /// Borrow a record from `bytes` (the first 20 bytes); `None` when
    /// fewer than 20 bytes remain. Callers must validate the section
    /// length first (see [`checked_records`]) — [`entries`] silently
    /// floors a trailing partial record, exactly like the old code,
    /// so a length check is what makes a partial record loud.
    pub fn from_record(bytes: &'a [u8]) -> Option<Self> {
        if bytes.len() < MRGN_RECORD_SIZE {
            None
        } else {
            Some(MrgnEntry {
                bytes: &bytes[..MRGN_RECORD_SIZE],
            })
        }
    }

    /// Whole-record entries of a section, in order. Silently floors a
    /// trailing partial record; validate with [`checked_records`]
    /// first when a partial record must be loud.
    pub fn entries(section: &'a [u8]) -> impl Iterator<Item = MrgnEntry<'a>> {
        section
            .chunks_exact(MRGN_RECORD_SIZE)
            .map(|c| MrgnEntry { bytes: c })
    }

    /// Location rectangle: 4 x u32 at offsets 0, 4, 8, 12.
    pub fn rect(&self) -> [u32; 4] {
        let mut out = [0u32; 4];
        for (i, slot) in out.iter_mut().enumerate() {
            *slot = u32::from_le_bytes(
                self.bytes[i * 4..i * 4 + 4].try_into().unwrap(),
            );
        }
        out
    }

    /// String id of the location name, at +16.
    pub fn strid(&self) -> u16 {
        u16::from_le_bytes(
            self.bytes[MRGN_STRID_OFFSET..MRGN_STRID_OFFSET + 2]
                .try_into()
                .unwrap(),
        )
    }

    /// Elevation of the location, at +18. This is the HIGH half of the
    /// dword at +16..+20, whose LOW half is the string id — the
    /// obfuscation loop's special case keeps this half and zeroes
    /// the string-id half (see `payload_init`).
    pub fn elevation(&self) -> u16 {
        u16::from_le_bytes(
            self.bytes[MRGN_ELEVATION_OFFSET..MRGN_ELEVATION_OFFSET + 2]
                .try_into()
                .unwrap(),
        )
    }
}

// ---------------------------------------------------------------------------
// UNIT (unit settings; index-based, not a struct decode)
// ---------------------------------------------------------------------------

/// One UNIT record.
pub const UNIT_RECORD_SIZE: usize = 36;
/// Byte positions `fix_unit_map` manipulates. UNIT is deliberately
/// NOT decoded as a struct: its fixup logic is flag-bit surgery
/// (clearing flag bits for default values, then zeroing values whose
/// flag bits are clear), which a struct would obscure. These names
/// describe the byte positions, not a full UNIT field decode.
pub const UNIT_FLAG_A_OFFSET: usize = 12;
pub const UNIT_FLAGS_OFFSET: usize = 14;
pub const UNIT_HP_PERCENT_OFFSET: usize = 17;
pub const UNIT_SHIELD_PERCENT_OFFSET: usize = 18;
pub const UNIT_ENERGY_OFFSET: usize = 19;
pub const UNIT_VALUE32_OFFSET: usize = 20;
pub const UNIT_VALUE16_OFFSET: usize = 24;
pub const UNIT_BITMASK_OFFSET: usize = 26;

// ---------------------------------------------------------------------------
// UPRP / UPUS (unit properties)
// ---------------------------------------------------------------------------

/// One UPRP property slot (see `PropertyFields` in `fixmapdata`).
pub const UPRP_RECORD_SIZE: usize = 20;
/// Number of UPRP slots.
pub const UPRP_SLOT_COUNT: usize = 64;
/// Whole UPRP table size: 64 slots x 20 bytes.
pub const UPRP_TABLE_SIZE: usize = 1280;
/// Number of UPUS enable bytes (one per UPRP slot).
pub const UPUS_COUNT: usize = 64;

// ---------------------------------------------------------------------------
// UNIx / UPGx / TECx (stats and settings groups)
// ---------------------------------------------------------------------------

/// UNIx: 228 unit entries.
pub const UNIX_COUNT: usize = 228;
/// UNIx setting-group sizes, in section order. The unit-name string
/// ids are the LAST group (size 2 per entry), so the name block
/// starts after the flag bytes plus the first six groups.
pub const UNIX_SETTING_GROUPS: &[usize] = &[4, 2, 1, 2, 2, 2, 2];
/// Index of the unit-name group inside [`UNIX_SETTING_GROUPS`].
pub const UNIX_NAME_GROUP_INDEX: usize = 6;
/// Byte offset of the unit-name string-id block inside UNIx,
/// derived from the groups table: flag bytes (228) plus the first
/// six groups (4+2+1+2+2+2 = 13 per entry), i.e. 228 * 14 = 3192.
/// This must equal the old literal 3192 — see the derivation test.
/// A future groups-table edit that moves the name block fails that
/// test loudly instead of silently changing the reserved set.
pub const UNIX_NAME_OFFSET: usize =
    UNIX_COUNT * (1 + sum_prefix(UNIX_SETTING_GROUPS, UNIX_NAME_GROUP_INDEX));
/// Whole UNIx section length: flag bytes plus all groups.
pub const UNIX_SECTION_LEN: usize =
    UNIX_COUNT * (1 + sum_all(UNIX_SETTING_GROUPS));
/// Stride of one unit-name string id inside the name block.
pub const UNIX_NAME_STRIDE: usize = 2;

/// Unit-name string id for entry `i` (0-based), or `None` when the
/// section is too short to hold it.
pub fn unix_name_strid(section: &[u8], i: usize) -> Option<u16> {
    let off = UNIX_NAME_OFFSET + i * UNIX_NAME_STRIDE;
    let b = section.get(off..off + 2)?;
    Some(u16::from_le_bytes([b[0], b[1]]))
}

/// UPGx: 61 upgrade entries.
pub const UPGX_COUNT: usize = 61;
/// UPGx setting-group sizes, in section order.
pub const UPGX_SETTING_GROUPS: &[usize] = &[2, 2, 2, 2, 2, 2];
/// UPGx has one extra base byte in its need calculation.
pub const UPGX_BASE_EXTRA: usize = 1;
/// TECx: 44 tech entries.
pub const TECX_COUNT: usize = 44;
/// TECx setting-group sizes, in section order.
pub const TECX_SETTING_GROUPS: &[usize] = &[2, 2, 2, 2];

/// Minimum section length for a settings section: flag bytes
/// (`count`) plus one slot per setting-group entry, plus `extra`
/// base bytes (UPGx only). This is exactly the `need` calculation
/// `fix_stats_and_settings` already used, now named.
pub const fn settings_section_need(
    count: usize,
    groups: &[usize],
    extra: usize,
) -> usize {
    count * (1 + sum_all(groups)) + extra
}

pub const UNIX_SECTION_NEED: usize =
    settings_section_need(UNIX_COUNT, UNIX_SETTING_GROUPS, 0);
pub const UPGX_SECTION_NEED: usize =
    settings_section_need(UPGX_COUNT, UPGX_SETTING_GROUPS, UPGX_BASE_EXTRA);
pub const TECX_SECTION_NEED: usize =
    settings_section_need(TECX_COUNT, TECX_SETTING_GROUPS, 0);

// ---------------------------------------------------------------------------
// SWNM / SPRP / FORC (name sections)
// ---------------------------------------------------------------------------

/// SWNM: 256 switch slots, each a u32 string id.
pub const SWNM_COUNT: usize = 256;
pub const SWNM_STRID_SIZE: usize = 4;
pub const SWNM_SECTION_SIZE: usize = 1024;

/// SPRP sprite-name string ids: two u16 ids at offsets 0 and 2.
pub const SPRP_NAME_OFFSETS: [usize; 2] = [0, 2];
pub const SPRP_MIN_LEN: usize = 4;

/// FORC force-name string ids: four u16 ids at offsets 8, 10, 12, 14.
pub const FORC_NAME_OFFSETS: [usize; 4] = [8, 10, 12, 14];
/// FORC player-to-force map: the first 8 bytes, used by the inline
/// trigger planner.
pub const FORC_FORCE_MAP_LEN: usize = 8;

// ---------------------------------------------------------------------------
// STR widths, DIM, MTXM, VER
// ---------------------------------------------------------------------------

/// Template STR section entry width (`STR `, u16 offsets).
pub const STR_LOAD_WIDTH: usize = 2;
/// Emitted STR section entry width (`STRx`, u32 offsets).
/// `save_tbl` always writes width 4. DISTINCT from [`STR_LOAD_WIDTH`]
/// by design — see the module docs.
pub const STR_SAVE_WIDTH: usize = 4;

/// DIM section: two u16 tile dimensions.
pub const DIM_MIN_LEN: usize = 4;
pub const DIM_MIN_TILES: u16 = 1;
pub const DIM_MAX_TILES: u16 = 256;

/// MTXM tile size: one u16 tile id per tile.
pub const MTXM_TILE_SIZE: usize = 2;

/// VER section bytes written by the remastered-CHK mark.
pub const VER_BYTES: [u8; 2] = [0xCE, 0x00];

// ---------------------------------------------------------------------------
// Classic triggers (template triggers)
// ---------------------------------------------------------------------------

// The classic 2400-byte layout and its encoder live in `eudkit-lir::layout`
// (issue #101: one layout module for both record encodings). They are
// re-exported here so map code keeps its `crate::layout::CLASSIC_*` paths.
pub use eudkit_lir::{
    ClassicTrigger, CLASSIC_ACTS_BYTE_OFFSET, CLASSIC_ACT_COUNT, CLASSIC_ACT_FLAG_OFFSET,
    CLASSIC_ACT_SIZE, CLASSIC_ACT_TYPE_OFFSET, CLASSIC_COND_COUNT, CLASSIC_COND_SIZE,
    CLASSIC_COND_TYPE_OFFSET, CLASSIC_CONDS_BYTE_OFFSET, CLASSIC_FLAG_BYTE_OFFSET,
    CLASSIC_PLAYERS_BYTE_OFFSET, CLASSIC_PLAYERS_LEN, CLASSIC_TRIGGER_BYTE_SIZE,
};

// ---------------------------------------------------------------------------
// Length validators (owned by this module; Chk stays dumb)
// ---------------------------------------------------------------------------

/// A section that must exist and whose length must be a multiple of
/// `record_size`. Missing -> `MissingSection`, non-multiple ->
/// `TruncatedSection`. This is the validator for MRGN (20), UNIT
/// (36), TRIG (2400, via `MalformedTrig` in the inline planner —
/// kept there, not here) and MTXM (2, kept in `fixmapdata`).
pub fn checked_records<'a>(
    section: Option<&'a [u8]>,
    section_name: &'static str,
    record_size: usize,
) -> Result<&'a [u8], MapTailError> {
    let bytes = section.ok_or(MapTailError::MissingSection(section_name))?;
    if bytes.len() % record_size != 0 {
        return Err(MapTailError::TruncatedSection(section_name));
    }
    Ok(bytes)
}

/// A section that must exist and be at least `min_len` bytes.
/// Missing -> `MissingSection`, shorter -> `TruncatedSection`.
pub fn checked_min<'a>(
    section: Option<&'a [u8]>,
    section_name: &'static str,
    min_len: usize,
) -> Result<&'a [u8], MapTailError> {
    let bytes = section.ok_or(MapTailError::MissingSection(section_name))?;
    if bytes.len() < min_len {
        return Err(MapTailError::TruncatedSection(section_name));
    }
    Ok(bytes)
}

/// A section of a `Chk` that must exist and be a multiple of
/// `record_size` — the `Chk`-taking form of [`checked_records`].
pub fn section_checked<'a>(
    chkt: &'a Chk,
    name: &[u8; 4],
    section_name: &'static str,
    record_size: usize,
) -> Result<&'a [u8], MapTailError> {
    checked_records(chkt.getsection(name), section_name, record_size)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn unix_name_offset_derivation_equals_old_literal() {
        // IMPLEMENTATION CONDITION: the derivation must reproduce
        // the old literal 3192 exactly. 228 flag bytes + the first
        // six groups (4+2+1+2+2+2 = 13 per entry) = 228 * 14.
        assert_eq!(UNIX_NAME_OFFSET, 3192);
        assert_eq!(UNIX_SECTION_LEN, 3648);
        assert_eq!(UNIX_NAME_OFFSET + UNIX_COUNT * UNIX_NAME_STRIDE, 3648);
        assert_eq!(UNIX_SECTION_NEED, 3648);
        assert_eq!(UPGX_SECTION_NEED, 794);
        assert_eq!(TECX_SECTION_NEED, 396);
    }

    #[test]
    fn mrgn_entry_decodes_known_bytes() {
        // Rect 1,2,3,4; strid 0x1234 at +16; elevation 0x5678 at +18.
        let mut rec = [0u8; 20];
        for (i, v) in [1u32, 2, 3, 4].iter().enumerate() {
            rec[i * 4..i * 4 + 4].copy_from_slice(&v.to_le_bytes());
        }
        rec[16..18].copy_from_slice(&0x1234u16.to_le_bytes());
        rec[18..20].copy_from_slice(&0x5678u16.to_le_bytes());
        let e = MrgnEntry::from_record(&rec).unwrap();
        assert_eq!(e.rect(), [1, 2, 3, 4]);
        assert_eq!(e.strid(), 0x1234);
        assert_eq!(e.elevation(), 0x5678);
        assert!(MrgnEntry::from_record(&rec[..19]).is_none());
        // entries() floors a trailing partial record.
        let mut sec = rec.to_vec();
        sec.extend_from_slice(&[9u8; 10]);
        assert_eq!(MrgnEntry::entries(&sec).count(), 1);
    }

    #[test]
    fn section_validators_loud_on_bad_lengths() {
        let mut chkt = Chk::new();
        chkt.setsection(*b"MRGN", vec![0u8; 38]);
        assert!(matches!(
            section_checked(&chkt, b"MRGN", "MRGN", MRGN_RECORD_SIZE),
            Err(MapTailError::TruncatedSection("MRGN"))
        ));
        assert!(matches!(
            section_checked(&chkt, b"UNIT", "UNIT", UNIT_RECORD_SIZE),
            Err(MapTailError::MissingSection("UNIT"))
        ));
        let ok = section_checked(&chkt, b"MRGN", "MRGN", MRGN_RECORD_SIZE);
        assert!(ok.is_err());
        let mut chkt = Chk::new();
        chkt.setsection(*b"MRGN", vec![0u8; 40]);
        assert!(section_checked(&chkt, b"MRGN", "MRGN", MRGN_RECORD_SIZE).is_ok());
        assert!(matches!(
            checked_min(Some(&[0u8; 3]), "SWNM", SWNM_SECTION_SIZE),
            Err(MapTailError::TruncatedSection("SWNM"))
        ));
    }

    #[test]
    fn str_widths_and_table_sizes_are_distinct() {
        assert_ne!(STR_LOAD_WIDTH, STR_SAVE_WIDTH);
        assert_eq!(STR_LOAD_WIDTH, 2);
        assert_eq!(STR_SAVE_WIDTH, 4);
        assert_eq!(UPRP_SLOT_COUNT * UPRP_RECORD_SIZE, UPRP_TABLE_SIZE);
        assert_eq!(SWNM_COUNT * SWNM_STRID_SIZE, SWNM_SECTION_SIZE);
        assert_eq!(VER_BYTES, [0xCE, 0x00]);
        assert_eq!(MRGN_OBFUSCATION_LEN, 5100);
    }
}
