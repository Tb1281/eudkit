//! `initialize_payload` port
//! (`src/eudkit/maprw/injector/payload_init.py`).
//!
//! Pure integer work: prt/ort fixups, STR section assembly, MRGN
//! obfuscation, and the TRIG section rebuild. The TRIG rebuild needs exactly
//! three classic-2400 trigger encodings from `trigtrg` (`Trigger`,
//! `SetMemory`/`SetDeaths`, `DeathsX`), ported here — not the full
//! condition/action vocabulary.
//!
//! Encoding notes (verified against `trigtrg.py`):
//! - `Action`: `struct.pack("<IIIIIIHBBBBH", locid1, strid, wavid, time,
//!   player1, player2, unitid, acttype, amount, flags, 0, eudx)` with
//!   `player1`/`player2` wrapped to unsigned 32-bit when negative.
//! - `Condition`: `struct.pack("<IIIHBBBBH", locid, player, amount, unitid,
//!   comparison, condtype, restype, flag, eudx)` with the same wrapping.
//! - `SetDeaths(player, modifier, number, unit)` =
//!   `Action(0,0,0,0, player, number, unit, 45, modifier, 20)`.
//! - `SetMemory(dest, modtype, value)` = `SetDeaths(EPD(dest), modtype,
//!   value, 0)`.
//! - `DeathsX(player, comparison, number, unit, mask)` =
//!   `Condition(mask, player, number, unit, comparison, 15, 0, 0,
//!   eudx=0x4353)`.
//! - `Trigger`: 16 conditions × 20 B + 64 actions × 32 B +
//!   `\x04\0\0\0` + 28 B player mask (`peff[player] = 1`).
//! - `EPD(p) = (p - 0x58A364) // 4` (Python floor division) for plain ints
//!   — verified empirically (`EPD(0x51A284) == -114744`,
//!   `EPD(0x6509B0) == 203155`, `EPD(4) == -1452248`).
//! Classic vs compiled triggers — do NOT merge them:
//! the template triggers handled here are CLASSIC 2400-byte
//! triggers (16 conditions x 20 B + 64 actions x 32 B + flag dword
//! at 2368 + 28-byte player mask). The COMPILED payload items
//! appended after the STR section are LIR `RawTrigger`s of 2408
//! bytes (flags at 2376). Both layouts live in `eudkit-lir::layout`
//! (issue #101) as distinct constants: `CLASSIC_FLAG_BYTE_OFFSET`
//! (2368) is not `FLAGS_BYTE_OFFSET` (2376), and applying one
//! encoding's flag offset to the other corrupts a different field.
//! The classic encoder itself is [`ClassicTrigger::from_parts`].
//!
//! - Constants: `SetTo = 7`, `Add = 8` (`constenc.py`); `AtLeast = 0`;
//!   `CurrentPlayer = 13`. `EncodePlayer`/`EncodeModifier`/
//!   `EncodeComparison`/`EncodeUnit` are identity on the ints used here.

use crate::chk::{section_name, Chk};
use crate::layout::{
    checked_records, ClassicTrigger, MrgnEntry, CLASSIC_ACTS_BYTE_OFFSET,
    CLASSIC_ACT_FLAG_OFFSET, CLASSIC_ACT_SIZE, CLASSIC_ACT_TYPE_OFFSET, CLASSIC_COND_SIZE,
    CLASSIC_COND_TYPE_OFFSET, CLASSIC_FLAG_BYTE_OFFSET, CLASSIC_PLAYERS_BYTE_OFFSET,
    CLASSIC_TRIGGER_BYTE_SIZE, MRGN_RECORD_SIZE,
};
use crate::tbl::BuiltStringTable;
use eudkit_lir::{
    CTrigAction, CTrigCondition, CLASSIC_PRESERVED_FLAG, PayloadFixups,
};
use crate::MapTailError;
use std::collections::HashSet;

// Trigger constants (trigtrg / constenc).
const SET_TO: u8 = 7;
const ADD: u8 = 8;
const AT_LEAST: u8 = 0;
const CURRENT_PLAYER: i64 = 13;
const ALL_PLAYERS: usize = 17;

const PTS: i64 = 0x51A284;
const CURPL: i64 = 0x6509B0;
const EUD_BASE: i64 = 0x58A364;

/// `ut.EPD` for plain ints: `(p - 0x58A364) // 4` with Python floor semantics.
pub(crate) fn epd(p: i64) -> i64 {
    let d = p - EUD_BASE;
    if d >= 0 {
        d / 4
    } else {
        -((-d + 3) / 4)
    }
}

/// Base address of the STR section in the assembled map's memory image:
/// a string at section offset `o` lives at `STR_SECTION_BASE + o`
/// (which is why `initialize_payload`'s payload offset starts here).
pub const STR_SECTION_BASE: i64 = 0x1919_43C8;

/// Payload offset for a STR section of `str_len` bytes: the STR base
/// plus the section length plus its padding to a 4-byte multiple.
/// This is the value `initialize_payload` returns (the TRIG rebuild
/// needs it, and the payload fixups are computed from it).
pub fn payload_base(str_len: usize) -> i64 {
    let str_padding = (4 - str_len % 4) % 4;
    STR_SECTION_BASE + str_len as i64 + str_padding as i64
}

/// Classic 32-byte action record: `SetDeaths` with the given fields.
///
/// `player1`/`player2` wrap negatives to unsigned 32-bit like
/// `+= 0x100000000; &= 0xFFFFFFFF` in the Python port.
fn set_deaths(player: i64, modifier: u8, number: i64, unit: u16) -> CTrigAction {
    CTrigAction::set_deaths(player as u32, modifier, number as u32, unit)
}

/// `SetMemory(dest, modtype, value)` = `SetDeaths(EPD(dest), modtype, value, 0)`.
fn set_memory(dest: i64, modtype: u8, value: i64) -> CTrigAction {
    set_deaths(epd(dest), modtype, value, 0)
}

/// `DeathsX(player, comparison, number, unit, mask)`.
fn deaths_x(player: i64, number: u32, mask: u32) -> CTrigCondition {
    CTrigCondition::deathsx(player as u32, AT_LEAST, number, 0, mask)
}

/// One classic 2400-byte trigger, encoded by the single classic encoder.
fn encode_trigger(
    players: &[usize],
    conditions: &[CTrigCondition],
    actions: &[CTrigAction],
) -> Vec<u8> {
    ClassicTrigger::from_parts(players, conditions, actions, CLASSIC_PRESERVED_FLAG)
        .expect("classic trigger condition/action counts are checked by the caller")
        .into_bytes()
        .to_vec()
}

/// Build the 70 stage-1 injector triggers (`payload_init.trglist`).
fn build_stage1_triggers(payload_offset: i64) -> Vec<u8> {
    let mut trigdata = Vec::with_capacity(70 * CLASSIC_TRIGGER_BYTE_SIZE);
    // pts[player].lasttrigger->next = value(strs) + strsled_offset
    for player in 0..8usize {
        let epd_pts = epd(PTS + 12 * player as i64);
        let triggerend = !(PTS + 12 * player as i64);
        let acts = [
            set_memory(CURPL, SET_TO, epd_pts),
            set_deaths(9, SET_TO, triggerend, 0),
        ];
        trigdata.extend_from_slice(&encode_trigger(&[player], &[], &acts));
    }
    // read pts[player].lasttrigger
    for e in 2..32u32 {
        let conds = [deaths_x(CURRENT_PLAYER, 1, 1 << e)];
        let acts = [set_deaths(11, ADD, (1 << e) as i64, 0)];
        trigdata.extend_from_slice(&encode_trigger(&[ALL_PLAYERS], &conds, &acts));
    }
    // apply to curpl
    {
        let epd4 = epd(4);
        let acts = [
            set_deaths(10, SET_TO, epd4, 0),
            set_memory(CURPL, SET_TO, epd4),
        ];
        trigdata.extend_from_slice(&encode_trigger(&[ALL_PLAYERS], &[], &acts));
    }
    for e in 2..32u32 {
        let conds = [deaths_x(11, 1, 1 << e)];
        let acts = [
            set_deaths(10, ADD, (1 << (e - 2)) as i64, 0),
            set_memory(CURPL, ADD, (1 << (e - 2)) as i64),
        ];
        trigdata.extend_from_slice(&encode_trigger(&[ALL_PLAYERS], &conds, &acts));
    }
    // now curpl = EPD(value(ptsprev) + 4); copy_deaths(...)
    {
        let acts = [
            set_deaths(CURRENT_PLAYER, SET_TO, payload_offset, 0),
            set_deaths(11, SET_TO, 0, 0),
        ];
        trigdata.extend_from_slice(&encode_trigger(&[ALL_PLAYERS], &[], &acts));
    }
    trigdata
}

/// The two synthetic classic triggers that bracket the authored classic
/// chain (ADR 0010).
///
/// Stage 1 leaves `curpl` (`0x6509B0`) pointing at the last trigger it
/// patched. The compiled payload (`lir2payload`) starts straight with
/// `DisplayText` and relies on `curpl` being a valid player, so without a
/// reset SC:R renders the map blank. eudplib's finalizer does the same
/// (`inj_finalizer.py`).
///
/// Both boundaries execute `SetMemory(0x6509B0, SetTo, 0)`. `Begin` runs
/// after stage 1 and before the first authored trigger; `End` runs after
/// the last authored trigger and before the synthetic blank, so an
/// authored trigger that changes `curpl` cannot corrupt the payload.
///
/// Both are intentionally preserved (never self-disabling) and run on
/// every trigger loop. The 70 stage-1 triggers are preserved too and
/// recompute `curpl` from scratch on each pass; a one-shot reset would only
/// protect the first loop. They are kept separate from the 70 Python
/// triggers solely so `stage1_trigs_py.bin` stays byte-identical.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum RunnerBoundary {
    /// `TrigTriggerBegin`: after stage 1, before the authored chain.
    Begin,
    /// `TrigTriggerEnd`: after the authored chain, before the blank.
    End,
}

impl RunnerBoundary {
    /// Encode this boundary as one classic 2400-byte trigger.
    ///
    /// The pair currently shares the same `curpl`-reset encoding; the
    /// separate variants keep the ordering contract explicit and give a
    /// later payload-first design a seam at [`RunnerBoundary::End`].
    pub fn trigger_bytes(self) -> [u8; CLASSIC_TRIGGER_BYTE_SIZE] {
        let acts = [set_memory(CURPL, SET_TO, 0)];
        ClassicTrigger::from_parts(&[ALL_PLAYERS], &[], &acts, CLASSIC_PRESERVED_FLAG)
            .expect("one action fits a classic trigger")
            .into_bytes()
    }
}

/// Whether Python's `_dispatch_inline_code` would compile this trigger
/// (`src/eudkit/maprw/inlinecode/ilcprocesstrig.py`).
///
/// All of these must hold (mirrors the Python checks exactly):
/// - `b2i4(seg, 20) == 0x10978D4A` (the inline-code magic);
/// - `GetInlineCodePlayerList`'s structural checks: the 28 effplayer bytes
///   at `320 + 2048 + 4` are all zero, `seg[15]` (`cond[0].condtype`) == 0,
///   `seg[346]` (`act[0].acttype`, 320 + 26) == 0;
/// - the returned player code `b2i4(seg, 24)` is nonzero
///   (`if not player_code: return None`).
///
/// A trigger failing any of these falls through to normal handling in
/// Python, so the native tail keeps it too — no false loud.
fn is_inline_code_trigger(seg: &[u8; CLASSIC_TRIGGER_BYTE_SIZE]) -> bool {
    if u32::from_le_bytes(seg[20..24].try_into().unwrap()) != 0x1097_8D4A {
        return false;
    }
    if seg[CLASSIC_PLAYERS_BYTE_OFFSET..CLASSIC_TRIGGER_BYTE_SIZE].iter().any(|&b| b != 0) {
        return false;
    }
    if seg[CLASSIC_COND_TYPE_OFFSET] != 0
        || seg[CLASSIC_ACTS_BYTE_OFFSET + CLASSIC_ACT_TYPE_OFFSET] != 0
    {
        return false;
    }
    u32::from_le_bytes(seg[24..28].try_into().unwrap()) != 0
}

/// Port of `_preprocess_inline_code`
/// (`src/eudkit/maprw/inlinecode/ilcprocesstrig.py`).
///
/// Two modes, selected by `wired_func_ids` (see below):
///
/// - **Inlined** (live-default, `_consecutive_inline_trig_section`): normal
///   template triggers are grouped per executing player and replaced by
///   dispatcher triggers (`_create_inline_code_dispatcher`); a blank
///   `Trigger(players=[AllPlayers])` is appended unconditionally. The
///   compiled inline-code bodies live in the LIR payload already (captured
///   from the live pipeline), so the tail only reproduces the TRIG-section
///   rewriting — the func_id assignment order is what must match live.
/// - **Kept** (P6-D10 rate-0 fallback): triggers are kept verbatim. This is
///   the behavior-preserving mapping when the payload carries no inline
///   bodies.
///
/// `wired_func_ids` is the set of inline-code func_ids the LIR payload's
/// finalizer dispatch wiring references (see `dispatch_wired_func_ids`).
/// The tail inlines iff the template generates dispatchers for func_ids
/// ALL present in that set; otherwise it keeps. Rationale: synthesizing a
/// dispatcher without its body would silently drop the trigger's behavior
/// (silent miscompile). Keeping is the pipeline's own
/// `PRT_SetInliningRate(0)` configuration — live, public, working.
///
/// The RNG stream is untouched: the ported code draws nothing (the live
/// rate-1.0 path, `_consecutive_inline_trig_section`, makes no random
/// draws; the finalizer's `v = random.randint(234, 65535)` already ran in
/// the seam that produced the LIR).
///
/// Loud (out-of-contract) inputs, never silently wrong:
/// - a TRIG section whose length is not a multiple of 2400:
///   `MapTailError::MalformedTrig` (Python silently drops the tail).
///
/// A trigger carrying valid compiled inline code (magic + structure) is
/// opaque to the pure path: it cannot `exec` the embedded source, and
/// rewriting or dropping it would silently change the template. Such a
/// template is preserved verbatim — every trigger unchanged plus the
/// unconditional trailing blank trigger — and reported as
/// [`InlineMode::Kept`]. This is the behavior-preserving mapping for
/// basemaps produced by eudplib that exercised inline code.
///
/// Returns whether the template was inlined or kept, so the caller can
/// report accurately.
pub fn preprocess_inline_code(
    chkt: &mut Chk,
    wired_func_ids: &HashSet<u32>,
) -> Result<InlineMode, MapTailError> {
    let trig = chkt
        .getsection(b"TRIG")
        .ok_or(MapTailError::MissingSection("TRIG"))?
        .to_vec();
    if trig.len() % CLASSIC_TRIGGER_BYTE_SIZE != 0 {
        return Err(MapTailError::MalformedTrig(trig.len()));
    }
    let forc: [u8; 8] = chkt
        .getsection(b"FORC")
        .and_then(|s| s.get(0..8))
        .map(|s| s.try_into().unwrap())
        .unwrap_or([1, 2, 3, 4, 1, 2, 3, 4]);
    let plan = match plan_inline_trig(&trig, &forc)? {
        InlinePlan::Preserve => {
            let mut out = trig;
            out.extend_from_slice(&encode_trigger(&[ALL_PLAYERS], &[], &[]));
            chkt.setsection(*b"TRIG", out);
            return Ok(InlineMode::Kept);
        }
        InlinePlan::Rewrite(plan) => plan,
    };
    let inline_ids: Vec<u32> = plan
        .iter()
        .filter_map(|s| match s {
            PlanSeg::Dispatch { func_id, .. } => Some(*func_id),
            _ => None,
        })
        .collect();
    let can_inline =
        !inline_ids.is_empty() && inline_ids.iter().all(|id| wired_func_ids.contains(id));
    let mut out = Vec::with_capacity(trig.len() + CLASSIC_TRIGGER_BYTE_SIZE);
    if can_inline {
        for seg in &plan {
            match seg {
                PlanSeg::Keep(t) => out.extend_from_slice(t),
                PlanSeg::Dispatch {
                    func_id,
                    player_code,
                } => out.extend_from_slice(&encode_dispatcher(*func_id, *player_code)),
                PlanSeg::Blank => {
                    out.extend_from_slice(&encode_trigger(&[ALL_PLAYERS], &[], &[]))
                }
            }
        }
        chkt.setsection(*b"TRIG", out);
        Ok(InlineMode::Inlined)
    } else {
        // Keep: rate-0 semantics (see doc comment above).
        for chunk in trig.chunks_exact(CLASSIC_TRIGGER_BYTE_SIZE) {
            out.extend_from_slice(chunk);
        }
        out.extend_from_slice(&encode_trigger(&[ALL_PLAYERS], &[], &[]));
        chkt.setsection(*b"TRIG", out);
        Ok(InlineMode::Kept)
    }
}

/// Whether `preprocess_inline_code` inlined the template's triggers
/// (live-default) or kept them verbatim (rate-0 fallback).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum InlineMode {
    Inlined,
    Kept,
}

/// How `initialize_payload` treats the basemap's authored classic-trigger
/// flags when it rebuilds the `TRIG` section.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum AuthoredTriggerFlags {
    /// The payload carries no stage-3 dispatch wiring, so nothing flips the
    /// stage-1 disabled bit back at runtime. Keep every authored flag dword
    /// verbatim; only the synthetic trailing blank is toggled.
    Preserve,
    /// The payload carries stage-3 wiring: its runtime flip restores every
    /// chain trigger, including synthesized dispatchers. Toggle every old
    /// trigger in stage 1 as eudplib does.
    ToggleWithStage1,
}

/// One output segment of the inline plan.
enum PlanSeg {
    /// Template trigger kept as-is.
    Keep([u8; CLASSIC_TRIGGER_BYTE_SIZE]),
    /// Dispatcher trigger to synthesize.
    Dispatch { func_id: u32, player_code: u32 },
    /// The unconditional trailing blank trigger.
    Blank,
}

/// Outcome of planning the template's TRIG rewrite.
enum InlinePlan {
    /// The template already carries valid compiled inline code: preserve
    /// every trigger verbatim (plus the blank terminator).
    Preserve,
    /// Rewrite normal triggers into per-player dispatchers.
    Rewrite(Vec<PlanSeg>),
}

/// Port of `_consecutive_inline_trig_section`'s TRIG-rewriting decisions
/// (not the body compilation — bodies arrive via the LIR payload).
///
/// Returns the output segment sequence, or a loud error for malformed
/// input. A valid compiled inline-code trigger short-circuits to
/// [`InlinePlan::Preserve`]: the pure path cannot `exec` its embedded
/// source, so the whole template is kept verbatim. `forc` is the map's
/// FORC section player→force bytes.
fn plan_inline_trig(trig: &[u8], forc: &[u8; 8]) -> Result<InlinePlan, MapTailError> {
    let mut out: Vec<PlanSeg> = Vec::new();
    let mut ptriggers: [Vec<[u8; CLASSIC_TRIGGER_BYTE_SIZE]>; 8] = Default::default();
    let mut next_func_id: u32 = 1024; // `_create_inline_code_dispatcher`

    for chunk in trig.chunks_exact(CLASSIC_TRIGGER_BYTE_SIZE) {
        let seg: [u8; CLASSIC_TRIGGER_BYTE_SIZE] = chunk
            .try_into()
            .expect("chunks_exact yields a classic trigger");
        if is_inline_code_trigger(&seg) {
            return Ok(InlinePlan::Preserve);
        }
        let propv = u32::from_le_bytes(
            seg[CLASSIC_FLAG_BYTE_OFFSET..CLASSIC_FLAG_BYTE_OFFSET + 4]
                .try_into()
                .unwrap(),
        );
        let executing = get_executing_players(&seg, forc);
        if propv < 0x8000_0000 {
            let player_count = executing.iter().filter(|&&b| b).count();
            let mut flushed_shared = false;
            if player_count >= 2 && !preserved_and_has_no_wait(&seg) {
                // `try_share_trigger` declined (not preserved or has
                // Wait/Transmission): the bytes branch with the size check.
                let size = get_trigger_size(&seg);
                if (size * player_count) as f64 * CUTOFF_RATE[player_count]
                    > CLASSIC_TRIGGER_BYTE_SIZE as f64
                {
                    for p in 0..8 {
                        if executing[p] {
                            flush_player(p, &mut ptriggers, &mut out, &mut next_func_id);
                        }
                    }
                    out.push(PlanSeg::Keep(seg));
                    flushed_shared = true;
                }
            }
            if !flushed_shared {
                for p in 0..8 {
                    if executing[p] {
                        ptriggers[p].push(seg);
                    }
                }
            }
        } else {
            for p in 0..8 {
                if executing[p] {
                    flush_player(p, &mut ptriggers, &mut out, &mut next_func_id);
                }
            }
            out.push(PlanSeg::Keep(seg));
        }
    }
    for p in 0..8 {
        flush_player(p, &mut ptriggers, &mut out, &mut next_func_id);
    }
    out.push(PlanSeg::Blank);
    Ok(InlinePlan::Rewrite(out))
}

/// `append_ptriggers(p)`: compile the player's queued triggers into one
/// dispatcher (body compilation is the seam's job — the func_id sequence
/// is what the tail must reproduce).
fn flush_player(
    p: usize,
    ptriggers: &mut [Vec<[u8; CLASSIC_TRIGGER_BYTE_SIZE]>; 8],
    out: &mut Vec<PlanSeg>,
    next_func_id: &mut u32,
) {
    if !ptriggers[p].is_empty() {
        let func_id = *next_func_id;
        *next_func_id += 1;
        out.push(PlanSeg::Dispatch {
            func_id,
            player_code: 1 << p,
        });
        ptriggers[p].clear();
    }
}

/// Port of `_create_inline_code_dispatcher` (ilcprocesstrig.py).
fn encode_dispatcher(func_id: u32, player_code: u32) -> [u8; CLASSIC_TRIGGER_BYTE_SIZE] {
    let players: Vec<usize> = (0..27)
        .filter(|p| player_code & (1u32 << p) != 0)
        .collect();
    // Four SetDeaths(0, SetTo, 0, 0); the func_id then replaces cond[0]'s
    // first dword (the dispatcher keys on its condition area).
    let acts = [CTrigAction::set_deaths(0, SET_TO, 0, 0); 4];
    let mut out = ClassicTrigger::from_parts(&players, &[], &acts, 0x1000_0000)
        .expect("four actions fit a classic trigger")
        .into_bytes();
    out[0..4].copy_from_slice(&func_id.to_le_bytes());
    out
}

/// Port of `get_executing_players` (btinliner.py). `forc` maps player
/// 0..8 to its force number (FORC section bytes).
fn get_executing_players(seg: &[u8; CLASSIC_TRIGGER_BYTE_SIZE], forc: &[u8; 8]) -> [bool; 8] {
    if seg[CLASSIC_PLAYERS_BYTE_OFFSET + 17] != 0 {
        return [true; 8];
    }
    let mut out = [false; 8];
    for p in 0..8 {
        let direct = seg[CLASSIC_PLAYERS_BYTE_OFFSET + p] != 0;
        let via_force = (18 + forc[p] as usize) < 28
            && seg[CLASSIC_PLAYERS_BYTE_OFFSET + 18 + forc[p] as usize] != 0;
        out[p] = direct || via_force;
    }
    out
}

/// Port of `_preserved_and_has_no_wait` (btinliner.py).
fn preserved_and_has_no_wait(seg: &[u8; CLASSIC_TRIGGER_BYTE_SIZE]) -> bool {
    let mut exec_once = true;
    for i in 0..64 {
        let actionflag = seg[CLASSIC_ACTS_BYTE_OFFSET + CLASSIC_ACT_SIZE * i + CLASSIC_ACT_FLAG_OFFSET];
        if actionflag & 0b10 == 0 {
            continue; // disabled action
        }
        match seg[CLASSIC_ACTS_BYTE_OFFSET + CLASSIC_ACT_SIZE * i + CLASSIC_ACT_TYPE_OFFSET] {
            4 | 7 => return false, // Wait or Transmission
            3 => exec_once = false, // PreserveTrigger
            _ => {}
        }
    }
    if exec_once && seg[CLASSIC_FLAG_BYTE_OFFSET] & 4 == 0 {
        return false;
    }
    true
}

/// Port of `_count_conditions_and_actions` + `get_trigger_size`
/// (btinliner.py): the compactness estimate deciding shared-vs-inline.
fn get_trigger_size(seg: &[u8; CLASSIC_TRIGGER_BYTE_SIZE]) -> usize {
    let mut cond_count = 0usize;
    for c in 0..16 {
        let t = seg[c * CLASSIC_COND_SIZE + CLASSIC_COND_TYPE_OFFSET];
        if t == 22 {
            continue; // Always
        } else if t >= 1 {
            cond_count += 1;
        }
    }
    let mut act_count = 0usize;
    for a in 0..64 {
        let t = seg[CLASSIC_ACTS_BYTE_OFFSET + a * CLASSIC_ACT_SIZE + CLASSIC_ACT_TYPE_OFFSET];
        if t == 47 {
            continue; // Comment
        } else if t >= 1 {
            act_count += 1;
        }
    }
    let min_size = 4 + 5 * cond_count + 8 * act_count;
    // Slot layout, Python dict insertion order: n, P, C, c, A, a.
    let mut slots: Vec<usize> = Vec::new();
    slots.push(1); // "n"
    slots.push(594); // "P"
    slots.extend(2..2 + 5 * cond_count); // "C"
    if cond_count != 16 {
        slots.push(5 + 5 * cond_count); // "c"
    }
    slots.extend(82..82 + 8 * act_count); // "A"
    if act_count != 64 {
        slots.push(88 + 8 * act_count); // "a"
    }
    for ret in min_size..2408 / 4 {
        let mut output = vec![false; ret];
        let mut ok = true;
        for &x in &slots {
            if output[x % ret] {
                ok = false;
                break;
            }
            output[x % ret] = true;
        }
        if ok {
            return ret * 4;
        }
    }
    2408
}

/// `_cutoff_rate[player_count]` (ilcprocesstrig.py):
/// `[1 + (i - 2) / 3 for i in range(9)]`.
const CUTOFF_RATE: [f64; 9] = [
    1.0 - 2.0 / 3.0,
    1.0 - 1.0 / 3.0,
    1.0,
    1.0 + 1.0 / 3.0,
    1.0 + 2.0 / 3.0,
    2.0,
    2.0 + 1.0 / 3.0,
    2.0 + 2.0 / 3.0,
    3.0,
];

/// Func_ids the LIR payload's finalizer dispatch wiring references.
///
/// The live `_dispatch_inline_code` emits, per inline-code func, a trigger
/// with a `Deaths(CurrentPlayer, Exactly, func_id, v)` condition. In LIR
/// cond params (`IIIHBBBBH`): `[_, 13, func_id, _, 10, 15, _, _, _]`
/// (13 = CurrentPlayer, 10 = Exactly, 15 = Deaths). Collecting the
/// referenced func_ids lets the tail prove the payload carries the inline
/// bodies before it dares to synthesize dispatchers for them.
pub fn dispatch_wired_func_ids(doc: &eudkit_lir::LirDocument) -> HashSet<u32> {
    let mut ids = HashSet::new();
    for item in &doc.module.items {
        let eudkit_lir::LirItem::Trigger(t) = item else {
            continue;
        };
        // Full wiring shape: the per-func trigger from `_dispatch_inline_code`
        // has exactly 1 cond (the Deaths) and 7 SetDeaths actions. The act
        // count distinguishes real wiring from a coincidental user Deaths
        // (e.g. strfunc's Deaths(CurrentPlayer, Exactly, 0x10000000, _) with
        // 2 actions — a false positive without this check).
        if t.conds.len() != 1 || t.acts.len() != 7 {
            continue;
        }
        let c = &t.conds[0];
        if c.cond.condtype == 15 && c.cond.player == 13 && c.cond.comparison == 10 {
            let fid = c.cond.amount;
            if (1024..=0xFFFF_FFFF).contains(&fid) {
                ids.insert(fid);
            }
        }
    }
    ids
}

/// Port of `initialize_payload(chkt, payload, mrgndata)`.
///
/// - `payload_data`: the payload bytes (copied; fixups applied in place).
/// - `fixups`: the payload's fixup tables as one borrowed view
///   (`PayloadOut::fixups`); `prt` entries are EPD fixups, `ort`
///   entries are byte-offset fixups.
/// - `table`: the built string table; it is STRx by construction
///   (`save_tbl` always writes width 4), so no section-name parameter
///   is needed — the old `str_name` parameter was always `"STRx"`
///   (mirroring `LoadMap`, which sets `strsection = "STRx"`
///   unconditionally) and only invited mislabeling.
/// - `mrgndata`: 5100 obfuscation bytes, or `None` to skip MRGN work.
/// - `gate_entry_bytes`: payload-relative byte offset of the once-per-tick
///   gate (`tick_gate::append_tick_gate`): the stage-1 chain target is
///   `payload_offset + gate_entry_bytes`, so the first chain pass of each
///   tick enters the gate, not the program's first item. Pass 0 to keep
///   the legacy behavior (stage-1 targets the payload start).
/// - `authored_flags`: whether to preserve the basemap's authored classic
///   flags ([`AuthoredTriggerFlags::Preserve`]) or apply eudplib's stage-1
///   toggle to every old trigger ([`AuthoredTriggerFlags::ToggleWithStage1`],
///   required when the payload carries stage-3 dispatch wiring). `Preserve`
///   treats the final `TRIG` record as the synthetic blank appended by
///   `preprocess_inline_code`; all preceding records are authored.
///
/// The `TRIG` rebuild brackets the authored chain with the
/// [`RunnerBoundary`] pair (ADR 0010): stage 1 -> begin -> authored ->
/// end -> synthetic blank.
///
/// Call `preprocess_inline_code` on the template `Chk` first (mirrors
/// `build()`/`SaveMap`, which run `_preprocess_inline_code` before the
/// injector tail).
///
/// Returns [`payload_base`] of the table's length (the TRIG rebuild
/// needs it; tests assert it too).
pub fn initialize_payload(
    chkt: &mut Chk,
    payload_data: &mut [u8],
    fixups: PayloadFixups<'_>,
    table: &BuiltStringTable,
    mrgndata: Option<&[u8]>,
    gate_entry_bytes: usize,
    authored_flags: AuthoredTriggerFlags,
) -> Result<i64, MapTailError> {
    let str_tbl = &table.bytes;
    let str_padding = (4 - str_tbl.len() % 4) % 4;
    let payload_offset = payload_base(str_tbl.len());

    for &prt in fixups.prt {
        let v = u32::from_le_bytes(
            payload_data[prt..prt + 4]
                .try_into()
                .map_err(|_| MapTailError::FixupOob(prt))?,
        );
        let v = v.wrapping_add((payload_offset / 4) as u32);
        payload_data[prt..prt + 4].copy_from_slice(&v.to_le_bytes());
    }
    for &ort in fixups.ort {
        let v = u32::from_le_bytes(
            payload_data[ort..ort + 4]
                .try_into()
                .map_err(|_| MapTailError::FixupOob(ort))?,
        );
        let v = v.wrapping_add(payload_offset as u32);
        payload_data[ort..ort + 4].copy_from_slice(&v.to_le_bytes());
    }

    let mut str_section = Vec::with_capacity(str_tbl.len() + str_padding + payload_data.len());
    str_section.extend_from_slice(str_tbl);
    str_section.extend(std::iter::repeat(0u8).take(str_padding));
    str_section.extend_from_slice(payload_data);
    // The table is STRx by construction; see the `table` parameter doc.
    chkt.setsection(
        section_name("STRx").map_err(MapTailError::SectionName)?,
        str_section,
    );

    if let Some(mrgndata) = mrgndata {
        let mrgn_name = section_name("MRGN").map_err(MapTailError::SectionName)?;
        let orig_mrgn = chkt
            .getsection(&mrgn_name)
            .ok_or(MapTailError::MissingSection("MRGN"))?
            .to_vec();
        // A trailing partial record cannot be obfuscated entry-wise:
        // the view below floors it, so make it loud here, consistent
        // with the tbl/mapinfo MRGN length validators.
        checked_records(Some(&orig_mrgn), "MRGN", MRGN_RECORD_SIZE)?;
        let mut mrgn_section = Vec::with_capacity(orig_mrgn.len());
        let mut i = 0usize;
        while i < orig_mrgn.len() {
            // b2i4/b2i2 are *unsigned* reads; i2b4 masks to 32 bits.
            // The special case is at i % 20 == 16, i.e. record+16:
            // it reads record bytes +18/+19 (the ELEVATION half) and
            // constructs (elevation << 16) with the string-id half
            // deliberately zeroed ("remove name") — NOT record+16's
            // strid. Changing i+2 to i would break the pinned output.
            let orig: u64 = if i % MRGN_RECORD_SIZE == MRGN_RECORD_SIZE - 4 {
                // remove name
                let entry = MrgnEntry::from_record(&orig_mrgn[i - 16..])
                    .expect("length validated above");
                (entry.elevation() as u64) << 16
            } else {
                u32::from_le_bytes(
                    orig_mrgn[i..i + 4]
                        .try_into()
                        .map_err(|_| MapTailError::MrgnOob(i))?,
                ) as u64
            };
            let obfus = u32::from_le_bytes(
                mrgndata
                    .get(i..i + 4)
                    .ok_or(MapTailError::MrgnDataShort(i))?
                    .try_into()
                    .unwrap(),
            ) as u64;
            let orig = if obfus > orig { orig + (1 << 32) } else { orig };
            mrgn_section.extend_from_slice(&((orig - obfus) as u32).to_le_bytes());
            i += 4;
        }
        chkt.setsection(mrgn_name, mrgn_section);
    }

    // TRIG section rebuild (ADR 0010): stage 1 -> runner begin -> authored
    // classic triggers -> runner end -> synthetic blank. The runner pair
    // sits outside the byte-parity stage-1 block so `stage1_trigs_py.bin`
    // stays identical. The stage-1 block itself is unchanged; only its
    // chain target moves from the payload start to the tick-gate entry
    // (same builder, one differing value field in the final trigger).
    let trigdata = build_stage1_triggers(payload_offset + gate_entry_bytes as i64);
    let begin_trigger = RunnerBoundary::Begin.trigger_bytes();
    let end_trigger = RunnerBoundary::End.trigger_bytes();
    let trig_name = section_name("TRIG").map_err(MapTailError::SectionName)?;
    let oldtrigraw = chkt
        .getsection(&trig_name)
        .ok_or(MapTailError::MissingSection("TRIG"))?
        .to_vec();
    let old_count = oldtrigraw.chunks(CLASSIC_TRIGGER_BYTE_SIZE).len();
    let mut new_trig = Vec::with_capacity(
        trigdata.len() + 2 * CLASSIC_TRIGGER_BYTE_SIZE + oldtrigraw.len(),
    );
    new_trig.extend_from_slice(&trigdata);
    new_trig.extend_from_slice(&begin_trigger);
    // `preprocess_inline_code` appends exactly one synthetic blank as the
    // final record on every path; everything before it is authored. With
    // no stage-3 wiring (Preserve), authored flags are the author's state
    // and stay verbatim; only that synthetic terminator keeps the stage-1
    // toggle. With wiring (ToggleWithStage1), the payload's runtime flip
    // restores the whole chain, so every old trigger is toggled as eudplib
    // stage 1 does.
    for (idx, old) in oldtrigraw.chunks(CLASSIC_TRIGGER_BYTE_SIZE).enumerate() {
        let synthetic = idx + 1 == old_count;
        // The runner end goes after the last authored record and before
        // the synthetic blank, so the blank remains the final chain node
        // that stage 1 links the payload after.
        if synthetic {
            new_trig.extend_from_slice(&end_trigger);
        }
        let preserve =
            authored_flags == AuthoredTriggerFlags::Preserve && !synthetic;
        let mut trig = old.to_vec();
        if !preserve {
            // Toggle the "disabled" flag (bit 3) at 320 + 2048 for every old
            // trigger — mirrors the Python loop exactly (`if flag & 8: pass`
            // is a no-op; the xor applies unconditionally).
            if let Some(mut classic) = ClassicTrigger::from_bytes(old) {
                classic.set_flag(classic.flag() ^ 8);
                trig = classic.into_bytes().to_vec();
            } else if trig.len() >= CLASSIC_PLAYERS_BYTE_OFFSET {
                // Short trailing chunk: the old guard toggles only when
                // the flag dword is present; preserved verbatim.
                let flag = u32::from_le_bytes(
                    trig[CLASSIC_FLAG_BYTE_OFFSET..CLASSIC_FLAG_BYTE_OFFSET + 4]
                        .try_into()
                        .unwrap(),
                );
                trig[CLASSIC_FLAG_BYTE_OFFSET..CLASSIC_FLAG_BYTE_OFFSET + 4]
                    .copy_from_slice(&(flag ^ 8).to_le_bytes());
            }
        }
        new_trig.extend_from_slice(&trig);
    }
    if old_count == 0 {
        // No preprocess blank to bracket: still emit the boundary pair.
        new_trig.extend_from_slice(&end_trigger);
    }
    chkt.setsection(trig_name, new_trig);

    Ok(payload_offset)
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Test helper: `preprocess_inline_code` with no dispatch wiring, i.e.
    /// the P6-D10 keep path (payloads in these unit tests carry no inline
    /// bodies).
    fn preprocess_keep(chkt: &mut Chk) -> Result<InlineMode, MapTailError> {
        preprocess_inline_code(chkt, &HashSet::new())
    }

    #[test]
    fn epd_matches_python() {
        assert_eq!(epd(0x51A284), -114744);
        assert_eq!(epd(0x6509B0), 203155);
        assert_eq!(epd(4), -1452248);
        assert_eq!(epd(0x58A364), 0);
        assert_eq!(epd(0x58A360), -1);
    }

    #[test]
    fn payload_base_lengths_and_padding() {
        // Len 0 → base; len 5 → base + 8 (padding 3); len 4 and 8 →
        // base + len (padding 0).
        assert_eq!(payload_base(0), STR_SECTION_BASE);
        assert_eq!(payload_base(5), STR_SECTION_BASE + 8);
        assert_eq!(payload_base(4), STR_SECTION_BASE + 4);
        assert_eq!(payload_base(8), STR_SECTION_BASE + 8);
    }

    #[test]
    fn trigger_count_and_size() {
        let t = build_stage1_triggers(0x1920_0000);
        assert_eq!(t.len(), 70 * 2400);
    }

    #[test]
    fn stage1_triggers_byte_identical_to_python() {
        // Fixture generated by replaying payload_init's exact trigger list
        // in Python (see tests/fixtures/stage1_trigs_py.bin; payload_offset
        // = 0x191943C8 + 100). Byte-exact match proves the classic-2400
        // encodings (Action/Condition/Trigger/SetMemory/SetDeaths/DeathsX)
        // are faithful.
        let want =
            std::fs::read(concat!(env!("CARGO_MANIFEST_DIR"), "/tests/fixtures/stage1_trigs_py.bin"))
                .unwrap();
        let got = build_stage1_triggers(0x1919_43C8 + 100);
        assert_eq!(got.len(), want.len());
        for (i, (g, w)) in got.iter().zip(want.iter()).enumerate() {
            assert_eq!(g, w, "byte {i} differs");
        }
    }

    #[test]
    fn setdeaths_encoding_spot() {
        // SetDeaths(9, SetTo, triggerend, 0) for player 0:
        // Action(0,0,0,0, 9, ~(0x51A284), 0, 45, 7, 20).
        let a = set_deaths(9, SET_TO, !(0x51A284i64), 0);
        assert_eq!(a.mask, 0);
        assert_eq!(a.strid, 0);
        assert_eq!(a.wavid, 0);
        assert_eq!(a.time, 0);
        assert_eq!(a.dest, 9);
        assert_eq!(a.value, !(0x51A284i64) as u32);
        assert_eq!(a.unit, 0);
        assert_eq!(a.acttype, 45);
        assert_eq!(a.modifier, 7); // amount slot
        assert_eq!(a.flags, 20);
        assert_eq!(a.eudx, 0);
        // The typed record encodes to the documented byte layout.
        let bytes = a.encode();
        assert_eq!(&bytes[0..16], &[0u8; 16]);
        assert_eq!(u32::from_le_bytes(bytes[16..20].try_into().unwrap()), 9);
        assert_eq!(bytes[26], 45);
        assert_eq!(bytes[27], 7);
        assert_eq!(bytes[28], 20);
    }

    #[test]
    fn deathsx_has_sc_maskflag() {
        let c = deaths_x(CURRENT_PLAYER, 1, 1 << 5);
        assert_eq!(c.eudx.to_le_bytes(), *b"SC");
        assert_eq!(c.condtype, 15);
        assert_eq!(c.locid, 32);
        assert_eq!(c.player, CURRENT_PLAYER as u32);
        assert_eq!(c.comparison, AT_LEAST);
        let bytes = c.encode();
        assert_eq!(&bytes[18..20], b"SC");
        assert_eq!(bytes[15], 15);
    }

    #[test]
    fn mrgn_obfuscation_matches_python() {
        // One 20-byte location; i%20==16 reads the name field.
        let mut chkt = Chk::new();
        let mut mrgn = vec![0u8; 20];
        mrgn[0..4].copy_from_slice(&0x11223344u32.to_le_bytes());
        mrgn[18..20].copy_from_slice(&0x5566u16.to_le_bytes());
        chkt.setsection(*b"MRGN", mrgn);
        chkt.setsection(*b"TRIG", vec![0u8; 2400]);
        let mut payload = vec![0u8; 8];
        let mrgndata = vec![0xABu8; 5100];
        initialize_payload(
            &mut chkt,
            &mut payload,
            PayloadFixups::empty(),
            &BuiltStringTable {
                bytes: vec![],
                offsets: None,
            },
            Some(&mrgndata),
            0,
            AuthoredTriggerFlags::ToggleWithStage1,
        )
        .unwrap();
        let out = chkt.getsection(b"MRGN").unwrap();
        // i=0: orig=0x11223344, obfus=0xABABABAB > orig → orig += 2^32.
        let want0 = (0x11223344u64 + (1 << 32) - 0xABABABAB) as u32;
        assert_eq!(u32::from_le_bytes(out[0..4].try_into().unwrap()), want0);
        // i=16: orig = u16 at [18..20] << 16 = 0x55660000; obfus > orig.
        let want16 = (0x55660000u64 + (1 << 32) - 0xABABABAB) as u32;
        assert_eq!(
            u32::from_le_bytes(out[16..20].try_into().unwrap()),
            want16
        );
    }

    #[test]
    fn mrgn_partial_record_is_loud_at_initialize_payload() {
        // Same probe fixture as the tbl test: 38-byte MRGN with a
        // strid in the partial entry's +16/+17 bytes. The %20 check
        // at the obfuscation block makes it loud instead of letting
        // the view loop silently floor the partial record.
        let mut chkt = Chk::new();
        let mut mrgn = vec![0u8; 38];
        mrgn[36..38].copy_from_slice(&3u16.to_le_bytes());
        chkt.setsection(*b"MRGN", mrgn);
        chkt.setsection(*b"TRIG", vec![0u8; 2400]);
        let mut payload = vec![0u8; 8];
        let mrgndata = vec![0xABu8; 5100];
        assert!(matches!(
            initialize_payload(
                &mut chkt,
                &mut payload,
                PayloadFixups::empty(),
                &BuiltStringTable {
                    bytes: vec![],
                    offsets: None,
                },
                Some(&mrgndata),
                0,
                AuthoredTriggerFlags::ToggleWithStage1,
            ),
            Err(MapTailError::TruncatedSection("MRGN"))
        ));
    }

    #[test]
    fn str_padding_and_offset() {
        let mut chkt = Chk::new();
        chkt.setsection(*b"TRIG", vec![0u8; 2400]);
        let mut payload = vec![0x11u8; 4];
        // str_tbl len 5 → padding 3; offset = 0x191943C8 + 5 + 3.
        let off = initialize_payload(
            &mut chkt,
            &mut payload,
            PayloadFixups::empty(),
            &BuiltStringTable {
                bytes: vec![1, 2, 3, 4, 5],
                offsets: None,
            },
            None,
            0,
            AuthoredTriggerFlags::ToggleWithStage1,
        )
        .unwrap();
        assert_eq!(off, 0x1919_43C8 + 5 + 3);
        let s = chkt.getsection(b"STRx").unwrap();
        assert_eq!(s.len(), 5 + 3 + 4);
        assert_eq!(&s[0..5], &[1, 2, 3, 4, 5]);
        assert_eq!(&s[5..8], &[0, 0, 0]);
        assert_eq!(&s[8..12], &[0x11, 0x11, 0x11, 0x11]);
    }

    #[test]
    fn prt_ort_fixups() {
        let mut chkt = Chk::new();
        chkt.setsection(*b"TRIG", vec![0u8; 2400]);
        let mut payload = vec![0u8; 16];
        payload[0..4].copy_from_slice(&100u32.to_le_bytes());
        payload[8..12].copy_from_slice(&200u32.to_le_bytes());
        // Discriminating case (d)(iii), via the view: prt and ort
        // values differ, and the view must keep their meanings
        // apart — prt adjusted by payload_offset/4, ort by the full
        // payload_offset.
        let out = eudkit_lir::PayloadOut {
            data: vec![],
            prttable: vec![0],
            orttable: vec![8],
            relocs: vec![],
            addrs: vec![],
        };
        let off = initialize_payload(
            &mut chkt,
            &mut payload,
            out.fixups(),
            &BuiltStringTable {
                bytes: vec![],
                offsets: None,
            },
            None,
            0,
            AuthoredTriggerFlags::ToggleWithStage1,
        )
        .unwrap();
        assert_eq!(
            u32::from_le_bytes(payload[0..4].try_into().unwrap()),
            100u32.wrapping_add((off / 4) as u32)
        );
        assert_eq!(
            u32::from_le_bytes(payload[8..12].try_into().unwrap()),
            200u32.wrapping_add(off as u32)
        );
    }

    fn blank_trigger_fixture() -> Vec<u8> {
        std::fs::read(concat!(
            env!("CARGO_MANIFEST_DIR"),
            "/tests/fixtures/blank_trigger_py.bin"
        ))
        .unwrap()
    }

    #[test]
    fn blank_trigger_byte_identical_to_python() {
        // `tt.Trigger(players=[AllPlayers])` as encoded by trigtrg.py
        // (see tests/fixtures/blank_trigger_py.bin).
        let want = blank_trigger_fixture();
        let got = encode_trigger(&[ALL_PLAYERS], &[], &[]);
        assert_eq!(got.len(), 2400);
        assert_eq!(got.as_slice(), want.as_slice());
    }

    #[test]
    fn tick_gate_present_in_strx_and_stage1_targets_it() {
        use crate::tick_gate::{
            LASTTIME_FIELD_OFFSET, LASTTIME_INIT, TICK_EPD, append_tick_gate, fixture_doc,
        };
        use eudkit_lir::{CONDS_BYTE_OFFSET, RAW_TRIGGER_BYTE_SIZE, read_u32_le};
        let mut doc = fixture_doc();
        let info = append_tick_gate(&mut doc);
        let payload = eudkit_lir::lir2payload(&doc).unwrap();
        let mut payload_data = payload.data.clone();
        let mut chkt = Chk::new();
        chkt.setsection(*b"TRIG", vec![0u8; CLASSIC_TRIGGER_BYTE_SIZE]);
        let off = initialize_payload(
            &mut chkt,
            &mut payload_data,
            payload.fixups(),
            &BuiltStringTable {
                bytes: vec![],
                offsets: None,
            },
            None,
            info.gate_byte_offset,
            AuthoredTriggerFlags::ToggleWithStage1,
        )
        .unwrap();
        // Empty string table: payload starts at the STR section base.
        assert_eq!(off, STR_SECTION_BASE);
        // The gate sits after the program's single trigger.
        assert_eq!(info.gate_byte_offset, RAW_TRIGGER_BYTE_SIZE);
        // STRx = table + padding + payload; the branch trigger's condition
        // at the gate offset reads the tick EPD against the all-ones
        // lasttime init.
        let strx = chkt.getsection(b"STRx").unwrap();
        let b = info.gate_byte_offset + CONDS_BYTE_OFFSET;
        assert_eq!(read_u32_le(strx, b + 4), TICK_EPD);
        assert_eq!(
            read_u32_le(strx, info.gate_byte_offset + LASTTIME_FIELD_OFFSET),
            LASTTIME_INIT
        );
        assert_eq!(strx[b + 15], 15);
        // TRIG = 70 stage-1 + runner begin + runner end + the kept
        // template trigger (the synthetic blank). The final stage-1
        // trigger's first action writes the payload entry into the current
        // player's chain: it must target the gate, not the program's first
        // item.
        let trig = chkt.getsection(b"TRIG").unwrap();
        assert_eq!(
            trig.len(),
            70 * CLASSIC_TRIGGER_BYTE_SIZE
                + 2 * CLASSIC_TRIGGER_BYTE_SIZE
                + CLASSIC_TRIGGER_BYTE_SIZE
        );
        let last_stage1 = 69 * CLASSIC_TRIGGER_BYTE_SIZE;
        let act0 = last_stage1 + 320;
        assert_eq!(
            u32::from_le_bytes(trig[act0 + 16..act0 + 20].try_into().unwrap()),
            13 // CURRENT_PLAYER
        );
        assert_eq!(
            u32::from_le_bytes(trig[act0 + 20..act0 + 24].try_into().unwrap()),
            (off + info.gate_byte_offset as i64) as u32
        );
    }

    #[test]
    fn preprocess_appends_blank_trigger_on_empty_trig() {
        // Regression (Tier-1 Bug 2): the blank template's TRIG is empty,
        // and `_preprocess_inline_code` unconditionally appends the blank
        // trigger — without this step `initialize_payload` has no old
        // triggers to preserve.
        let mut chkt = Chk::new();
        chkt.setsection(*b"TRIG", vec![]);
        preprocess_keep(&mut chkt).unwrap();
        assert_eq!(
            chkt.getsection(b"TRIG").unwrap(),
            blank_trigger_fixture().as_slice()
        );
    }

    /// A synthetic normal trigger: Always + SetDeaths, player 0, flag 0.
    fn normal_trigger(player0: bool) -> Vec<u8> {
        let conds = [CTrigCondition::always()];
        let acts = [set_deaths(0, SET_TO, 1, 0)];
        let mut t = encode_trigger(&[], &conds, &acts);
        // flag dword 0 (like a StarEdit-saved trigger), peff player 0.
        t[2368..2372].copy_from_slice(&0u32.to_le_bytes());
        if player0 {
            t[2372] = 1;
        }
        t
    }

    /// A synthetic compiled-inline-code trigger: magic at byte 20 with
    /// valid `GetInlineCodePlayerList` structure (peff all zero,
    /// cond[0].condtype == 0, act[0].acttype == 0, player code at 24).
    fn magic_trigger(player_code: u32, act0_type: u8) -> Vec<u8> {
        let mut t = encode_trigger(&[], &[], &[]);
        t[20..24].copy_from_slice(&0x1097_8D4Au32.to_le_bytes());
        t[24..28].copy_from_slice(&player_code.to_le_bytes());
        t[346] = act0_type; // act[0].acttype
        t
    }

    #[test]
    fn preprocess_keeps_normal_triggers_verbatim() {
        // T1b (P6-D10): normal template triggers are kept verbatim
        // (rate-0 semantics), in order, with the blank trigger appended.
        let t1 = normal_trigger(true);
        let t2 = normal_trigger(false);
        let mut chkt = Chk::new();
        chkt.setsection(*b"TRIG", [t1.clone(), t2.clone()].concat());
        preprocess_keep(&mut chkt).unwrap();
        let trig = chkt.getsection(b"TRIG").unwrap();
        assert_eq!(trig.len(), 3 * 2400);
        assert_eq!(&trig[0..2400], t1.as_slice());
        assert_eq!(&trig[2400..4800], t2.as_slice());
        assert_eq!(&trig[4800..7200], blank_trigger_fixture().as_slice());
    }

    #[test]
    fn preprocess_preserves_inline_code_magic() {
        // A trigger Python would compile (magic + valid structure) is
        // opaque to the pure path: the whole template is kept verbatim
        // instead of being recompiled or dropped. This is the basemap
        // migration path (issue #41).
        let normal = normal_trigger(true);
        let magic = magic_trigger(1, 0);
        let mut chkt = Chk::new();
        chkt.setsection(*b"TRIG", [normal.clone(), magic.clone()].concat());
        // Even with the would-be dispatcher func_id wired, the magic
        // trigger forces the keep path so normal triggers are not
        // partially rewritten into dispatchers.
        let wired: HashSet<u32> = [1024].into_iter().collect();
        let mode = preprocess_inline_code(&mut chkt, &wired).unwrap();
        assert_eq!(mode, InlineMode::Kept);
        let trig = chkt.getsection(b"TRIG").unwrap();
        assert_eq!(trig.len(), 3 * CLASSIC_TRIGGER_BYTE_SIZE);
        assert_eq!(&trig[0..2400], normal.as_slice());
        assert_eq!(&trig[2400..4800], magic.as_slice());
        assert_eq!(&trig[4800..7200], blank_trigger_fixture().as_slice());

        // Missing TRIG is loud too (mirrors Python's KeyError).
        let mut chkt = Chk::new();
        assert!(matches!(
            preprocess_keep(&mut chkt),
            Err(MapTailError::MissingSection("TRIG"))
        ));
    }

    #[test]
    fn preprocess_keeps_magic_with_broken_structure() {
        // Faithful fallthrough: Python's `_dispatch_inline_code` returns
        // None (→ normal handling) when the structural checks fail, even
        // with the magic present. act[0].acttype != 0 here.
        let mut chkt = Chk::new();
        let m = magic_trigger(1, 9);
        chkt.setsection(*b"TRIG", m.clone());
        preprocess_keep(&mut chkt).unwrap();
        let trig = chkt.getsection(b"TRIG").unwrap();
        assert_eq!(&trig[0..2400], m.as_slice());
        // player_code == 0 also falls through (`if not player_code`).
        let mut chkt = Chk::new();
        let m = magic_trigger(0, 0);
        chkt.setsection(*b"TRIG", m.clone());
        preprocess_keep(&mut chkt).unwrap();
        assert_eq!(&chkt.getsection(b"TRIG").unwrap()[0..2400], m.as_slice());
        // Nonzero effplayer bytes also fail GetInlineCodePlayerList.
        let mut chkt = Chk::new();
        let mut m = magic_trigger(3, 0);
        m[2372] = 1;
        chkt.setsection(*b"TRIG", m.clone());
        preprocess_keep(&mut chkt).unwrap();
        assert_eq!(&chkt.getsection(b"TRIG").unwrap()[0..2400], m.as_slice());
    }

    fn dispatcher_fixture() -> Vec<u8> {
        std::fs::read(concat!(
            env!("CARGO_MANIFEST_DIR"),
            "/tests/fixtures/dispatcher_py.bin"
        ))
        .unwrap()
    }

    #[test]
    fn dispatcher_byte_identical_to_python() {
        // `_create_inline_code_dispatcher` as encoded by ilcprocesstrig.py
        // (see tests/fixtures/dispatcher_py.bin): two dispatchers,
        // func_id 1024 player_code 1 and func_id 1025 player_code 0b101.
        let want = dispatcher_fixture();
        assert_eq!(want.len(), 2 * 2400);
        let d0 = encode_dispatcher(1024, 1);
        assert_eq!(d0.as_slice(), &want[0..2400]);
        let d1 = encode_dispatcher(1025, 0b101);
        assert_eq!(d1.as_slice(), &want[2400..4800]);
    }

    #[test]
    fn preprocess_inlines_when_wiring_present() {
        // P6-D35: with dispatch wiring for func_id 1024 in the LIR, the
        // template's normal trigger becomes a dispatcher (live-default),
        // not a kept trigger.
        let t = normal_trigger(true);
        let mut chkt = Chk::new();
        chkt.setsection(*b"TRIG", t.clone());
        // FORC section needed for get_executing_players (player 0 force 1).
        chkt.setsection(*b"FORC", vec![1, 2, 3, 4, 1, 2, 3, 4]);
        let mut wired = HashSet::new();
        wired.insert(1024u32);
        let mode = preprocess_inline_code(&mut chkt, &wired).unwrap();
        assert_eq!(mode, InlineMode::Inlined);
        let trig = chkt.getsection(b"TRIG").unwrap();
        assert_eq!(trig.len(), 2 * 2400);
        assert_eq!(&trig[0..2400], &dispatcher_fixture()[0..2400]);
        assert_eq!(&trig[2400..4800], blank_trigger_fixture().as_slice());
    }

    #[test]
    fn preprocess_keeps_when_wiring_absent() {
        // Same template, but the LIR has no dispatch wiring: keep
        // (P6-D10 rate-0) — synthesizing a dispatcher without its body
        // would silently drop the trigger.
        let t = normal_trigger(true);
        let mut chkt = Chk::new();
        chkt.setsection(*b"TRIG", t.clone());
        chkt.setsection(*b"FORC", vec![1, 2, 3, 4, 1, 2, 3, 4]);
        let mode = preprocess_inline_code(&mut chkt, &HashSet::new()).unwrap();
        assert_eq!(mode, InlineMode::Kept);
        let trig = chkt.getsection(b"TRIG").unwrap();
        assert_eq!(trig.len(), 2 * 2400);
        assert_eq!(&trig[0..2400], t.as_slice());
        assert_eq!(&trig[2400..4800], blank_trigger_fixture().as_slice());
    }

    #[test]
    fn preprocess_plan_matches_python_on_basemap_strx() {
        // End-to-end plan check on the real basemap_strx.scx template TRIG
        // (1 normal trigger, player 0): the plan must be exactly
        // [dispatch(1024, player 0), blank], matching the live
        // `_consecutive_inline_trig_section` output validated in Python.
        let forc = [1u8, 2, 3, 4, 1, 2, 3, 4];
        // Reconstruct the template trigger shape: player 0, flag 0.
        let t = normal_trigger(true);
        let seg: [u8; 2400] = t.as_slice().try_into().unwrap();
        assert!(!is_inline_code_trigger(&seg));
        assert_eq!(
            get_executing_players(&seg, &forc),
            [true, false, false, false, false, false, false, false]
        );
        let plan = match plan_inline_trig(t.as_slice(), &forc).unwrap() {
            InlinePlan::Rewrite(plan) => plan,
            InlinePlan::Preserve => panic!("normal trigger must not take the preserve path"),
        };
        assert_eq!(plan.len(), 2);
        match &plan[0] {
            PlanSeg::Dispatch {
                func_id,
                player_code,
            } => {
                assert_eq!(*func_id, 1024);
                assert_eq!(*player_code, 1);
            }
            _ => panic!("expected dispatcher"),
        }
        assert!(matches!(plan[1], PlanSeg::Blank));
    }

    #[test]
    fn preprocess_rejects_malformed_trig() {
        // A TRIG section that is not a multiple of 2400 is malformed.
        // Python silently drops the partial tail; the native tail refuses.
        for len in [1usize, 100, 2401, 4800 + 7] {
            let mut chkt = Chk::new();
            chkt.setsection(*b"TRIG", vec![0u8; len]);
            assert!(
                matches!(
                    preprocess_keep(&mut chkt),
                    Err(MapTailError::MalformedTrig(n)) if n == len
                ),
                "len {len}"
            );
        }
    }

    fn flag(record: &[u8]) -> u32 {
        u32::from_le_bytes(record[2368..2372].try_into().unwrap())
    }

    /// The runner pair's shared encoding: no conditions, one
    /// `SetMemory(0x6509B0, SetTo, 0)` action, enabled + preserved
    /// (`0x04`), executing for `AllPlayers`.
    fn expected_runner_trigger() -> Vec<u8> {
        let acts = [set_memory(CURPL, SET_TO, 0)];
        encode_trigger(&[ALL_PLAYERS], &[], &acts)
    }

    fn assert_runner_trigger(record: &[u8]) {
        assert_eq!(record.len(), CLASSIC_TRIGGER_BYTE_SIZE);
        // No conditions.
        assert_eq!(&record[0..320], &[0u8; 320]);
        // One action: SetDeaths(EPD(CURPL), SetTo, 0, 0).
        let a = &record[320..352];
        assert_eq!(
            u32::from_le_bytes(a[16..20].try_into().unwrap()),
            203155
        );
        assert_eq!(u32::from_le_bytes(a[20..24].try_into().unwrap()), 0);
        assert_eq!(u16::from_le_bytes(a[24..26].try_into().unwrap()), 0);
        assert_eq!(a[26], 45); // acttype: SetDeaths
        assert_eq!(a[27], SET_TO); // modifier
        assert_eq!(a[28], 20); // flags
        // All remaining action slots empty.
        assert_eq!(&record[352..2368], &[0u8; 2368 - 352]);
        // Preserved flag 0x04, all players (peff[17]).
        assert_eq!(flag(record), 0x04);
        assert_eq!(&record[2372..2389], &[0u8; 17]);
        assert_eq!(record[2389], 1); // peff[17] = AllPlayers
        assert_eq!(&record[2390..2400], &[0u8; 10]);
        assert_eq!(record, expected_runner_trigger().as_slice());
    }

    #[test]
    fn runner_pair_encoding_and_placement() {
        // The existing post-stage-1 curpl reset becomes
        // `TrigTriggerBegin`; `TrigTriggerEnd` is inserted before the
        // synthetic blank. Both encode the same boundary contract
        // (ADR 0010).
        let mut chkt = Chk::new();
        chkt.setsection(*b"TRIG", blank_trigger_fixture());
        let mut payload = vec![0u8; 8];
        initialize_payload(
            &mut chkt,
            &mut payload,
            PayloadFixups::empty(),
            &BuiltStringTable {
                bytes: vec![],
                offsets: None,
            },
            None,
            0,
            AuthoredTriggerFlags::ToggleWithStage1,
        )
            .unwrap();
        let trig = chkt.getsection(b"TRIG").unwrap();
        assert_eq!(trig.len(), 73 * 2400);
        let begin = &trig[70 * 2400..71 * 2400];
        let end = &trig[71 * 2400..72 * 2400];
        let blank = &trig[72 * 2400..73 * 2400];
        assert_runner_trigger(begin);
        assert_runner_trigger(end);
        assert_eq!(begin, end, "the pair shares the boundary encoding");
        assert_eq!(flag(blank), 0x0C, "the synthetic blank still toggles");
    }

    #[test]
    fn runner_pair_brackets_authored_chain() {
        // Classic-first ordering: begin -> authored -> end -> blank. The
        // authored record keeps its bytes and flag; the runner pair is
        // synthetic scaffolding (ADR 0010).
        let authored = normal_trigger(true);
        let mut chkt = Chk::new();
        chkt.setsection(*b"TRIG", authored.clone());
        preprocess_keep(&mut chkt).unwrap();
        let mut payload = vec![0u8; 8];
        initialize_payload(
            &mut chkt,
            &mut payload,
            PayloadFixups::empty(),
            &BuiltStringTable {
                bytes: vec![],
                offsets: None,
            },
            None,
            0,
            AuthoredTriggerFlags::Preserve,
        )
            .unwrap();
        let trig = chkt.getsection(b"TRIG").unwrap();
        assert_eq!(trig.len(), 74 * 2400);
        assert_runner_trigger(&trig[70 * 2400..71 * 2400]);
        assert_eq!(
            &trig[71 * 2400..72 * 2400],
            authored.as_slice(),
            "the authored trigger sits between begin and end, byte-identical"
        );
        assert_runner_trigger(&trig[72 * 2400..73 * 2400]);
        assert_eq!(flag(&trig[73 * 2400..74 * 2400]), 0x0C);
    }

    #[test]
    fn runner_pair_is_emitted_without_template_records() {
        // initialize_payload is public and may be called without the
        // preprocess blank. The boundary pair is still emitted so the
        // contract does not depend on a synthetic terminator being present.
        let mut chkt = Chk::new();
        chkt.setsection(*b"TRIG", vec![]);
        let mut payload = vec![0u8; 8];
        initialize_payload(
            &mut chkt,
            &mut payload,
            PayloadFixups::empty(),
            &BuiltStringTable {
                bytes: vec![],
                offsets: None,
            },
            None,
            0,
            AuthoredTriggerFlags::Preserve,
        )
            .unwrap();
        let trig = chkt.getsection(b"TRIG").unwrap();
        assert_eq!(trig.len(), 72 * 2400);
        assert_runner_trigger(&trig[70 * 2400..71 * 2400]);
        assert_runner_trigger(&trig[71 * 2400..72 * 2400]);
    }

    #[test]
    fn authored_flags_preserved_without_dispatch_wiring() {
        // No stage-3 wiring: nothing flips bit 8 back at runtime, so the
        // tail must keep the author's enabled/disabled state verbatim.
        // Only the synthetic trailing blank keeps the stage-1 toggle.
        let enabled = normal_trigger(true); // flag 0x00
        let mut disabled = normal_trigger(true);
        disabled[CLASSIC_FLAG_BYTE_OFFSET..CLASSIC_FLAG_BYTE_OFFSET + 4]
            .copy_from_slice(&0x08u32.to_le_bytes());
        let mut chkt = Chk::new();
        chkt.setsection(*b"TRIG", [enabled.clone(), disabled.clone()].concat());
        preprocess_keep(&mut chkt).unwrap();
        let mut payload = vec![0u8; 8];
        initialize_payload(
            &mut chkt,
            &mut payload,
            PayloadFixups::empty(),
            &BuiltStringTable {
                bytes: vec![],
                offsets: None,
            },
            None,
            0,
            AuthoredTriggerFlags::Preserve,
        )
            .unwrap();
        let trig = chkt.getsection(b"TRIG").unwrap();
        assert_eq!(trig.len(), 75 * 2400);
        assert_eq!(
            &trig[71 * 2400..72 * 2400],
            enabled.as_slice(),
            "an authored enabled trigger must stay byte-identical"
        );
        assert_eq!(
            &trig[72 * 2400..73 * 2400],
            disabled.as_slice(),
            "an authored disabled trigger must stay byte-identical"
        );
        assert_runner_trigger(&trig[73 * 2400..74 * 2400]);
        let last = &trig[74 * 2400..75 * 2400];
        assert_eq!(
            flag(last),
            0x0C,
            "the synthetic blank terminator still toggles"
        );
    }

    #[test]
    fn dispatch_wiring_keeps_stage1_toggle() {
        // The payload carries stage-3 dispatch wiring, so its runtime flip
        // restores every chain trigger. Stage 1 must therefore keep the
        // blanket toggle: the synthesized dispatcher starts at 0x1000_0000
        // (bit 8 clear) and is enabled only by that flip.
        let t = normal_trigger(true);
        let mut chkt = Chk::new();
        chkt.setsection(*b"TRIG", t);
        let wired: HashSet<u32> = [1024u32].into_iter().collect();
        assert_eq!(
            preprocess_inline_code(&mut chkt, &wired).unwrap(),
            InlineMode::Inlined
        );
        let mut payload = vec![0u8; 8];
        initialize_payload(
            &mut chkt,
            &mut payload,
            PayloadFixups::empty(),
            &BuiltStringTable {
                bytes: vec![],
                offsets: None,
            },
            None,
            0,
            AuthoredTriggerFlags::ToggleWithStage1,
        )
            .unwrap();
        let trig = chkt.getsection(b"TRIG").unwrap();
        assert_eq!(trig.len(), 74 * 2400);
        let dispatcher = &trig[71 * 2400..72 * 2400];
        assert_eq!(flag(dispatcher), 0x1000_0008);
        assert_runner_trigger(&trig[72 * 2400..73 * 2400]);
        let last = &trig[73 * 2400..74 * 2400];
        assert_eq!(flag(last), 0x0C);
    }

    #[test]
    fn preserved_authored_flags_keep_the_gate_target() {
        // The tick-gate interaction is independent of the flag policy: the
        // stage-1 chain target stays payload_offset + gate_entry_bytes.
        let t = normal_trigger(true);
        let mut chkt = Chk::new();
        chkt.setsection(*b"TRIG", t);
        preprocess_keep(&mut chkt).unwrap();
        let mut payload = vec![0u8; 8];
        let gate_entry_bytes = 12_345usize;
        let off = initialize_payload(
            &mut chkt,
            &mut payload,
            PayloadFixups::empty(),
            &BuiltStringTable {
                bytes: vec![],
                offsets: None,
            },
            None,
            gate_entry_bytes,
            AuthoredTriggerFlags::Preserve,
        )
            .unwrap();
        let trig = chkt.getsection(b"TRIG").unwrap();
        // Stage-1 trigger 70 (0-based 69) ends the setup block by writing
        // the chain target into CURPL-relative action 0.
        let stage1_last = &trig[69 * 2400..70 * 2400];
        let target = u32::from_le_bytes(stage1_last[320 + 20..320 + 24].try_into().unwrap());
        assert_eq!(target as i64, off + gate_entry_bytes as i64);
        // The authored trigger keeps its flag while the gate target moves.
        assert_eq!(
            u32::from_le_bytes(
                trig[71 * 2400 + 2368..71 * 2400 + 2372]
                    .try_into()
                    .unwrap()
            ),
            0x00
        );
    }

    #[test]
    fn old_trig_preserved_end_to_end() {
        // Full Bug 2 regression: preprocess + initialize_payload on a
        // blank template yields 70 stage-1 triggers, the runner begin/end
        // pair, and the flag-toggled blank trigger (0x04 ^ 0x08 = 0x0C),
        // like the Python tail plus the boundary.
        let mut chkt = Chk::new();
        chkt.setsection(*b"TRIG", vec![]);
        preprocess_keep(&mut chkt).unwrap();
        let mut payload = vec![0u8; 8];
        initialize_payload(
            &mut chkt,
            &mut payload,
            PayloadFixups::empty(),
            &BuiltStringTable {
                bytes: vec![],
                offsets: None,
            },
            None,
            0,
            AuthoredTriggerFlags::Preserve,
        )
            .unwrap();
        let trig = chkt.getsection(b"TRIG").unwrap();
        assert_eq!(trig.len(), 73 * 2400);
        assert_runner_trigger(&trig[70 * 2400..71 * 2400]);
        assert_runner_trigger(&trig[71 * 2400..72 * 2400]);
        let last = &trig[72 * 2400..73 * 2400];
        assert_eq!(flag(last), 0x0C);
        // Un-toggling the flag recovers the exact Python blank trigger.
        let mut untoggled = last.to_vec();
        let f = u32::from_le_bytes(untoggled[2368..2372].try_into().unwrap()) ^ 8;
        untoggled[2368..2372].copy_from_slice(&f.to_le_bytes());
        assert_eq!(untoggled, blank_trigger_fixture());
    }

    #[test]
    fn strx_written_when_template_lacks_it() {
        // Regression (Tier-1 Bug 1): the template may only have "STR "
        // (e.g. basemap_wireframe.scx), but the tail must still write
        // "STRx" — `optimize()` drops anything else, like Python's
        // `chktok.optimize`, which only keeps STRx.
        let mut chkt = Chk::new();
        chkt.setsection(*b"STR ", vec![1, 2, 3]);
        chkt.setsection(*b"TRIG", vec![]);
        let mut payload = vec![0u8; 4];
        initialize_payload(
            &mut chkt,
            &mut payload,
            PayloadFixups::empty(),
            &BuiltStringTable {
                bytes: vec![9, 9],
                offsets: None,
            },
            None,
            0,
            AuthoredTriggerFlags::ToggleWithStage1,
        )
        .unwrap();
        chkt.optimize();
        assert!(chkt.getsection(b"STRx").is_some());
        assert!(chkt.getsection(b"STR ").is_none());
    }
}
