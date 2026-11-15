//! General once-per-tick gate around the compiled payload (issue #39).
//!
//! With two or more active players, the stage-1 injector links the compiled
//! payload into every active player's trigger chain, so the payload executes
//! once per active player per trigger loop. The missing piece is eudplib's
//! stage-3 finalizer gate (`inj_finalizer.py`): a shared check comparing the
//! game tick counter against a stored last tick, so the payload runs once per
//! game tick total.
//!
//! Ground truth (verified against eudplib upstream, not taken on faith):
//!
//! * The tick counter is `0x57F23C` (`f_getgametick`: "Get current game
//!   tick value", read via `f_dwread_epd(EPD(0x57F23C))`; the finalizer
//!   compares it with `Memory(0x57F23C, Exactly, lasttime)`).
//! * `Memory(addr, comp, value)` is exactly `Deaths(EPD(addr), comp, value,
//!   0)` — condtype 15, no separate Memory encoding exists.
//! * Conditional branching in StarCraft works by actions rewriting the
//!   branch trigger's own next pointer (`EUDBranch`: the branch trigger's
//!   next is statically the false target; its actions, which run only when
//!   the conditions hold, patch its next to the true-path trigger, which
//!   restores it). A failed condition does NOT fall through to the next
//!   list item — the next pointer is always followed.
//! * A DWORD copy costs 32 bit triggers (`f_dwepdread_epd`: one
//!   `DeathsX(CP, AtLeast, 1, mask)` + `AddNumber(2**i)` per bit, after a
//!   zeroing write).
//!
//! Gate shape (35 triggers, appended after the program's items):
//!
//! ```text
//! B  (branch): conds [Deaths(tick_epd, Exactly, lasttime)], next = C0,
//!                acts [SetNextPtr(B -> T)]
//! T  (restore): next = CHAIN_END, acts [SetNextPtr(B -> C0)]
//! C0 (clear):  acts [SetDeathsX(lasttime, SetTo, 0, mask = all)]
//! C1..C32:     conds [DeathsX(tick_epd, AtLeast, 1, mask = 2**i)],
//!                acts [SetDeathsX(lasttime, Add, 2**i, mask = 2**i)],
//!                last next = the program's original entry
//! ```
//!
//! Same tick: B's conditions hold, so B's actions patch B.next to T; T
//! restores B.next to C0 and jumps to the chain end — the payload is
//! skipped. New tick: B's conditions fail, B.next stays C0, the copy
//! refreshes lasttime to the current tick, and execution falls into the
//! program's original entry.
//!
//! The lasttime cell is B's own condition amount field (cond slot 0,
//! offset +8). This avoids a memory-vs-memory comparison, which no classic
//! condition expresses, and needs no extra storage dword: the copy actions
//! write the amount field through its EPD, exactly like any other payload
//! cell. The field is initialized to `0xFFFFFFFF`, so the first chain of
//! the first tick always runs (eudplib initializes its separate lasttime
//! variable to 0, which skips the payload when the game tick itself starts
//! at 0; the all-ones init is strictly better and costs nothing).
//!
//! All gate triggers are preserved: the branch and the copy must run on
//! every chain pass, not just once.
//!
//! The gate is optional for maps with exactly one active chain (ADR 0013):
//! [`assemble_map`](crate::assemble_map) appends it only when
//! `TailInputs::assembly.enable_tick_gate` is set, and stage 1 otherwise links
//! directly to the program entry. An ungated payload runs once per trigger
//! loop on that sole chain; a multi-chain map must keep the gate or every
//! active chain runs the payload once per loop.

use eudkit_lir::{
    ActData, CHAIN_END_PTR, CONDS_BYTE_OFFSET, CTrigAction, CTrigCondition, CondData,
    DEST_FIELD_OFFSET, LirDocument, LirItem, RAW_TRIGGER_BYTE_SIZE, RelocData, TriggerItem,
    set_next_ptr_action,
};

/// Absolute address of the StarCraft game tick counter (eudplib
/// `f_getgametick` / stage-3 finalizer).
pub const TICK_ADDR: u32 = 0x57F23C;

/// EPD of [`TICK_ADDR`]: `(0x57F23C - 0x58A364) / 4 = -11338`, as the
/// unsigned 32-bit player field StarCraft reads.
pub const TICK_EPD: u32 = 0xFFFFD3B6;

/// Initial value of the lasttime field: all-ones, so the first chain of the
/// first tick always runs (no tick value a real game produces on its first
/// pass equals this, and the field is refreshed to the live tick on that
/// pass).
pub const LASTTIME_INIT: u32 = 0xFFFF_FFFF;

/// Number of triggers the gate appends: branch + restore + clear + 32 bit
/// copies.
pub const GATE_TRIGGER_COUNT: usize = 35;

/// Number of conditions the gate appends: one branch condition plus one per
/// bit-copy trigger.
pub const GATE_CONDITION_COUNT: usize = 1 + 32;

/// Number of actions the gate appends: branch, restore, clear, plus one per
/// bit-copy trigger.
pub const GATE_ACTION_COUNT: usize = 3 + 32;

/// Byte offset of the lasttime field inside the branch trigger: cond slot 0
/// (`CONDS_BYTE_OFFSET`) plus the amount field (`+8`).
pub const LASTTIME_FIELD_OFFSET: usize = CONDS_BYTE_OFFSET + 8;

/// What [`append_tick_gate`] did, for the map-assembly caller.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct TickGateInfo {
    /// Payload-relative byte offset of the branch trigger: the stage-1
    /// chain target is `payload_offset + gate_byte_offset`.
    pub gate_byte_offset: usize,
}

/// Append the once-per-tick gate to `doc` (see the module docs for the
/// shape) and return where it landed. The program's original items keep
/// their indices; only `doc.module.entry` moves, to the branch trigger.
///
/// The gate's relocs reuse the payload's existing fixup machinery: dest
/// fields that name the branch trigger's own fields use mode 1 (EPD
/// fixups, adjusted by `payload_offset / 4` at map assembly), and next
/// targets use mode 4 (byte-offset fixups, adjusted by `payload_offset`).
/// All reloc targets are 4-aligned by construction (every trigger item is
/// 2408 bytes; blob items are dword multiples), so the EPD round-trip
/// through `epd_u32` is exact.
pub fn append_tick_gate(doc: &mut LirDocument) -> TickGateInfo {
    let orig_len = doc.module.items.len();
    let orig_entry = doc.module.entry;
    let gate_byte_offset: usize = doc.module.items.iter().map(LirItem::size).sum();
    let b = orig_len;
    let t = b + 1;
    let c0 = b + 2;

    // SetNextPtr(branch -> target): the shared branch idiom, resolving
    // both endpoints to payload items.
    let set_next_ptr = |target_item: usize| -> ActData { set_next_ptr_action(b, target_item) };
    // Copy write into the lasttime field (the branch condition's amount).
    let write_lasttime = |modifier: u8, value: u32, mask: u32| -> ActData {
        let mut act = CTrigAction::set_deathsx(0, modifier, value, 0, mask).to_act_data();
        act.relocs = vec![RelocData {
            field_offset: DEST_FIELD_OFFSET,
            target_item: b,
            target_offset: LASTTIME_FIELD_OFFSET,
            mode: 1,
        }];
        act
    };
    let trigger = |prev: i64,
                   prev_reloc: bool,
                   next: i64,
                   next_reloc: bool,
                   conds: Vec<CondData>,
                   acts: Vec<ActData>| {
        TriggerItem {
            size: RAW_TRIGGER_BYTE_SIZE,
            prev: Some(prev),
            next: Some(next),
            prev_reloc,
            next_reloc,
            conds,
            acts,
            preserved: true,
            flag: None,
        }
    };

    let branch = trigger(
        -1,
        false,
        c0 as i64,
        true,
        vec![CTrigCondition::deaths(TICK_EPD, 10, LASTTIME_INIT, 0).to_cond_data()],
        vec![set_next_ptr(t)],
    );
    let restore = trigger(
        b as i64,
        true,
        CHAIN_END_PTR,
        false,
        vec![],
        vec![set_next_ptr(c0)],
    );
    let clear = trigger(
        t as i64,
        true,
        (c0 + 1) as i64,
        true,
        vec![],
        vec![write_lasttime(7, 0, 0xFFFF_FFFF)],
    );
    doc.module.items.push(LirItem::Trigger(branch));
    doc.module.items.push(LirItem::Trigger(restore));
    doc.module.items.push(LirItem::Trigger(clear));
    for i in 0..32usize {
        let idx = c0 + 1 + i;
        let bit = 1u32 << i;
        // Last copy jumps to the program's original entry; with no
        // program items (or a dangling entry) it ends the chain instead.
        let (next, next_reloc) = if i < 31 {
            ((idx + 1) as i64, true)
        } else if orig_entry < orig_len {
            (orig_entry as i64, true)
        } else {
            (CHAIN_END_PTR, false)
        };
        doc.module.items.push(LirItem::Trigger(trigger(
            (idx - 1) as i64,
            true,
            next,
            next_reloc,
            vec![CTrigCondition::deathsx(TICK_EPD, 0, 1, 0, bit).to_cond_data()],
            vec![write_lasttime(8, bit, bit)],
        )));
    }
    doc.module.entry = b;
    TickGateInfo { gate_byte_offset }
}

#[cfg(test)]
pub(crate) fn fixture_doc() -> LirDocument {
    use eudkit_lir::{LIR_VERSION, LirModule};
    LirDocument {
        version: LIR_VERSION.to_string(),
        module: LirModule {
            entry: 0,
            items: vec![LirItem::Trigger(TriggerItem {
                size: RAW_TRIGGER_BYTE_SIZE,
                prev: Some(-1),
                next: Some(CHAIN_END_PTR),
                prev_reloc: false,
                next_reloc: false,
                conds: vec![],
                acts: vec![CTrigAction::set_deaths(9000, 8, 1, 0).to_act_data()],
                preserved: true,
                flag: None,
            })],
            strings: vec![],
            str_addrs: vec![],
        },
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use eudkit_lir::{
        NEXT_BYTE_OFFSET, SimulationState, VALUE_FIELD_OFFSET, init_simulation, lir2payload,
        read_u32_le,
    };

    fn gated_doc() -> (LirDocument, TickGateInfo) {
        let mut doc = fixture_doc();
        let info = append_tick_gate(&mut doc);
        (doc, info)
    }

    fn trigger<'a>(doc: &'a LirDocument, idx: usize) -> &'a TriggerItem {
        match &doc.module.items[idx] {
            LirItem::Trigger(t) => t,
            LirItem::Blob(_) => panic!("item {idx} is a blob, expected a trigger"),
        }
    }

    fn reloc_fields(r: &RelocData) -> (usize, usize, usize, i64) {
        (r.field_offset, r.target_item, r.target_offset, r.mode)
    }

    #[test]
    fn gate_appends_35_items_and_moves_entry_to_branch() {
        let (doc, info) = gated_doc();
        assert_eq!(doc.module.items.len(), 1 + GATE_TRIGGER_COUNT);
        assert_eq!(doc.module.entry, 1);
        // The program's single 2408-byte trigger precedes the gate.
        assert_eq!(info.gate_byte_offset, RAW_TRIGGER_BYTE_SIZE);
        for (i, item) in doc.module.items.iter().enumerate().skip(1) {
            assert_eq!(item.size(), RAW_TRIGGER_BYTE_SIZE, "gate item {i} size");
            assert!(
                trigger(&doc, i).preserved,
                "gate item {i} must be preserved"
            );
        }
    }

    #[test]
    fn branch_trigger_shape() {
        let (doc, _) = gated_doc();
        let b = doc.module.entry;
        let t = b + 1;
        let c0 = b + 2;
        let br = trigger(&doc, b);
        assert_eq!(br.prev, Some(-1));
        assert!(!br.prev_reloc);
        assert_eq!(br.next, Some(c0 as i64));
        assert!(br.next_reloc);
        assert_eq!(br.conds.len(), 1);
        let cond = &br.conds[0];
        assert_eq!(cond.cond, CTrigCondition::deaths(TICK_EPD, 10, LASTTIME_INIT, 0));
        assert!(cond.relocs.is_empty());
        assert_eq!(br.acts.len(), 1);
        let act = &br.acts[0];
        assert_eq!(act.act.acttype, 45);
        assert_eq!(act.act, CTrigAction::set_deaths(0, 7, 0, 0));
        assert_eq!(act.relocs.len(), 2);
        assert_eq!(
            reloc_fields(&act.relocs[0]),
            (DEST_FIELD_OFFSET, b, NEXT_BYTE_OFFSET, 1)
        );
        assert_eq!(reloc_fields(&act.relocs[1]), (VALUE_FIELD_OFFSET, t, 0, 4));
    }

    #[test]
    fn restore_trigger_shape() {
        let (doc, _) = gated_doc();
        let b = doc.module.entry;
        let t = b + 1;
        let c0 = b + 2;
        let rt = trigger(&doc, t);
        assert_eq!(rt.prev, Some(b as i64));
        assert!(rt.prev_reloc);
        assert_eq!(rt.next, Some(CHAIN_END_PTR));
        assert!(!rt.next_reloc);
        assert!(rt.conds.is_empty());
        assert_eq!(rt.acts.len(), 1);
        let act = &rt.acts[0];
        assert_eq!(act.act.acttype, 45);
        assert_eq!(act.act, CTrigAction::set_deaths(0, 7, 0, 0));
        assert_eq!(act.relocs.len(), 2);
        assert_eq!(
            reloc_fields(&act.relocs[0]),
            (DEST_FIELD_OFFSET, b, NEXT_BYTE_OFFSET, 1)
        );
        assert_eq!(reloc_fields(&act.relocs[1]), (VALUE_FIELD_OFFSET, c0, 0, 4));
    }

    #[test]
    fn copy_triggers_shape() {
        let (doc, _) = gated_doc();
        let b = doc.module.entry;
        let c0 = b + 2;
        let clear = trigger(&doc, c0);
        assert_eq!(clear.prev, Some((b + 1) as i64));
        assert!(clear.prev_reloc);
        assert_eq!(clear.next, Some((c0 + 1) as i64));
        assert!(clear.next_reloc);
        assert!(clear.conds.is_empty());
        assert_eq!(clear.acts.len(), 1);
        let act = &clear.acts[0];
        assert_eq!(
            act.act,
            CTrigAction::set_deathsx(0, 7, 0, 0, 0xFFFF_FFFF)
        );
        assert_eq!(act.relocs.len(), 1);
        assert_eq!(
            reloc_fields(&act.relocs[0]),
            (DEST_FIELD_OFFSET, b, LASTTIME_FIELD_OFFSET, 1)
        );
        for i in 0..32usize {
            let idx = c0 + 1 + i;
            let bit = 1u32 << i;
            let tr = trigger(&doc, idx);
            assert_eq!(tr.prev, Some((idx - 1) as i64), "copy {i} prev");
            assert!(tr.prev_reloc, "copy {i} prev_reloc");
            if i < 31 {
                assert_eq!(tr.next, Some((idx + 1) as i64), "copy {i} next");
                assert!(tr.next_reloc, "copy {i} next_reloc");
            } else {
                // Last copy jumps to the program's original entry (item 0).
                assert_eq!(tr.next, Some(0), "last copy next");
                assert!(tr.next_reloc, "last copy next_reloc");
            }
            assert_eq!(tr.conds.len(), 1, "copy {i} conds");
            let cond = &tr.conds[0];
            assert_eq!(
                cond.cond,
                CTrigCondition::deathsx(TICK_EPD, 0, 1, 0, bit),
                "copy {i} cond record"
            );
            assert!(cond.relocs.is_empty(), "copy {i} cond relocs");
            assert_eq!(tr.acts.len(), 1, "copy {i} acts");
            let act = &tr.acts[0];
            assert_eq!(
                act.act,
                CTrigAction::set_deathsx(0, 8, bit, 0, bit),
                "copy {i} act record"
            );
            assert_eq!(act.relocs.len(), 1, "copy {i} act relocs");
            assert_eq!(
                reloc_fields(&act.relocs[0]),
                (DEST_FIELD_OFFSET, b, LASTTIME_FIELD_OFFSET, 1),
                "copy {i} act reloc"
            );
        }
    }

    #[test]
    fn empty_doc_gate_runs_and_ends_without_a_program() {
        use eudkit_lir::{LIR_VERSION, LirModule};
        let mut doc = LirDocument {
            version: LIR_VERSION.to_string(),
            module: LirModule {
                entry: 0,
                items: vec![],
                strings: vec![],
                str_addrs: vec![],
            },
        };
        let info = append_tick_gate(&mut doc);
        assert_eq!(doc.module.items.len(), GATE_TRIGGER_COUNT);
        assert_eq!(doc.module.entry, 0);
        assert_eq!(info.gate_byte_offset, 0);
        // With no program, the last copy ends the chain instead of jumping.
        let last = trigger(&doc, GATE_TRIGGER_COUNT - 1);
        assert_eq!(last.next, Some(CHAIN_END_PTR));
        assert!(!last.next_reloc);
        let payload = lir2payload(&doc).unwrap();
        let mut sim = init_simulation(&doc).unwrap();
        assert_eq!(payload.addrs.len(), GATE_TRIGGER_COUNT);
        sim.external_epds.insert(TICK_EPD, 5);
        sim.step_loop();
        // Branch + clear + 32 copies; the restore trigger is not visited
        // on the run path.
        let mut want: Vec<usize> = vec![0, 2];
        want.extend(3..GATE_TRIGGER_COUNT);
        assert_eq!(sim.last_executed_cost.visited_items, want);
        // Second pass on the same tick skips everything but branch+restore.
        sim.step_loop();
        assert_eq!(sim.last_executed_cost.visited_items, vec![0, 1]);
    }

    #[test]
    fn sim_same_tick_skips_and_new_tick_runs() {
        let (doc, _) = gated_doc();
        let b = doc.module.entry;
        let mut sim = init_simulation(&doc).unwrap();
        let branch_bytes = |sim: &SimulationState| {
            let base = sim.payload.addrs[b] as usize;
            read_u32_le(&sim.payload.data, base + LASTTIME_FIELD_OFFSET)
        };
        // Tick 5, first pass: run path (branch + copy + program).
        sim.external_epds.insert(TICK_EPD, 5);
        sim.step_loop();
        let mut want: Vec<usize> = vec![b, b + 2];
        want.extend(b + 3..b + GATE_TRIGGER_COUNT);
        want.push(0);
        assert_eq!(sim.last_executed_cost.visited_items, want);
        assert_eq!(sim.external_epds.get(&9000), Some(&1));
        assert_eq!(branch_bytes(&sim), 5);
        // Same tick again: skip path (branch + restore only).
        sim.step_loop();
        assert_eq!(sim.last_executed_cost.visited_items, vec![b, b + 1]);
        assert_eq!(sim.external_epds.get(&9000), Some(&1));
        assert_eq!(branch_bytes(&sim), 5);
        // New tick: run path again, program act executes a second time.
        sim.external_epds.insert(TICK_EPD, 6);
        sim.step_loop();
        assert_eq!(sim.last_executed_cost.visited_items, want);
        assert_eq!(sim.external_epds.get(&9000), Some(&2));
        assert_eq!(branch_bytes(&sim), 6);
    }
}
