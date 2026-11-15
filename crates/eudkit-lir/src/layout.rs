//! The single home for eudkit's binary trigger-record layouts and encoders.
//!
//! Two trigger encodings exist in one output map and must NOT be merged:
//!
//! - LIR `RawTrigger`s are `2,408` bytes (`prev` 0, `next` 4, conds
//!   `8..328`, acts `328..2376`, flags 2376, players 2380, current
//!   action 2407). [`crate::lir2payload`] emits them; [`RawTrigger`]
//!   owns their encoder.
//! - Classic template triggers are `2,400` bytes (conds `0..320`, acts
//!   `320..2368`, flag dword at 2368, 28-byte player mask at 2372).
//!   The map tail's TRIG rebuild emits them; [`ClassicTrigger`] owns
//!   their encoder.
//!
//! The *record* layouts are shared: one `CTrigCondition` is 20 bytes and
//! one `CTrigAction` is 32 bytes in both encodings, with identical field
//! offsets ([`CTrigCondition::encode`] / [`CTrigAction::encode`]). Only
//! the surrounding trigger header differs, so the classic encoder places
//! the same typed records at different slots. Applying 2368 to a
//! `RawTrigger`, or 2376 to a classic trigger, corrupts a different
//! field; the two constants stay distinct and pinned by tests.
//!
//! Relocations target a **4-byte window inside one record**, addressed by
//! a byte offset. The typed records make that byte-accurate
//! ([`CTrigCondition::write_field_u32`] /
//! [`CTrigAction::add_field_u32`]) instead of indexing a `[i64; N]`
//! parameter array: every offset whose window fits
//! (`field_offset + 4 <= record size`) is accepted, including
//! sub-dword offsets, and an out-of-record window is the loud
//! [`LirError::RelocFieldOutOfBounds`]. A sub-dword window writes bytes
//! `field_offset..field_offset + 4` exactly as asked; it never lands on
//! a different dword.

use serde::{Deserialize, Serialize};

use crate::{ActData, CondData, LirError};

/// StarCraft death-table base address used by `EPD(addr) = (addr - 0x58A364) / 4`.
pub const EPD_BASE: u32 = 0x0058_A364;
pub const CURRENT_PLAYER: u32 = 13;
pub const EPD_CURRENT_PLAYER: u32 = 203155;

/// EUDX `"SC"` magic stored at `+18` of `CTrigCondition` and `+30` of `CTrigAction`.
pub const EUDX_MAGIC: u16 = 0x4353;

/// Number of conditions in a StarCraft `RawTrigger`.
pub const COND_COUNT: usize = 16;
/// Number of actions in a StarCraft `RawTrigger`.
pub const ACT_COUNT: usize = 64;

/// Byte size of one `CTrigCondition` (`5` EPDs = `20` bytes).
pub const COND_BYTE_SIZE: usize = 20;
/// EPD stride of one `CTrigCondition` (`20 / 4 = 5`).
pub const COND_EPD_SIZE: usize = 5;

/// Byte size of one `CTrigAction` (`8` EPDs = `32` bytes).
pub const ACT_BYTE_SIZE: usize = 32;
/// EPD stride of one `CTrigAction` (`32 / 4 = 8`).
pub const ACT_EPD_SIZE: usize = 8;

/// Byte size of one `RawTrigger (16C, 64A)` (`602` EPDs = `2,408` bytes).
pub const RAW_TRIGGER_BYTE_SIZE: usize = 2408;
/// EPD size of one `RawTrigger (16C, 64A)` (`2408 / 4 = 602`).
pub const RAW_TRIGGER_EPD_SIZE: usize = 602;

/// Sentinel `next` pointer (`0x8000_0000`) terminating a StarCraft trigger
/// chain. Shared by lowering's control-flow pass and map assembly's
/// once-per-tick gate.
pub const CHAIN_END_PTR: i64 = -0x8000_0000;

/// Byte offset of `prev` pointer inside `RawTrigger`.
pub const PREV_BYTE_OFFSET: usize = 0;
/// Byte offset of `next` pointer inside `RawTrigger`.
pub const NEXT_BYTE_OFFSET: usize = 4;
/// Byte offset of `cond[0..16]` array inside `RawTrigger` (`+8..+328`).
pub const CONDS_BYTE_OFFSET: usize = 8;
/// Byte offset of `act[0..64]` array inside `RawTrigger` (`+328..+2376`).
pub const ACTS_BYTE_OFFSET: usize = 328;
/// Byte offset of the `dest` field inside one `CTrigAction` record.
/// A transfer setup act patches this field to point at the page slot
/// whose value is written when the page fires.
pub const DEST_FIELD_OFFSET: usize = 16;
/// Byte offset of the `mask` field inside one `CTrigAction` record.
/// A transfer setup act patches this field to restrict which bits of
/// the page slot's value are written.
pub const MASK_FIELD_OFFSET: usize = 0;
/// Byte offset of the dword containing the `modifier` byte inside one
/// `CTrigAction` record. The dword at this offset holds `unit` (+24),
/// `acttype` (+26), and `modifier` (+27); a transfer setup act relocates
/// the whole dword, so this constant is the dword offset (24), not the
/// modifier byte offset (27).
pub const MODIFIER_DWORD_OFFSET: usize = 24;
/// Byte offset of the `value` field inside one `CTrigAction` record.
/// A variable relocation whose target offset is this value accesses the
/// variable's plain value; any other offset accesses a different field.
pub const VALUE_FIELD_OFFSET: usize = 20;
/// Byte offset of the `player` field inside one `CTrigCondition` record.
/// A paged-variable condition names its storage through a `player` reloc at
/// this offset; every other cond kind names an absolute location and reads no
/// variable.
pub const COND_PLAYER_FIELD_OFFSET: usize = 4;
/// Byte offset of `execution_flags` (`u32`) inside `RawTrigger` (`+2376..+2380`).
pub const FLAGS_BYTE_OFFSET: usize = 2376;
/// Byte offset of `players[0..27]` inside `RawTrigger` (`+2380..+2407`).
pub const PLAYERS_BYTE_OFFSET: usize = 2380;
/// Byte offset of `current_action` (`u8`) inside `RawTrigger` (`+2407`).
pub const CURRENT_ACTION_BYTE_OFFSET: usize = 2407;

/// Byte offset of field `field_offset` of act `k` inside `RawTrigger`.
///
/// This is the single production spelling of the `328 + 32*k + field`
/// formula: act `k` starts at `ACTS_BYTE_OFFSET + k * ACT_BYTE_SIZE`,
/// and the field sits `field_offset` bytes into that record. Callers
/// must not recompute the formula from literals; the helper test below
/// pins it against literals for `k = 0, 1, 63` (including the last slot,
/// whose value field at `2364` sits just before `flags` at `2376`).
pub const fn act_field_byte_offset(k: usize, field_offset: usize) -> usize {
    ACTS_BYTE_OFFSET + k * ACT_BYTE_SIZE + field_offset
}

/// Compute the signed EPD index for a 4-byte-aligned StarCraft memory address:
/// `EPD(addr) = (addr - 0x58A364) / 4`.
pub const fn epd(addr: u32) -> i32 {
    (addr.wrapping_sub(EPD_BASE) as i32) / 4
}

/// Compute the 32-bit unsigned EPD representation (matching StarCraft's 32-bit player field):
/// `((addr - 0x58A364) / 4) as u32`.
pub const fn epd_u32(addr: u32) -> u32 {
    epd(addr) as u32
}

/// Overwrite (mode 4) or add into (mode 1) the 4-byte window at
/// `field_offset` of one encoded record.
///
/// This is the single relocation primitive shared by every record kind:
/// the record is encoded to its exact binary layout, the window is patched
/// byte-accurately, and the bytes are decoded back into the typed record.
/// `field_offset` may be any byte offset whose 4-byte window fits inside
/// the record (sub-dword windows included); anything else is the loud
/// [`LirError::RelocFieldOutOfBounds`].
fn patch_window<const N: usize>(
    bytes: &mut [u8; N],
    record: &'static str,
    field_offset: usize,
    patch: impl FnOnce(u32) -> u32,
) -> Result<(), LirError> {
    let out_of_bounds = || LirError::RelocFieldOutOfBounds {
        record,
        field_offset,
        size: N,
    };
    let end = field_offset.checked_add(4).ok_or_else(out_of_bounds)?;
    if end > N {
        return Err(out_of_bounds());
    }
    let cur = u32::from_le_bytes(bytes[field_offset..end].try_into().unwrap());
    bytes[field_offset..end].copy_from_slice(&patch(cur).to_le_bytes());
    Ok(())
}

/// First-class 20-byte (`5`-EPD) StarCraft `CTrigCondition` record.
///
/// Layout (`<IIIHBBBBH`):
/// - `+0..+4`: `locid` (`u32`, or EUDX bitmask when `eudx == 0x4353`)
/// - `+4..+8`: `player` (`u32`, or EPD address for `Deaths`/`DeathsX`)
/// - `+8..+12`: `amount` (`u32`)
/// - `+12..+14`: `unit` (`u16`)
/// - `+14`: `comparison` (`u8`)
/// - `+15`: `condtype` (`u8`)
/// - `+16`: `restype` (`u8`)
/// - `+17`: `flags` (`u8`)
/// - `+18..+20`: `eudx` (`u16`, `0x4353` `"SC"` for EUDX)
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct CTrigCondition {
    pub locid: u32,
    pub player: u32,
    pub amount: u32,
    pub unit: u16,
    pub comparison: u8,
    pub condtype: u8,
    pub restype: u8,
    pub flags: u8,
    pub eudx: u16,
}

impl CTrigCondition {
    pub const fn empty() -> Self {
        Self {
            locid: 0,
            player: 0,
            amount: 0,
            unit: 0,
            comparison: 0,
            condtype: 0,
            restype: 0,
            flags: 0,
            eudx: 0,
        }
    }

    pub const fn always() -> Self {
        Self {
            condtype: 22,
            ..Self::empty()
        }
    }

    pub const fn never() -> Self {
        Self {
            condtype: 23,
            ..Self::empty()
        }
    }

    pub const fn deaths(player: u32, comparison: u8, amount: u32, unit: u16) -> Self {
        Self {
            locid: 0,
            player,
            amount,
            unit,
            comparison,
            condtype: 15,
            restype: 0,
            flags: 0,
            eudx: 0,
        }
    }

    pub const fn deathsx(player: u32, comparison: u8, amount: u32, unit: u16, mask: u32) -> Self {
        Self {
            locid: mask,
            player,
            amount,
            unit,
            comparison,
            condtype: 15,
            restype: 0,
            flags: 0,
            eudx: EUDX_MAGIC,
        }
    }

    pub fn encode(&self) -> [u8; COND_BYTE_SIZE] {
        let mut buf = [0u8; COND_BYTE_SIZE];
        buf[0..4].copy_from_slice(&self.locid.to_le_bytes());
        buf[4..8].copy_from_slice(&self.player.to_le_bytes());
        buf[8..12].copy_from_slice(&self.amount.to_le_bytes());
        buf[12..14].copy_from_slice(&self.unit.to_le_bytes());
        buf[14] = self.comparison;
        buf[15] = self.condtype;
        buf[16] = self.restype;
        buf[17] = self.flags;
        buf[18..20].copy_from_slice(&self.eudx.to_le_bytes());
        buf
    }

    pub fn decode(bytes: &[u8; COND_BYTE_SIZE]) -> Self {
        Self {
            locid: u32::from_le_bytes(bytes[0..4].try_into().unwrap()),
            player: u32::from_le_bytes(bytes[4..8].try_into().unwrap()),
            amount: u32::from_le_bytes(bytes[8..12].try_into().unwrap()),
            unit: u16::from_le_bytes(bytes[12..14].try_into().unwrap()),
            comparison: bytes[14],
            condtype: bytes[15],
            restype: bytes[16],
            flags: bytes[17],
            eudx: u16::from_le_bytes(bytes[18..20].try_into().unwrap()),
        }
    }

    /// Assign `value` to the 4-byte window at `field_offset` (string
    /// relocation). Any byte offset whose window fits the record is
    /// accepted; sub-dword windows are patched in place.
    pub fn write_field_u32(&self, field_offset: usize, value: u32) -> Result<Self, LirError> {
        let mut bytes = self.encode();
        patch_window(&mut bytes, "condition", field_offset, |_| value)?;
        Ok(Self::decode(&bytes))
    }

    /// Add `value` into the 4-byte window at `field_offset` (payload
    /// relocation). Same window rules as [`Self::write_field_u32`].
    pub fn add_field_u32(&self, field_offset: usize, value: u32) -> Result<Self, LirError> {
        let mut bytes = self.encode();
        patch_window(&mut bytes, "condition", field_offset, |cur| {
            cur.wrapping_add(value)
        })?;
        Ok(Self::decode(&bytes))
    }

    /// The typed condition plus no relocations.
    pub fn to_cond_data(&self) -> CondData {
        CondData {
            cond: *self,
            relocs: Vec::new(),
        }
    }
}

/// First-class 32-byte (`8`-EPD) StarCraft `CTrigAction` record.
///
/// Layout (`<IIIIIIHBBBBH`):
/// - `+0..+4`: `locid1` / `mask` (`u32`, per-action bitmask when `eudx == 0x4353`)
/// - `+4..+8`: `strid` (`u32`)
/// - `+8..+12`: `wavid` (`u32`)
/// - `+12..+16`: `time` (`u32`)
/// - `+16..+20`: `player1` / `dest` (`u32`, EPD destination for `SetDeaths`/`SetDeathsX`)
/// - `+20..+24`: `player2` / `value` (`u32`, value for `SetDeaths`/`SetDeathsX`)
/// - `+24..+26`: `unit` (`u16`)
/// - `+26`: `acttype` (`u8`)
/// - `+27`: `amount` / `modifier` (`u8`)
/// - `+28`: `flags` (`u8`)
/// - `+29`: `padding` (`u8`)
/// - `+30..+32`: `eudx` (`u16`, `0x4353` `"SC"` for EUDX)
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct CTrigAction {
    pub mask: u32,
    pub strid: u32,
    pub wavid: u32,
    pub time: u32,
    pub dest: u32,
    pub value: u32,
    pub unit: u16,
    pub acttype: u8,
    pub modifier: u8,
    pub flags: u8,
    pub padding: u8,
    pub eudx: u16,
}

impl CTrigAction {
    pub const fn empty() -> Self {
        Self {
            mask: 0,
            strid: 0,
            wavid: 0,
            time: 0,
            dest: 0,
            value: 0,
            unit: 0,
            acttype: 0,
            modifier: 0,
            flags: 0,
            padding: 0,
            eudx: 0,
        }
    }

    pub const fn set_deaths(dest: u32, modifier: u8, value: u32, unit: u16) -> Self {
        Self {
            mask: 0,
            strid: 0,
            wavid: 0,
            time: 0,
            dest,
            value,
            unit,
            acttype: 45,
            modifier,
            flags: 20,
            padding: 0,
            eudx: 0,
        }
    }

    pub const fn set_deathsx(dest: u32, modifier: u8, value: u32, unit: u16, mask: u32) -> Self {
        Self {
            mask,
            strid: 0,
            wavid: 0,
            time: 0,
            dest,
            value,
            unit,
            acttype: 45,
            modifier,
            flags: 20,
            padding: 0,
            eudx: EUDX_MAGIC,
        }
    }

    pub const fn display_text(strid: u32, always_display: u8) -> Self {
        Self {
            mask: 0,
            strid,
            wavid: 0,
            time: 0,
            dest: 0,
            value: 0,
            unit: 0,
            acttype: 9,
            modifier: 0,
            flags: always_display,
            padding: 0,
            eudx: 0,
        }
    }

    pub fn encode(&self) -> [u8; ACT_BYTE_SIZE] {
        let mut buf = [0u8; ACT_BYTE_SIZE];
        buf[0..4].copy_from_slice(&self.mask.to_le_bytes());
        buf[4..8].copy_from_slice(&self.strid.to_le_bytes());
        buf[8..12].copy_from_slice(&self.wavid.to_le_bytes());
        buf[12..16].copy_from_slice(&self.time.to_le_bytes());
        buf[16..20].copy_from_slice(&self.dest.to_le_bytes());
        buf[20..24].copy_from_slice(&self.value.to_le_bytes());
        buf[24..26].copy_from_slice(&self.unit.to_le_bytes());
        buf[26] = self.acttype;
        buf[27] = self.modifier;
        buf[28] = self.flags;
        buf[29] = self.padding;
        buf[30..32].copy_from_slice(&self.eudx.to_le_bytes());
        buf
    }

    pub fn decode(bytes: &[u8; ACT_BYTE_SIZE]) -> Self {
        Self {
            mask: u32::from_le_bytes(bytes[0..4].try_into().unwrap()),
            strid: u32::from_le_bytes(bytes[4..8].try_into().unwrap()),
            wavid: u32::from_le_bytes(bytes[8..12].try_into().unwrap()),
            time: u32::from_le_bytes(bytes[12..16].try_into().unwrap()),
            dest: u32::from_le_bytes(bytes[16..20].try_into().unwrap()),
            value: u32::from_le_bytes(bytes[20..24].try_into().unwrap()),
            unit: u16::from_le_bytes(bytes[24..26].try_into().unwrap()),
            acttype: bytes[26],
            modifier: bytes[27],
            flags: bytes[28],
            padding: bytes[29],
            eudx: u16::from_le_bytes(bytes[30..32].try_into().unwrap()),
        }
    }

    /// Assign `value` to the 4-byte window at `field_offset` (string
    /// relocation). Any byte offset whose window fits the record is
    /// accepted; sub-dword windows are patched in place.
    pub fn write_field_u32(&self, field_offset: usize, value: u32) -> Result<Self, LirError> {
        let mut bytes = self.encode();
        patch_window(&mut bytes, "action", field_offset, |_| value)?;
        Ok(Self::decode(&bytes))
    }

    /// Add `value` into the 4-byte window at `field_offset` (payload
    /// relocation). Same window rules as [`Self::write_field_u32`].
    pub fn add_field_u32(&self, field_offset: usize, value: u32) -> Result<Self, LirError> {
        let mut bytes = self.encode();
        patch_window(&mut bytes, "action", field_offset, |cur| {
            cur.wrapping_add(value)
        })?;
        Ok(Self::decode(&bytes))
    }

    /// The typed action plus no relocations.
    pub fn to_act_data(&self) -> ActData {
        ActData {
            act: *self,
            uprp: None,
            relocs: Vec::new(),
        }
    }
}

/// First-class `RawTrigger (16C, 64A)` record representing StarCraft's
/// `2,408`-byte (`602`-EPD) runtime trigger node.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RawTrigger {
    pub prev: u32,
    pub next: u32,
    pub conds: [CTrigCondition; COND_COUNT],
    pub acts: [CTrigAction; ACT_COUNT],
    pub execution_flags: u32,
    pub players: [u8; 27],
    pub current_action: u8,
}

impl Default for RawTrigger {
    fn default() -> Self {
        Self::new()
    }
}

impl RawTrigger {
    pub const fn new() -> Self {
        Self {
            prev: 0,
            next: 0,
            conds: [CTrigCondition::empty(); COND_COUNT],
            acts: [CTrigAction::empty(); ACT_COUNT],
            execution_flags: 0x04, // PreserveTrigger enabled by default
            players: [0u8; 27],
            current_action: 0,
        }
    }

    /// Encode this `RawTrigger` into its exact `2,408`-byte (`602`-EPD) StarCraft layout.
    pub fn encode(&self) -> [u8; RAW_TRIGGER_BYTE_SIZE] {
        let mut buf = [0u8; RAW_TRIGGER_BYTE_SIZE];
        buf[PREV_BYTE_OFFSET..PREV_BYTE_OFFSET + 4].copy_from_slice(&self.prev.to_le_bytes());
        buf[NEXT_BYTE_OFFSET..NEXT_BYTE_OFFSET + 4].copy_from_slice(&self.next.to_le_bytes());
        for (i, cond) in self.conds.iter().enumerate() {
            let off = CONDS_BYTE_OFFSET + i * COND_BYTE_SIZE;
            buf[off..off + COND_BYTE_SIZE].copy_from_slice(&cond.encode());
        }
        for (k, act) in self.acts.iter().enumerate() {
            let off = ACTS_BYTE_OFFSET + k * ACT_BYTE_SIZE;
            buf[off..off + ACT_BYTE_SIZE].copy_from_slice(&act.encode());
        }
        buf[FLAGS_BYTE_OFFSET..FLAGS_BYTE_OFFSET + 4]
            .copy_from_slice(&self.execution_flags.to_le_bytes());
        buf[PLAYERS_BYTE_OFFSET..PLAYERS_BYTE_OFFSET + 27].copy_from_slice(&self.players);
        buf[CURRENT_ACTION_BYTE_OFFSET] = self.current_action;
        buf
    }

    /// Decode a `2,408`-byte StarCraft trigger block into a [`RawTrigger`].
    pub fn decode(bytes: &[u8; RAW_TRIGGER_BYTE_SIZE]) -> Self {
        let prev = u32::from_le_bytes(bytes[0..4].try_into().unwrap());
        let next = u32::from_le_bytes(bytes[4..8].try_into().unwrap());
        let mut conds = [CTrigCondition::empty(); COND_COUNT];
        for (i, slot) in conds.iter_mut().enumerate() {
            let off = CONDS_BYTE_OFFSET + i * COND_BYTE_SIZE;
            let slice: &[u8; COND_BYTE_SIZE] = bytes[off..off + COND_BYTE_SIZE].try_into().unwrap();
            *slot = CTrigCondition::decode(slice);
        }
        let mut acts = [CTrigAction::empty(); ACT_COUNT];
        for (k, slot) in acts.iter_mut().enumerate() {
            let off = ACTS_BYTE_OFFSET + k * ACT_BYTE_SIZE;
            let slice: &[u8; ACT_BYTE_SIZE] = bytes[off..off + ACT_BYTE_SIZE].try_into().unwrap();
            *slot = CTrigAction::decode(slice);
        }
        let execution_flags = u32::from_le_bytes(
            bytes[FLAGS_BYTE_OFFSET..FLAGS_BYTE_OFFSET + 4]
                .try_into()
                .unwrap(),
        );
        let mut players = [0u8; 27];
        players.copy_from_slice(&bytes[PLAYERS_BYTE_OFFSET..PLAYERS_BYTE_OFFSET + 27]);
        let current_action = bytes[CURRENT_ACTION_BYTE_OFFSET];
        Self {
            prev,
            next,
            conds,
            acts,
            execution_flags,
            players,
            current_action,
        }
    }
}

// ---------------------------------------------------------------------------
// Classic template triggers (2400 bytes; the map tail's TRIG rebuild)
// ---------------------------------------------------------------------------

/// One classic trigger: 16 conditions x 20 B + 64 actions x 32 B +
/// a 4-byte flag dword + a 28-byte player mask = 2400 bytes.
/// NOT the LIR `RawTrigger` size (2408) — see the module docs.
pub const CLASSIC_TRIGGER_BYTE_SIZE: usize = 2400;
pub const CLASSIC_COND_COUNT: usize = 16;
pub const CLASSIC_COND_SIZE: usize = COND_BYTE_SIZE;
pub const CLASSIC_ACT_COUNT: usize = 64;
pub const CLASSIC_ACT_SIZE: usize = ACT_BYTE_SIZE;
/// Byte offset of the condition array inside a classic trigger.
pub const CLASSIC_CONDS_BYTE_OFFSET: usize = 0;
/// Byte offset of the action array: 16 conditions x 20 B = 320.
pub const CLASSIC_ACTS_BYTE_OFFSET: usize = 320;
/// Byte offset of the flag dword: 320 + 64 x 32 = 2368.
pub const CLASSIC_FLAG_BYTE_OFFSET: usize = 2368;
/// Byte offset of the player mask: flag dword + 4 = 2372.
pub const CLASSIC_PLAYERS_BYTE_OFFSET: usize = 2372;
/// Player mask length: 2400 - 2372 = 28 bytes.
pub const CLASSIC_PLAYERS_LEN: usize = 28;
/// Byte offset of `condtype` inside one 20-byte condition record.
pub const CLASSIC_COND_TYPE_OFFSET: usize = 15;
/// Byte offset of `acttype` inside one 32-byte action record.
pub const CLASSIC_ACT_TYPE_OFFSET: usize = 26;
/// Byte offset of the action flag byte inside one action record.
pub const CLASSIC_ACT_FLAG_OFFSET: usize = 28;
/// The classic flag dword value the synthetic injector triggers carry:
/// `0x04` = PreserveTrigger.
pub const CLASSIC_PRESERVED_FLAG: u32 = 0x04;

/// One classic 2400-byte trigger, as a value (not a slice view, so
/// callers can mutate a copy — e.g. the TRIG rebuild's flag toggle —
/// without aliasing the template section).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ClassicTrigger(pub [u8; CLASSIC_TRIGGER_BYTE_SIZE]);

impl ClassicTrigger {
    /// The single classic `2,400`-byte encoder.
    ///
    /// Every classic record in the map tail (stage-1 injectors, the
    /// runner-boundary pair, inline-code dispatchers, the synthetic
    /// blank) is produced here. `players` holds player-slot indices
    /// (`0..=27`) whose effect byte is set; `conds`/`acts` are typed
    /// records, zero-filled to the 16/64 slots; `flag` is the dword at
    /// [`CLASSIC_FLAG_BYTE_OFFSET`].
    ///
    /// The per-record layout is shared with `RawTrigger`
    /// (`CTrigCondition::encode` / `CTrigAction::encode`); only the
    /// surrounding slot offsets differ.
    pub fn from_parts(
        players: &[usize],
        conds: &[CTrigCondition],
        acts: &[CTrigAction],
        flag: u32,
    ) -> Result<Self, LirError> {
        if conds.len() > CLASSIC_COND_COUNT {
            return Err(LirError::TooManyConditions(conds.len()));
        }
        if acts.len() > CLASSIC_ACT_COUNT {
            return Err(LirError::TooManyActions(acts.len()));
        }
        let mut out = [0u8; CLASSIC_TRIGGER_BYTE_SIZE];
        for (i, cond) in conds.iter().enumerate() {
            let off = CLASSIC_CONDS_BYTE_OFFSET + i * CLASSIC_COND_SIZE;
            out[off..off + CLASSIC_COND_SIZE].copy_from_slice(&cond.encode());
        }
        for (k, act) in acts.iter().enumerate() {
            let off = CLASSIC_ACTS_BYTE_OFFSET + k * CLASSIC_ACT_SIZE;
            out[off..off + CLASSIC_ACT_SIZE].copy_from_slice(&act.encode());
        }
        out[CLASSIC_FLAG_BYTE_OFFSET..CLASSIC_FLAG_BYTE_OFFSET + 4]
            .copy_from_slice(&flag.to_le_bytes());
        for &p in players {
            assert!(
                p < CLASSIC_PLAYERS_LEN,
                "classic player slot {p} out of range"
            );
            out[CLASSIC_PLAYERS_BYTE_OFFSET + p] = 1;
        }
        Ok(ClassicTrigger(out))
    }

    /// Copy a 2400-byte slice into a trigger; `None` for any other
    /// length (a short chunk is not a classic trigger).
    pub fn from_bytes(bytes: &[u8]) -> Option<Self> {
        if bytes.len() != CLASSIC_TRIGGER_BYTE_SIZE {
            return None;
        }
        let mut out = [0u8; CLASSIC_TRIGGER_BYTE_SIZE];
        out.copy_from_slice(bytes);
        Some(ClassicTrigger(out))
    }

    pub fn as_bytes(&self) -> &[u8; CLASSIC_TRIGGER_BYTE_SIZE] {
        &self.0
    }

    pub fn into_bytes(self) -> [u8; CLASSIC_TRIGGER_BYTE_SIZE] {
        self.0
    }

    /// Flag dword at 2368.
    pub fn flag(&self) -> u32 {
        u32::from_le_bytes(
            self.0[CLASSIC_FLAG_BYTE_OFFSET..CLASSIC_FLAG_BYTE_OFFSET + 4]
                .try_into()
                .unwrap(),
        )
    }

    /// Mutable flag bytes at 2368..2372.
    pub fn flag_mut(&mut self) -> &mut [u8] {
        &mut self.0[CLASSIC_FLAG_BYTE_OFFSET..CLASSIC_FLAG_BYTE_OFFSET + 4]
    }

    /// Set the flag dword at 2368.
    pub fn set_flag(&mut self, flag: u32) {
        self.flag_mut().copy_from_slice(&flag.to_le_bytes());
    }

    /// Condition array bytes (0..320).
    pub fn conds_bytes(&self) -> &[u8] {
        &self.0[CLASSIC_CONDS_BYTE_OFFSET..CLASSIC_ACTS_BYTE_OFFSET]
    }

    /// Action array bytes (320..2368).
    pub fn acts_bytes(&self) -> &[u8] {
        &self.0[CLASSIC_ACTS_BYTE_OFFSET..CLASSIC_FLAG_BYTE_OFFSET]
    }

    /// Player mask bytes (2372..2400).
    pub fn players_bytes(&self) -> &[u8] {
        &self.0[CLASSIC_PLAYERS_BYTE_OFFSET..CLASSIC_TRIGGER_BYTE_SIZE]
    }

    /// Condition `i` (0..16) as a 20-byte record, or `None`.
    pub fn cond(&self, i: usize) -> Option<&[u8]> {
        if i >= CLASSIC_COND_COUNT {
            return None;
        }
        let off = CLASSIC_CONDS_BYTE_OFFSET + i * CLASSIC_COND_SIZE;
        Some(&self.0[off..off + CLASSIC_COND_SIZE])
    }

    /// Action `i` (0..64) as a 32-byte record, or `None`.
    pub fn act(&self, i: usize) -> Option<&[u8]> {
        if i >= CLASSIC_ACT_COUNT {
            return None;
        }
        let off = CLASSIC_ACTS_BYTE_OFFSET + i * CLASSIC_ACT_SIZE;
        Some(&self.0[off..off + CLASSIC_ACT_SIZE])
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn epd_formula_matches_starcraft_death_table_base() {
        assert_eq!(epd(0x58A364), 0);
        assert_eq!(epd(0x58A368), 1);
        assert_eq!(epd(0x58A360), -1);
        assert_eq!(epd_u32(0x58A364), 0);
        assert_eq!(epd_u32(0x58A368), 1);
    }

    #[test]
    fn action_field_offsets_match_ctriga_record_layout() {
        assert_eq!(MASK_FIELD_OFFSET, 0);
        assert_eq!(DEST_FIELD_OFFSET, 16);
        assert_eq!(VALUE_FIELD_OFFSET, 20);
        assert_eq!(MODIFIER_DWORD_OFFSET, 24);
        assert_eq!(COND_PLAYER_FIELD_OFFSET, 4);
    }

    #[test]
    fn raw_trigger_16c_64a_binary_layout_and_roundtrip() {
        assert_eq!(RAW_TRIGGER_BYTE_SIZE, 2408);
        assert_eq!(RAW_TRIGGER_EPD_SIZE, 602);
        assert_eq!(COND_BYTE_SIZE, 20);
        assert_eq!(COND_EPD_SIZE, 5);
        assert_eq!(ACT_BYTE_SIZE, 32);
        assert_eq!(ACT_EPD_SIZE, 8);
        assert_eq!(CONDS_BYTE_OFFSET, 8);
        assert_eq!(ACTS_BYTE_OFFSET, 328);
        assert_eq!(FLAGS_BYTE_OFFSET, 2376);
        assert_eq!(PLAYERS_BYTE_OFFSET, 2380);
        assert_eq!(CURRENT_ACTION_BYTE_OFFSET, 2407);

        let mut trig = RawTrigger::new();
        trig.prev = 0x11223344;
        trig.next = 0x55667788;
        trig.conds[0] = CTrigCondition::deathsx(epd_u32(0x58A368), 10, 42, 0, 0xFF00FF00);
        trig.conds[15] = CTrigCondition::always();
        trig.acts[0] = CTrigAction::set_deathsx(epd_u32(0x58A36C), 7, 0xDEAD_BEEF, 0, 0x00FF00FF);
        trig.acts[63] = CTrigAction::display_text(8, 4);
        trig.execution_flags = 0x04;
        trig.players[0] = 1;
        trig.current_action = 3;

        let encoded = trig.encode();
        assert_eq!(encoded.len(), 2408);

        // Verify exact byte offsets
        assert_eq!(&encoded[0..4], &0x11223344u32.to_le_bytes());
        assert_eq!(&encoded[4..8], &0x55667788u32.to_le_bytes());
        // cond[0] at +8..+28
        assert_eq!(&encoded[8..12], &0xFF00FF00u32.to_le_bytes()); // mask at +0
        assert_eq!(&encoded[12..16], &1u32.to_le_bytes()); // player (EPD) at +4
        assert_eq!(&encoded[16..20], &42u32.to_le_bytes()); // amount at +8
        assert_eq!(encoded[22], 10); // comparison (Exactly = 10) at +14
        assert_eq!(encoded[23], 15); // condtype (Deaths = 15) at +15
        assert_eq!(&encoded[26..28], &0x4353u16.to_le_bytes()); // eudx at +18

        // act[0] at +328..+360
        assert_eq!(&encoded[328..332], &0x00FF00FFu32.to_le_bytes()); // mask at +0
        assert_eq!(&encoded[344..348], &2u32.to_le_bytes()); // dest (EPD) at +16
        assert_eq!(&encoded[348..352], &0xDEAD_BEEFu32.to_le_bytes()); // value at +20
        assert_eq!(encoded[354], 45); // acttype (SetDeaths = 45) at +26
        assert_eq!(encoded[355], 7); // modifier (SetTo = 7) at +27
        assert_eq!(encoded[356], 20); // flags at +28
        assert_eq!(&encoded[358..360], &0x4353u16.to_le_bytes()); // eudx at +30

        // execution_flags at +2376..+2380
        assert_eq!(&encoded[2376..2380], &4u32.to_le_bytes());
        assert_eq!(encoded[2380], 1);
        assert_eq!(encoded[2407], 3);

        let decoded = RawTrigger::decode(&encoded);
        assert_eq!(decoded, trig);
    }

    #[test]
    fn act_field_byte_offset_matches_literal_formula() {
        // Oracle side stays literal on purpose, following the round-trip
        // test's pattern: the helper must reproduce 328 + 32*k + field.
        // k = 63 is the last slot: its value field at 2364 sits just
        // before flags at 2376, where an off-by-one would land.
        assert_eq!(act_field_byte_offset(0, VALUE_FIELD_OFFSET), 348);
        assert_eq!(act_field_byte_offset(0, DEST_FIELD_OFFSET), 344);
        assert_eq!(act_field_byte_offset(1, VALUE_FIELD_OFFSET), 380);
        assert_eq!(act_field_byte_offset(1, DEST_FIELD_OFFSET), 376);
        assert_eq!(act_field_byte_offset(63, VALUE_FIELD_OFFSET), 2364);
        assert_eq!(act_field_byte_offset(63, DEST_FIELD_OFFSET), 2360);
    }

    // -- typed relocation windows ------------------------------------------

    #[test]
    fn aligned_field_write_replaces_the_whole_dword() {
        let act = CTrigAction::set_deaths(0x1111_1111, 7, 0xAABB_CCDD, 0);
        let patched = act.write_field_u32(VALUE_FIELD_OFFSET, 0x2233_4455).unwrap();
        assert_eq!(patched.value, 0x2233_4455);
        assert_eq!(patched.dest, act.dest);
        assert_eq!(patched.unit, act.unit);
        assert_eq!(patched.acttype, act.acttype);
    }

    #[test]
    fn sub_dword_field_write_lands_on_the_exact_byte_window() {
        // ADR 0022 / issue #101: a field offset need not be dword
        // aligned. The 4-byte window is patched at the exact byte
        // offset instead of indexing a `[i64; N]` parameter slot.
        let act = CTrigAction::empty();
        let patched = act.write_field_u32(2, 0x1122_3344).unwrap();
        let bytes = patched.encode();
        assert_eq!(&bytes[0..2], &[0, 0]);
        assert_eq!(&bytes[2..6], &0x1122_3344u32.to_le_bytes());
        assert_eq!(&bytes[6..32], &[0u8; 26]);
        // The typed view round-trips through the patched bytes, so the
        // sub-dword write survives serde and re-encoding.
        assert_eq!(CTrigAction::decode(&bytes), patched);
        assert_eq!(patched.encode(), bytes);
    }

    #[test]
    fn sub_dword_condition_write_spans_the_record_fields() {
        let cond = CTrigCondition::always();
        // Window 15..19 straddles condtype/flags/eudx and is not dword
        // aligned; it is the last window that fits a 20-byte record.
        // The previous `[i64; N]` model would have rounded 15 down to
        // parameter slot 3 (`unit`) and corrupted a different field.
        let patched = cond.write_field_u32(15, 0xDEAD_BEEF).unwrap();
        let bytes = patched.encode();
        assert_eq!(&bytes[15..19], &[0xEF, 0xBE, 0xAD, 0xDE]);
        assert_eq!(bytes[19], 0); // byte 19 stays outside the window
        assert_eq!(CTrigCondition::decode(&bytes), patched);
        assert_eq!(patched.encode(), bytes);
        // The aligned last dword still assigns exactly as before.
        let aligned = cond.write_field_u32(16, 0xC0DE_1234).unwrap();
        assert_eq!(&aligned.encode()[16..20], &0xC0DE_1234u32.to_le_bytes());
    }

    #[test]
    fn out_of_record_relocation_windows_are_loud() {
        // A 4-byte window must fit inside the record: a condition has
        // no window at 17 (17 + 4 > 20), an action none at 29.
        let cond = CTrigCondition::always();
        assert_eq!(
            cond.write_field_u32(17, 0).unwrap_err(),
            LirError::RelocFieldOutOfBounds {
                record: "condition",
                field_offset: 17,
                size: COND_BYTE_SIZE,
            }
        );
        let act = CTrigAction::empty();
        assert_eq!(
            act.add_field_u32(29, 4).unwrap_err(),
            LirError::RelocFieldOutOfBounds {
                record: "action",
                field_offset: 29,
                size: ACT_BYTE_SIZE,
            }
        );
        assert!(act.write_field_u32(28, 0).is_ok());
        assert!(cond.write_field_u32(16, 0).is_ok());
    }

    // -- classic 2400-byte encoder -----------------------------------------

    #[test]
    fn classic_trigger_layout_constants_are_consistent() {
        assert_eq!(
            CLASSIC_COND_COUNT * CLASSIC_COND_SIZE
                + CLASSIC_ACT_COUNT * CLASSIC_ACT_SIZE
                + 4
                + CLASSIC_PLAYERS_LEN,
            CLASSIC_TRIGGER_BYTE_SIZE
        );
        assert_eq!(CLASSIC_ACTS_BYTE_OFFSET, 320);
        assert_eq!(CLASSIC_FLAG_BYTE_OFFSET, 2368);
        assert_eq!(CLASSIC_PLAYERS_BYTE_OFFSET, 2372);
        // Classic 2368 is NOT the RawTrigger flag offset 2376.
        assert_ne!(CLASSIC_FLAG_BYTE_OFFSET, FLAGS_BYTE_OFFSET);
        assert_ne!(CLASSIC_TRIGGER_BYTE_SIZE, RAW_TRIGGER_BYTE_SIZE);
    }

    #[test]
    fn classic_encoder_places_typed_records_at_the_classic_offsets() {
        let cond = CTrigCondition::deaths(7, 10, 42, 0);
        let act = CTrigAction::set_deaths(9, 7, 5, 0);
        let trig = ClassicTrigger::from_parts(&[1, 2], &[cond], &[act], CLASSIC_PRESERVED_FLAG)
            .unwrap();
        let bytes = trig.as_bytes();
        assert_eq!(&bytes[0..20], &cond.encode());
        assert_eq!(&bytes[320..352], &act.encode());
        assert_eq!(trig.flag(), CLASSIC_PRESERVED_FLAG);
        assert_eq!(&trig.players_bytes()[..3], &[0, 1, 1]);
        assert_eq!(&bytes[2368..2372], &CLASSIC_PRESERVED_FLAG.to_le_bytes());
        // The shared record encoder means cond[0].condtype is at +15 and
        // act[0].acttype at +346, matching the classic offset constants.
        assert_eq!(bytes[CLASSIC_COND_TYPE_OFFSET], 15);
        assert_eq!(bytes[CLASSIC_ACTS_BYTE_OFFSET + CLASSIC_ACT_TYPE_OFFSET], 45);
    }

    #[test]
    fn classic_encoder_rejects_oversized_condition_and_action_lists() {
        let conds = vec![CTrigCondition::always(); CLASSIC_COND_COUNT + 1];
        assert!(matches!(
            ClassicTrigger::from_parts(&[], &conds, &[], CLASSIC_PRESERVED_FLAG),
            Err(LirError::TooManyConditions(17))
        ));
        let acts = vec![CTrigAction::empty(); CLASSIC_ACT_COUNT + 1];
        assert!(matches!(
            ClassicTrigger::from_parts(&[], &[], &acts, CLASSIC_PRESERVED_FLAG),
            Err(LirError::TooManyActions(65))
        ));
    }

    #[test]
    fn classic_trigger_flag_read_write_and_slices() {
        let trigger = ClassicTrigger::from_parts(&[], &[], &[], 0).unwrap();
        let mut bytes = *trigger.as_bytes();
        bytes[2368..2372].copy_from_slice(&0x0C00u32.to_le_bytes());
        bytes[2372] = 1;
        bytes[15] = 22; // cond[0].condtype
        bytes[320 + 26] = 45; // act[0].acttype
        let mut t = ClassicTrigger::from_bytes(&bytes).unwrap();
        assert_eq!(t.flag(), 0x0C00);
        assert_eq!(t.cond(0).unwrap()[15], 22);
        assert_eq!(t.act(0).unwrap()[26], 45);
        assert_eq!(t.players_bytes()[0], 1);
        assert_eq!(t.conds_bytes().len(), 320);
        assert_eq!(t.acts_bytes().len(), 2048);
        t.set_flag(t.flag() ^ 8);
        assert_eq!(t.flag(), 0x0C08);
        assert!(ClassicTrigger::from_bytes(&bytes[..2399]).is_none());
        assert!(t.cond(16).is_none());
        assert!(t.act(64).is_none());
    }

    #[test]
    fn classic_encode_toggles_2368_not_2376() {
        // Discriminating case (d)(ii): a classic encode toggles the
        // flag at 2368, while a RawTrigger encode writes flags at
        // 2376. Unifying the two offsets would corrupt a field.
        let mut t = ClassicTrigger::from_parts(&[], &[], &[], 0x04).unwrap();
        t.set_flag(t.flag() ^ 8);
        assert_eq!(t.flag(), 0x0C);
        assert_eq!(t.as_bytes()[2376], 0);
        // And the LIR side, for contrast: flags live at 2376 there.
        let mut raw = RawTrigger::default();
        raw.execution_flags = 0x1000_0000;
        let encoded = raw.encode();
        assert_eq!(
            u32::from_le_bytes(
                encoded[FLAGS_BYTE_OFFSET..FLAGS_BYTE_OFFSET + 4]
                    .try_into()
                    .unwrap()
            ),
            0x1000_0000
        );
        assert_eq!(encoded[2368], 0);
    }
}
