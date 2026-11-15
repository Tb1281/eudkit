//! Port of `src/eudkit/core/mapdata/fixmapdata.py` (`_fix_map_data`) and
//! the `update_map_data` half the native tail was missing
//! (`proptable.apply_property_map`; `apply_string_map`'s effect is already
//! carried by the `table` argument of `initialize_payload`).
//!
//! Python call order in both `SaveMap` and the pure `build()`:
//! `_preprocess_inline_code` → `update_map_data()` → `_fix_map_data(chkt)`
//! → injector tail. [`assemble_map`](crate::assemble_map) mirrors it:
//! [`apply_property_map`] then [`fix_map_data`], right before
//! `initialize_payload`.
//!
//! Unlike the TBL state, the property table needs no build-time capture:
//! the pure HIR pipeline never calls `GetPropertyIndex`, so the load-time
//! table rebuilt here from the template's `UPRP`/`UPUS` is exactly what
//! Python's `apply_property_map` writes.

use crate::chk::Chk;
use crate::layout::{
    settings_section_need, TECX_COUNT, TECX_SETTING_GROUPS,
    MTXM_TILE_SIZE, UNIX_COUNT, UNIX_SETTING_GROUPS,
    UNIT_BITMASK_OFFSET, UNIT_ENERGY_OFFSET, UNIT_FLAG_A_OFFSET,
    UNIT_FLAGS_OFFSET, UNIT_HP_PERCENT_OFFSET, UNIT_RECORD_SIZE,
    UNIT_SHIELD_PERCENT_OFFSET, UNIT_VALUE16_OFFSET, UNIT_VALUE32_OFFSET,
    UPGX_BASE_EXTRA, UPGX_COUNT, UPGX_SETTING_GROUPS,
    UPRP_RECORD_SIZE, UPRP_SLOT_COUNT, UPRP_TABLE_SIZE, UPUS_COUNT,
    VER_BYTES,
};
use crate::MapTailError;
use std::collections::HashMap;

/// `fixmapdata._fix_map_data`: UNIT/UNIx/UPGx/TECx fixups, `VER ` bump,
/// MTXM null-tile repair.
pub fn fix_map_data(chkt: &mut Chk) -> Result<(), MapTailError> {
    fix_unit_map(chkt)?;
    fix_stats_and_settings(chkt)?;
    apply_remastered_chk(chkt);
    fix_mtxm_0_0_null(chkt)?;
    Ok(())
}

fn section(chkt: &Chk, name: &[u8; 4]) -> Result<Vec<u8>, MapTailError> {
    chkt.getsection(name)
        .ok_or(MapTailError::MissingSection(section_static(name)))
        .map(<[u8]>::to_vec)
}

/// Map a 4-byte section name to the `&'static str` used in errors. Only
/// called for names this module handles.
fn section_static(name: &[u8; 4]) -> &'static str {
    match name {
        b"UNIT" => "UNIT",
        b"UNIx" => "UNIx",
        b"UPGx" => "UPGx",
        b"TECx" => "TECx",
        b"VER " => "VER ",
        b"MTXM" => "MTXM",
        b"DIM " => "DIM ",
        b"UPRP" => "UPRP",
        b"UPUS" => "UPUS",
        _ => "CHK",
    }
}

/// `fixmapdata._fix_unit_map`: per 36-byte UNIT record, clear the flag bits
/// of defaulted values and zero the values whose flag bits are clear.
fn fix_unit_map(chkt: &mut Chk) -> Result<(), MapTailError> {
    let mut unit = section(chkt, b"UNIT")?;
    if unit.len() % UNIT_RECORD_SIZE != 0 {
        return Err(MapTailError::TruncatedSection("UNIT"));
    }
    for rec in unit.chunks_exact_mut(UNIT_RECORD_SIZE) {
        // Disable flags for default value.
        if rec[UNIT_HP_PERCENT_OFFSET] == 100 {
            rec[UNIT_FLAGS_OFFSET] &= !2;
        }
        if rec[UNIT_SHIELD_PERCENT_OFFSET] == 100 {
            rec[UNIT_FLAGS_OFFSET] &= !4;
        }
        if rec[UNIT_VALUE16_OFFSET] == 0 && rec[UNIT_VALUE16_OFFSET + 1] == 0 {
            rec[UNIT_FLAGS_OFFSET] &= !32;
        }
        for k in 0..5 {
            if rec[UNIT_BITMASK_OFFSET] & (1 << k) == 0 {
                rec[UNIT_FLAG_A_OFFSET] &= !(1 << k);
            }
        }

        // Remove disabled values.
        if rec[UNIT_FLAGS_OFFSET] & 2 == 0 {
            rec[UNIT_HP_PERCENT_OFFSET] = 0;
        }
        if rec[UNIT_FLAGS_OFFSET] & 4 == 0 {
            rec[UNIT_SHIELD_PERCENT_OFFSET] = 0;
        }
        if rec[UNIT_FLAGS_OFFSET] & 8 == 0 {
            rec[UNIT_ENERGY_OFFSET] = 0;
        }
        if rec[UNIT_FLAGS_OFFSET] & 16 == 0 {
            rec[UNIT_VALUE32_OFFSET..UNIT_VALUE32_OFFSET + 4].fill(0);
        }
        if rec[UNIT_FLAGS_OFFSET] & 32 == 0 {
            rec[UNIT_VALUE16_OFFSET..UNIT_VALUE16_OFFSET + 2].fill(0);
        }
        for k in 0..5 {
            if rec[UNIT_FLAG_A_OFFSET] & (1 << k) == 0 {
                rec[UNIT_BITMASK_OFFSET] &= !(1 << k);
            }
        }
    }
    chkt.setsection(*b"UNIT", unit);
    Ok(())
}

/// `fixmapdata._fix_stats_and_settings`: for each flagged unit/upgrade/
/// tech entry, zero its per-setting value slots.
fn fix_stats_and_settings(chkt: &mut Chk) -> Result<(), MapTailError> {
    const SECTIONS: [(&[u8; 4], usize, &[usize], usize); 3] = [
        (b"UNIx", UNIX_COUNT, UNIX_SETTING_GROUPS, 0),
        (b"UPGx", UPGX_COUNT, UPGX_SETTING_GROUPS, UPGX_BASE_EXTRA),
        (b"TECx", TECX_COUNT, TECX_SETTING_GROUPS, 0),
    ];
    for (name, count, settings, extra) in SECTIONS {
        let mut data = section(chkt, name)?;
        // Largest index written is `count * (1 + sum(settings)) - 1`
        // (`+1` base offset for UPGx).
        let need = settings_section_need(count, settings, extra);
        if data.len() < need {
            return Err(MapTailError::TruncatedSection(section_static(name)));
        }
        for i in 0..count {
            if data[i] == 0 {
                continue;
            }
            let mut offset = count;
            if name == b"UPGx" {
                offset += 1;
            }
            for &setting in settings {
                for k in 0..setting {
                    data[offset + setting * i + k] = 0;
                }
                offset += setting * count;
            }
        }
        chkt.setsection(*name, data);
    }
    Ok(())
}

/// `fixmapdata._apply_remastered_chk`: mark the map remastered.
fn apply_remastered_chk(chkt: &mut Chk) {
    chkt.setsection(*b"VER ", VER_BYTES.to_vec());
}

/// `fixmapdata._fix_mtxm_0_0_null`: replace 0000.00 null tiles with
/// 0000.01 (they cause desyncs), warning like `ep_warn` does.
fn fix_mtxm_0_0_null(chkt: &mut Chk) -> Result<(), MapTailError> {
    let mut mtxm = section(chkt, b"MTXM")?;
    if mtxm.len() % MTXM_TILE_SIZE != 0 {
        return Err(MapTailError::TruncatedSection("MTXM"));
    }
    let mut null_tiles = Vec::new();
    for (idx, tile) in mtxm.chunks_exact_mut(MTXM_TILE_SIZE).enumerate() {
        if tile == [0, 0] {
            tile.copy_from_slice(&[1, 0]);
            null_tiles.push(idx);
        }
    }
    if !null_tiles.is_empty() {
        let dim = section(chkt, b"DIM ")?;
        if dim.len() < 2 {
            return Err(MapTailError::TruncatedSection("DIM "));
        }
        let width = u16::from_le_bytes([dim[0], dim[1]]) as usize;
        // Python prints divmod(t, width)[::-1], i.e. (x, y) tuples.
        let at = null_tiles
            .iter()
            .map(|t| format!("({}, {})", t % width, t / width))
            .collect::<Vec<_>>()
            .join(", ");
        println!("Null tiles at: {at}");
        eprintln!(
            "[Warning] Input map has 0000.00 null tiles\n\
             Replaced them to 0000.01, because they cause desync."
        );
    }
    chkt.setsection(*b"MTXM", mtxm);
    Ok(())
}

// ---------------------------------------------------------------------------
// update_map_data half: proptable.apply_property_map
// ---------------------------------------------------------------------------

/// One decoded `<HHBBBBIHHI` unit-property slot (20 bytes):
/// x0 = state-valid flags, x1 = value-valid flags, x2 = player,
/// x3 = HP%, x4 = shield%, x5 = energy, x6 = resource amount,
/// x7 = hanger amount, x8 = state flags, x9 = padding.
struct PropertyFields {
    x0: u16,
    x1: u16,
    x2: u8,
    x3: u8,
    x4: u8,
    x5: u8,
    x6: u32,
    x7: u16,
    x8: u16,
    x9: u32,
}

impl PropertyFields {
    fn decode(slot: &[u8]) -> Self {
        PropertyFields {
            x0: u16::from_le_bytes([slot[0], slot[1]]),
            x1: u16::from_le_bytes([slot[2], slot[3]]),
            x2: slot[4],
            x3: slot[5],
            x4: slot[6],
            x5: slot[7],
            x6: u32::from_le_bytes([slot[8], slot[9], slot[10], slot[11]]),
            x7: u16::from_le_bytes([slot[12], slot[13]]),
            x8: u16::from_le_bytes([slot[14], slot[15]]),
            x9: u32::from_le_bytes([slot[16], slot[17], slot[18], slot[19]]),
        }
    }

    /// `proptable.property_key` normalization.
    fn normalize(&mut self) {
        self.x0 &= 0b11111; // remove unused flags
        self.x1 &= 0b111111; // remove unused flags
        self.x8 &= 0b11111; // remove unused flags

        self.x2 = 0; // player number, always NULL
        self.x9 = 0; // padding?

        if self.x3 == 100 {
            // 100% HP is equals to not using HP%
            self.x1 &= !0b10;
        }
        if self.x4 == 100 {
            // 100% Shields is equals to not using Shields%
            self.x1 &= !0b100;
        }
        // Can't apply to Energy: default energy differs between max
        // energy (200 and 250). Can't apply to Resource amount: default
        // amount differs between Mineral Fields and Vespene Geyser.
        if self.x7 == 0 {
            // 0 in hanger is equals to not using hanger amount
            self.x1 &= !0b100000;
        }

        if self.x1 & 0b10 == 0 {
            self.x3 = 0; // HP is not valid
        }
        if self.x1 & 0b100 == 0 {
            self.x4 = 0; // Shields is not valid
        }
        if self.x1 & 0b1000 == 0 {
            self.x5 = 0; // Energy is not valid
        }
        if self.x1 & 0b10000 == 0 {
            self.x6 = 0; // Resource amount is not valid
        }
        if self.x1 & 0b100000 == 0 {
            self.x7 = 0; // Amount in hanger is not valid
        }

        if self.x8 & 0b1 == 0 {
            self.x0 &= !0b1; // No Cloak is equals to not using Cloak
        }
        if self.x8 & 0b10 == 0 {
            self.x0 &= !0b10; // No Burrowed is equals to not using Burrowed
        }
        if self.x8 & 0b100 == 0 {
            self.x0 &= !0b100; // No Lifted is equals to not using Lifted
        }
        if self.x8 & 0b1000 == 0 {
            self.x0 &= !0b1000; // No Hallucinated is equals to not using Hallucinated
        }
        if self.x8 & 0b10000 == 0 {
            self.x0 &= !0b10000; // No Invincible is equals to not using Invincible
        }

        if self.x0 & 0b1 == 0 {
            self.x8 &= !0b1; // Cloak is not valid
        }
        if self.x0 & 0b10 == 0 {
            self.x8 &= !0b10; // Burrowed is not valid
        }
        if self.x0 & 0b100 == 0 {
            self.x8 &= !0b100; // In transit is not valid
        }
        if self.x0 & 0b1000 == 0 {
            self.x8 &= !0b1000; // Hallucinated is not valid
        }
        if self.x0 & 0b10000 == 0 {
            self.x8 &= !0b10000; // Invincible is not valid
        }
    }

    fn encode(&self) -> [u8; 20] {
        let mut out = [0u8; 20];
        out[0..2].copy_from_slice(&self.x0.to_le_bytes());
        out[2..4].copy_from_slice(&self.x1.to_le_bytes());
        out[4] = self.x2;
        out[5] = self.x3;
        out[6] = self.x4;
        out[7] = self.x5;
        out[8..12].copy_from_slice(&self.x6.to_le_bytes());
        out[12..14].copy_from_slice(&self.x7.to_le_bytes());
        out[14..16].copy_from_slice(&self.x8.to_le_bytes());
        out[16..20].copy_from_slice(&self.x9.to_le_bytes());
        out
    }
}

/// `proptable.property_key`: normalize one 20-byte UPRP slot.
pub(crate) fn property_key(slot: &[u8]) -> [u8; 20] {
    let mut f = PropertyFields::decode(slot);
    f.normalize();
    f.encode()
}

/// The authored UPRP table in normalized form: every UPUS-enabled slot
/// carries its [`property_key`] record, and disabled slots are empty.
///
/// `by_record` resolves a record to its last enabled slot, matching the
/// dictionary-overwrite order of eudplib's `init_property_map`. Module
/// allocation extends the same table, so both map-assembly paths agree on
/// what "the same property" means and which slot wins.
pub(crate) struct PropertyTable {
    slots: Vec<Option<[u8; UPRP_RECORD_SIZE]>>,
    by_record: HashMap<[u8; UPRP_RECORD_SIZE], usize>,
}

impl PropertyTable {
    /// Load `UPRP`/`UPUS`, normalizing each enabled row. Callers must
    /// validate the section lengths first.
    pub(crate) fn parse(uprp: &[u8], upus: &[u8]) -> Self {
        let mut slots: Vec<Option<[u8; UPRP_RECORD_SIZE]>> = vec![None; UPRP_SLOT_COUNT];
        let mut by_record = HashMap::new();
        for (i, slot) in slots.iter_mut().enumerate() {
            if upus[i] == 0 {
                continue;
            }
            let start = UPRP_RECORD_SIZE * i;
            let record = property_key(&uprp[start..start + UPRP_RECORD_SIZE]);
            *slot = Some(record);
            // eudplib's dict overwrites, so the last duplicate wins.
            by_record.insert(record, i);
        }
        PropertyTable { slots, by_record }
    }

    /// The last enabled slot holding `record`.
    pub(crate) fn slot_of(&self, record: &[u8; UPRP_RECORD_SIZE]) -> Option<usize> {
        self.by_record.get(record).copied()
    }

    /// Put `record` in the lowest free slot and return it, or `None` when
    /// all 64 slots are occupied.
    pub(crate) fn insert(&mut self, record: [u8; UPRP_RECORD_SIZE]) -> Option<usize> {
        let slot = self.slots.iter().position(Option::is_none)?;
        self.slots[slot] = Some(record);
        self.by_record.insert(record, slot);
        Some(slot)
    }

    /// Encode the UPRP table: normalized records at enabled slots, 20 zero
    /// bytes at empty ones.
    pub(crate) fn to_bytes(&self) -> Vec<u8> {
        let mut out = Vec::with_capacity(UPRP_TABLE_SIZE);
        for slot in &self.slots {
            match slot {
                Some(record) => out.extend_from_slice(record),
                None => out.extend_from_slice(&[0u8; UPRP_RECORD_SIZE]),
            }
        }
        out
    }
}

/// `proptable.apply_property_map` (with `init_property_map` folded in):
/// rebuild the 64-slot UPRP table from the template's `UPRP`/`UPUS`
/// sections. Ports `init_property_map`'s load (slot kept iff its UPUS
/// byte is set; missing UPUS means all 64 slots kept) and
/// `apply_property_map`'s write-back (empty slots → 20 zero bytes).
pub fn apply_property_map(chkt: &mut Chk) -> Result<(), MapTailError> {
    let uprp = section(chkt, b"UPRP")?;
    if uprp.len() < UPRP_TABLE_SIZE {
        return Err(MapTailError::TruncatedSection("UPRP"));
    }
    // init_property_map: `except KeyError: upus = bytes([1] * 64)`.
    let upus = chkt
        .getsection(b"UPUS")
        .map(<[u8]>::to_vec)
        .unwrap_or_else(|| vec![1u8; UPRP_SLOT_COUNT]);
    if upus.len() < UPUS_COUNT {
        return Err(MapTailError::TruncatedSection("UPUS"));
    }
    chkt.setsection(*b"UPRP", PropertyTable::parse(&uprp, &upus).to_bytes());
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn chk_with(sections: &[(&[u8; 4], Vec<u8>)]) -> Chk {
        let mut chkt = Chk::new();
        for (name, data) in sections {
            chkt.setsection(**name, data.clone());
        }
        chkt
    }

    #[test]
    fn unit_map_clears_default_flags_and_values() {
        // One 36-byte record: HP%=100 (bit 2), shield%=50 (bit 4 kept),
        // energy flag clear (bit 8) with nonzero energy value.
        let mut rec = [0u8; 36];
        rec[12] = 0b11111;
        rec[14] = 0b110111; // energy flag (bit 8) clear
        rec[17] = 100; // HP%
        rec[18] = 50; // shield%
        rec[19] = 33; // energy
        rec[26] = 0b11111;
        let mut chkt = chk_with(&[(b"UNIT", rec.to_vec())]);
        fix_unit_map(&mut chkt).unwrap();
        let out = chkt.getsection(b"UNIT").unwrap();
        // HP 100% -> flag bit 2 cleared, then value zeroed.
        assert_eq!(out[14] & 2, 0);
        assert_eq!(out[17], 0);
        // Shield 50% -> flag bit 4 kept, value kept.
        assert_ne!(out[14] & 4, 0);
        assert_eq!(out[18], 50);
        // Energy flag (bit 8) was clear -> value zeroed.
        assert_eq!(out[19], 0);
    }

    #[test]
    fn unit_map_truncated_record_is_loud() {
        let chkt = &mut chk_with(&[(b"UNIT", vec![0u8; 35])]);
        assert!(matches!(
            fix_unit_map(chkt),
            Err(MapTailError::TruncatedSection("UNIT"))
        ));
    }

    #[test]
    fn property_map_short_uprp_is_loud() {
        let mut chkt = chk_with(&[(b"UPRP", vec![0u8; 1279])]);
        assert!(matches!(
            apply_property_map(&mut chkt),
            Err(MapTailError::TruncatedSection("UPRP"))
        ));
    }

    #[test]
    fn stats_short_unix_is_loud() {
        let mut chkt = chk_with(&[
            (b"UNIx", vec![0u8; 3647]),
            (b"UPGx", vec![0u8; 794]),
            (b"TECx", vec![0u8; 396]),
        ]);
        assert!(matches!(
            fix_stats_and_settings(&mut chkt),
            Err(MapTailError::TruncatedSection("UNIx"))
        ));
    }

    #[test]
    fn stats_zero_flagged_settings() {
        // TECx: 44 entries; flag entry 3, expect its 4 setting slots
        // (each 2 bytes at 44 + s*44 .. ) zeroed.
        let mut tecx = vec![0xAAu8; 396];
        tecx[..44].fill(0); // flag bytes: only entry 3 flagged
        tecx[3] = 1;
        let mut chkt = chk_with(&[
            (b"UNIx", vec![0u8; 3648]),
            (b"UPGx", vec![0u8; 794]),
            (b"TECx", tecx),
        ]);
        fix_stats_and_settings(&mut chkt).unwrap();
        let out = chkt.getsection(b"TECx").unwrap();
        // settings (2,2,2,2): offsets 44, 132, 220, 308 + 2*3.
        for off in [44usize, 132, 220, 308] {
            assert_eq!(&out[off + 6..off + 8], &[0, 0]);
        }
        // Untouched neighbor entry 4's slots keep 0xAA.
        for off in [44usize, 132, 220, 308] {
            assert_eq!(&out[off + 8..off + 10], &[0xAA, 0xAA]);
        }
        // UNIx/UPGx missing -> loud.
        let mut chkt2 = chk_with(&[(b"TECx", vec![0u8; 396])]);
        assert!(matches!(
            fix_stats_and_settings(&mut chkt2),
            Err(MapTailError::MissingSection("UNIx"))
        ));
    }

    #[test]
    fn remastered_ver_bump() {
        let mut chkt = chk_with(&[(b"VER ", vec![0x3D, 0x00])]);
        apply_remastered_chk(&mut chkt);
        assert_eq!(chkt.getsection(b"VER "), Some([0xCE, 0x00].as_slice()));
    }

    #[test]
    fn mtxm_null_tiles_replaced() {
        // 2x2 map: tiles [0000, 1234, 0000, 5678].
        let mtxm = vec![0, 0, 0x34, 0x12, 0, 0, 0x78, 0x56];
        let dim = vec![2, 0, 2, 0];
        let mut chkt = chk_with(&[(b"MTXM", mtxm), (b"DIM ", dim)]);
        fix_mtxm_0_0_null(&mut chkt).unwrap();
        assert_eq!(
            chkt.getsection(b"MTXM").unwrap(),
            [1, 0, 0x34, 0x12, 1, 0, 0x78, 0x56].as_slice()
        );
    }

    #[test]
    fn property_key_normalizes() {
        // Slot: all value-valid flags, HP%=100, player=7, padding junk.
        let mut slot = [0u8; 20];
        slot[0..2].copy_from_slice(&0xFFFFu16.to_le_bytes());
        slot[2..4].copy_from_slice(&0xFFFFu16.to_le_bytes());
        slot[4] = 7; // player -> 0
        slot[5] = 100; // HP% -> flag cleared, then value zeroed
        slot[12..14].copy_from_slice(&5u16.to_le_bytes()); // hanger != 0
        slot[14..16].copy_from_slice(&0xFFFFu16.to_le_bytes());
        slot[16..20].copy_from_slice(&0xFFFFFFFFu32.to_le_bytes()); // padding -> 0
        let out = property_key(&slot);
        assert_eq!(u16::from_le_bytes([out[0], out[1]]), 0b11111);
        assert_eq!(out[4], 0);
        // x1: 0b111111 minus HP% bit.
        assert_eq!(
            u16::from_le_bytes([out[2], out[3]]),
            0b111111 & !0b10
        );
        // HP value zeroed (flag clear), x8 masked then x0-masked.
        assert_eq!(out[5], 0);
        assert_eq!(u16::from_le_bytes([out[14], out[15]]), 0b11111 & 0b11111);
        assert_eq!(u32::from_le_bytes([out[16], out[17], out[18], out[19]]), 0);
    }

    #[test]
    fn property_key_matches_python_vectors() {
        // Differential vectors from eudplib's property_key (seed 1234).
        let vectors = [
            (
                "f7c7701d0117e9f1f0ce9508abb11519c45a3c04",
                "1500300000000000f0ce9508abb1150000000000",
            ),
            (
                "07fcc90458a59f7b9d7626172eb61dd803807ce1",
                "050009000000007b000000000000050000000000",
            ),
            (
                "f73f10aa8ae9771199ac1683940b45d38e10a54d",
                "050010000000000099ac16830000050000000000",
            ),
            (
                "7bd95a443fa7ee3374e087aa04f18212015524b5",
                "02001a0000a7003374e087aa0000020000000000",
            ),
            (
                "79a8602a0d13967f29138e372406ef58c51cf487",
                "0900200000000000000000002406090000000000",
            ),
            (
                "24ab4e740a6a46a9ce0a2d45274cf4d827c7d576",
                "04000e00006a46a9000000000000040000000000",
            ),
            (
                "0a9062c3597e5795d177790bc8ee78ded198e12b",
                "08002200007e000000000000c8ee080000000000",
            ),
            (
                "70bbc784d0704b461fc72f6a6fc41ef0871ad4d7",
                "1000070000704b00000000000000100000000000",
            ),
        ];
        fn unhex(s: &str) -> Vec<u8> {
            (0..s.len())
                .step_by(2)
                .map(|i| u8::from_str_radix(&s[i..i + 2], 16).unwrap())
                .collect()
        }
        for (input, want) in vectors {
            assert_eq!(property_key(&unhex(input))[..], unhex(want)[..]);
        }
    }

    #[test]
    fn apply_property_map_rebuilds_uprp() {
        // 64 slots; UPUS enables slot 0 and 63 only.
        let mut uprp = vec![0xCCu8; UPRP_TABLE_SIZE];
        // Slot 0: HP%=100 with all-valid flags -> normalized.
        uprp[2..4].copy_from_slice(&0x3Fu16.to_le_bytes());
        uprp[5] = 100;
        let mut upus = vec![0u8; 64];
        upus[0] = 1;
        upus[63] = 2;
        let mut chkt = chk_with(&[(b"UPRP", uprp), (b"UPUS", upus)]);
        apply_property_map(&mut chkt).unwrap();
        let out = chkt.getsection(b"UPRP").unwrap();
        assert_eq!(out.len(), UPRP_TABLE_SIZE);
        // Slot 0 normalized: x1 lost bit 2, HP value zeroed.
        assert_eq!(u16::from_le_bytes([out[2], out[3]]), 0x3F & !0b10);
        assert_eq!(out[5], 0);
        // Slot 1 (disabled) -> zeros.
        assert_eq!(&out[UPRP_RECORD_SIZE..2 * UPRP_RECORD_SIZE], &[0u8; UPRP_RECORD_SIZE]);
        // Slot 63 (enabled, was 0xCC fill) -> property_key(0xCC*20).
        let want63 = property_key(&[0xCCu8; UPRP_RECORD_SIZE]);
        assert_eq!(&out[63 * UPRP_RECORD_SIZE..64 * UPRP_RECORD_SIZE], &want63);
    }

    #[test]
    fn apply_property_map_normalizes_duplicate_authored_rows_in_place() {
        // Slots 2 and 5 hold the same property payload but different
        // player/padding noise, so both normalize to one record. The
        // fixed-map-data write-back keeps slot positions: it must
        // normalize both rows without collapsing them into one slot.
        let mut slot = [0u8; 20];
        slot[2..4].copy_from_slice(&0b10u16.to_le_bytes()); // HP valid
        slot[4] = 7; // player, normalized away
        slot[5] = 50;
        slot[16] = 0xAA; // padding, normalized away
        let mut duplicate = slot;
        duplicate[4] = 9;
        duplicate[16] = 0x55;

        let mut uprp = vec![0u8; UPRP_TABLE_SIZE];
        uprp[2 * UPRP_RECORD_SIZE..3 * UPRP_RECORD_SIZE].copy_from_slice(&slot);
        uprp[5 * UPRP_RECORD_SIZE..6 * UPRP_RECORD_SIZE].copy_from_slice(&duplicate);
        let mut upus = vec![0u8; 64];
        upus[2] = 1;
        upus[5] = 1;

        let mut chkt = chk_with(&[(b"UPRP", uprp), (b"UPUS", upus)]);
        apply_property_map(&mut chkt).unwrap();
        let out = chkt.getsection(b"UPRP").unwrap();
        let want = property_key(&slot);
        assert_eq!(&out[2 * UPRP_RECORD_SIZE..3 * UPRP_RECORD_SIZE], &want);
        assert_eq!(&out[5 * UPRP_RECORD_SIZE..6 * UPRP_RECORD_SIZE], &want);
        // The normalized rows lost the player and padding noise.
        assert_eq!(out[2 * UPRP_RECORD_SIZE + 4], 0);
        assert_eq!(out[5 * UPRP_RECORD_SIZE + 4], 0);
        // Disabled slot 3 is zeroed.
        assert_eq!(
            &out[3 * UPRP_RECORD_SIZE..4 * UPRP_RECORD_SIZE],
            &[0u8; UPRP_RECORD_SIZE]
        );
    }

    #[test]
    fn apply_property_map_missing_upus_keeps_all() {
        let uprp = vec![0x11u8; UPRP_TABLE_SIZE];
        let mut chkt = chk_with(&[(b"UPRP", uprp)]);
        apply_property_map(&mut chkt).unwrap();
        let out = chkt.getsection(b"UPRP").unwrap();
        let want = property_key(&[0x11u8; 20]);
        assert_eq!(&out[0..20], &want);
        assert_eq!(&out[1260..1280], &want);
    }
}
