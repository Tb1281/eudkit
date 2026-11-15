//! LIR 3.0 types, `RawTrigger (16C, 64A)` 2,408-byte encoder/decoder,
//! `static cost` reporting, and `lir2payload` binary emitter for `eudkit`.

use serde::{Deserialize, Serialize};
use thiserror::Error;

pub mod dump;

pub const LIR_VERSION: &str = "3.0";

/// Accept LIR 3.0 as well as legacy 0.1/0.2 fixtures used by `eudkit-map` tests.
pub fn version_supported(v: &str) -> bool {
    matches!(v, "3.0" | "0.2" | "0.1")
}

mod layout;

pub use layout::*;

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct LirDocument {
    pub version: String,
    pub module: LirModule,
}

impl LirDocument {
    pub fn from_json(json: &str) -> Result<Self, LirError> {
        let doc: Self = serde_json::from_str(json).map_err(|e| LirError::BadJson(e.to_string()))?;
        if !version_supported(&doc.version) {
            return Err(LirError::BadVersion(doc.version));
        }
        Ok(doc)
    }

    pub fn to_json(&self) -> Result<String, LirError> {
        if !version_supported(&self.version) {
            return Err(LirError::BadVersion(self.version.clone()));
        }
        serde_json::to_string(self).map_err(|e| LirError::BadJson(e.to_string()))
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct LirModule {
    pub entry: usize,
    pub items: Vec<LirItem>,
    pub strings: Vec<String>,
    /// Recorded section-relative byte offsets (`u32`) for each id in
    /// `1..=strings.len()`, populated when the frontend pre-computed a
    /// string-section layout (to fold `StrAddr(id)` at lower time).
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub str_addrs: Vec<u32>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "lowercase")]
pub enum LirItem {
    Trigger(TriggerItem),
    Blob(BlobItem),
}

impl LirItem {
    pub fn size(&self) -> usize {
        match self {
            LirItem::Trigger(t) => t.size,
            LirItem::Blob(b) => b.size,
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct TriggerItem {
    pub size: usize,
    pub prev: Option<i64>,
    pub next: Option<i64>,
    pub prev_reloc: bool,
    pub next_reloc: bool,
    pub conds: Vec<CondData>,
    pub acts: Vec<ActData>,
    pub preserved: bool,
    /// Optional explicit execution flags (`u32`). When omitted (`None`),
    /// `preserved == true` encodes `4` (`0x04`) and `preserved == false` encodes `0`.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub flag: Option<u32>,
}

/// One LIR condition: a typed [`CTrigCondition`] record plus the payload
/// relocations that supply its remaining bytes at `lir2payload` time.
///
/// The record is stored typed, not as a `[i64; 9]` parameter array: the
/// binary layout lives once in [`super::layout`], and a relocation is a
/// byte window inside the 20-byte record ([`CTrigCondition::write_field_u32`]
/// / [`CTrigCondition::add_field_u32`]).
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct CondData {
    #[serde(flatten)]
    pub cond: CTrigCondition,
    #[serde(default)]
    pub relocs: Vec<RelocData>,
}

/// One LIR action: a typed [`CTrigAction`] record, an optional UPRP record
/// for `CreateUnitWithProperties`, and the payload relocations that supply
/// its remaining bytes at `lir2payload` time.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ActData {
    #[serde(flatten)]
    pub act: CTrigAction,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub uprp: Option<[u8; 20]>,
    #[serde(default)]
    pub relocs: Vec<RelocData>,
}

impl ActData {
    /// Write the 1-based UPRP slot index into the action's `value` field
    /// (`+20`). `CreateUnitWithProperties` reads the property slot from
    /// that field; SC counts slots from 1.
    pub fn set_uprp_slot(&mut self, slot: usize) {
        self.act.value = slot as u32;
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct RelocData {
    pub field_offset: usize,
    pub target_item: usize,
    pub target_offset: usize,
    pub mode: i64,
}

/// Shared `SetNextPtr(branch -> target)` construction, used by lowering's
/// control-flow pass and map assembly's once-per-tick gate.
///
/// `branch` and `target` are caller-defined tokens that carry their own
/// target offsets: lowering passes its unresolved `ExecRelocTarget`, while
/// map assembly passes `(item, offset)` pairs. The returned action payload
/// and two relocations `(field_offset, target, mode)` are:
///
/// * `DEST_FIELD_OFFSET` -> `branch`'s own `next` field (mode 1, EPD fixup)
/// * `VALUE_FIELD_OFFSET` -> `target`'s start (mode 4, byte-offset fixup)
pub fn set_next_ptr<T>(branch: T, target: T) -> (CTrigAction, [(usize, T, i64); 2]) {
    (
        CTrigAction::set_deaths(0, 7, 0, 0),
        [
            (DEST_FIELD_OFFSET, branch, 1),
            (VALUE_FIELD_OFFSET, target, 4),
        ],
    )
}

/// Materialize [`set_next_ptr`] for resolved LIR item targets.
pub fn set_next_ptr_action(branch_item: usize, target_item: usize) -> ActData {
    let (ctrig, relocs) = set_next_ptr((branch_item, NEXT_BYTE_OFFSET), (target_item, 0));
    let mut act = ctrig.to_act_data();
    act.relocs = relocs
        .into_iter()
        .map(
            |(field_offset, (target_item, target_offset), mode)| RelocData {
                field_offset,
                target_item,
                target_offset,
                mode,
            },
        )
        .collect();
    act
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct BlobItem {
    pub size: usize,
    pub data: String,
    pub relocs: Vec<BlobRelocData>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct BlobRelocData {
    pub offset: usize,
    pub target_item: usize,
    pub target_offset: usize,
    pub mode: i64,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PayloadReloc {
    pub offset: usize,
    pub mode: i64,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PayloadOut {
    pub data: Vec<u8>,
    pub prttable: Vec<usize>,
    pub orttable: Vec<usize>,
    pub relocs: Vec<PayloadReloc>,
    pub addrs: Vec<u32>,
}

pub type Payload = PayloadOut;

/// Borrowed fixup tables from a [`PayloadOut`]: the two separate
/// `&[usize]` parameters `initialize_payload` used to take, passed
/// as one argument.
///
/// This is a call-site bundling only (candidate 6): `PayloadOut`
/// itself is NOT reshaped — its `prttable`/`orttable` fields stay
/// as they are, and `lir2payload`'s return shape is unchanged.
/// The two tables keep their distinct meanings: `prt` entries are
/// EPD fixups (adjusted by `payload_offset / 4`) and `ort` entries
/// are byte-offset fixups (adjusted by `payload_offset`).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct PayloadFixups<'a> {
    /// EPD fixup offsets into the payload.
    pub prt: &'a [usize],
    /// Byte-offset fixup offsets into the payload.
    pub ort: &'a [usize],
}

impl<'a> PayloadFixups<'a> {
    /// Both tables empty.
    pub const fn empty() -> Self {
        PayloadFixups { prt: &[], ort: &[] }
    }

    /// Borrow both tables directly.
    pub const fn new(prt: &'a [usize], ort: &'a [usize]) -> Self {
        PayloadFixups { prt, ort }
    }
}

impl PayloadOut {
    /// Borrow this payload's fixup tables as one view.
    pub fn fixups(&self) -> PayloadFixups<'_> {
        PayloadFixups {
            prt: &self.prttable,
            ort: &self.orttable,
        }
    }
}

/// Static cost (`NT static`, conditions, actions, and payload bytes) of an LIR module.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct StaticCost {
    /// Total `RawTrigger` records emitted in the payload (`NT static`).
    pub triggers: usize,
    /// Total non-empty conditions across all triggers.
    pub conditions: usize,
    /// Total non-empty actions across all triggers.
    pub actions: usize,
    /// Total trigger bytes (`triggers * 2408`).
    pub trigger_bytes: usize,
    /// Total blob bytes across all `BlobItem`s: the **raw** data-byte count,
    /// excluding the 0–3 B inter-item pad that [`total_bytes`] includes.
    ///
    /// [`total_bytes`]: StaticCost::total_bytes
    pub blob_bytes: usize,
    /// Total payload bytes: `sum(align4(item.size))`, including the 0–3 B
    /// inter-item pad that keeps every payload item start dword-aligned.
    /// Equal to `lir2payload(...).data.len()`.
    pub total_bytes: usize,
}

/// Round `n` up to the next multiple of 4 (payload item stride).
#[inline]
pub const fn align4(n: usize) -> usize {
    (n + 3) & !3
}

/// Compute the [`StaticCost`] of a [`LirDocument`].
pub fn static_cost(doc: &LirDocument) -> StaticCost {
    let mut triggers = 0usize;
    let mut conditions = 0usize;
    let mut actions = 0usize;
    let mut trigger_bytes = 0usize;
    let mut blob_bytes = 0usize;
    let mut raw_total = 0usize;

    for item in &doc.module.items {
        // `total_bytes` follows the real layout: each item occupies its
        // dword-rounded stride (see `lir2payload`), so this equals
        // `lir2payload(doc).data.len()`. `blob_bytes` below stays raw.
        raw_total += align4(item.size());
        match item {
            LirItem::Trigger(t) => {
                triggers += 1;
                conditions += t.conds.iter().filter(|c| c.cond.condtype != 0).count();
                actions += t.acts.iter().filter(|a| a.act.acttype != 0).count();
                trigger_bytes += t.size;
            }
            LirItem::Blob(b) => {
                blob_bytes += b.size;
            }
        }
    }

    StaticCost {
        triggers,
        conditions,
        actions,
        trigger_bytes,
        blob_bytes,
        total_bytes: raw_total,
    }
}

/// Count the total number of `RawTrigger` items in a [`LirDocument`] (`NT static`).
pub fn count_lir_triggers(doc: &LirDocument) -> usize {
    static_cost(doc).triggers
}

/// Executed cost (`NT executed`, evaluated conditions, and executed actions) of one
/// StarCraft trigger loop over a lowered LIR payload.
#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct ExecutedCost {
    /// Total trigger evaluations during the trigger loop (`NT executed`).
    pub triggers: usize,
    /// Total non-empty conditions evaluated across those triggers.
    pub conditions: usize,
    /// Total non-empty actions executed across triggers whose conditions held.
    pub actions: usize,
    /// Sequence of LIR item indices visited during the trigger loop.
    pub visited_items: Vec<usize>,
}

/// Runtime simulator state for a compiled LIR payload, allowing tests to execute
/// trigger loops (`step_loop`) and inspect `TriggerPage` slots and `executed cost`.
#[derive(Debug, Clone)]
pub struct SimulationState {
    pub entry: usize,
    pub payload: PayloadOut,
    pub external_epds: std::collections::HashMap<u32, u32>,
    /// Switch states (`condition 11` / `action 13`). Conditions and actions
    /// are decoded from trigger bytes; tests seed them with real actions.
    pub switches: [bool; 256],
    /// Deterministic generator for the random switch action (modifier 11).
    pub switch_rng: u32,
    pub last_executed_cost: ExecutedCost,
    /// When true, each trigger's bytes are snapshotted when the trigger
    /// starts. SC:R does not observe writes made to a later action of the
    /// *currently executing* trigger; the default live-read mode is useful
    /// for other analyses but can hide that failure mode.
    pub snapshot_triggers: bool,
}

impl SimulationState {
    /// Decode the `2,408`-byte [`RawTrigger`] at `item_idx`.
    pub fn raw_trigger(&self, item_idx: usize) -> RawTrigger {
        let base = self.payload.addrs[item_idx] as usize;
        let slice: &[u8; RAW_TRIGGER_BYTE_SIZE] = self.payload.data
            [base..base + RAW_TRIGGER_BYTE_SIZE]
            .try_into()
            .unwrap();
        RawTrigger::decode(slice)
    }

    /// Read the 32-bit `.value` (`+20`) of action slot `slot` (`0..63`) on `page_item_idx`.
    pub fn read_page_slot_value(&self, page_item_idx: usize, slot: usize) -> u32 {
        self.raw_trigger(page_item_idx).acts[slot].value
    }

    pub fn read_epd(&self, epd_idx: u32) -> u32 {
        let addr = EPD_BASE.wrapping_add(epd_idx.wrapping_mul(4)) as usize;
        if addr + 4 <= self.payload.data.len() {
            read_u32_le(&self.payload.data, addr)
        } else {
            *self.external_epds.get(&epd_idx).unwrap_or(&0)
        }
    }

    pub fn write_epd(&mut self, epd_idx: u32, val: u32) {
        let addr = EPD_BASE.wrapping_add(epd_idx.wrapping_mul(4)) as usize;
        if addr + 4 <= self.payload.data.len() {
            write_u32_le(&mut self.payload.data, addr, val as i64);
        } else {
            self.external_epds.insert(epd_idx, val);
        }
    }

    /// Read a trigger field, either live or from the per-trigger snapshot.
    fn trigger_u32(&self, snap: Option<&[u8]>, base: usize, off: usize) -> u32 {
        match snap {
            Some(s) => read_u32_le(s, off - base),
            None => read_u32_le(&self.payload.data, off),
        }
    }

    fn trigger_byte(&self, snap: Option<&[u8]>, base: usize, off: usize) -> u8 {
        match snap {
            Some(s) => s[off - base],
            None => self.payload.data[off],
        }
    }

    /// Execute one pass of the StarCraft trigger loop starting from `entry`.
    pub fn step_loop(&mut self) -> ExecutedCost {
        let mut cost = ExecutedCost::default();
        if self.payload.addrs.is_empty() || self.entry >= self.payload.addrs.len() {
            self.last_executed_cost = cost.clone();
            return cost;
        }

        let mut cur_addr = self.payload.addrs[self.entry];
        let mut guard = 0usize;
        while cur_addr != 0x8000_0000 && cur_addr != u32::MAX {
            let base = cur_addr as usize;
            if base + RAW_TRIGGER_BYTE_SIZE > self.payload.data.len() {
                break;
            }
            guard += 1;
            assert!(
                guard <= 100_000,
                "Infinite trigger loop detected in simulation"
            );

            let snap_vec = if self.snapshot_triggers {
                Some(self.payload.data[base..base + RAW_TRIGGER_BYTE_SIZE].to_vec())
            } else {
                None
            };
            let snap = snap_vec.as_deref();

            let exec_flags = self.trigger_u32(snap, base, base + FLAGS_BYTE_OFFSET);
            let disabled = (exec_flags & 0x02) != 0;

            if disabled {
                cur_addr = self.trigger_u32(snap, base, base + NEXT_BYTE_OFFSET);
                continue;
            }

            if let Some(item_idx) = self.payload.addrs.iter().position(|&a| a == cur_addr) {
                cost.visited_items.push(item_idx);
            }
            cost.triggers += 1;

            let mut all_conds_true = true;
            for ci in 0..COND_COUNT {
                let cb = base + CONDS_BYTE_OFFSET + ci * COND_BYTE_SIZE;
                let condtype = self.trigger_byte(snap, base, cb + 15);
                if condtype == 0 {
                    continue;
                }
                cost.conditions += 1;
                let holds = match condtype {
                    22 => true,  // Always
                    23 => false, // Never
                    11 => {
                        // Switch (condition 11): comparison 2 = set, 3 = cleared.
                        let switch_id = self.trigger_byte(snap, base, cb + 16) as usize;
                        let state = self.trigger_byte(snap, base, cb + 14);
                        let is_set = self.switches[switch_id];
                        match state {
                            2 => is_set,
                            3 => !is_set,
                            _ => false,
                        }
                    }
                    15 => {
                        // Deaths / DeathsX
                        let mask = self.trigger_u32(snap, base, cb);
                        let raw_player = self.trigger_u32(snap, base, cb + 4);
                        let amount = self.trigger_u32(snap, base, cb + 8);
                        let comparison = self.trigger_byte(snap, base, cb + 14);
                        let eudx = u16::from_le_bytes([
                            self.trigger_byte(snap, base, cb + 18),
                            self.trigger_byte(snap, base, cb + 19),
                        ]);
                        let player = if raw_player == CURRENT_PLAYER {
                            self.read_epd(EPD_CURRENT_PLAYER)
                        } else {
                            raw_player
                        };
                        let raw_val = self.read_epd(player);
                        let lhs = if eudx == EUDX_MAGIC {
                            raw_val & mask
                        } else {
                            raw_val
                        };
                        let rhs = amount;
                        match comparison {
                            0 => lhs >= rhs,  // AtLeast
                            1 => lhs <= rhs,  // AtMost
                            10 => lhs == rhs, // Exactly
                            _ => false,
                        }
                    }
                    _ => true,
                };
                if !holds {
                    all_conds_true = false;
                    break;
                }
            }

            if all_conds_true {
                for ai in 0..ACT_COUNT {
                    let ab = base + ACTS_BYTE_OFFSET + ai * ACT_BYTE_SIZE;
                    let acttype = self.trigger_byte(snap, base, ab + 26);
                    if acttype == 0 {
                        continue;
                    }
                    cost.actions += 1;
                    if acttype == 13 {
                        // SetSwitch (action 13): value = switch id, modifier
                        // 4 = set, 5 = clear, 6 = toggle, 11 = random.
                        let switch_id = (self.trigger_u32(snap, base, ab + 20) & 0xFF) as usize;
                        let state = self.trigger_byte(snap, base, ab + 27);
                        match state {
                            4 => self.switches[switch_id] = true,
                            5 => self.switches[switch_id] = false,
                            6 => self.switches[switch_id] = !self.switches[switch_id],
                            11 => {
                                self.switch_rng = self
                                    .switch_rng
                                    .wrapping_mul(1_664_525)
                                    .wrapping_add(1_013_904_223);
                                self.switches[switch_id] = (self.switch_rng >> 31) != 0;
                            }
                            _ => {}
                        }
                    }
                    if acttype == 45 {
                        // SetDeaths / SetDeathsX
                        let mask = self.trigger_u32(snap, base, ab);
                        let raw_dest = self.trigger_u32(snap, base, ab + 16);
                        let value = self.trigger_u32(snap, base, ab + 20);
                        let modifier = self.trigger_byte(snap, base, ab + 27);
                        let eudx = u16::from_le_bytes([
                            self.trigger_byte(snap, base, ab + 30),
                            self.trigger_byte(snap, base, ab + 31),
                        ]);
                        let dest = if raw_dest == CURRENT_PLAYER {
                            self.read_epd(EPD_CURRENT_PLAYER)
                        } else {
                            raw_dest
                        };
                        let old_val = self.read_epd(dest);
                        let eff_mask = if eudx == EUDX_MAGIC {
                            mask
                        } else {
                            0xFFFF_FFFF
                        };
                        let new_val = match modifier {
                            7 => (old_val & !eff_mask) | (value & eff_mask), // SetTo
                            8 => {
                                // Add
                                let sum = (old_val & eff_mask).wrapping_add(value & eff_mask);
                                (old_val & !eff_mask) | (sum & eff_mask)
                            }
                            9 => {
                                // Subtract (StarCraft saturates at 0 within masked bits)
                                let cur_m = old_val & eff_mask;
                                let sub_m = value & eff_mask;
                                let diff = cur_m.saturating_sub(sub_m);
                                (old_val & !eff_mask) | (diff & eff_mask)
                            }
                            _ => old_val,
                        };
                        self.write_epd(dest, new_val);
                    }
                }
                // If PreserveTrigger (0x04) is not set, disable trigger (0x02)
                let post_flags = self.trigger_u32(snap, base, base + FLAGS_BYTE_OFFSET);
                if (post_flags & 0x04) == 0 {
                    write_u32_le(
                        &mut self.payload.data,
                        base + FLAGS_BYTE_OFFSET,
                        (post_flags | 0x02) as i64,
                    );
                }
            }

            cur_addr = self.trigger_u32(snap, base, base + NEXT_BYTE_OFFSET);
        }

        self.last_executed_cost = cost.clone();
        cost
    }
}

/// Initialize a [`SimulationState`] without stepping through the trigger loop.
pub fn init_simulation(doc: &LirDocument) -> Result<SimulationState, LirError> {
    let payload = lir2payload(doc)?;
    Ok(SimulationState {
        entry: doc.module.entry,
        payload,
        external_epds: std::collections::HashMap::new(),
        switches: [false; 256],
        switch_rng: 0x1234_5678,
        last_executed_cost: ExecutedCost::default(),
        snapshot_triggers: false,
    })
}

/// Encode a [`LirDocument`] into a binary payload and simulate one StarCraft trigger loop.
pub fn simulate_trigger_loop(doc: &LirDocument) -> Result<SimulationState, LirError> {
    let mut state = init_simulation(doc)?;
    state.step_loop();
    Ok(state)
}

#[derive(Debug, Error, PartialEq, Eq)]
pub enum LirError {
    #[error("Unsupported LIR version: {0}")]
    BadVersion(String),
    #[error("Invalid LIR JSON: {0}")]
    BadJson(String),
    #[error("Invalid base64 in blob item")]
    BadBase64,
    #[error("Trigger condition count {0} exceeds maximum {COND_COUNT} (EUDSL-BOUNDS)")]
    TooManyConditions(usize),
    #[error("Trigger action count {0} exceeds maximum {ACT_COUNT} (EUDSL-BOUNDS)")]
    TooManyActions(usize),
    #[error("Relocation target item {0} out of bounds (items len = {1})")]
    RelocTargetOutOfBounds(usize, usize),
    #[error(
        "EUDSL-BOUNDS: relocation field offset {field_offset} does not fit a 4-byte window inside one {record} record ({size} bytes)"
    )]
    RelocFieldOutOfBounds {
        record: &'static str,
        field_offset: usize,
        size: usize,
    },
}

/// Decode a base64 blob payload (the inverse of [`b64_encode`]).
///
/// Only the standard padded form is accepted: `=` may appear at most twice,
/// and only as a contiguous suffix of the final chunk. Leading or mid-chunk
/// padding and padding followed by more data (`"=AAA"`, `"AA==AAAA"`) are
/// rejected.
pub fn b64_decode(s: &str) -> Result<Vec<u8>, LirError> {
    let bytes = s.as_bytes();
    if bytes.is_empty() {
        return Ok(Vec::new());
    }
    if bytes.len() % 4 != 0 {
        return Err(LirError::BadBase64);
    }
    let chunk_count = bytes.len() / 4;
    let mut out = Vec::with_capacity(chunk_count * 3);
    for (index, chunk) in bytes.chunks_exact(4).enumerate() {
        let mut vals = [0u8; 4];
        let mut pad = 0;
        for (i, &b) in chunk.iter().enumerate() {
            vals[i] = match b {
                b'A'..=b'Z' => b - b'A',
                b'a'..=b'z' => b - b'a' + 26,
                b'0'..=b'9' => b - b'0' + 52,
                b'+' => 62,
                b'/' => 63,
                b'=' => {
                    pad += 1;
                    0
                }
                _ => return Err(LirError::BadBase64),
            };
        }
        // Standard base64 pads at most two bytes in the final chunk.
        // Three or more `=` cannot encode any real payload and would
        // otherwise produce a spurious byte, so reject the chunk.
        if pad > 2 {
            return Err(LirError::BadBase64);
        }
        if pad > 0 {
            // Padding may only close the whole input: a padded non-final chunk
            // would leave decodable data after the padding (`"AA==AAAA"`).
            if index + 1 != chunk_count {
                return Err(LirError::BadBase64);
            }
            // Padding must be a suffix of the chunk, never leading or in the
            // middle (`"=AAA"`, `"A=AA"`).
            let first_pad = chunk
                .iter()
                .position(|&b| b == b'=')
                .unwrap_or(chunk.len());
            if chunk[first_pad..].iter().any(|&b| b != b'=') {
                return Err(LirError::BadBase64);
            }
        }
        let n = ((vals[0] as u32) << 18)
            | ((vals[1] as u32) << 12)
            | ((vals[2] as u32) << 6)
            | (vals[3] as u32);
        out.push((n >> 16) as u8);
        if pad < 2 {
            out.push((n >> 8) as u8);
        }
        if pad < 1 {
            out.push(n as u8);
        }
    }
    Ok(out)
}

#[inline]
fn write_u32_le(buf: &mut [u8], off: usize, v: i64) {
    let bytes = (v as u32).to_le_bytes();
    buf[off..off + 4].copy_from_slice(&bytes);
}


/// Read a little-endian `u32` at `off` from `buf`.
///
/// Shared by payload decoding and the tests that assert gate bytes.
#[inline]
pub fn read_u32_le(buf: &[u8], off: usize) -> u32 {
    u32::from_le_bytes([buf[off], buf[off + 1], buf[off + 2], buf[off + 3]])
}

pub fn b64_encode(data: &[u8]) -> String {
    const ALPHABET: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    let mut out = String::with_capacity(data.len().div_ceil(3) * 4);
    for chunk in data.chunks(3) {
        let mut n: u32 = 0;
        for &b in chunk {
            n = (n << 8) | b as u32;
        }
        n <<= 8 * (3 - chunk.len());
        for i in 0..4 {
            if i <= chunk.len() {
                out.push(ALPHABET[((n >> (18 - 6 * i)) & 63) as usize] as char);
            } else {
                out.push('=');
            }
        }
    }
    out
}

pub fn lir2payload(doc: &LirDocument) -> Result<PayloadOut, LirError> {
    if !version_supported(&doc.version) {
        return Err(LirError::BadVersion(doc.version.clone()));
    }
    let items = &doc.module.items;
    let mut offsets = Vec::with_capacity(items.len());
    let mut cur = 0usize;
    for item in items {
        offsets.push(cur);
        // Every payload object starts on a 4-byte boundary (eudplib's
        // dword allocation, `allocator/payload.rs`). This is what makes
        // every `Db` base dword-aligned, so a mode-1 (EPD) relocation
        // `offsets[target_item] + target_offset` names the intended dword
        // even for the second or later blob (#120).
        cur += align4(item.size());
    }
    let total_size = cur;
    let mut data = vec![0u8; total_size];
    let mut relocs = Vec::new();
    let mut prttable = Vec::new();
    let mut orttable = Vec::new();
    let addrs: Vec<u32> = offsets.iter().map(|&o| o as u32).collect();

    for (idx, item) in items.iter().enumerate() {
        let base = offsets[idx];
        match item {
            LirItem::Trigger(t) => {
                if t.conds.len() > COND_COUNT {
                    return Err(LirError::TooManyConditions(t.conds.len()));
                }
                if t.acts.len() > ACT_COUNT {
                    return Err(LirError::TooManyActions(t.acts.len()));
                }
                let mut tb = [0u8; RAW_TRIGGER_BYTE_SIZE];
                if let Some(prev) = t.prev {
                    if t.prev_reloc {
                        let target = prev as usize;
                        if target >= offsets.len() {
                            return Err(LirError::RelocTargetOutOfBounds(target, offsets.len()));
                        }
                        write_u32_le(&mut tb, PREV_BYTE_OFFSET, offsets[target] as i64);
                        let abs = base + PREV_BYTE_OFFSET;
                        relocs.push(PayloadReloc {
                            offset: abs,
                            mode: 4,
                        });
                        orttable.push(abs);
                    } else {
                        write_u32_le(&mut tb, PREV_BYTE_OFFSET, prev);
                    }
                }
                if let Some(next) = t.next {
                    if t.next_reloc {
                        let target = next as usize;
                        if target >= offsets.len() {
                            return Err(LirError::RelocTargetOutOfBounds(target, offsets.len()));
                        }
                        write_u32_le(&mut tb, NEXT_BYTE_OFFSET, offsets[target] as i64);
                        let abs = base + NEXT_BYTE_OFFSET;
                        relocs.push(PayloadReloc {
                            offset: abs,
                            mode: 4,
                        });
                        orttable.push(abs);
                    } else {
                        write_u32_le(&mut tb, NEXT_BYTE_OFFSET, next);
                    }
                }
                for (ci, c) in t.conds.iter().enumerate() {
                    let cb = CONDS_BYTE_OFFSET + ci * COND_BYTE_SIZE;
                    // The typed record encodes the whole 20-byte layout;
                    // relocations patch byte windows of that encoding.
                    let mut cond = c.cond;
                    for r in &c.relocs {
                        if r.target_item >= offsets.len() {
                            return Err(LirError::RelocTargetOutOfBounds(
                                r.target_item,
                                offsets.len(),
                            ));
                        }
                        let target_addr = offsets[r.target_item] + r.target_offset;
                        let addend = if r.mode == 1 {
                            epd_u32(target_addr as u32)
                        } else {
                            target_addr as u32
                        };
                        cond = cond.add_field_u32(r.field_offset, addend)?;
                        let abs = base + cb + r.field_offset;
                        relocs.push(PayloadReloc {
                            offset: abs,
                            mode: r.mode,
                        });
                        if r.mode == 1 {
                            prttable.push(abs);
                        } else {
                            orttable.push(abs);
                        }
                    }
                    tb[cb..cb + COND_BYTE_SIZE].copy_from_slice(&cond.encode());
                }
                for (ai, a) in t.acts.iter().enumerate() {
                    let ab = ACTS_BYTE_OFFSET + ai * ACT_BYTE_SIZE;
                    let mut act = a.act;
                    for r in &a.relocs {
                        if r.target_item >= offsets.len() {
                            return Err(LirError::RelocTargetOutOfBounds(
                                r.target_item,
                                offsets.len(),
                            ));
                        }
                        let target_addr = offsets[r.target_item] + r.target_offset;
                        let addend = if r.mode == 1 {
                            epd_u32(target_addr as u32)
                        } else {
                            target_addr as u32
                        };
                        act = act.add_field_u32(r.field_offset, addend)?;
                        let abs = base + ab + r.field_offset;
                        relocs.push(PayloadReloc {
                            offset: abs,
                            mode: r.mode,
                        });
                        if r.mode == 1 {
                            prttable.push(abs);
                        } else {
                            orttable.push(abs);
                        }
                    }
                    tb[ab..ab + ACT_BYTE_SIZE].copy_from_slice(&act.encode());
                }
                let exec_flags = t.flag.unwrap_or(if t.preserved { 4 } else { 0 });
                write_u32_le(&mut tb, FLAGS_BYTE_OFFSET, exec_flags as i64);
                data[base..base + RAW_TRIGGER_BYTE_SIZE].copy_from_slice(&tb);
            }
            LirItem::Blob(b) => {
                let mut raw = b64_decode(&b.data)?;
                raw.resize(b.size, 0);
                for r in &b.relocs {
                    if r.target_item >= offsets.len() {
                        return Err(LirError::RelocTargetOutOfBounds(
                            r.target_item,
                            offsets.len(),
                        ));
                    }
                    let target_addr = offsets[r.target_item] + r.target_offset;
                    let cur_val = read_u32_le(&raw, r.offset);
                    let new_val = if r.mode == 1 {
                        cur_val.wrapping_add(epd_u32(target_addr as u32))
                    } else {
                        cur_val.wrapping_add(target_addr as u32)
                    };
                    write_u32_le(&mut raw, r.offset, new_val as i64);
                    let abs = base + r.offset;
                    relocs.push(PayloadReloc {
                        offset: abs,
                        mode: r.mode,
                    });
                    if r.mode == 1 {
                        prttable.push(abs);
                    } else {
                        orttable.push(abs);
                    }
                }
                data[base..base + b.size].copy_from_slice(&raw);
            }
        }
    }

    Ok(PayloadOut {
        data,
        prttable,
        orttable,
        relocs,
        addrs,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn b64_decode_rejects_more_than_two_padding_bytes() {
        // A chunk may carry at most two padding bytes; `"===="` and
        // `"A==="` are not base64, even though the generic 6-bit loop
        // could produce a byte from them.
        assert_eq!(b64_decode("===="), Err(LirError::BadBase64));
        assert_eq!(b64_decode("A==="), Err(LirError::BadBase64));
        // Length and alphabet rejections still hold.
        assert_eq!(b64_decode("ABC"), Err(LirError::BadBase64));
        assert_eq!(b64_decode("!!!"), Err(LirError::BadBase64));
    }

    #[test]
    fn b64_decode_rejects_padding_outside_the_final_chunk_suffix() {
        // Leading and mid-chunk padding are not base64.
        assert_eq!(b64_decode("=AAA"), Err(LirError::BadBase64));
        assert_eq!(b64_decode("A=AA"), Err(LirError::BadBase64));
        assert_eq!(b64_decode("AA=A"), Err(LirError::BadBase64));
        // A padded chunk must be the last one: data cannot follow padding.
        assert_eq!(b64_decode("AA==AAAA"), Err(LirError::BadBase64));
        assert_eq!(b64_decode("AAAA=AAA"), Err(LirError::BadBase64));
    }

    #[test]
    fn b64_decode_accepts_the_valid_corpus() {
        assert_eq!(b64_decode(""), Ok(Vec::new()));
        assert_eq!(b64_decode("aGVsbG8="), Ok(b"hello".to_vec()));
        assert_eq!(b64_decode("YWI="), Ok(b"ab".to_vec()));
        assert_eq!(b64_decode("YQ=="), Ok(b"a".to_vec()));
        assert_eq!(b64_decode("AAAA"), Ok(vec![0, 0, 0]));
    }

    #[test]
    fn act_data_set_uprp_slot_mutates_value_field() {
        let mut act = CTrigAction::empty().to_act_data();
        act.set_uprp_slot(4);
        assert_eq!(act.act.value, 4);
    }

    #[test]
    fn set_next_ptr_builder_matches_the_branch_idiom() {
        let act = set_next_ptr_action(3, 7);
        assert_eq!(act.act.acttype, 45);
        assert_eq!(act.act, CTrigAction::set_deaths(0, 7, 0, 0));
        assert_eq!(act.relocs.len(), 2);
        assert_eq!(
            (
                act.relocs[0].field_offset,
                act.relocs[0].target_item,
                act.relocs[0].target_offset,
                act.relocs[0].mode,
            ),
            (DEST_FIELD_OFFSET, 3, NEXT_BYTE_OFFSET, 1)
        );
        assert_eq!(
            (
                act.relocs[1].field_offset,
                act.relocs[1].target_item,
                act.relocs[1].target_offset,
                act.relocs[1].mode,
            ),
            (VALUE_FIELD_OFFSET, 7, 0, 4)
        );
    }

    #[test]
    fn lir2payload_emits_2408_byte_trigger_and_reports_static_cost() {
        let doc = LirDocument {
            version: LIR_VERSION.to_string(),
            module: LirModule {
                entry: 0,
                items: vec![LirItem::Trigger(TriggerItem {
                    size: RAW_TRIGGER_BYTE_SIZE,
                    prev: Some(-1),
                    next: Some(-1),
                    prev_reloc: false,
                    next_reloc: false,
                    conds: vec![CTrigCondition::always().to_cond_data()],
                    acts: vec![
                        CTrigAction::set_deathsx(1, 7, 99, 0, 0xFFFF).to_act_data(),
                        CTrigAction::display_text(1, 4).to_act_data(),
                    ],
                    preserved: true,
                    flag: None,
                })],
                strings: vec!["SGVsbG8=".to_string()],
                str_addrs: vec![],
            },
        };

        let cost = static_cost(&doc);
        assert_eq!(cost.triggers, 1);
        assert_eq!(cost.conditions, 1);
        assert_eq!(cost.actions, 2);
        assert_eq!(cost.trigger_bytes, 2408);
        assert_eq!(cost.total_bytes, 2408);

        let payload = lir2payload(&doc).unwrap();
        assert_eq!(payload.data.len(), 2408);
        let raw = RawTrigger::decode(payload.data.as_slice().try_into().unwrap());
        assert_eq!(raw.conds[0].condtype, 22);
        assert_eq!(raw.acts[0].acttype, 45);
        assert_eq!(raw.acts[0].mask, 0xFFFF);
        assert_eq!(raw.acts[0].dest, 1);
        assert_eq!(raw.acts[0].value, 99);
        assert_eq!(raw.acts[0].eudx, 0x4353);
        assert_eq!(raw.acts[1].acttype, 9);
        assert_eq!(raw.acts[1].strid, 1);
        assert_eq!(raw.execution_flags, 4);
    }

    #[test]
    fn simulate_trigger_loop_supports_current_player_13_relative_addressing() {
        // Trigger 0:
        // 1. SetDeaths(203155, SetTo, 5000, 0) -> binds CurrentPlayer to EPD 5000
        // 2. SetDeaths(13, SetTo, 1234, 0)     -> writes 1234 to EPD 5000 via Player 13
        // 3. SetDeaths(203155, Add, 2, 0)      -> advances CurrentPlayer by 2 to EPD 5002
        // 4. SetDeaths(13, SetTo, 5678, 0)     -> writes 5678 to EPD 5002 via Player 13
        // Trigger 1 (chained from 0):
        // Condition: Deaths(13, Exactly, 5678, 0) -> tests EPD 5002 via Player 13
        // Action: SetDeaths(9000, SetTo, 1, 0)
        let doc = LirDocument {
            version: LIR_VERSION.to_string(),
            module: LirModule {
                entry: 0,
                items: vec![
                    LirItem::Trigger(TriggerItem {
                        size: RAW_TRIGGER_BYTE_SIZE,
                        prev: Some(-1),
                        next: Some(1),
                        prev_reloc: false,
                        next_reloc: true,
                        conds: vec![CTrigCondition::always().to_cond_data()],
                        acts: vec![
                            CTrigAction::set_deaths(203155, 7, 5000, 0).to_act_data(),
                            CTrigAction::set_deaths(13, 7, 1234, 0).to_act_data(),
                            CTrigAction::set_deaths(203155, 8, 2, 0).to_act_data(),
                            CTrigAction::set_deaths(13, 7, 5678, 0).to_act_data(),
                        ],
                        preserved: true,
                        flag: None,
                    }),
                    LirItem::Trigger(TriggerItem {
                        size: RAW_TRIGGER_BYTE_SIZE,
                        prev: Some(0),
                        next: Some(-1),
                        prev_reloc: true,
                        next_reloc: false,
                        conds: vec![CTrigCondition::deaths(13, 10, 5678, 0).to_cond_data()],
                        acts: vec![CTrigAction::set_deaths(9000, 7, 1, 0).to_act_data()],
                        preserved: true,
                        flag: None,
                    }),
                ],
                strings: vec![],
                str_addrs: vec![],
            },
        };

        let sim = simulate_trigger_loop(&doc).unwrap();
        assert_eq!(sim.read_epd(5000), 1234);
        assert_eq!(sim.read_epd(5002), 5678);
        assert_eq!(sim.read_epd(203155), 5002);
        assert_eq!(sim.read_epd(9000), 1);
    }

    #[test]
    fn simulate_trigger_loop_reads_and_writes_switches() {
        // Trigger 0: always             -> set switch 7 (action 13, modifier 4).
        // Trigger 1: switch 7 set       -> EPD 9000 = 1.
        // Trigger 2: switch 7 cleared   -> EPD 9001 = 1.
        // Trigger 3: always             -> clear switch 7 (modifier 5).
        // Trigger 4: switch 7 set       -> EPD 9002 = 1.
        // Trigger 5: switch 7 cleared   -> EPD 9003 = 1.
        //
        // The switch id lives in `restype` (condition) / `value` (action);
        // the state lives in `comparison` / `modifier`.
        let switch_cond = |comparison: u8| CTrigCondition {
            condtype: 11,
            comparison,
            restype: 7,
            ..CTrigCondition::empty()
        };
        let switch_act = |modifier: u8| CTrigAction {
            acttype: 13,
            value: 7,
            modifier,
            flags: 20,
            ..CTrigAction::empty()
        };
        let hit = |dest: u32| CTrigAction::set_deaths(dest, 7, 1, 0);

        let items = vec![
            (vec![CTrigCondition::always()], vec![switch_act(4)]),
            (vec![switch_cond(2)], vec![hit(9000)]),
            (vec![switch_cond(3)], vec![hit(9001)]),
            (vec![CTrigCondition::always()], vec![switch_act(5)]),
            (vec![switch_cond(2)], vec![hit(9002)]),
            (vec![switch_cond(3)], vec![hit(9003)]),
        ];
        let last = items.len() - 1;
        let doc = LirDocument {
            version: LIR_VERSION.to_string(),
            module: LirModule {
                entry: 0,
                items: items
                    .into_iter()
                    .enumerate()
                    .map(|(index, (conds, acts))| {
                        LirItem::Trigger(TriggerItem {
                            size: RAW_TRIGGER_BYTE_SIZE,
                            prev: Some(if index > 0 { index as i64 - 1 } else { -1 }),
                            next: Some(if index < last { index as i64 + 1 } else { -1 }),
                            prev_reloc: index > 0,
                            next_reloc: index < last,
                            conds: conds.iter().map(CTrigCondition::to_cond_data).collect(),
                            acts: acts.iter().map(CTrigAction::to_act_data).collect(),
                            preserved: true,
                            flag: None,
                        })
                    })
                    .collect(),
                strings: vec![],
                str_addrs: vec![],
            },
        };

        let sim = simulate_trigger_loop(&doc).unwrap();
        assert_eq!(
            sim.read_epd(9000),
            1,
            "set switch must satisfy condition 11/2"
        );
        assert_eq!(sim.read_epd(9001), 0, "set switch must fail condition 11/3");
        assert_eq!(
            sim.read_epd(9002),
            0,
            "cleared switch must fail condition 11/2"
        );
        assert_eq!(
            sim.read_epd(9003),
            1,
            "cleared switch must satisfy condition 11/3"
        );
        assert!(!sim.switches[7]);
    }

    #[test]
    fn simulate_trigger_loop_toggles_and_randomizes_switches() {
        // Toggle (modifier 6) flips the stored state in both directions;
        // random (modifier 11) draws from `switch_rng` (LCG seeded with
        // 0x1234_5678), taking bit 31 of each draw.
        //
        // Trigger 0: always         -> toggle switch 8 (set).
        // Trigger 1: switch 8 set   -> EPD 9000 = 1.
        // Trigger 2: always         -> toggle switch 8 (cleared).
        // Trigger 3: switch 8 set   -> EPD 9001 = 1.
        // Trigger 4: always         -> random switch 9 (draw 0: bit31 = 0).
        // Trigger 5: switch 9 set   -> EPD 9002 = 1.
        // Trigger 6: always         -> random switch 9 (draw 1: bit31 = 1).
        // Trigger 7: switch 9 set   -> EPD 9003 = 1.
        let switch_cond = |id: u8, comparison: u8| CTrigCondition {
            condtype: 11,
            comparison,
            restype: id,
            ..CTrigCondition::empty()
        };
        let switch_act = |value: u32, modifier: u8| CTrigAction {
            acttype: 13,
            value,
            modifier,
            flags: 20,
            ..CTrigAction::empty()
        };
        let hit = |dest: u32| CTrigAction::set_deaths(dest, 7, 1, 0);

        let items = vec![
            (
                vec![CTrigCondition::always()],
                vec![switch_act(8, 6)],
            ),
            (vec![switch_cond(8, 2)], vec![hit(9000)]),
            (
                vec![CTrigCondition::always()],
                vec![switch_act(8, 6)],
            ),
            (vec![switch_cond(8, 2)], vec![hit(9001)]),
            (
                vec![CTrigCondition::always()],
                vec![switch_act(9, 11)],
            ),
            (vec![switch_cond(9, 2)], vec![hit(9002)]),
            (
                vec![CTrigCondition::always()],
                vec![switch_act(9, 11)],
            ),
            (vec![switch_cond(9, 2)], vec![hit(9003)]),
        ];
        let last = items.len() - 1;
        let doc = LirDocument {
            version: LIR_VERSION.to_string(),
            module: LirModule {
                entry: 0,
                items: items
                    .into_iter()
                    .enumerate()
                    .map(|(index, (conds, acts))| {
                        LirItem::Trigger(TriggerItem {
                            size: RAW_TRIGGER_BYTE_SIZE,
                            prev: Some(if index > 0 { index as i64 - 1 } else { -1 }),
                            next: Some(if index < last { index as i64 + 1 } else { -1 }),
                            prev_reloc: index > 0,
                            next_reloc: index < last,
                            conds: conds.iter().map(CTrigCondition::to_cond_data).collect(),
                            acts: acts.iter().map(CTrigAction::to_act_data).collect(),
                            preserved: true,
                            flag: None,
                        })
                    })
                    .collect(),
                strings: vec![],
                str_addrs: vec![],
            },
        };

        let sim = simulate_trigger_loop(&doc).unwrap();
        assert_eq!(
            sim.read_epd(9000),
            1,
            "toggle must set a cleared switch"
        );
        assert_eq!(
            sim.read_epd(9001),
            0,
            "the second toggle must clear it again"
        );
        assert_eq!(
            sim.read_epd(9002),
            0,
            "draw 0 has bit31 = 0, so switch 9 stays cleared"
        );
        assert_eq!(
            sim.read_epd(9003),
            1,
            "draw 1 has bit31 = 1, so switch 9 is set"
        );
        assert!(!sim.switches[8]);
        assert_eq!(sim.switch_rng, 0xCD30_5E6A, "two LCG draws consumed");
    }

    /// A leading always-trigger followed by one blob per `(size, data)` pair.
    /// `data` may be shorter than `size` (zero-padded), matching `Db(size)`.
    fn blob_layout_doc(blobs: &[(usize, &[u8])]) -> LirDocument {
        let mut items = vec![LirItem::Trigger(TriggerItem {
            size: RAW_TRIGGER_BYTE_SIZE,
            prev: Some(-1),
            next: Some(-1),
            prev_reloc: false,
            next_reloc: false,
            conds: vec![CTrigCondition::always().to_cond_data()],
            acts: vec![],
            preserved: true,
            flag: None,
        })];
        for &(size, data) in blobs {
            items.push(LirItem::Blob(BlobItem {
                size,
                data: b64_encode(data),
                relocs: vec![],
            }));
        }
        LirDocument {
            version: LIR_VERSION.to_string(),
            module: LirModule {
                entry: 0,
                items,
                strings: vec![],
                str_addrs: vec![],
            },
        }
    }

    /// Issue #120: every payload item starts at `align4(previous end)`, so
    /// every `Db` base is a multiple of 4 and a mode-1 (EPD) access names the
    /// blob's own first dword. Before the fix the second blob sat at 2411
    /// (`%4 == 3`), `epd_u32` rounded it up to 2412, and the read returned
    /// the last byte of blob 1 plus the first three of blob 2.
    #[test]
    fn lir2payload_aligns_every_blob_base_to_dwords() {
        let doc = blob_layout_doc(&[
            (3, &[0x11, 0x22, 0x33]),
            (8, &[0xAA, 0xBB, 0xCC, 0xDD, 0x55, 0x66, 0x77, 0x88]),
        ]);
        let payload = lir2payload(&doc).unwrap();
        assert_eq!(
            payload.addrs,
            vec![0, 2408, 2412],
            "each item base must be align4(previous end)"
        );
        for (idx, &addr) in payload.addrs.iter().enumerate() {
            assert_eq!(addr % 4, 0, "item {idx} base {addr} is not dword-aligned");
        }

        let cost = static_cost(&doc);
        assert_eq!(cost.blob_bytes, 11, "blob_bytes stays the raw data total");
        assert_eq!(cost.total_bytes, 2420, "2408 + align4(3) + align4(8)");
        assert_eq!(payload.data.len(), cost.total_bytes);

        let second = payload.addrs[2] as usize;
        assert_eq!(second, 2412);
        // Mode-1 relocations store `epd_u32(addr)`; decode it back the way
        // the client does. `epd` divides a *negative* i32 by 4 with
        // truncation toward zero, so an unaligned base rounds **up** to the
        // next dword (2411 -> 2412) and never names its own first dword.
        let resolved = ((epd_u32(second as u32) as i32)
            .wrapping_mul(4)
            .wrapping_add(EPD_BASE as i32)) as u32;
        assert_eq!(
            resolved as usize, second,
            "the second blob's EPD must resolve to its own base"
        );
        assert_eq!(
            read_u32_le(&payload.data, resolved as usize),
            0xDDCC_BBAA,
            "a mode-1 read of the second blob's first dword must be its own bytes"
        );
        // The 1-byte gap at 2411 is the inter-item pad: blob 1 occupies
        // 2408..2411 (its last byte is at 2410), so without the pad blob 2
        // started at 2411 and `epd_u32(2411)` rounded up to the dword at
        // 2412 — reading past blob 1's tail. With the pad, 2411 is zero and
        // blob 2's first dword is whole at its own base.
        assert_eq!(payload.data[2411], 0, "inter-item pad byte must be zero");
        assert_ne!(
            read_u32_le(&payload.data, 2411),
            0xDDCC_BBAA,
            "an unaligned read must not masquerade as the blob's first dword"
        );
    }

    /// Issue #120: `static_cost.total_bytes` follows the real layout, so it
    /// equals `lir2payload(...).data.len()` for any mix of odd item sizes.
    #[test]
    fn static_cost_total_bytes_matches_the_payload_length() {
        for blobs in [
            vec![],
            vec![(1, &b"a"[..])],
            vec![(3, &b"abc"[..]), (8, &b"abcdefgh"[..])],
            vec![(5, &b"abcde"[..]), (2, &b"xy"[..]), (7, &b"abcdefg"[..])],
        ] {
            let doc = blob_layout_doc(&blobs);
            let cost = static_cost(&doc);
            let payload = lir2payload(&doc).unwrap();
            assert_eq!(
                cost.total_bytes,
                payload.data.len(),
                "total_bytes must be the real payload length for {blobs:?}"
            );
            assert_eq!(cost.blob_bytes, blobs.iter().map(|(s, _)| s).sum::<usize>());
            assert_eq!(payload.data.len() % 4, 0);
        }
    }

    /// Issue #120 negative control: a single blob already sat at 2408, so the
    /// inter-item alignment leaves the common one-blob case untouched.
    #[test]
    fn single_blob_layout_is_unaffected_by_item_alignment() {
        let doc = blob_layout_doc(&[(5, &b"abcde"[..])]);
        let payload = lir2payload(&doc).unwrap();
        assert_eq!(payload.addrs, vec![0, 2408]);
        let cost = static_cost(&doc);
        assert_eq!(cost.total_bytes, 2408 + 8);
        assert_eq!(payload.data.len(), cost.total_bytes);
        assert_eq!(read_u32_le(&payload.data, 2408), 0x6463_6261);
    }

    /// Issue #120 structural invariant: every mode-1 relocation target
    /// (`addrs[target_item] + target_offset`) is dword-aligned once the
    /// frontend's offset is. The fixture puts a mode-1 act relocation on the
    /// second blob and a mode-1 blob relocation inside it.
    #[test]
    fn every_mode_1_reloc_target_is_dword_aligned() {
        let doc = blob_layout_doc(&[
            (3, &[0x11, 0x22, 0x33]),
            (8, &[0xAA, 0xBB, 0xCC, 0xDD, 0x55, 0x66, 0x77, 0x88]),
        ]);
        let mut doc = doc;
        if let LirItem::Trigger(t) = &mut doc.module.items[0] {
            let mut act = CTrigAction::set_deaths(0, 7, 0, 0).to_act_data();
            act.relocs.push(RelocData {
                field_offset: VALUE_FIELD_OFFSET,
                target_item: 2,
                target_offset: 0,
                mode: 1,
            });
            t.acts.push(act);
        }
        if let LirItem::Blob(b) = &mut doc.module.items[2] {
            b.relocs.push(BlobRelocData {
                offset: 4,
                target_item: 1,
                target_offset: 0,
                mode: 1,
            });
        }

        let payload = lir2payload(&doc).unwrap();
        assert_eq!(payload.addrs, vec![0, 2408, 2412]);
        let mut checked = 0;
        for item in &doc.module.items {
            match item {
                LirItem::Trigger(t) => {
                    for act in &t.acts {
                        for r in &act.relocs {
                            assert_mode1_target_aligned(&payload.addrs, r);
                            checked += 1;
                        }
                    }
                    for cond in &t.conds {
                        for r in &cond.relocs {
                            assert_mode1_target_aligned(&payload.addrs, r);
                            checked += 1;
                        }
                    }
                }
                LirItem::Blob(b) => {
                    for r in &b.relocs {
                        if r.mode == 1 {
                            assert_eq!(
                                (payload.addrs[r.target_item] as usize + r.target_offset) % 4,
                                0,
                                "blob relocation targets an unaligned dword"
                            );
                            checked += 1;
                        }
                    }
                }
            }
        }
        assert_eq!(checked, 2, "fixture must exercise both mode-1 paths");
    }

    fn assert_mode1_target_aligned(addrs: &[u32], r: &RelocData) {
        if r.mode == 1 {
            assert_eq!(
                (addrs[r.target_item] as usize + r.target_offset) % 4,
                0,
                "mode-1 relocation target must be a dword address"
            );
        }
    }
}
