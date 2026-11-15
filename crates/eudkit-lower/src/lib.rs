//! HIR 3.0 → LIR 3.0 lowering and `.scx` map compilation for `eudkit`.
//!
//! `eudkit-lower`:
//! 1. Runs `eudkit-opt` to validate `Trigger (16C, 64A)` bounds (`EUDSL-BOUNDS`)
//!    and bin-pack adjacent unconditional top-level actions up to 64 per `Trigger`.
//! 2. Lowers every `HirTrigger` 1-to-1 into a `2,408`-byte (`602`-EPD)
//!    `TriggerItem` in `LirDocument`, wiring the runner chain (`prev`/`next`
//!    and `SetNextPtr` / `MemView` relocations) and interning strings.
//! 3. Integrates with `eudkit-map` (`compile_hir_to_map`) to emit a valid
//!    StarCraft `.scx` map with `TRIG` and `STRx` sections.

use std::collections::{BTreeSet, HashMap};
use std::path::Path;

use eudkit_hir::{
    HirAct, HirCall, HirCallArg, HirCond, HirDocument, HirError, HirFunc, HirModule, HirReloc,
    HirStmt, HirTriggerPtr, HirVarBatch, HirVarTransfer, TriggerRefs, hir_from_json,
};
use eudkit_lir::{
    ACT_BYTE_SIZE, ACT_COUNT, ACTS_BYTE_OFFSET, ActData, BlobItem, BlobRelocData, CHAIN_END_PTR,
    CTrigAction, CondData, DEST_FIELD_OFFSET, EUDX_MAGIC, LIR_VERSION, LirDocument, LirError,
    LirItem, LirModule, MASK_FIELD_OFFSET, MODIFIER_DWORD_OFFSET, NEXT_BYTE_OFFSET,
    RAW_TRIGGER_BYTE_SIZE, RelocData, StaticCost, TriggerItem, VALUE_FIELD_OFFSET,
    act_field_byte_offset, b64_encode,
};
use eudkit_map::{MapTailError, TailInputs, assemble_map, basemap::BasemapContext};
pub use eudkit_map::AssemblyOptions;
use eudkit_map::stage::Stage;
use eudkit_opt::cell::{
    StorageClass, StoragePlan, analyze_storage, bit_mask, is_value_field_offset,
};
use eudkit_opt::transfer::{TransferDest, TransferPlan, plan_var_batch, plan_var_store};
use eudkit_opt::optimize_module;
use thiserror::Error;

pub(crate) mod control;

#[derive(Debug, Error)]
pub enum LowerError {
    #[error(transparent)]
    Hir(#[from] HirError),
    #[error(transparent)]
    Lir(#[from] LirError),
    #[error(transparent)]
    Map(#[from] MapTailError),
    #[error("Unknown symbolic trigger ID {0} referenced in relocation or pointer")]
    UnknownTriggerId(usize),
    #[error("Unknown symbolic variable ID {0} referenced in relocation or transfer")]
    UnknownVarId(usize),
    #[error("Unknown callable subroutine ID {0} referenced by a call")]
    UnknownFuncId(usize),
    #[error("Unknown symbolic blob ID {0} referenced in relocation")]
    UnknownBlobId(usize),
    #[error(
        "BitCell variable {0} in a condition/action with multiple variable relocations cannot be rewritten to a masked field"
    )]
    MultiRelocBitCell(usize),
    #[error(
        "Cell-backed variable {0} referenced at non-value field offset {1}; only the value field may be accessed"
    )]
    CellField(usize, usize),
    #[error("Continuation target {0} is past the last executable trigger ({1})")]
    InvalidContinuation(usize, usize),
    #[error("End-of-chain relocation is not valid in a trigger condition")]
    ChainEndInCondition,
    #[error("EUDSL-BOUNDS: VarStore must set exactly one of dst_addr or dst_trigger")]
    InvalidVarStore,
    #[error("EUDSL-BOUNDS: module string index {0} has no STR offset")]
    MissingStringOffset(usize),
    #[error("HIR statement kind {0} has no lowering yet")]
    UnsupportedStmt(&'static str),
}

/// StarCraft string-table base address (`STR_TABLE_BASE + section offset`).
pub const STR_TABLE_BASE: u32 = 0x1919_43C8;

/// Resolve a module string index to an absolute byte address.
///
/// With real STR offsets (map compile) the address is
/// `STR_TABLE_BASE + str_addrs[template_count + index] + offset`. Generic
/// lowering uses deterministic synthetic addresses (`0x2000_0000` range)
/// so raw-builder HIR can still be lowered and simulated.
fn module_string_addr(
    index: usize,
    offset: usize,
    template_count: usize,
    str_addrs: Option<&[u32]>,
) -> Result<u32, LowerError> {
    if let Some(addrs) = str_addrs {
        let off = addrs
            .get(template_count + index)
            .copied()
            .ok_or(LowerError::MissingStringOffset(index))?;
        return Ok(STR_TABLE_BASE
            .wrapping_add(off)
            .wrapping_add(offset as u32));
    }
    Ok(0x2000_0000u32
        .wrapping_add((index as u32).wrapping_mul(0x10000))
        .wrapping_add(offset as u32))
}

#[derive(Debug, Clone)]
enum ExecRelocTarget {
    HirTrigger { id: usize, offset: usize },
    Var { var_id: usize, slot_offset: usize },
    Page { page_idx: usize, offset: usize },
    ExecItem { exec_idx: usize, offset: usize },
    /// Exit (return) trigger of subroutine `func_id`; `offset` is the field
    /// inside it, e.g. `NEXT_BYTE_OFFSET` to patch its return address.
    FuncExit { func_id: usize, offset: usize },
    /// End of the current trigger chain.
    ChainEnd,
    Blob { blob_idx: usize, offset: usize },
}

#[derive(Debug, Clone)]
struct ExecReloc {
    field_offset: usize,
    target: ExecRelocTarget,
    mode: i64,
}

#[derive(Debug, Clone, Default)]
pub(crate) struct ExecAct {
    pub(crate) ctrig: CTrigAction,
    pub(crate) text: Option<String>,
    pub(crate) wav: Option<String>,
    pub(crate) uprp: Option<[u8; 20]>,
    pub(crate) str_index: Option<usize>,
    pub(crate) relocs: Vec<ExecReloc>,
    /// String relocations resolved directly into action fields (absolute
    /// address/EPD) rather than into `RelocData`.
    pub(crate) string_relocs: Vec<HirReloc>,
}

impl ExecAct {
    pub(crate) fn new(ctrig: CTrigAction, relocs: Vec<ExecReloc>) -> Self {
        Self {
            ctrig,
            relocs,
            ..Default::default()
        }
    }

    #[cfg(test)]
    pub(crate) fn from_ctrig(ctrig: CTrigAction) -> Self {
        Self::new(ctrig, Vec::new())
    }
}

#[derive(Debug, Clone)]
enum ExecNext {
    DefaultChain,
    Hir(HirTriggerPtr),
    Page(usize),
    ExecItem(usize),
    /// Jump into subroutine `func_id`'s entry trigger.
    Func(usize),
    /// Terminate the chain (the subroutine region follows the main chain).
    ChainEnd,
}

#[derive(Debug, Clone)]
struct ExecTrigger {
    hir_id: Option<usize>,
    conds: Vec<HirCond>,
    acts: Vec<ExecAct>,
    preserved: bool,
    flags: u32,
    prev: Option<HirTriggerPtr>,
    next: ExecNext,
}

fn convert_hir_reloc(r: &HirReloc) -> Result<ExecReloc, LowerError> {
    if let Some(var_id) = r.target_var {
        Ok(ExecReloc {
            field_offset: r.field_offset,
            target: ExecRelocTarget::Var {
                var_id,
                slot_offset: r.target_offset,
            },
            mode: r.mode,
        })
    } else if let Some(trig_id) = r.target_trigger {
        Ok(ExecReloc {
            field_offset: r.field_offset,
            target: ExecRelocTarget::HirTrigger {
                id: trig_id,
                offset: r.target_offset,
            },
            mode: r.mode,
        })
    } else if let Some(blob_idx) = r.target_blob {
        Ok(ExecReloc {
            field_offset: r.field_offset,
            target: ExecRelocTarget::Blob {
                blob_idx,
                offset: r.target_offset,
            },
            mode: r.mode,
        })
    } else {
        Err(LowerError::UnknownTriggerId(usize::MAX))
    }
}

fn convert_hir_act(a: &HirAct) -> Result<ExecAct, LowerError> {
    let mut relocs = Vec::with_capacity(a.relocs.len());
    let mut string_relocs = Vec::new();
    for r in &a.relocs {
        if r.target_string.is_some() {
            string_relocs.push(r.clone());
        } else {
            relocs.push(convert_hir_reloc(r)?);
        }
    }
    Ok(ExecAct {
        ctrig: a.to_ctrig_action(),
        text: a.text.clone(),
        wav: a.wav.clone(),
        uprp: a.uprp,
        str_index: a.str_index,
        relocs,
        string_relocs,
    })
}

/// Redirect a destination page slot's self-write to an unused field in its
/// own action (`wavid` at `+8`) for the duration of one page firing.
///
/// SC:R snapshots each trigger's action fields when the trigger starts. A
/// transfer into slot `dst` writes the new value into `dst.value`, but `dst`'s
/// own self-write would then restore the *snapshot* (old) value, erasing the
/// transfer whenever the source slot runs first. Parking redirects `dst.dest`
/// to `dst.wavid` (`+8`), which `SetDeathsX` ignores, so the self-write lands
/// in an inert field and the transfer survives regardless of slot order.
///
/// Setup and restore are unconditional parts of every firing: the setup
/// trigger re-parks before the pages run and the continuation trigger restores
/// the self-targeted invariant (`dest = EPD(value)`) before the next loop, on
/// every pass. `parked_dsts` only deduplicates multiple transfers into the
/// same destination slot within one firing; it does not make parking one-shot.
/// The engine assumption is that `wavid` has no meaning for `SetDeathsX`; if
/// that changes, parking must move to another unused action field.
fn make_park_dest_act(dst_var: usize) -> ExecAct {
    ExecAct::new(
        CTrigAction::set_deathsx(0, 7, 0, 0, 0xFFFF_FFFF),
        vec![
            ExecReloc {
                field_offset: DEST_FIELD_OFFSET,
                target: ExecRelocTarget::Var {
                    var_id: dst_var,
                    slot_offset: 16, // .dest
                },
                mode: 1,
            },
            ExecReloc {
                field_offset: VALUE_FIELD_OFFSET,
                target: ExecRelocTarget::Var {
                    var_id: dst_var,
                    slot_offset: 8, // .wavid (unused by SetDeathsX)
                },
                mode: 1,
            },
        ],
    )
}

fn make_patch_modifier_act(src_var: usize, modifier: u8) -> ExecAct {
    ExecAct::new(
        CTrigAction::set_deathsx(0, 7, (modifier as u32) << 24, 0, 0xFF00_0000),
        vec![ExecReloc {
            field_offset: DEST_FIELD_OFFSET,
            target: ExecRelocTarget::Var {
                var_id: src_var,
                slot_offset: MODIFIER_DWORD_OFFSET, // aligned dword containing unit (+24), acttype (+26), modifier (+27)
            },
            mode: 1,
        }],
    )
}

fn make_patch_mask_act(src_var: usize, target_mask: u32) -> ExecAct {
    ExecAct::new(
        CTrigAction::set_deathsx(0, 7, target_mask, 0, 0xFFFF_FFFF),
        vec![ExecReloc {
            field_offset: DEST_FIELD_OFFSET,
            target: ExecRelocTarget::Var {
                var_id: src_var,
                slot_offset: MASK_FIELD_OFFSET, // .mask at offset +0
            },
            mode: 1,
        }],
    )
}

fn make_page_set_next_ptr_act(page_idx: usize, next_target: ExecRelocTarget) -> ExecAct {
    ExecAct::new(
        CTrigAction::set_deaths(0, 7, 0, 0),
        vec![
            ExecReloc {
                field_offset: DEST_FIELD_OFFSET,
                target: ExecRelocTarget::Page {
                    page_idx,
                    offset: NEXT_BYTE_OFFSET,
                },
                mode: 1,
            },
            ExecReloc {
                field_offset: VALUE_FIELD_OFFSET,
                target: next_target,
                mode: 4,
            },
        ],
    )
}

/// Lower a [`HirDocument`] into a [`LirDocument`] (`3.0`).
pub fn lower_document(doc: &HirDocument) -> Result<LirDocument, LowerError> {
    doc.validate()?;
    lower_module(&doc.module)
}

/// Lower a [`HirModule`] into a [`LirDocument`] (`3.0`) without basemap template strings.
pub fn lower_module(module: &HirModule) -> Result<LirDocument, LowerError> {
    lower_module_with_template_strings(module, &[])
}

/// Lower a [`HirModule`] into a [`LirDocument`] (`3.0`), seeding the string table
/// with `template_strings_b64` when strings are interned against a basemap.
///
/// This is the "optimize then lower" wrapper for external callers and tests.
/// The compile pipeline optimizes once in [`prepare_lowering`] and calls
/// [`lower_optimized_module`] directly, so no pass re-runs `eudkit-opt`.
pub fn lower_module_with_template_strings(
    module: &HirModule,
    template_strings_b64: &[String],
) -> Result<LirDocument, LowerError> {
    let optimized = optimize_module(module)?;
    let storage = analyze_storage(&optimized);
    lower_optimized_module(&optimized, &storage, template_strings_b64)
}

/// Lower with the final basemap STR offset table. `str_addrs[i]` is the
/// section-relative offset of game string id `i + 1` (0-based index).
///
/// Optimize-then-lower wrapper, like [`lower_module_with_template_strings`].
pub fn lower_module_with_string_addrs(
    module: &HirModule,
    template_strings_b64: &[String],
    str_addrs: &[u32],
) -> Result<LirDocument, LowerError> {
    let optimized = optimize_module(module)?;
    let storage = analyze_storage(&optimized);
    lower_optimized_module_with_string_addrs(&optimized, &storage, template_strings_b64, str_addrs)
}

/// Lower an already-optimized module against basemap template strings.
///
/// `storage` must be `analyze_storage(optimized)`. The caller owns the
/// once-per-compile optimize/storage pair ([`prepare_lowering`]); lowering
/// itself never re-runs either.
pub fn lower_optimized_module(
    optimized: &HirModule,
    storage: &StoragePlan,
    template_strings_b64: &[String],
) -> Result<LirDocument, LowerError> {
    let plan = LoweringPlan::build(optimized, storage)?;
    lower_optimized_module_with_plan(optimized, storage, &plan, template_strings_b64, None)
}

/// [`lower_optimized_module`] with the final basemap STR offset table.
pub fn lower_optimized_module_with_string_addrs(
    optimized: &HirModule,
    storage: &StoragePlan,
    template_strings_b64: &[String],
    str_addrs: &[u32],
) -> Result<LirDocument, LowerError> {
    let plan = LoweringPlan::build(optimized, storage)?;
    lower_optimized_module_with_plan(
        optimized,
        storage,
        &plan,
        template_strings_b64,
        Some(str_addrs),
    )
}

/// Lower from an already-built [`LoweringPlan`].
///
/// The compile pipeline builds the plan once, uses it for
/// [`collect_strings`], and then lowers items here — no second control-flow
/// flatten. `str_addrs` is `None` when the module is lowered without a
/// basemap (no STR addressing).
pub fn lower_optimized_module_with_plan(
    optimized: &HirModule,
    storage: &StoragePlan,
    plan: &LoweringPlan,
    template_strings_b64: &[String],
    str_addrs: Option<&[u32]>,
) -> Result<LirDocument, LowerError> {
    lower_optimized_module_inner(
        optimized,
        storage,
        plan,
        template_strings_b64,
        str_addrs,
    )
}

fn flush_cur_acts(cur: &mut Vec<ExecAct>, must_emit: &mut bool, out: &mut Vec<ExecTrigger>) {
    if cur.is_empty() && !*must_emit {
        return;
    }
    if cur.is_empty() {
        out.push(ExecTrigger {
            hir_id: None,
            conds: Vec::new(),
            acts: Vec::new(),
            preserved: true,
            flags: 4,
            prev: None,
            next: ExecNext::DefaultChain,
        });
    } else {
        for chunk in cur.chunks(ACT_COUNT) {
            out.push(ExecTrigger {
                hir_id: None,
                conds: Vec::new(),
                acts: chunk.to_vec(),
                preserved: true,
                flags: 4,
                prev: None,
                next: ExecNext::DefaultChain,
            });
        }
        cur.clear();
    }
    *must_emit = false;
}

fn make_patch_dest_for_plan(src_var: usize, dst: TransferDest) -> ExecAct {
    let mut relocs = vec![ExecReloc {
        field_offset: DEST_FIELD_OFFSET,
        target: ExecRelocTarget::Var {
            var_id: src_var,
            slot_offset: DEST_FIELD_OFFSET,
        },
        mode: 1,
    }];
    let value = match dst {
        TransferDest::Var(dst_var) => {
            relocs.push(ExecReloc {
                field_offset: VALUE_FIELD_OFFSET,
                target: ExecRelocTarget::Var {
                    var_id: dst_var,
                    slot_offset: VALUE_FIELD_OFFSET,
                },
                mode: 1,
            });
            0
        }
        TransferDest::TriggerField { trigger_id, offset } => {
            relocs.push(ExecReloc {
                field_offset: VALUE_FIELD_OFFSET,
                target: ExecRelocTarget::HirTrigger {
                    id: trigger_id,
                    offset,
                },
                mode: 1,
            });
            0
        }
        TransferDest::AbsAddr(addr) => eudkit_lir::epd_u32(addr),
    };
    ExecAct::new(
        CTrigAction::set_deathsx(0, 7, value, 0, 0xFFFF_FFFF),
        relocs,
    )
}

fn emit_transfer_plans(
    plans: &[TransferPlan],
    var_to_page_slot: &HashMap<usize, (usize, usize)>,
    cur: &mut Vec<ExecAct>,
    must_emit: &mut bool,
    out: &mut Vec<ExecTrigger>,
) -> Result<(), LowerError> {
    if plans.is_empty() {
        return Ok(());
    }
    let mut setup_acts: Vec<ExecAct> = Vec::new();
    let mut restore_acts: Vec<ExecAct> = Vec::new();

    let active_pages_set: BTreeSet<usize> = plans
        .iter()
        .filter_map(|p| var_to_page_slot.get(&p.src_var).map(|&(page, _)| page))
        .collect();

    // A variable that is also the source of a non-self transfer has its action
    // redirected away from itself, so it cannot erase an incoming transfer. A
    // self-only source keeps its self-targeted action and still needs parking
    // when it is also a destination.
    let redirected_sources: BTreeSet<usize> = plans
        .iter()
        .filter(|p| !p.is_self_transfer())
        .map(|p| p.src_var)
        .collect();

    // A destination page slot still fires its own snapshot when its page runs.
    // It only needs parking if it fires *after* a source that writes it: then
    // the snapshot would restore the old value over the transferred one. If
    // the destination slot fires first, its self-write is harmless, and if its
    // page never fires there is nothing to erase.
    let mut needs_park: BTreeSet<usize> = BTreeSet::new();
    for plan in plans {
        let TransferDest::Var(dst_var) = plan.dst else {
            continue;
        };
        if dst_var == plan.src_var || redirected_sources.contains(&dst_var) {
            continue;
        }
        let (Some(&(src_page, src_slot)), Some(&(dst_page, dst_slot))) = (
            var_to_page_slot.get(&plan.src_var),
            var_to_page_slot.get(&dst_var),
        ) else {
            continue;
        };
        if active_pages_set.contains(&dst_page) && (dst_page, dst_slot) > (src_page, src_slot) {
            needs_park.insert(dst_var);
        }
    }

    let mut parked_dsts: BTreeSet<usize> = BTreeSet::new();

    for plan in plans {
        if !var_to_page_slot.contains_key(&plan.src_var) {
            return Err(LowerError::UnknownVarId(plan.src_var));
        }

        let is_self = plan.is_self_transfer();
        if !is_self {
            setup_acts.push(make_patch_dest_for_plan(plan.src_var, plan.dst));
            restore_acts.push(make_patch_dest_for_plan(
                plan.src_var,
                TransferDest::Var(plan.src_var),
            ));
        }
        if let TransferDest::Var(dst_var) = plan.dst {
            if needs_park.contains(&dst_var) && parked_dsts.insert(dst_var) {
                setup_acts.push(make_park_dest_act(dst_var));
                restore_acts.push(make_patch_dest_for_plan(
                    dst_var,
                    TransferDest::Var(dst_var),
                ));
            }
        }
        if plan.modifier != 7 {
            setup_acts.push(make_patch_modifier_act(plan.src_var, plan.modifier));
            restore_acts.push(make_patch_modifier_act(plan.src_var, 7));
        }
        if plan.src_mask != plan.dst_mask {
            setup_acts.push(make_patch_mask_act(plan.src_var, plan.dst_mask));
            restore_acts.push(make_patch_mask_act(plan.src_var, plan.src_mask));
        }
    }

    let active_pages: Vec<usize> = active_pages_set.into_iter().collect();
    let total_setup_needed = setup_acts.len() + active_pages.len();

    if !cur.is_empty() && cur.len() + total_setup_needed > ACT_COUNT {
        flush_cur_acts(cur, must_emit, out);
    }

    let mut all_setup = std::mem::take(cur);
    all_setup.extend(setup_acts);

    let total_setup_acts = all_setup.len() + active_pages.len();
    let num_setup_triggers = total_setup_acts.div_ceil(ACT_COUNT).max(1);
    let cont_exec_idx = out.len() + num_setup_triggers;

    for (i, &page_idx) in active_pages.iter().enumerate() {
        let next_target = if i + 1 < active_pages.len() {
            ExecRelocTarget::Page {
                page_idx: active_pages[i + 1],
                offset: 0,
            }
        } else {
            ExecRelocTarget::ExecItem {
                exec_idx: cont_exec_idx,
                offset: 0,
            }
        };
        all_setup.push(make_page_set_next_ptr_act(page_idx, next_target));
    }

    let chunks: Vec<Vec<ExecAct>> = all_setup.chunks(ACT_COUNT).map(|c| c.to_vec()).collect();
    let num_chunks = chunks.len();
    for (idx, chunk) in chunks.into_iter().enumerate() {
        let is_last = idx + 1 == num_chunks;
        let next = if is_last {
            ExecNext::Page(active_pages[0])
        } else {
            ExecNext::DefaultChain
        };
        out.push(ExecTrigger {
            hir_id: None,
            conds: Vec::new(),
            acts: chunk,
            preserved: true,
            flags: 4,
            prev: None,
            next,
        });
    }

    *cur = restore_acts;
    *must_emit = true;
    Ok(())
}

fn emit_var_batch(
    batch: &HirVarBatch,
    var_to_page_slot: &HashMap<usize, (usize, usize)>,
    storage_plan: &StoragePlan,
    var_masks: &HashMap<usize, u32>,
    cur: &mut Vec<ExecAct>,
    must_emit: &mut bool,
    out: &mut Vec<ExecTrigger>,
) -> Result<(), LowerError> {
    for t in &batch.transfers {
        if !storage_plan.allocations.contains_key(&t.dst_var) {
            return Err(LowerError::UnknownVarId(t.dst_var));
        }
    }
    // One firing for the whole batch: every member patches its own source
    // slot, whatever its destination kind, and the page fires once.
    let mut plans = plan_var_batch(batch, var_masks);
    for store in &batch.stores {
        let dst = match (store.dst_addr, store.dst_trigger) {
            (Some(addr), None) => TransferDest::AbsAddr(addr),
            (None, Some(trigger_id)) => TransferDest::TriggerField {
                trigger_id,
                offset: store.dst_offset,
            },
            _ => return Err(LowerError::InvalidVarStore),
        };
        plans.push(plan_var_store(
            store.src_var,
            dst,
            store.modifier,
            // `emit_transfer_plans` needs the source mask to restore the
            // page slot after a field/address write.
            *var_masks.get(&store.src_var).unwrap_or(&0xFFFF_FFFF),
        ));
    }
    emit_transfer_plans(&plans, var_to_page_slot, cur, must_emit, out)
}

/// Finds the single boolean Cell bit targeted by a condition's or an
/// action's variable relocs, or `None` when no bit is targeted. This is
/// the one bit semantics shared by condition and action rewriting: a
/// boolean value is rewritten to a masked field, with the mask taken
/// from [`bit_mask`]. A bit targeted among multiple variable relocs is
/// ambiguous — the masked rewrite would silently apply to only one of
/// them — so it is a loud [`LowerError::MultiRelocBitCell`] instead.
/// DSL-valid programs never hit it: `var_deaths`/`set_var_const` carry
/// exactly one variable reloc.
fn single_bit_cell(
    var_ids: impl IntoIterator<Item = usize>,
    plan: &StoragePlan,
) -> Result<Option<(usize, u8)>, LowerError> {
    let mut bit_cell = None;
    let mut bit_var = 0usize;
    let mut var_count = 0usize;
    for vid in var_ids {
        var_count += 1;
        if let Some(StorageClass::BitCell {
            dword_index,
            bit_index,
        }) = plan.storage(vid)
        {
            bit_cell = Some((dword_index, bit_index));
            bit_var = vid;
        }
    }
    if let Some(bit) = bit_cell {
        if var_count != 1 {
            return Err(LowerError::MultiRelocBitCell(bit_var));
        }
        return Ok(Some(bit));
    }
    Ok(None)
}

fn rewrite_cell_cond(cond: &mut HirCond, plan: &StoragePlan) -> Result<(), LowerError> {
    let var_ids: Vec<usize> = cond
        .relocs
        .iter()
        .filter_map(|r| r.target_var)
        .collect();
    if let Some((_dword, bit)) = single_bit_cell(var_ids, plan)? {
        let mask = bit_mask(bit);
        cond.locid = mask;
        cond.eudx = EUDX_MAGIC;
        cond.amount = if cond.amount == 0 { 0 } else { mask };
    }
    Ok(())
}

fn rewrite_cell_act(
    act: &mut CTrigAction,
    relocs: &[ExecReloc],
    plan: &StoragePlan,
) -> Result<(), LowerError> {
    let var_ids: Vec<usize> = relocs
        .iter()
        .filter_map(|r| {
            if let ExecRelocTarget::Var { var_id, .. } = &r.target {
                Some(*var_id)
            } else {
                None
            }
        })
        .collect();
    if let Some((_dword, bit)) = single_bit_cell(var_ids, plan)? {
        let mask = bit_mask(bit);
        act.mask = mask;
        act.eudx = EUDX_MAGIC;
        act.value = if act.value == 0 { 0 } else { mask };
    }
    Ok(())
}

/// Push one action onto the current basic block, starting a new preserved
/// 64-action trigger when the block is full.
fn push_cur_act(
    act: ExecAct,
    cur: &mut Vec<ExecAct>,
    must_emit: &mut bool,
    out: &mut Vec<ExecTrigger>,
) {
    if cur.len() == ACT_COUNT {
        out.push(ExecTrigger {
            hir_id: None,
            conds: Vec::new(),
            acts: std::mem::take(cur),
            preserved: true,
            flags: 4,
            prev: None,
            next: ExecNext::DefaultChain,
        });
        *must_emit = false;
    }
    cur.push(act);
}


/// Emit one call site: bind arguments, jump into the subroutine, and copy its
/// result back after the return.
///
/// Layout (in chain order):
///
/// ```text
/// call trigger   -- acts: pending caller acts + constant arg binds + the
///                   branch act (`<function exit>.next = <ret address>`),
///                   next = function entry
/// ret trigger    -- the return jump lands here; it carries the result copy
///                   (or is empty when the body returns nothing)
/// ```
///
/// Folding the argument binds and any pending caller acts into the call
/// trigger keeps a site at one trigger + one return landing rather than a
/// separate setup trigger per group. The return path is a single `next`
/// pointer patched by the call trigger, so a nested call must not clobber an
/// outer return address; [`HirModule::validate`] rejects recursion up front.
#[allow(clippy::too_many_arguments)]
fn emit_call(
    call: &HirCall,
    funcs: &HashMap<usize, &HirFunc>,
    var_to_page_slot: &HashMap<usize, (usize, usize)>,
    storage_plan: &StoragePlan,
    var_masks: &HashMap<usize, u32>,
    cur: &mut Vec<ExecAct>,
    must_emit: &mut bool,
    out: &mut Vec<ExecTrigger>,
) -> Result<(), LowerError> {
    let func = *funcs
        .get(&call.func)
        .ok_or(LowerError::UnknownFuncId(call.func))?;

    let mut var_args: Vec<HirVarTransfer> = Vec::new();
    for (param, arg) in func.params.iter().zip(&call.args) {
        match arg {
            HirCallArg::Const { value } => {
                push_cur_act(
                    convert_hir_act(&HirAct::set_var_const(*param, 7, *value, 0xFFFF_FFFF))?,
                    cur,
                    must_emit,
                    out,
                );
            }
            HirCallArg::Var { id } => var_args.push(HirVarTransfer {
                dst_var: *param,
                src_var: *id,
                modifier: 7, // SetTo
            }),
        }
    }
    if !var_args.is_empty() {
        emit_var_batch(
            &HirVarBatch {
                transfers: var_args,
                stores: Vec::new(),
            },
            var_to_page_slot,
            storage_plan,
            var_masks,
            cur,
            must_emit,
            out,
        )?;
    }
    // The constant args, any pending caller acts, and the restore half of the
    // transfer batch must all run before the jump; the subroutine body assumes
    // restored page slots. They ride in the call trigger itself, so a site
    // pays one trigger for them, not a separate setup trigger.
    if cur.len() >= ACT_COUNT {
        flush_cur_acts(cur, must_emit, out);
    }

    let ret_idx = out.len() + 1;
    let (ctrig, relocs) = eudkit_lir::set_next_ptr(
        ExecRelocTarget::FuncExit {
            func_id: call.func,
            offset: NEXT_BYTE_OFFSET,
        },
        ExecRelocTarget::ExecItem {
            exec_idx: ret_idx,
            offset: 0,
        },
    );
    let mut call_acts = std::mem::take(cur);
    call_acts.push(ExecAct::new(
        ctrig,
        relocs
            .into_iter()
            .map(|(field_offset, target, mode)| ExecReloc {
                field_offset,
                target,
                mode,
            })
            .collect(),
    ));
    out.push(ExecTrigger {
        hir_id: None,
        conds: Vec::new(),
        acts: call_acts,
        preserved: true,
        flags: 4,
        prev: None,
        next: ExecNext::Func(call.func),
    });
    *must_emit = false;

    // The return lands on the next emitted item. Emit the result copy first so
    // that item is the copy itself; a body that returns nothing gets an empty
    // landing trigger instead.
    if let (Some(dst), Some(result)) = (call.dst, func.result) {
        emit_var_batch(
            &HirVarBatch {
                transfers: vec![HirVarTransfer {
                    dst_var: dst,
                    src_var: result,
                    modifier: 7, // SetTo
                }],
                stores: Vec::new(),
            },
            var_to_page_slot,
            storage_plan,
            var_masks,
            cur,
            must_emit,
            out,
        )?;
    }
    Ok(())
}

fn lower_hir_stmts(
    stmts: &[HirStmt],
    var_to_page_slot: &HashMap<usize, (usize, usize)>,
    storage_plan: &StoragePlan,
    funcs: &HashMap<usize, &HirFunc>,
    var_masks: &HashMap<usize, u32>,
    referenced_ids: &TriggerRefs,
    cur: &mut Vec<ExecAct>,
    must_emit: &mut bool,
    out: &mut Vec<ExecTrigger>,
) -> Result<(), LowerError> {
    // Per-block, not module-wide: an unconditional preserved block folds
    // into *this* block's actions, so only a batch in this same list can
    // receive its acts in the setup trigger. A `VarStore` now lowers through
    // the same firing as a transfer, so it opens the gate too.
    let has_var_batch = stmts.iter().any(|s| {
        matches!(
            s,
            HirStmt::VarBatch(_) | HirStmt::VarTransfer(_) | HirStmt::VarStore(_)
        )
    });
    for stmt in stmts {
        match stmt {
            HirStmt::Act(a) => {
                push_cur_act(
                    convert_hir_act(a)?,
                    cur,
                    must_emit,
                    out,
                );
            }
            HirStmt::VarTransfer(t) => {
                let batch = HirVarBatch::transfers(vec![t.clone()]);
                emit_var_batch(
                    &batch,
                    var_to_page_slot,
                    storage_plan,
                    var_masks,
                    cur,
                    must_emit,
                    out,
                )?;
            }
            HirStmt::VarBatch(batch) => {
                emit_var_batch(
                    batch,
                    var_to_page_slot,
                    storage_plan,
                    var_masks,
                    cur,
                    must_emit,
                    out,
                )?;
            }
            HirStmt::VarStore(store) => {
                let batch = HirVarBatch {
                    transfers: Vec::new(),
                    stores: vec![store.clone()],
                };
                emit_var_batch(
                    &batch,
                    var_to_page_slot,
                    storage_plan,
                    var_masks,
                    cur,
                    must_emit,
                    out,
                )?;
            }
            HirStmt::Call(call) => {
                emit_call(
                    call,
                    funcs,
                    var_to_page_slot,
                    storage_plan,
                    var_masks,
                    cur,
                    must_emit,
                    out,
                )?;
            }
            HirStmt::Trigger(trig) => {
                trig.validate()?;
                // `has_var_batch` is a block property, so it stays here; the
                // trigger shape and the reference check are shared with the
                // optimizer through `HirTrigger::is_unpackable`.
                let can_unpack_into_basic_block =
                    has_var_batch && trig.is_unpackable(referenced_ids);

                if can_unpack_into_basic_block {
                    for a in &trig.acts {
                        push_cur_act(
                            convert_hir_act(a)?,
                            cur,
                            must_emit,
                            out,
                        );
                    }
                } else {
                    flush_cur_acts(cur, must_emit, out);
                    let mut acts = Vec::with_capacity(trig.acts.len());
                    for a in &trig.acts {
                        acts.push(convert_hir_act(a)?);
                    }
                    out.push(ExecTrigger {
                        hir_id: Some(trig.id),
                        conds: trig.conds.clone(),
                        acts,
                        preserved: trig.preserved,
                        flags: trig.flags,
                        prev: trig.prev.clone(),
                        next: match &trig.next {
                            Some(ptr) => ExecNext::Hir(ptr.clone()),
                            None => ExecNext::DefaultChain,
                        },
                    });
                }
            }
            HirStmt::When(w) => {
                flush_cur_acts(cur, must_emit, out);
                control::lower_when(w, out, |sub, sub_out| {
                    let mut sub_acts = Vec::new();
                    let mut sub_must_emit = false;
                    lower_hir_stmts(
                        sub,
                        var_to_page_slot,
                        storage_plan,
                        funcs,
                        var_masks,
                        referenced_ids,
                        &mut sub_acts,
                        &mut sub_must_emit,
                        sub_out,
                    )?;
                    flush_cur_acts(&mut sub_acts, &mut sub_must_emit, sub_out);
                    Ok(())
                })?;
            }
            HirStmt::Once(o) => {
                flush_cur_acts(cur, must_emit, out);
                control::lower_once(o, out, |sub, sub_out| {
                    let mut sub_acts = Vec::new();
                    let mut sub_must_emit = false;
                    lower_hir_stmts(
                        sub,
                        var_to_page_slot,
                        storage_plan,
                        funcs,
                        var_masks,
                        referenced_ids,
                        &mut sub_acts,
                        &mut sub_must_emit,
                        sub_out,
                    )?;
                    flush_cur_acts(&mut sub_acts, &mut sub_must_emit, sub_out);
                    Ok(())
                })?;
            }
            HirStmt::While(w) => {
                flush_cur_acts(cur, must_emit, out);
                control::lower_while(w, out, |sub, sub_out| {
                    let mut sub_acts = Vec::new();
                    let mut sub_must_emit = false;
                    lower_hir_stmts(
                        sub,
                        var_to_page_slot,
                        storage_plan,
                        funcs,
                        var_masks,
                        referenced_ids,
                        &mut sub_acts,
                        &mut sub_must_emit,
                        sub_out,
                    )?;
                    flush_cur_acts(&mut sub_acts, &mut sub_must_emit, sub_out);
                    Ok(())
                })?;
            }
            // Candidate 11 phase 1: the new temporal nodes have no
            // lowering by design — their opt desugar tickets (C11-02/03/04)
            // run first in real programs. A hand-written node reaching
            // lowering fails LOUD here instead of being silently dropped.
            // control.rs is untouched; this is a new dispatch arm with no
            // lowering logic — it only fails loud until C11-02/03/04 land.
            HirStmt::Until(_) => Err(LowerError::UnsupportedStmt("until"))?,
            HirStmt::Edge(_) => Err(LowerError::UnsupportedStmt("edge"))?,
            HirStmt::Every(_) => Err(LowerError::UnsupportedStmt("every"))?,
            HirStmt::After(_) => Err(LowerError::UnsupportedStmt("after"))?,
        }
    }
    Ok(())
}

/// The HIR control-flow pre-pass, computed once per compile and shared by
/// the string-list walk and the item lowering.
///
/// `lower_hir_stmts` flattens HIR control flow into `ExecTrigger`s —
/// splitting, duplicating and reordering actions (`when`/`once`/`while`
/// bodies, 64-action chunking). That flattening never reads STR offsets and
/// has no dependency on the string list, so it is a pure pre-pass:
///
/// - [`collect_strings`] walks the triggers to reproduce the final ordered
///   `module.strings` list exactly (the list `lower_optimized_module_inner`
///   interns), replacing the `lower.probe` full lowering pass that used to
///   exist only to discover it;
/// - the final lowering ([`lower_optimized_module_with_plan`]) emits items
///   from the same triggers instead of re-flattening.
///
/// Sharing the plan is what makes the string list exact: a plain HIR walk is
/// *not* equivalent because this pre-pass determines the intern order.
pub struct LoweringPlan {
    exec_triggers: Vec<ExecTrigger>,
    /// Exec item index of each subroutine's entry trigger.
    func_entry: HashMap<usize, usize>,
    /// Exec item index of each subroutine's exit (return) trigger.
    func_exit: HashMap<usize, usize>,
}

impl LoweringPlan {
    /// Run the storage var-plan setup and flatten `opt`'s control flow.
    pub fn build(opt: &HirModule, storage_plan: &StoragePlan) -> Result<Self, LowerError> {
        // Pinned scalar variables live in TriggerPage slots; the mask map is
        // consumed by every action/condition rewrite inside the pre-pass.
        let mut var_to_page_slot: HashMap<usize, (usize, usize)> =
            HashMap::with_capacity(storage_plan.trigger_page_vars);
        let mut var_masks: HashMap<usize, u32> = HashMap::with_capacity(opt.vars.len());
        for v in &opt.vars {
            match storage_plan.storage(v.id) {
                Some(StorageClass::TriggerPage {
                    page_index,
                    slot_index,
                }) => {
                    var_to_page_slot.insert(v.id, (page_index, slot_index));
                }
                Some(StorageClass::DwordCell { .. } | StorageClass::BitCell { .. }) => {}
                None => return Err(LowerError::UnknownVarId(v.id)),
            }
            var_masks.insert(v.id, v.mask);
        }

        // Build execution triggers, coalescing VarBatch setup/restore actions
        // into basic-block triggers.
        let referenced_ids = TriggerRefs::from_module(opt);
        let mut exec_triggers: Vec<ExecTrigger> = Vec::new();
        let mut cur_acts: Vec<ExecAct> = Vec::new();
        let mut must_emit_cont = false;
        let funcs_by_id: HashMap<usize, &HirFunc> =
            opt.funcs.iter().map(|f| (f.id, f)).collect();
        lower_hir_stmts(
            &opt.stmts,
            &var_to_page_slot,
            storage_plan,
            &funcs_by_id,
            &var_masks,
            &referenced_ids,
            &mut cur_acts,
            &mut must_emit_cont,
            &mut exec_triggers,
        )?;
        flush_cur_acts(&mut cur_acts, &mut must_emit_cont, &mut exec_triggers);

        // Subroutines live in a region after the main chain. The main chain
        // must terminate at its end instead of falling into that region;
        // control-flow joins recorded as `ExecItem(main_end)` mean "after the
        // last construct", i.e. the chain terminator.
        if exec_triggers.is_empty() && !opt.funcs.is_empty() {
            // Functions without any top-level statement: emit an empty
            // terminator so item 0 (the chain entry) is not a body.
            exec_triggers.push(ExecTrigger {
                hir_id: None,
                conds: Vec::new(),
                acts: Vec::new(),
                preserved: true,
                flags: 4,
                prev: None,
                next: ExecNext::ChainEnd,
            });
        }
        let main_end = exec_triggers.len();
        // The main chain must not fall through into the subroutine region:
        // the last flattened trigger's default fall-through is a chain end.
        if main_end > 0 && matches!(exec_triggers[main_end - 1].next, ExecNext::DefaultChain) {
            exec_triggers[main_end - 1].next = ExecNext::ChainEnd;
        }
        for trig in &mut exec_triggers[..main_end] {
            if matches!(trig.next, ExecNext::ExecItem(idx) if idx == main_end) {
                trig.next = ExecNext::ChainEnd;
            }
            for act in &mut trig.acts {
                for reloc in &mut act.relocs {
                    if matches!(&reloc.target, ExecRelocTarget::ExecItem { exec_idx, .. } if *exec_idx == main_end)
                    {
                        reloc.target = ExecRelocTarget::ChainEnd;
                    }
                }
            }
        }

        // Emit each subroutine body once. Call sites patch the return point's
        // `next` to their own return trigger, so one static copy serves every
        // call (O(K + M) instead of K x M).
        let mut func_entry: HashMap<usize, usize> = HashMap::with_capacity(opt.funcs.len());
        let mut func_exit: HashMap<usize, usize> = HashMap::with_capacity(opt.funcs.len());
        for func in &opt.funcs {
            let entry = exec_triggers.len();
            func_entry.insert(func.id, entry);
            let mut func_acts: Vec<ExecAct> = Vec::new();
            let mut func_must_emit = false;
            lower_hir_stmts(
                &func.body,
                &var_to_page_slot,
                storage_plan,
                &funcs_by_id,
                &var_masks,
                &referenced_ids,
                &mut func_acts,
                &mut func_must_emit,
                &mut exec_triggers,
            )?;
            flush_cur_acts(&mut func_acts, &mut func_must_emit, &mut exec_triggers);
            // A body that simply falls off the end of its last trigger needs no
            // separate return trigger: the call patches that trigger's `next`
            // before jumping anyway, so it can serve as the return point. This
            // saves one executed trigger per call. A body with an explicit join
            // to its end (a `when`/loop continuation) still gets the dedicated
            // empty trigger that those joins land on.
            let exit_idx = exec_triggers.len();
            let tail = exec_triggers
                .last()
                .map(|_| exit_idx - 1)
                .filter(|&idx| idx >= entry)
                .filter(|&idx| matches!(exec_triggers[idx].next, ExecNext::DefaultChain))
                .filter(|_| !references_exec_item(&exec_triggers, exit_idx));
            match tail {
                Some(idx) => {
                    // The static fall-through is never taken (the call rewrites
                    // it at runtime); ending the chain keeps the static model
                    // from walking into the next body.
                    exec_triggers[idx].next = ExecNext::ChainEnd;
                    func_exit.insert(func.id, idx);
                }
                None => {
                    exec_triggers.push(ExecTrigger {
                        hir_id: None,
                        conds: Vec::new(),
                        acts: Vec::new(),
                        preserved: true,
                        flags: 4,
                        prev: None,
                        next: ExecNext::DefaultChain,
                    });
                    func_exit.insert(func.id, exit_idx);
                }
            }
        }
        Ok(Self {
            exec_triggers,
            func_entry,
            func_exit,
        })
    }

    /// The flattened triggers, in emission order.
    pub(crate) fn exec_triggers(&self) -> &[ExecTrigger] {
        &self.exec_triggers
    }
}

/// Whether any trigger jumps or points into exec item `exec_idx`.
///
/// Used to decide when a subroutine's body end can serve as its return point:
/// a body whose statements only fall through (no `when`/loop continuation that
/// joins on the end) leaves the would-be exit item unreferenced.
fn references_exec_item(triggers: &[ExecTrigger], exec_idx: usize) -> bool {
    triggers.iter().any(|t| {
        if matches!(&t.next, ExecNext::ExecItem(idx) if *idx == exec_idx) {
            return true;
        }
        t.acts.iter().any(|a| {
            a.relocs.iter().any(|r| {
                matches!(&r.target, ExecRelocTarget::ExecItem { exec_idx: idx, .. } if *idx == exec_idx)
            })
        })
    })
}

/// The final ordered `module.strings` list (base64, template-seeded) the final
/// lowering will emit, without lowering a single item.
///
/// Must stay in lock-step with the interning order in
/// [`lower_optimized_module_inner`]: template strings (when any string is
/// used) → `opt.strings` → per-action `text` (unless `str_index` suppresses
/// it) and `wav`, in `ExecTrigger` order. The
/// `collect_strings_matches_the_final_string_table` test pins the equivalence.
pub fn collect_strings(
    opt: &HirModule,
    plan: &LoweringPlan,
    template_strings_b64: &[String],
) -> Vec<String> {
    let exec_triggers = plan.exec_triggers();

    let has_any_action_string = exec_triggers
        .iter()
        .any(|t| t.acts.iter().any(|a| a.text.is_some() || a.wav.is_some()));
    let has_any_strings = has_any_action_string || !opt.strings.is_empty();

    let mut strings_b64: Vec<String> = if has_any_strings && !template_strings_b64.is_empty() {
        template_strings_b64.to_vec()
    } else {
        Vec::new()
    };
    let module_string_base = strings_b64.len();
    for s in &opt.strings {
        strings_b64.push(b64_encode(s.as_bytes()));
    }
    let mut interned_ids: HashMap<String, u32> = HashMap::new();
    for (i, s) in opt.strings.iter().enumerate() {
        interned_ids
            .entry(s.clone())
            .or_insert((module_string_base + i + 1) as u32);
    }

    for trig in exec_triggers {
        for a in &trig.acts {
            if a.str_index.is_none() {
                if let Some(text) = &a.text {
                    if !interned_ids.contains_key(text.as_str()) {
                        strings_b64.push(b64_encode(text.as_bytes()));
                        let id = strings_b64.len() as u32;
                        interned_ids.insert(text.to_string(), id);
                    }
                }
            }
            if let Some(wav) = &a.wav {
                if !interned_ids.contains_key(wav.as_str()) {
                    strings_b64.push(b64_encode(wav.as_bytes()));
                    let id = strings_b64.len() as u32;
                    interned_ids.insert(wav.to_string(), id);
                }
            }
        }
    }
    strings_b64
}

fn lower_optimized_module_inner(
    opt: &HirModule,
    storage_plan: &StoragePlan,
    plan: &LoweringPlan,
    template_strings_b64: &[String],
    str_addrs: Option<&[u32]>,
) -> Result<LirDocument, LowerError> {
    // Seeded template strings always occupy ids 1..=template_count; module
    // strings and interned display text follow them.
    let template_count = template_strings_b64.len();

    // 1. Page slot initial values and the demoted Cell data blob. These are
    //    resolved here rather than in the pre-pass because `String`
    //    initializers need the final STR addresses; `LoweringPlan::build`
    //    already validated every variable id and built the page/slot map.
    let num_pages = storage_plan.trigger_pages;
    let mut page_slot_inits: Vec<[(u32, u32); ACT_COUNT]> =
        vec![[(0u32, 0xFFFF_FFFFu32); ACT_COUNT]; num_pages];
    let mut cell_init = vec![0u8; storage_plan.cell_dwords * 4];
    for v in &opt.vars {
        match storage_plan.storage(v.id) {
            Some(StorageClass::TriggerPage {
                page_index,
                slot_index,
            }) => {
                let init_val = match &v.init_string {
                    Some(init) => {
                        let addr = module_string_addr(
                            init.index,
                            init.offset,
                            template_count,
                            str_addrs,
                        )?;
                        eudkit_lir::epd_u32(addr)
                    }
                    None => v.init,
                };
                page_slot_inits[page_index][slot_index] = (init_val, v.mask);
            }
            Some(StorageClass::DwordCell { dword_index }) => {
                let value = (v.init & v.mask).to_le_bytes();
                let off = dword_index * 4;
                cell_init[off..off + 4].copy_from_slice(&value);
            }
            Some(StorageClass::BitCell {
                dword_index,
                bit_index,
            }) => {
                if v.init != 0 {
                    // Bit cells are 32-bit dwords (see bit_mask): bit
                    // `bit_index` lives in byte `bit_index / 8` of the
                    // dword. Writing it into the dword's first byte with
                    // the full shift panicked in debug (and set the wrong
                    // bit in release) for any bit index >= 8.
                    let byte = dword_index * 4 + (bit_index as usize) / 8;
                    cell_init[byte] |= 1 << (bit_index % 8);
                }
            }
            None => return Err(LowerError::UnknownVarId(v.id)),
        }
    }

    // 2. Items are emitted from the shared pre-pass rather than a second
    //    control-flow flatten.
    let exec_triggers = plan.exec_triggers();
    let num_exec = exec_triggers.len();
    let mut id_to_index: HashMap<usize, usize> = HashMap::new();
    for (idx, trig) in exec_triggers.iter().enumerate() {
        if let Some(id) = trig.hir_id {
            id_to_index.insert(id, idx);
        }
    }

    let cell_blob_idx = num_exec + num_pages;
    let has_cell_blob = storage_plan.cell_dwords > 0;
    // HIR blobs are appended last, after the pages and the optional demoted
    // Cell blob, in declaration order.
    let blob_item_base = cell_blob_idx + usize::from(has_cell_blob);
    // A `None` result means "end of the trigger chain": control-flow joins use
    // `ExecItem { exec_idx: num_exec }` to mean "continue after this construct",
    // which is the chain terminator when no further executable items exist.
    let resolve_reloc_target =
        |target: &ExecRelocTarget| -> Result<Option<(usize, usize)>, LowerError> {
        match target {
            ExecRelocTarget::HirTrigger { id, offset } => {
                let item_idx = *id_to_index
                    .get(id)
                    .ok_or(LowerError::UnknownTriggerId(*id))?;
                Ok(Some((item_idx, *offset)))
            }
            ExecRelocTarget::Var {
                var_id,
                slot_offset,
            } => match storage_plan.storage(*var_id) {
                Some(StorageClass::TriggerPage {
                    page_index,
                    slot_index,
                }) => {
                    let item_idx = num_exec + page_index;
                    let byte_offset =
                        ACTS_BYTE_OFFSET + slot_index * ACT_BYTE_SIZE + *slot_offset;
                    Ok(Some((item_idx, byte_offset)))
                }
                Some(class @ (StorageClass::DwordCell { .. } | StorageClass::BitCell { .. })) => {
                    // Cell storage exposes a single 4-byte value address. The
                    // analysis pins every variable whose `.dest`/`.modifier`/
                    // `.mask` field is consumed to a TriggerPage, so any
                    // remaining relocation must be a plain value access.
                    if !is_value_field_offset(*slot_offset) {
                        return Err(LowerError::CellField(*var_id, *slot_offset));
                    }
                    if !has_cell_blob {
                        return Err(LowerError::UnknownVarId(*var_id));
                    }
                    Ok(Some((
                        cell_blob_idx,
                        class.cell_byte_offset().expect("cell offset"),
                    )))
                }
                None => Err(LowerError::UnknownVarId(*var_id)),
            },
            ExecRelocTarget::Page { page_idx, offset } => Ok(Some((num_exec + *page_idx, *offset))),
            ExecRelocTarget::FuncExit { func_id, offset } => {
                let item_idx = *plan
                    .func_exit
                    .get(func_id)
                    .ok_or(LowerError::UnknownFuncId(*func_id))?;
                Ok(Some((item_idx, *offset)))
            }
            ExecRelocTarget::Blob { blob_idx, offset } => {
                if *blob_idx >= opt.blobs.len() {
                    return Err(LowerError::UnknownBlobId(*blob_idx));
                }
                Ok(Some((blob_item_base + *blob_idx, *offset)))
            }
            ExecRelocTarget::ExecItem { exec_idx, offset } => {
                if *exec_idx == num_exec {
                    Ok(None)
                } else if *exec_idx < num_exec {
                    Ok(Some((*exec_idx, *offset)))
                } else {
                    Err(LowerError::InvalidContinuation(*exec_idx, num_exec))
                }
            }
            ExecRelocTarget::ChainEnd => Ok(None),
        }
    };

    // `PlayWAV` interns its archive name into the same string table as
    // `DisplayText` (see the `intern_str` call below), so a module whose only
    // string use is a WAV name still needs the basemap's string table seeded
    // before its own names are appended; otherwise `build_ordered` rejects the
    // LIR for being shorter than the template table.
    let has_any_action_string = exec_triggers
        .iter()
        .any(|t| t.acts.iter().any(|a| a.text.is_some() || a.wav.is_some()));
    let has_any_strings = has_any_action_string || !opt.strings.is_empty();

    let mut strings_b64: Vec<String> = if has_any_strings && !template_strings_b64.is_empty() {
        template_strings_b64.to_vec()
    } else {
        Vec::new()
    };
    let module_string_base = strings_b64.len();
    for s in &opt.strings {
        strings_b64.push(b64_encode(s.as_bytes()));
    }
    let mut interned_ids: HashMap<String, u32> = HashMap::new();
    for (i, s) in opt.strings.iter().enumerate() {
        interned_ids
            .entry(s.clone())
            .or_insert((module_string_base + i + 1) as u32);
    }

    let mut intern_str = |text: &str| -> u32 {
        if let Some(&id) = interned_ids.get(text) {
            return id;
        }
        strings_b64.push(b64_encode(text.as_bytes()));
        let id = strings_b64.len() as u32; // 1-based StarCraft string ID
        interned_ids.insert(text.to_string(), id);
        id
    };

    let mut items: Vec<LirItem> = Vec::with_capacity(num_exec + num_pages);

    for (idx, trig) in exec_triggers.iter().enumerate() {
        let (prev, prev_reloc) = match &trig.prev {
            Some(HirTriggerPtr::Const { value }) => (Some(*value), false),
            Some(HirTriggerPtr::Trigger { id, .. }) => {
                let target = *id_to_index
                    .get(id)
                    .ok_or(LowerError::UnknownTriggerId(*id))?;
                (Some(target as i64), true)
            }
            None => {
                if idx == 0 {
                    (Some(-1), false)
                } else {
                    (Some((idx - 1) as i64), true)
                }
            }
        };

        let (next, next_reloc) = match &trig.next {
            ExecNext::Hir(HirTriggerPtr::Const { value }) => (Some(*value), false),
            ExecNext::Hir(HirTriggerPtr::Trigger { id, .. }) => {
                let target = *id_to_index
                    .get(id)
                    .ok_or(LowerError::UnknownTriggerId(*id))?;
                (Some(target as i64), true)
            }
            ExecNext::Page(page_idx) => (Some((num_exec + *page_idx) as i64), true),
            ExecNext::Func(func_id) => {
                let target = *plan
                    .func_entry
                    .get(func_id)
                    .ok_or(LowerError::UnknownFuncId(*func_id))?;
                (Some(target as i64), true)
            }
            ExecNext::ChainEnd => (Some(CHAIN_END_PTR), false),
            ExecNext::ExecItem(target_idx) => {
                if *target_idx == num_exec {
                    (Some(CHAIN_END_PTR), false)
                } else if *target_idx < num_exec {
                    (Some(*target_idx as i64), true)
                } else {
                    return Err(LowerError::InvalidContinuation(*target_idx, num_exec));
                }
            }
            ExecNext::DefaultChain => {
                if idx + 1 < num_exec {
                    (Some((idx + 1) as i64), true)
                } else {
                    (Some(CHAIN_END_PTR), false)
                }
            }
        };

        let mut conds: Vec<CondData> = Vec::with_capacity(trig.conds.len());
        for c in &trig.conds {
            let mut c = c.clone();
            rewrite_cell_cond(&mut c, storage_plan)?;
            let mut data = c.to_ctrig_condition().to_cond_data();
            for r in &c.relocs {
                if let Some(string_index) = r.target_string {
                    let addr = module_string_addr(
                        string_index,
                        r.target_offset,
                        template_count,
                        str_addrs,
                    )?;
                    let value = if r.mode == 1 {
                        eudkit_lir::epd_u32(addr)
                    } else {
                        addr
                    };
                    // Byte-accurate 4-byte window; a window that does not
                    // fit the 20-byte record is the loud LirError.
                    data.cond = data.cond.write_field_u32(r.field_offset, value)?;
                    continue;
                }
                let exec_r = convert_hir_reloc(r)?;
                let (target_item, target_offset) = resolve_reloc_target(&exec_r.target)?
                    .ok_or(LowerError::ChainEndInCondition)?;
                data.relocs.push(RelocData {
                    field_offset: exec_r.field_offset,
                    target_item,
                    target_offset,
                    mode: exec_r.mode,
                });
            }
            conds.push(data);
        }

        let mut acts: Vec<ActData> = Vec::with_capacity(trig.acts.len());
        for a in &trig.acts {
            let mut ctrig = a.ctrig;
            rewrite_cell_act(&mut ctrig, &a.relocs, storage_plan)?;
            if let Some(index) = a.str_index {
                ctrig.strid = (module_string_base + index + 1) as u32;
            } else if let Some(text) = &a.text {
                ctrig.strid = intern_str(text);
            }
            if let Some(wav) = &a.wav {
                ctrig.wavid = intern_str(wav);
            }
            let mut data = ctrig.to_act_data();
            data.uprp = a.uprp;
            for r in &a.string_relocs {
                let string_index = r.target_string.expect("string reloc");
                let addr =
                    module_string_addr(string_index, r.target_offset, template_count, str_addrs)?;
                let value = if r.mode == 1 {
                    eudkit_lir::epd_u32(addr)
                } else {
                    addr
                };
                // Byte-accurate 4-byte window; a window that does not fit
                // the 32-byte record is the loud LirError.
                data.act = data.act.write_field_u32(r.field_offset, value)?;
            }
            for r in &a.relocs {
                match resolve_reloc_target(&r.target)? {
                    Some((target_item, target_offset)) => {
                        data.relocs.push(RelocData {
                            field_offset: r.field_offset,
                            target_item,
                            target_offset,
                            mode: r.mode,
                        });
                    }
                    None => {
                        // Writing the chain terminator needs no payload
                        // relocation: store the constant directly so the LIR
                        // item surviving at `num_exec` (a TriggerPage or cell
                        // blob) can never be mistaken for a continuation.
                        if r.mode != 4 {
                            return Err(LowerError::InvalidContinuation(num_exec, num_exec));
                        }
                        data.act.value = CHAIN_END_PTR as u32;
                    }
                }
            }
            acts.push(data);
        }

        let default_flag = if trig.preserved { 4 } else { 0 };
        let flag = if trig.flags != default_flag {
            Some(trig.flags)
        } else {
            None
        };

        items.push(LirItem::Trigger(TriggerItem {
            size: RAW_TRIGGER_BYTE_SIZE,
            prev,
            next,
            prev_reloc,
            next_reloc,
            conds,
            acts,
            preserved: trig.preserved,
            flag,
        }));
    }

    // 3. Emit 64A TriggerPages with Self-Targeted Action Invariant at rest
    for (p, slot_inits) in page_slot_inits.iter().enumerate() {
        let page_item_idx = num_exec + p;
        let mut page_acts: Vec<ActData> = Vec::with_capacity(ACT_COUNT);
        for (k, &(init_val, mask)) in slot_inits.iter().enumerate() {
            let ctrig = CTrigAction {
                mask,
                strid: 0,
                wavid: 0,
                time: 0,
                dest: 0,
                value: init_val,
                unit: 0,
                acttype: 45, // SetDeaths
                modifier: 7, // SetTo
                flags: 20,
                padding: 0,
                eudx: EUDX_MAGIC,
            };
            let mut act_data = ctrig.to_act_data();
            // Self-targeted invariant: dest = EPD(self.act[k].value)
            act_data.relocs.push(RelocData {
                field_offset: DEST_FIELD_OFFSET,
                target_item: page_item_idx,
                target_offset: act_field_byte_offset(k, VALUE_FIELD_OFFSET),
                mode: 1,
            });
            page_acts.push(act_data);
        }

        items.push(LirItem::Trigger(TriggerItem {
            size: RAW_TRIGGER_BYTE_SIZE,
            prev: Some(-1),
            next: Some(CHAIN_END_PTR),
            prev_reloc: false,
            next_reloc: false,
            conds: Vec::new(),
            acts: page_acts,
            preserved: true,
            flag: None,
        }));
    }

    // 4. Emit the demoted Cell data blob (4-byte dwords, addressable by EPD).
    if has_cell_blob {
        let data = b64_encode(&cell_init);
        items.push(LirItem::Blob(BlobItem {
            size: cell_init.len(),
            data,
            relocs: Vec::new(),
        }));
    }

    // 5. Emit the frontend-authored data blobs (`Db(bytes | size)`).
    for (blob_idx, blob) in opt.blobs.iter().enumerate() {
        let mut relocs = Vec::with_capacity(blob.relocs.len());
        for r in &blob.relocs {
            let target = if let Some(target_blob) = r.target_blob {
                if target_blob >= opt.blobs.len() {
                    return Err(LowerError::UnknownBlobId(target_blob));
                }
                ExecRelocTarget::Blob {
                    blob_idx: target_blob,
                    offset: r.target_offset,
                }
            } else if let Some(target_trigger) = r.target_trigger {
                ExecRelocTarget::HirTrigger {
                    id: target_trigger,
                    offset: r.target_offset,
                }
            } else {
                return Err(LowerError::Hir(HirError::InvalidBlobReloc(
                    blob_idx,
                    r.offset,
                )));
            };
            let (target_item, target_offset) = resolve_reloc_target(&target)?
                .ok_or(LowerError::ChainEndInCondition)?;
            relocs.push(BlobRelocData {
                offset: r.offset,
                target_item,
                target_offset,
                mode: r.mode,
            });
        }
        items.push(LirItem::Blob(BlobItem {
            size: blob.size,
            data: blob.data.clone(),
            relocs,
        }));
    }

    Ok(LirDocument {
        version: LIR_VERSION.to_string(),
        module: LirModule {
            entry: 0,
            items,
            strings: strings_b64,
            str_addrs: str_addrs.map(|a| a.to_vec()).unwrap_or_default(),
        },
    })
}

/// Parse HIR 3.0 JSON, lower to LIR 3.0, and serialize to LIR JSON.
pub fn lower_hir_json(hir_json: &str) -> Result<String, LowerError> {
    let doc = hir_from_json(hir_json)?;
    let lir = lower_document(&doc)?;
    Ok(lir.to_json()?)
}

/// Compile HIR 3.0 JSON against a template `.scx` basemap and write the output `.scx` map.
/// Returns the [`StaticCost`] of the assembled map: the lowered program plus
/// the once-per-tick gate appended by [`assemble_map`].
///
/// This is the default (gate enabled) path. Single-chain builds that omit
/// the gate use [`compile_hir_to_map_with_options`].
pub fn compile_hir_to_map(
    hir_json: &str,
    basemap: &Path,
    out_path: &Path,
) -> Result<StaticCost, LowerError> {
    compile_hir_to_map_with_options(hir_json, basemap, out_path, AssemblyOptions::default())
}

/// Everything one compile computes between HIR and map assembly: the
/// optimized module, its storage plan, and the lowered LIR.
///
/// The optimize/storage pair is computed once here and both lowering passes
/// consume it, so `eudkit-opt` never runs twice for the same program.
pub struct PreparedLowering {
    /// The module after `eudkit-opt` desugaring/bin-packing.
    pub optimized: HirModule,
    /// `analyze_storage(optimized)`, shared by both lowering passes.
    pub storage: StoragePlan,
    /// The program LIR produced by the final lowering pass.
    pub lir: LirDocument,
    /// The basemap parse used for template strings and STR offsets, for
    /// [`eudkit_map::assemble_map_with_context`] to reuse instead of
    /// re-opening the MPQ and re-parsing `scenario.chk`. `None` when
    /// `basemap` was `None` (tools that build no map, e.g. `dump-ir`).
    pub basemap_context: Option<BasemapContext>,
}

/// The single pre-assembly pipeline shared by `eudkit compile`, `dump-ir`,
/// and `eudkit-compile map`.
///
/// `basemap` is `None` for tools that never assemble a map (`dump-ir`). With
/// a basemap the sequence is: basemap info → control-flow pre-pass →
/// final string list + STR offsets → final lowering. `assemble_map` still
/// recomputes and verifies the layout as a soundness backstop, and the
/// context is returned so map assembly can reuse its parse.
pub fn prepare_lowering(
    doc: &HirDocument,
    basemap: Option<&Path>,
) -> Result<PreparedLowering, LowerError> {
    doc.validate()?;
    let stage = Stage::start("opt.optimize");
    let optimized = optimize_module(&doc.module)?;
    stage.finish();
    let stage = Stage::start("opt.storage");
    let storage = analyze_storage(&optimized);
    stage.finish();
    let (lir, basemap_context) = match basemap {
        Some(path) => {
            let stage = Stage::start("lower.basemap-info");
            // One basemap open + CHK read + template string parse serves
            // both the MapInfo (template strings) and the STR offsets
            // below; the two stages remain for timing continuity.
            let ctx = BasemapContext::open(path)?;
            let info = ctx.map_info()?;
            stage.finish();
            let stage = Stage::start("lower.str-offsets");
            // The control-flow pre-pass (flatten HIR into ExecTriggers) runs
            // once; the ordered string list is walked from it instead of a
            // second full lowering, and the same triggers feed the final
            // pass below.
            let plan = LoweringPlan::build(&optimized, &storage)?;
            let strings = collect_strings(&optimized, &plan, &info.strings_b64);
            let str_addrs = if strings.is_empty() {
                Vec::new()
            } else {
                ctx.str_offsets_for_full_strings(&strings)?
            };
            stage.finish();
            let stage = Stage::start("lower.final");
            let lir = lower_optimized_module_with_plan(
                &optimized,
                &storage,
                &plan,
                &info.strings_b64,
                Some(&str_addrs),
            )?;
            stage.finish();
            (lir, Some(ctx))
        }
        None => {
            let stage = Stage::start("lower.final");
            let lir = lower_optimized_module(&optimized, &storage, &[])?;
            stage.finish();
            (lir, None)
        }
    };
    Ok(PreparedLowering {
        optimized,
        storage,
        lir,
        basemap_context,
    })
}

/// Compile HIR 3.0 JSON to a `.scx` map with explicit map-assembly options.
///
/// The returned cost is the cost of the exact document assembly encoded, so
/// a caller that disables the tick gate gets the ungated program cost.
///
/// A thin wrapper over [`prepare_lowering`]: the typed [`LirDocument`] goes
/// straight into [`assemble_map`] with no JSON round-trip.
pub fn compile_hir_to_map_with_options(
    hir_json: &str,
    basemap: &Path,
    out_path: &Path,
    options: AssemblyOptions,
) -> Result<StaticCost, LowerError> {
    let doc = hir_from_json(hir_json)?;
    let prepared = prepare_lowering(&doc, Some(basemap))?;
    let inputs = TailInputs {
        lir: &prepared.lir,
        basemap,
        out_path,
        sector_size: None,
        mrgndata: None,
        draw_log_json: None,
        queued_files: Vec::new(),
        debug_emit_chk: None,
        no_fake_isom: false,
        assembly: options,
    };
    // Reuse the basemap parse lowering already paid for.
    let summary = match prepared.basemap_context.as_ref() {
        Some(ctx) => eudkit_map::assemble_map_with_context(inputs, ctx)?,
        None => assemble_map(inputs)?,
    };
    // Report the cost of the exact document assembly encoded, so the
    // returned number and the written map cannot disagree.
    Ok(summary.static_cost)
}

#[cfg(test)]
mod tests {
    use super::*;
    use eudkit_hir::{
        HirBlob, HirBlobReloc, HirCond, HirOnce, HirReloc, HirTrigger, HirVarDecl, HirVarStore,
        HirVarTransfer, HirWhen,
    };
    use eudkit_lir::{
        ACT_COUNT, EUDX_MAGIC, RawTrigger, epd_u32, init_simulation, lir2payload,
        simulate_trigger_loop, static_cost,
    };

    /// The pre-pass string walk must reproduce the exact ordered list the
    /// final lowering interns — that equivalence is what lets the pipeline
    /// drop the full `lower.probe` pass. Covers template seeding, duplicate
    /// module/action text, WAV names, `str_index` suppression (FixedString
    /// buffers) and actions emitted from control flow.
    #[test]
    fn collect_strings_matches_the_final_string_table() {
        let mut module = HirModule::new();
        module.strings = vec![
            "module-alpha".to_string(),
            "module-beta".to_string(),
            "module-alpha".to_string(),
        ];
        let mut fixed_buffer = HirAct::display_text("suppressed", 0);
        fixed_buffer.str_index = Some(1);
        let mut wav_text = HirAct::display_text("wav-text", 0);
        wav_text.wav = Some("sound\\bark.wav".to_string());
        module.stmts = vec![
            HirStmt::Act(HirAct::display_text("hello", 4)),
            HirStmt::When(HirWhen::new(
                vec![HirCond::always()],
                vec![
                    HirStmt::Act(HirAct::display_text("hello", 4)),
                    HirStmt::Act(wav_text),
                ],
            )),
            HirStmt::Once(HirOnce::new(
                vec![HirCond::always()],
                vec![HirStmt::Act(fixed_buffer)],
            )),
            HirStmt::Act(HirAct::display_text("module-beta", 4)),
        ];

        let optimized = optimize_module(&module).unwrap();
        let storage = analyze_storage(&optimized);
        let template = vec![b64_encode(b"TEMPLATE-0"), b64_encode(b"TEMPLATE-1")];
        let plan = LoweringPlan::build(&optimized, &storage).unwrap();
        let collected = collect_strings(&optimized, &plan, &template);
        let lir =
            lower_optimized_module_with_plan(&optimized, &storage, &plan, &template, None).unwrap();

        assert_eq!(collected, lir.module.strings);
        // Sanity: the table is template-seeded, not a module-local list.
        assert_eq!(&collected[..2], template.as_slice());
        assert!(collected.len() > 3, "fixture must intern action strings");
    }

    /// The pipeline must hand the lowering parse back for map assembly; a
    /// regression here silently restores the second ~11.4 ms template parse.
    #[test]
    fn prepare_lowering_hands_back_the_basemap_context() {
        let doc = HirDocument::new(HirModule {
            vars: Vec::new(),
            stmts: vec![HirStmt::Act(HirAct::display_text("Hello EUD!", 4))],
            strings: Vec::new(),
            files: Vec::new(),
            blobs: Vec::new(),
            funcs: Vec::new(),
        });
        let basemap = Path::new(concat!(
            env!("CARGO_MANIFEST_DIR"),
            "/../../tests/outputmap/basemap/basemap.scx"
        ));

        let prepared = prepare_lowering(&doc, Some(basemap)).unwrap();
        let ctx = prepared
            .basemap_context
            .as_ref()
            .expect("a basemap compile must hand its parse to map assembly");
        // The recorded offsets were resolved against this very context.
        assert_eq!(
            ctx.str_offsets_for_full_strings(&prepared.lir.module.strings)
                .unwrap(),
            prepared.lir.module.str_addrs
        );

        let prepared = prepare_lowering(&doc, None).unwrap();
        assert!(prepared.basemap_context.is_none());
    }

    #[test]
    fn new_temporal_nodes_lower_successfully_after_desugar() {
        // All four C11 phase-1 nodes desugar in opt (which
        // lower_module_inner runs first), so each now lowers
        // successfully end to end; the dispatch arms stay as
        // unreachable defense-in-depth. This replaces the retired
        // fail-loud pin: no new node fails loud anymore.
        for stmt in [
            HirStmt::Until(eudkit_hir::HirUntil::new(
                vec![eudkit_hir::HirCond::always()],
                vec![HirStmt::Act(eudkit_hir::HirAct::set_deaths(0, 7, 1, 0))],
                0,
            )),
            HirStmt::Edge(eudkit_hir::HirEdge::new(
                eudkit_hir::HirEdgeDirection::Rising,
                vec![eudkit_hir::HirCond::always()],
                vec![HirStmt::Act(eudkit_hir::HirAct::set_deaths(0, 7, 1, 0))],
                0,
                1,
            )),
            HirStmt::Every(eudkit_hir::HirEvery::new(
                3,
                0,
                vec![HirStmt::Act(eudkit_hir::HirAct::set_deaths(0, 7, 1, 0))],
            )),
            HirStmt::After(eudkit_hir::HirAfter::new(
                2,
                0,
                vec![HirStmt::Act(eudkit_hir::HirAct::set_deaths(0, 7, 1, 0))],
            )),
        ] {
            // Every node-carried id must name a declared variable for
            // the opt-entry validation to pass.
            let doc = HirDocument::new(HirModule {
                vars: vec![HirVarDecl::new(0, 0), HirVarDecl::new(1, 0)],
                stmts: vec![stmt],
                strings: Vec::new(),
                files: Vec::new(),
                blobs: Vec::new(),
                funcs: Vec::new(),
            });
            assert!(
                lower_document(&doc).is_ok(),
                "desugared temporal node must lower successfully"
            );
        }
    }

    #[test]
    fn lowers_trigger_16c_64a_to_2408_byte_raw_trigger_and_chains_next() {
        let mut t0 = HirTrigger::new(
            0,
            vec![HirCond::always(); 16],
            vec![HirAct::set_deathsx(epd_u32(0x58A364), 7, 123, 0, 0xFFFF_FFFF); 64],
            true,
        )
        .unwrap();
        // Wire SetNextPtr(t0, t1) as act[0] of t0
        t0.acts[0] = HirAct {
            mask: 0,
            strid: 0,
            wavid: 0,
            time: 0,
            dest: 0,
            value: 0,
            unit: 0,
            acttype: 45,
            modifier: 7,
            flags: 20,
            padding: 0,
            eudx: 0,
            text: None,
            wav: None,
            uprp: None,
            str_index: None,
            relocs: vec![
                HirReloc::trigger(16, 0, 4, 1), // dest = EPD(t0 + 4)
                HirReloc::trigger(20, 1, 0, 4), // value = ptr(t1 + 0)
            ],
        };

        let t1 = HirTrigger::new(
            1,
            vec![HirCond::deaths(0, 10, 1, 0)],
            vec![HirAct::display_text("Hello EUD!", 4)],
            true,
        )
        .unwrap();

        let doc = HirDocument::new(HirModule {
            vars: Vec::new(),
            stmts: vec![HirStmt::Trigger(t0), HirStmt::Trigger(t1)],

            strings: Vec::new(),
            files: Vec::new(),
            blobs: Vec::new(),
            funcs: Vec::new(),
        });

        let lir = lower_document(&doc).unwrap();
        let cost = static_cost(&lir);
        assert_eq!(cost.triggers, 2);
        assert_eq!(cost.conditions, 17);
        assert_eq!(cost.actions, 65);
        assert_eq!(cost.trigger_bytes, 4816);
        assert_eq!(cost.total_bytes, 4816);

        let payload = lir2payload(&lir).unwrap();
        assert_eq!(payload.data.len(), 4816);

        let raw0 = RawTrigger::decode(payload.data[0..RAW_TRIGGER_BYTE_SIZE].try_into().unwrap());
        let raw1 = RawTrigger::decode(payload.data[RAW_TRIGGER_BYTE_SIZE..2 * RAW_TRIGGER_BYTE_SIZE].try_into().unwrap());

        // t0.prev == -1, t0.next == offset of t1 (2408)
        assert_eq!(raw0.prev, u32::MAX);
        assert_eq!(raw0.next, 2408);
        // SetNextPtr(t0, t1) in raw0.acts[0]: dest == EPD(4), value == 2408
        assert_eq!(raw0.acts[0].dest, epd_u32(4));
        assert_eq!(raw0.acts[0].value, 2408);

        // t1.prev == 0, t1.next == 0x80000000
        assert_eq!(raw1.prev, 0);
        assert_eq!(raw1.next, 0x8000_0000);
        assert_eq!(raw1.acts[0].acttype, 9);
        assert_eq!(raw1.acts[0].strid, 1);
    }

    #[test]
    fn allocates_self_targeted_64a_trigger_pages_and_firing_at_rest_is_pure_noop() {
        // 64 variables fit in 1 TriggerPage (2,408 bytes)
        let vars: Vec<HirVarDecl> = (0..64)
            .map(|k| HirVarDecl::pinned(k, 1000 + k as u32))
            .collect();
        let doc = HirDocument::new(HirModule {
            vars,
            stmts: Vec::new(),

            strings: Vec::new(),
            files: Vec::new(),
            blobs: Vec::new(),
            funcs: Vec::new(),
        });
        let lir = lower_document(&doc).unwrap();
        let cost = static_cost(&lir);
        assert_eq!(cost.triggers, 1);
        assert_eq!(cost.trigger_bytes, 2408);

        let payload = lir2payload(&lir).unwrap();
        let page0 = RawTrigger::decode(payload.data[0..RAW_TRIGGER_BYTE_SIZE].try_into().unwrap());
        for k in 0..ACT_COUNT {
            let expected_value_addr = act_field_byte_offset(k, VALUE_FIELD_OFFSET) as u32;
            assert_eq!(page0.acts[k].acttype, 45);
            assert_eq!(page0.acts[k].modifier, 7); // SetTo
            assert_eq!(page0.acts[k].mask, 0xFFFF_FFFF);
            assert_eq!(page0.acts[k].eudx, EUDX_MAGIC);
            assert_eq!(page0.acts[k].dest, epd_u32(expected_value_addr));
            assert_eq!(page0.acts[k].value, 1000 + k as u32);
        }

        // Firing the TriggerPage at rest must be a 100% no-op on all 64 slots
        let sim = simulate_trigger_loop(&lir).unwrap();
        assert_eq!(sim.last_executed_cost.triggers, 1);
        assert_eq!(sim.raw_trigger(0), page0);
    }

    #[test]
    fn lowers_1a_in_place_constant_ops_and_1c_conditions_with_0t_marginal_cost() {
        // x = 10, y = 20, z = 0
        // x.set(100); x.iadd(5); y.isub(3);
        // Trigger: if x == 105 && y >= 17 && y <= 17 -> z.set(777)
        let doc = HirDocument::new(HirModule {
            vars: vec![
                HirVarDecl::pinned(0, 10),
                HirVarDecl::pinned(1, 20),
                HirVarDecl::pinned(2, 0),
            ],
            stmts: vec![
                HirStmt::Act(HirAct::set_var_const(0, 7, 100, 0xFFFF_FFFF)),
                HirStmt::Act(HirAct::set_var_const(0, 8, 5, 0xFFFF_FFFF)),
                HirStmt::Act(HirAct::set_var_const(1, 9, 3, 0xFFFF_FFFF)),
                HirStmt::Trigger(
                    HirTrigger::new(
                        0,
                        vec![
                            HirCond::var_deaths(0, 10, 105), // x == 105 (Exactly)
                            HirCond::var_deaths(1, 0, 17),   // y >= 17 (AtLeast)
                            HirCond::var_deaths(1, 1, 17),   // y <= 17 (AtMost)
                        ],
                        vec![HirAct::set_var_const(2, 7, 777, 0xFFFF_FFFF)],
                        true,
                    )
                    .unwrap(),
                ),
            ],

            strings: Vec::new(),
            files: Vec::new(),
            blobs: Vec::new(),
            funcs: Vec::new(),
        });

        let lir = lower_document(&doc).unwrap();
        let cost = static_cost(&lir);
        // 1 packed trigger for the 3 constant updates + 1 conditional trigger + 1 TriggerPage = 3 triggers
        assert_eq!(cost.triggers, 3);

        let sim = simulate_trigger_loop(&lir).unwrap();
        // Only the 2 execution triggers run (TriggerPage at index 2 is NOT fired for constant ops!)
        assert_eq!(sim.last_executed_cost.triggers, 2);
        assert_eq!(sim.last_executed_cost.visited_items, vec![0, 1]);
        assert_eq!(sim.read_page_slot_value(2, 0), 105);
        assert_eq!(sim.read_page_slot_value(2, 1), 17);
        assert_eq!(sim.read_page_slot_value(2, 2), 777);
    }

    #[test]
    fn coalesced_var_batch_fires_shared_trigger_page_once_and_restores_self_targeted_invariant() {
        // x = 10, y = 20, z = 30, a = 0, b = 0, c = 5
        // a.set(x); b.set(y); c.iadd(z);
        let doc = HirDocument::new(HirModule {
            vars: vec![
                HirVarDecl::pinned(0, 10),
                HirVarDecl::pinned(1, 20),
                HirVarDecl::pinned(2, 30),
                HirVarDecl::pinned(3, 0),
                HirVarDecl::pinned(4, 0),
                HirVarDecl::pinned(5, 5),
            ],
            stmts: vec![
                HirStmt::VarTransfer(HirVarTransfer {
                    dst_var: 3,
                    src_var: 0,
                    modifier: 7,
                }),
                HirStmt::VarTransfer(HirVarTransfer {
                    dst_var: 4,
                    src_var: 1,
                    modifier: 7,
                }),
                HirStmt::VarTransfer(HirVarTransfer {
                    dst_var: 5,
                    src_var: 2,
                    modifier: 8,
                }),
            ],

            strings: Vec::new(),
            files: Vec::new(),
            blobs: Vec::new(),
            funcs: Vec::new(),
        });

        let lir = lower_document(&doc).unwrap();
        let cost = static_cost(&lir);
        // Item 0: T_setup, Item 1: T_cont, Item 2: TriggerPage 0 -> 3 triggers total
        assert_eq!(cost.triggers, 3);

        let sim = simulate_trigger_loop(&lir).unwrap();
        // Executed path: T_setup (0) -> TriggerPage 0 (2) -> T_cont (1) -> 3 triggers executed!
        // TriggerPage 0 (item 2) is fired ONLY ONCE for all 3 variable reads!
        assert_eq!(sim.last_executed_cost.triggers, 3);
        assert_eq!(sim.last_executed_cost.visited_items, vec![0, 2, 1]);

        // Verify runtime values in TriggerPage 0 (item 2):
        assert_eq!(sim.read_page_slot_value(2, 0), 10); // x
        assert_eq!(sim.read_page_slot_value(2, 1), 20); // y
        assert_eq!(sim.read_page_slot_value(2, 2), 30); // z
        assert_eq!(sim.read_page_slot_value(2, 3), 10); // a = x = 10
        assert_eq!(sim.read_page_slot_value(2, 4), 20); // b = y = 20
        assert_eq!(sim.read_page_slot_value(2, 5), 35); // c = 5 + z = 35

        // Verify all 64 slots of TriggerPage 0 are restored to self-targeted rest invariant!
        let page0_after = sim.raw_trigger(2);
        let page0_base = sim.payload.addrs[2];
        for k in 0..ACT_COUNT {
            let self_val_addr = page0_base + act_field_byte_offset(k, VALUE_FIELD_OFFSET) as u32;
            assert_eq!(page0_after.acts[k].dest, epd_u32(self_val_addr));
            assert_eq!(page0_after.acts[k].modifier, 7); // SetTo restored!
            assert_eq!(page0_after.acts[k].acttype, 45);
            assert_eq!(page0_after.acts[k].eudx, EUDX_MAGIC);
            assert_eq!(page0_after.acts[k].mask, 0xFFFF_FFFF);
        }

        // SC:R snapshots a trigger's action fields when the trigger starts.
        // Re-run with that model for three loops to prove destination slots'
        // self-writes neither erase the transfers nor leave the pages parked
        // (the pre-parking implementation loses all three values on loop 1).
        let mut snap = init_simulation(&lir).unwrap();
        snap.snapshot_triggers = true;
        for (loop_idx, expected_c) in [(1usize, 35u32), (2, 65), (3, 95)] {
            snap.step_loop();
            assert_eq!(snap.read_page_slot_value(2, 3), 10, "loop {loop_idx}"); // a = x
            assert_eq!(snap.read_page_slot_value(2, 4), 20, "loop {loop_idx}"); // b = y
            assert_eq!(
                snap.read_page_slot_value(2, 5),
                expected_c,
                "loop {loop_idx}"
            ); // c = 5 + loop * z

            // Parking is per firing, not one-shot: every loop must restore the
            // self-targeted rest invariant before the next firing.
            let page = snap.raw_trigger(2);
            let page_base = snap.payload.addrs[2];
            for k in 0..ACT_COUNT {
                let self_val_addr = page_base + act_field_byte_offset(k, VALUE_FIELD_OFFSET) as u32;
                assert_eq!(
                    page.acts[k].dest,
                    epd_u32(self_val_addr),
                    "loop {loop_idx} slot {k} must be restored"
                );
                assert_eq!(page.acts[k].modifier, 7, "loop {loop_idx} slot {k}");
                assert_eq!(page.acts[k].acttype, 45, "loop {loop_idx} slot {k}");
                assert_eq!(page.acts[k].eudx, EUDX_MAGIC, "loop {loop_idx} slot {k}");
                assert_eq!(page.acts[k].mask, 0xFFFF_FFFF, "loop {loop_idx} slot {k}");
            }
        }
    }

    #[test]
    fn compiles_hir_to_valid_scx_map_with_trig_and_strx() {
        let t0 = HirTrigger::new(
            0,
            vec![HirCond::always()],
            vec![
                HirAct::set_deaths(epd_u32(0x58A364), 7, 42, 0),
                HirAct::display_text("Ticket 1 Tracer Bullet!", 4),
            ],
            true,
        )
        .unwrap();
        let doc = HirDocument::new(HirModule {
            vars: Vec::new(),
            stmts: vec![HirStmt::Trigger(t0)],

            strings: Vec::new(),
            files: Vec::new(),
            blobs: Vec::new(),
            funcs: Vec::new(),
        });
        let hir_json = eudkit_hir::hir_to_json(&doc).unwrap();

        let basemap = Path::new(concat!(
            env!("CARGO_MANIFEST_DIR"),
            "/../../tests/outputmap/basemap/basemap.scx"
        ));
        let dir = tempfile::tempdir().unwrap();
        let out_scx = dir.path().join("tracer_bullet.scx");

        let cost = compile_hir_to_map(&hir_json, basemap, &out_scx).unwrap();
        let lowered = lower_document(&doc).unwrap();
        // `compile_hir_to_map` must report the assembled cost: the program
        // plus the 35-trigger once-per-tick gate appended during assembly.
        // This equality is the same pre-write contract the plan cost gate
        // uses; the exact counts below keep it independent of the helper.
        assert_eq!(cost, eudkit_map::assembled_static_cost(&lowered));
        assert_eq!(
            cost.triggers,
            1 + eudkit_map::tick_gate::GATE_TRIGGER_COUNT
        );
        assert_eq!(
            cost.conditions,
            1 + eudkit_map::tick_gate::GATE_CONDITION_COUNT
        );
        assert_eq!(cost.actions, 2 + eudkit_map::tick_gate::GATE_ACTION_COUNT);
        assert!(out_scx.is_file());

        // Open output .scx with stormlib and verify scenario.chk contains TRIG and STRx
        let mut ar =
            stormlib::Archive::open(&out_scx, stormlib::OpenArchiveFlags::empty()).unwrap();
        let chk_bytes = ar
            .open_file("staredit\\scenario.chk")
            .unwrap()
            .read_all()
            .unwrap();
        drop(ar);
        let mut chkt = eudkit_map::chk::Chk::new();
        assert!(chkt.loadchk(&chk_bytes));
        assert!(chkt.getsection(b"TRIG").is_some());
        let strx = chkt.getsection(b"STRx").expect("STRx section must exist");
        assert!(
            strx.windows(b"Ticket 1 Tracer Bullet!".len())
                .any(|w| w == b"Ticket 1 Tracer Bullet!"),
            "STRx must contain both DisplayText string and 2408-byte trigger payload"
        );
    }

    #[test]
    fn compile_hir_to_map_with_options_can_omit_the_tick_gate() {
        let t0 = HirTrigger::new(
            0,
            vec![HirCond::always()],
            vec![HirAct::set_deaths(epd_u32(0x58A364), 7, 42, 0)],
            true,
        )
        .unwrap();
        let doc = HirDocument::new(HirModule {
            vars: Vec::new(),
            stmts: vec![HirStmt::Trigger(t0)],
            strings: Vec::new(),
            files: Vec::new(),
            blobs: Vec::new(),
            funcs: Vec::new(),
        });
        let hir_json = eudkit_hir::hir_to_json(&doc).unwrap();
        let basemap = Path::new(concat!(
            env!("CARGO_MANIFEST_DIR"),
            "/../../tests/outputmap/basemap/basemap.scx"
        ));
        let dir = tempfile::tempdir().unwrap();
        let gated_out = dir.path().join("gated.scx");
        let ungated_out = dir.path().join("ungated.scx");

        let gated = compile_hir_to_map(&hir_json, basemap, &gated_out).unwrap();
        let ungated = compile_hir_to_map_with_options(
            &hir_json,
            basemap,
            &ungated_out,
            AssemblyOptions {
                enable_tick_gate: false,
            },
        )
        .unwrap();

        let lowered = lower_document(&doc).unwrap();
        let program = eudkit_lir::static_cost(&lowered);
        // Default stays gated; opt-out writes exactly the program payload.
        assert_eq!(gated, eudkit_map::assembled_static_cost(&lowered));
        assert_eq!(ungated, program);
        assert_eq!(
            gated.triggers,
            program.triggers + eudkit_map::tick_gate::GATE_TRIGGER_COUNT
        );
        assert_eq!(
            gated.total_bytes,
            program.total_bytes
                + eudkit_map::tick_gate::GATE_TRIGGER_COUNT
                    * eudkit_lir::RAW_TRIGGER_BYTE_SIZE
        );
        assert_eq!(
            ungated.total_bytes, program.total_bytes,
            "opt-out must write exactly the program payload"
        );
        assert!(gated_out.is_file());
        assert!(ungated_out.is_file());
    }

    #[test]
    fn demotes_boolean_and_integer_scalars_to_cells_and_simulates_them() {
        use eudkit_opt::cell::analyze_storage;

        let module = HirModule {
            vars: vec![HirVarDecl::new(0, 0), HirVarDecl::new(1, 42)],
            stmts: vec![
                HirStmt::Act(HirAct::set_var_const(0, 7, 1, 0xFFFF_FFFF)),
                HirStmt::Act(HirAct::set_var_const(1, 7, 7, 0xFFFF_FFFF)),
                HirStmt::Trigger(
                    HirTrigger::new(
                        0,
                        vec![HirCond::var_deaths(0, 10, 1), HirCond::var_deaths(1, 10, 7)],
                        vec![HirAct::set_var_const(0, 7, 0, 0xFFFF_FFFF)],
                        true,
                    )
                    .unwrap(),
                ),
            ],

            strings: Vec::new(),
            files: Vec::new(),
            blobs: Vec::new(),
            funcs: Vec::new(),
        };
        let opt = eudkit_opt::optimize_module(&module).unwrap();
        let plan = analyze_storage(&opt);
        assert_eq!(plan.trigger_pages, 0);
        assert_eq!(plan.dword_cell_vars, 1);
        assert_eq!(plan.bit_cell_vars, 1);
        assert_eq!(plan.cell_dwords, 2);

        let lir = lower_module(&module).unwrap();
        let cost = static_cost(&lir);
        // Two execution triggers and no TriggerPage; one cell blob item.
        assert_eq!(cost.triggers, 2);
        assert_eq!(cost.blob_bytes, 8);

        let sim = simulate_trigger_loop(&lir).unwrap();
        let blob_idx = lir
            .module
            .items
            .iter()
            .position(|item| matches!(item, LirItem::Blob(_)))
            .unwrap();
        let blob = sim.payload.addrs[blob_idx] as usize;
        let dword0 = u32::from_le_bytes(sim.payload.data[blob..blob + 4].try_into().unwrap());
        let dword1 = u32::from_le_bytes(sim.payload.data[blob + 4..blob + 8].try_into().unwrap());
        assert_eq!(dword0, 7); // v1 = 7
        assert_eq!(dword1 & 1, 0); // v0 cleared back to 0
        // Every executed write used SetDeathsX with a value-address relocation.
        assert_eq!(sim.last_executed_cost.actions, 3);
    }

    #[test]
    fn bit_cell_init_sets_bits_past_the_first_byte() {
        // Regression test (exposed by C11-02): an init-1 bit var at bit
        // index >= 8 must set its bit in the correct blob byte. The old
        // code wrote `cell_init[dword * 4] |= 1 << bit_index` on a u8,
        // which panics in debug (and silently sets the wrong bit in
        // release) for any bit index >= 8.
        let mut vars: Vec<HirVarDecl> = (0..10).map(|id| HirVarDecl::new(id, 0)).collect();
        vars[9] = HirVarDecl::new(9, 1);
        let module = HirModule {
            vars,
            stmts: vec![HirStmt::Trigger(
                HirTrigger::new(0, vec![HirCond::always()], vec![], true).unwrap(),
            )],
            strings: Vec::new(),
            files: Vec::new(),
            blobs: Vec::new(),
            funcs: Vec::new(),
        };
        let plan = eudkit_opt::cell::analyze_storage(&module);
        assert_eq!(plan.bit_cell_vars, 10);
        assert_eq!(plan.cell_dwords, 1);
        assert!(matches!(
            plan.storage(9),
            Some(StorageClass::BitCell {
                dword_index: 0,
                bit_index: 9,
            })
        ));

        let lir = lower_module(&module).unwrap();
        let sim = simulate_trigger_loop(&lir).unwrap();
        let blob_idx = lir
            .module
            .items
            .iter()
            .position(|item| matches!(item, LirItem::Blob(_)))
            .unwrap();
        let blob = sim.payload.addrs[blob_idx] as usize;
        let dword0 = u32::from_le_bytes(sim.payload.data[blob..blob + 4].try_into().unwrap());
        assert_eq!(dword0, 1 << 9);
    }

    #[test]
    fn lowers_and_simulates_hardware_masked_struct_fields_and_1t_whole_struct_copy() {
        // Struct Hero { hp: dword (0xFFFFFFFF), mp: word (0xFFFF), armor: byte (0xFF), flags: bit(4) (0x0F) }
        // s1 (vars 0..3): hp=1000, mp=0x1234, armor=0xAB, flags=0x0D
        // s2 (vars 4..7): hp=0, mp=0, armor=0, flags=0
        // Dword var raw (var 8) = 0x12345678, out_armor (var 9) = 0xDEAD0000
        let vars = vec![
            HirVarDecl::pinned_with_mask(0, 1000, 0xFFFF_FFFF),
            HirVarDecl::pinned_with_mask(1, 0x1234, 0x0000_FFFF),
            HirVarDecl::pinned_with_mask(2, 0xAB, 0x0000_00FF),
            HirVarDecl::pinned_with_mask(3, 0x0D, 0x0000_000F),
            HirVarDecl::pinned_with_mask(4, 0, 0xFFFF_FFFF),
            HirVarDecl::pinned_with_mask(5, 0, 0x0000_FFFF),
            HirVarDecl::pinned_with_mask(6, 0, 0x0000_00FF),
            HirVarDecl::pinned_with_mask(7, 0, 0x0000_000F),
            HirVarDecl::pinned_with_mask(8, 0x1234_5678, 0xFFFF_FFFF),
            HirVarDecl::pinned_with_mask(9, 0xDEAD_0000, 0xFFFF_FFFF),
        ];

        // 1) Whole-struct copy s2.set(s1) (4 transfers on Page 0)
        // 2) Cross-mask transfer: s2.armor.set(raw) -> should hardware-mask 0x12345678 to 0x78
        // 3) Read sub-dword field into dirty dword: out_armor.set(s2.armor) -> should produce 0x78
        let stmts = vec![
            HirStmt::VarTransfer(HirVarTransfer {
                dst_var: 4,
                src_var: 0,
                modifier: 7,
            }),
            HirStmt::VarTransfer(HirVarTransfer {
                dst_var: 5,
                src_var: 1,
                modifier: 7,
            }),
            HirStmt::VarTransfer(HirVarTransfer {
                dst_var: 6,
                src_var: 2,
                modifier: 7,
            }),
            HirStmt::VarTransfer(HirVarTransfer {
                dst_var: 7,
                src_var: 3,
                modifier: 7,
            }),
            HirStmt::VarTransfer(HirVarTransfer {
                dst_var: 6,
                src_var: 8,
                modifier: 7,
            }),
            HirStmt::VarTransfer(HirVarTransfer {
                dst_var: 9,
                src_var: 6,
                modifier: 7,
            }),
        ];

        let doc = HirDocument::new(HirModule {
            vars,
            stmts,
            strings: Vec::new(),
            files: Vec::new(),
            blobs: Vec::new(),
            funcs: Vec::new(),
        });
        let lir = lower_document(&doc).unwrap();
        let sim = simulate_trigger_loop(&lir).unwrap();
        let page_item = lir.module.items.len() - 1;

        assert_eq!(sim.read_page_slot_value(page_item, 4), 1000);
        assert_eq!(sim.read_page_slot_value(page_item, 5), 0x1234);
        assert_eq!(sim.read_page_slot_value(page_item, 6), 0x78);
        assert_eq!(sim.read_page_slot_value(page_item, 7), 0x0D);
        assert_eq!(sim.read_page_slot_value(page_item, 9), 0x78);

        // Verify per-action hardware masks and self-targeted invariant are preserved at rest
        let page_raw = sim.raw_trigger(page_item);
        assert_eq!(page_raw.acts[0].mask, 0xFFFF_FFFF);
        assert_eq!(page_raw.acts[1].mask, 0x0000_FFFF);
        assert_eq!(page_raw.acts[2].mask, 0x0000_00FF);
        assert_eq!(page_raw.acts[3].mask, 0x0000_000F);
        assert_eq!(page_raw.acts[8].mask, 0xFFFF_FFFF);
    }

    #[test]
    fn lowers_and_simulates_64_field_whole_struct_copy_firing_source_page_once() {
        let mut vars = Vec::with_capacity(128);
        for k in 0..64 {
            vars.push(HirVarDecl::pinned_with_mask(k, 500 + k as u32, 0xFFFF_FFFF));
        }
        for k in 0..64 {
            vars.push(HirVarDecl::pinned_with_mask(64 + k, 0, 0xFFFF_FFFF));
        }
        let stmts: Vec<HirStmt> = (0..64)
            .map(|k| {
                HirStmt::VarTransfer(HirVarTransfer {
                    dst_var: 64 + k,
                    src_var: k,
                    modifier: 7,
                })
            })
            .collect();
        let doc = HirDocument::new(HirModule {
            vars,
            stmts,
            strings: Vec::new(),
            files: Vec::new(),
            blobs: Vec::new(),
            funcs: Vec::new(),
        });
        let lir = lower_document(&doc).unwrap();
        let sim = simulate_trigger_loop(&lir).unwrap();

        let page0_item = lir.module.items.len() - 2;
        let page1_item = lir.module.items.len() - 1;
        // Source TriggerPage 0 must be visited ONLY ONCE (`1T executed` page firing)!
        let page0_visits = sim
            .last_executed_cost
            .visited_items
            .iter()
            .filter(|&&i| i == page0_item)
            .count();
        assert_eq!(page0_visits, 1);

        for k in 0..64 {
            assert_eq!(sim.read_page_slot_value(page1_item, k), 500 + k as u32);
        }
    }

    // Helpers for inspecting transfer setup/restore actions without
    // relying on `ExecAct` equality (it derives only `Debug`/`Clone`).
    fn var_targets(act: &ExecAct) -> Vec<(usize, usize, usize)> {
        act.relocs
            .iter()
            .filter_map(|r| match r.target {
                ExecRelocTarget::Var {
                    var_id,
                    slot_offset,
                } => Some((r.field_offset, var_id, slot_offset)),
                _ => None,
            })
            .collect()
    }

    fn page_set_next_targets(act: &ExecAct) -> Option<(usize, ExecRelocTarget)> {
        if act.relocs.len() != 2 {
            return None;
        }
        match (&act.relocs[0].target, &act.relocs[1].target) {
            (ExecRelocTarget::Page { page_idx, .. }, next) => {
                Some((*page_idx, next.clone()))
            }
            _ => None,
        }
    }

    #[test]
    fn exec_act_default_and_new_constructors() {
        let def = ExecAct::default();
        assert!(def.text.is_none());
        assert!(def.wav.is_none());
        assert!(def.uprp.is_none());
        assert!(def.str_index.is_none());
        assert!(def.relocs.is_empty());
        assert!(def.string_relocs.is_empty());

        let ctrig = CTrigAction::set_deaths(0, 7, 42, 0);
        let relocs = vec![ExecReloc {
            field_offset: DEST_FIELD_OFFSET,
            target: ExecRelocTarget::Var {
                var_id: 1,
                slot_offset: 16,
            },
            mode: 1,
        }];
        let act = ExecAct::new(ctrig, relocs.clone());
        assert_eq!(act.ctrig, ctrig);
        assert_eq!(act.relocs.len(), 1);
        assert!(act.text.is_none());
        assert!(act.string_relocs.is_empty());
    }

    #[test]
    fn transfer_plans_build_setup_restore_and_page_chain_in_order() {
        use crate::emit_transfer_plans;
        // Plan A touches modifier and mask, plan B is dest-only, and the
        // plans sit on different pages, so every sequence rule is visible.
        let plans = vec![
            TransferPlan {
                src_var: 0,
                dst: TransferDest::Var(2),
                modifier: 8,
                src_mask: 0xFFFF_FFFF,
                dst_mask: 0x0000_FFFF,
            },
            TransferPlan {
                src_var: 1,
                dst: TransferDest::Var(3),
                modifier: 7,
                src_mask: 0xFFFF_FFFF,
                dst_mask: 0xFFFF_FFFF,
            },
        ];
        let mut page_slot = HashMap::new();
        page_slot.insert(0usize, (0usize, 0usize));
        page_slot.insert(1usize, (1usize, 0usize));
        let mut cur = Vec::new();
        let mut must_emit = false;
        let mut out = Vec::new();
        emit_transfer_plans(&plans, &page_slot, &mut cur, &mut must_emit, &mut out).unwrap();

        // One setup trigger: 4 setup acts + 2 page-set-next acts, whose
        // next is the first active page.
        assert_eq!(out.len(), 1);
        assert_eq!(out[0].acts.len(), 6);
        assert!(matches!(out[0].next, ExecNext::Page(0)));
        assert!(must_emit);
        // Setup order is dest, then modifier, then mask per plan.
        assert_eq!(
            var_targets(&out[0].acts[0]),
            vec![
                (DEST_FIELD_OFFSET, 0, DEST_FIELD_OFFSET),
                (VALUE_FIELD_OFFSET, 2, VALUE_FIELD_OFFSET),
            ]
        );
        assert_eq!(var_targets(&out[0].acts[1]), vec![(
            DEST_FIELD_OFFSET,
            0,
            MODIFIER_DWORD_OFFSET,
        )]);
        assert_eq!(out[0].acts[1].ctrig.value, (8u32 << 24));
        assert_eq!(out[0].acts[1].ctrig.mask, 0xFF00_0000);
        assert_eq!(
            var_targets(&out[0].acts[2]),
            vec![(DEST_FIELD_OFFSET, 0, MASK_FIELD_OFFSET)]
        );
        assert_eq!(out[0].acts[2].ctrig.value, 0x0000_FFFF);
        assert_eq!(
            var_targets(&out[0].acts[3]),
            vec![
                (DEST_FIELD_OFFSET, 1, DEST_FIELD_OFFSET),
                (VALUE_FIELD_OFFSET, 3, VALUE_FIELD_OFFSET),
            ]
        );
        // Page chain: page 0 -> page 1 -> continuation (exec index 1).
        assert_eq!(
            page_set_next_targets(&out[0].acts[4]).map(|(p, _)| p),
            Some(0)
        );
        assert!(matches!(
            page_set_next_targets(&out[0].acts[4]).map(|(_, t)| t),
            Some(ExecRelocTarget::Page { page_idx: 1, .. })
        ));
        assert_eq!(
            page_set_next_targets(&out[0].acts[5]).map(|(p, _)| p),
            Some(1)
        );
        assert!(matches!(
            page_set_next_targets(&out[0].acts[5]).map(|(_, t)| t),
            Some(ExecRelocTarget::ExecItem { exec_idx: 1, .. })
        ));
        // Restore acts are the inverse per-slot mapping, in plan order
        // (not reversed): dest restore, modifier restore to SetTo, mask
        // restore to the source mask, then plan B's dest restore.
        assert_eq!(cur.len(), 4);
        assert_eq!(
            var_targets(&cur[0]),
            vec![
                (DEST_FIELD_OFFSET, 0, DEST_FIELD_OFFSET),
                (VALUE_FIELD_OFFSET, 0, VALUE_FIELD_OFFSET),
            ]
        );
        assert_eq!(cur[1].ctrig.value, (7u32 << 24));
        assert_eq!(
            var_targets(&cur[2]),
            vec![(DEST_FIELD_OFFSET, 0, MASK_FIELD_OFFSET)]
        );
        assert_eq!(cur[2].ctrig.value, 0xFFFF_FFFF);
        assert_eq!(
            var_targets(&cur[3]),
            vec![
                (DEST_FIELD_OFFSET, 1, DEST_FIELD_OFFSET),
                (VALUE_FIELD_OFFSET, 1, VALUE_FIELD_OFFSET),
            ]
        );
    }

    #[test]
    fn transfer_setup_chunking_computes_continuation_index_from_trigger_count() {
        use crate::emit_transfer_plans;
        // 30 plans x 3 setup acts + 1 page act = 91 acts -> 2 setup
        // triggers, so the continuation is exec index 2.
        let plans: Vec<TransferPlan> = (0..30)
            .map(|k| TransferPlan {
                src_var: k,
                dst: TransferDest::Var(100 + k),
                modifier: 8,
                src_mask: 0xFFFF_FFFF,
                dst_mask: 0x0000_FFFF,
            })
            .collect();
        let mut page_slot = HashMap::new();
        for k in 0..30 {
            page_slot.insert(k, (0usize, k));
        }
        let mut cur = Vec::new();
        let mut must_emit = false;
        let mut out = Vec::new();
        emit_transfer_plans(&plans, &page_slot, &mut cur, &mut must_emit, &mut out).unwrap();
        assert_eq!(out.len(), 2);
        assert_eq!(out[0].acts.len(), 64);
        assert_eq!(out[1].acts.len(), 27);
        assert!(matches!(out[0].next, ExecNext::DefaultChain));
        assert!(matches!(out[1].next, ExecNext::Page(0)));
        let last = out[1].acts.last().unwrap();
        assert!(matches!(
            page_set_next_targets(last).map(|(_, t)| t),
            Some(ExecRelocTarget::ExecItem { exec_idx: 2, .. })
        ));
    }

    #[test]
    fn transfer_setup_carries_previous_restore_acts_before_new_setup_acts() {
        use crate::emit_transfer_plans;
        // A previous batch's restore acts already in `cur` execute before
        // the next batch's setup acts in the same trigger.
        let prior = ExecAct {
            ctrig: CTrigAction::set_deaths(0, 7, 0, 0),
            text: Some("prior-restore".to_string()),
            ..Default::default()
        };
        let mut cur = vec![prior];
        let mut page_slot = HashMap::new();
        page_slot.insert(0usize, (0usize, 0usize));
        let plans = vec![TransferPlan {
            src_var: 0,
            dst: TransferDest::Var(1),
            modifier: 7,
            src_mask: 0xFFFF_FFFF,
            dst_mask: 0xFFFF_FFFF,
        }];
        let mut must_emit = false;
        let mut out = Vec::new();
        emit_transfer_plans(&plans, &page_slot, &mut cur, &mut must_emit, &mut out).unwrap();
        assert_eq!(out.len(), 1);
        assert_eq!(out[0].acts.len(), 3);
        assert_eq!(out[0].acts[0].text.as_deref(), Some("prior-restore"));
        assert_eq!(
            var_targets(&out[0].acts[1]),
            vec![
                (DEST_FIELD_OFFSET, 0, DEST_FIELD_OFFSET),
                (VALUE_FIELD_OFFSET, 1, VALUE_FIELD_OFFSET),
            ]
        );
        assert_eq!(cur.len(), 1);
        assert_eq!(
            var_targets(&cur[0]),
            vec![
                (DEST_FIELD_OFFSET, 0, DEST_FIELD_OFFSET),
                (VALUE_FIELD_OFFSET, 0, VALUE_FIELD_OFFSET),
            ]
        );
    }

    #[test]
    fn hand_written_duplicate_source_batch_is_re_split_by_lower() {
        // The optimizer re-splits an illegal hand-written batch (it is
        // NOT rejected loudly — loud rejection is deferred), so lowering
        // still applies both transfers, each with its own page firing.
        let batch = HirVarBatch::transfers(vec![
            HirVarTransfer {
                dst_var: 1,
                src_var: 0,
                modifier: 7,
            },
            HirVarTransfer {
                dst_var: 2,
                src_var: 0,
                modifier: 7,
            },
        ]);
        let module = HirModule {
            vars: vec![
                HirVarDecl::pinned(0, 10),
                HirVarDecl::pinned(1, 0),
                HirVarDecl::pinned(2, 0),
            ],
            stmts: vec![HirStmt::VarBatch(batch)],
            strings: Vec::new(),
            files: Vec::new(),
            blobs: Vec::new(),
            funcs: Vec::new(),
        };
        let optimized = optimize_module(&module).unwrap();
        assert_eq!(optimized.stmts.len(), 2);
        assert!(optimized.stmts.iter().all(|s| matches!(
            s,
            HirStmt::VarBatch(b) if b.transfers.len() == 1
        )));

        let doc = HirDocument::new(module);
        let lir = lower_document(&doc).unwrap();
        let sim = simulate_trigger_loop(&lir).unwrap();
        let page_item = lir.module.items.len() - 1;
        assert_eq!(sim.read_page_slot_value(page_item, 1), 10);
        assert_eq!(sim.read_page_slot_value(page_item, 2), 10);
        // The shared page fired once per split batch, not once total.
        assert_eq!(
            sim.last_executed_cost
                .visited_items
                .iter()
                .filter(|&&i| i == page_item)
                .count(),
            2
        );
    }

    #[test]
    fn transfer_plan_source_without_page_slot_is_a_loud_error() {
        use crate::{emit_transfer_plans, emit_var_batch};
        // Pinned at the exact branch in the thin consumer. Through
        // `lower_document` this branch is unreachable for validated HIR
        // (scan marks every transfer source as a page var, and validation
        // rejects undeclared variables), so the direct call pins the
        // defensive loud error rather than a reachable program path.
        let plans = vec![TransferPlan {
            src_var: 99,
            dst: TransferDest::Var(1),
            modifier: 7,
            src_mask: 0xFFFF_FFFF,
            dst_mask: 0xFFFF_FFFF,
        }];
        let page_slot = HashMap::new();
        let mut cur = Vec::new();
        let mut must_emit = false;
        let mut out = Vec::new();
        assert!(matches!(
            emit_transfer_plans(&plans, &page_slot, &mut cur, &mut must_emit, &mut out),
            Err(LowerError::UnknownVarId(99))
        ));

        // The destination-allocation check in the batch emitter is the
        // same kind of defensive loud error, pinned directly.
        let batch = HirVarBatch::transfers(vec![HirVarTransfer {
            dst_var: 5,
            src_var: 0,
            modifier: 7,
        }]);
        let plan = StoragePlan::default();
        assert!(matches!(
            emit_var_batch(
                &batch,
                &page_slot,
                &plan,
                &HashMap::new(),
                &mut cur,
                &mut must_emit,
                &mut out
            ),
            Err(LowerError::UnknownVarId(5))
        ));
    }

    #[test]
    fn batch_and_store_share_one_firing_with_both_setup_patches() {
        use crate::emit_var_batch;
        // One firing for a transfer and a store: each patches its own source
        // slot in the same setup trigger, the page fires once, and the
        // restore acts continue the chain. The store here is an address
        // store, the same shape as String's DISPLAY_LINE_ADDR store; ticket9
        // remains the String lock.
        let mut page_slot = HashMap::new();
        page_slot.insert(0usize, (0usize, 0usize));
        page_slot.insert(2usize, (0usize, 2usize));
        let mut plan = StoragePlan::default();
        plan.allocations.insert(
            1,
            StorageClass::TriggerPage {
                page_index: 0,
                slot_index: 1,
            },
        );
        let batch = HirVarBatch {
            transfers: vec![HirVarTransfer {
                dst_var: 1,
                src_var: 0,
                modifier: 7,
            }],
            stores: vec![HirVarStore::to_addr(2, 0x58A364, 7)],
        };
        let mut cur = Vec::new();
        let mut must_emit = false;
        let mut out = Vec::new();
        emit_var_batch(
            &batch,
            &page_slot,
            &plan,
            &HashMap::new(),
            &mut cur,
            &mut must_emit,
            &mut out,
        )
        .unwrap();
        assert_eq!(out.len(), 1);
        let setup = &out[0];
        // [transfer dest patch, store dest patch, page-set-next], in plan
        // order, in one setup trigger. The transfer's patch points its dest at
        // its source slot (var 0) and its value field at the destination
        // variable (var 1), which is what makes the act write var 1's slot.
        assert_eq!(
            var_targets(&setup.acts[0]),
            vec![
                (DEST_FIELD_OFFSET, 0, DEST_FIELD_OFFSET),
                (VALUE_FIELD_OFFSET, 1, VALUE_FIELD_OFFSET),
            ]
        );
        assert_eq!(
            var_targets(&setup.acts[1]),
            vec![(DEST_FIELD_OFFSET, 2, DEST_FIELD_OFFSET)]
        );
        assert_eq!(setup.acts[1].ctrig.value, eudkit_lir::epd_u32(0x58A364));
        // Both sources are restored to the self-targeted invariant: each
        // source points back at itself for both fields.
        assert_eq!(cur.len(), 2);
        assert_eq!(
            var_targets(&cur[0]),
            vec![
                (DEST_FIELD_OFFSET, 0, DEST_FIELD_OFFSET),
                (VALUE_FIELD_OFFSET, 0, VALUE_FIELD_OFFSET),
            ]
        );
        assert_eq!(
            var_targets(&cur[1]),
            vec![
                (DEST_FIELD_OFFSET, 2, DEST_FIELD_OFFSET),
                (VALUE_FIELD_OFFSET, 2, VALUE_FIELD_OFFSET),
            ]
        );
    }

    #[test]
    fn transfer_as_last_statement_of_when_body_lands_value_and_restores_page() {
        // End-to-end: the transfer is the When body's last (only)
        // statement, so its restore trigger is the body's last trigger and
        // control appends the gate restore after the transfer restore acts.
        // The value landing and the self-targeted rest invariant are the
        // behavioural pins; the ordering itself is pinned by the graph
        // test in `control` (a straight-line sim never returns to the gate,
        // so a stale gate next pointer is unobservable here).
        let doc = HirDocument::new(HirModule {
            vars: vec![
                HirVarDecl::pinned(0, 1),
                HirVarDecl::pinned(1, 42),
                HirVarDecl::pinned(2, 0),
            ],
            stmts: vec![HirStmt::When(HirWhen::new(
                vec![HirCond::var_deaths(0, 10, 1)],
                vec![HirStmt::VarTransfer(HirVarTransfer {
                    dst_var: 2,
                    src_var: 1,
                    modifier: 7,
                })],
            ))],
            strings: Vec::new(),
            files: Vec::new(),
            blobs: Vec::new(),
            funcs: Vec::new(),
        });
        let lir = lower_document(&doc).unwrap();
        let sim = simulate_trigger_loop(&lir).unwrap();
        let page_item = lir.module.items.len() - 1;
        assert_eq!(sim.read_page_slot_value(page_item, 2), 42);
        let page_after = sim.raw_trigger(page_item);
        let page_base = sim.payload.addrs[page_item];
        for k in 0..ACT_COUNT {
            let self_val_addr = page_base + (328 + 32 * k + 20) as u32;
            assert_eq!(page_after.acts[k].dest, epd_u32(self_val_addr));
            assert_eq!(page_after.acts[k].modifier, 7);
            assert_eq!(page_after.acts[k].mask, 0xFFFF_FFFF);
        }
    }

    #[test]
    fn when_appends_gate_restore_after_transfer_restore_in_last_trigger() {
        // Graph pin for transfer-last-in-body, kept in the lower tests
        // (control.rs itself is untouched): the body's last trigger is
        // the transfer's restore trigger (unconditional, with room), so
        // control appends the gate restore AFTER the transfer restore act
        // in that same trigger, rather than in a separate trigger. A
        // straight-line sim never returns to the gate, so this ordering
        // is unobservable in sim and must be pinned here, at the seam.
        fn unconditional_trigger() -> ExecTrigger {
            ExecTrigger {
                hir_id: None,
                conds: Vec::new(),
                acts: vec![ExecAct::from_ctrig(CTrigAction::set_deaths(0, 7, 0, 0))],
                preserved: true,
                flags: 4,
                prev: None,
                next: ExecNext::DefaultChain,
            }
        }
        let mut out = Vec::new();
        crate::control::lower_when(
            &HirWhen::new(vec![HirCond::deaths(0, 10, 1, 0)], Vec::new()),
            &mut out,
            |_, o| {
                // Setup trigger, then the transfer's restore trigger.
                o.push(unconditional_trigger());
                o.push(unconditional_trigger());
                Ok(())
            },
        )
        .unwrap();
        // Gate + setup + restore-with-gate-restore; join is virtual index 3.
        assert_eq!(out.len(), 3);
        let gate_target = match &out[0].acts[0].relocs[1].target {
            ExecRelocTarget::ExecItem { exec_idx, .. } => *exec_idx,
            _ => panic!("gate next-pointer act must target an exec item"),
        };
        assert_eq!(gate_target, 1);
        assert!(matches!(out[0].next, ExecNext::ExecItem(3)));
        assert_eq!(out[1].acts.len(), 1);
        assert_eq!(out[2].acts.len(), 2);
        let restore = &out[2].acts[1];
        assert_eq!(restore.relocs.len(), 2);
        assert!(matches!(
            restore.relocs[0].target,
            ExecRelocTarget::ExecItem {
                exec_idx: 0,
                offset,
            } if offset == NEXT_BYTE_OFFSET
        ));
        assert!(matches!(
            restore.relocs[1].target,
            ExecRelocTarget::ExecItem { exec_idx: 3, .. }
        ));
        assert!(matches!(out[2].next, ExecNext::ExecItem(3)));
    }

    #[test]
    fn sixty_five_transfers_split_64_plus_1_with_all_values_correct() {
        // The cap branch, end to end: 65 transfers split into batches of
        // 64 and 1, fire two pages, and every destination lands.
        let mut vars = Vec::new();
        for k in 0..65 {
            vars.push(HirVarDecl::pinned(k, 100 + k as u32));
        }
        for k in 65..130 {
            vars.push(HirVarDecl::pinned(k, 0));
        }
        let stmts: Vec<HirStmt> = (0..65)
            .map(|k| {
                HirStmt::VarTransfer(HirVarTransfer {
                    dst_var: 65 + k,
                    src_var: k,
                    modifier: 7,
                })
            })
            .collect();
        let module = HirModule {
            vars,
            stmts,
            strings: Vec::new(),
            files: Vec::new(),
            blobs: Vec::new(),
            funcs: Vec::new(),
        };
        let optimized = optimize_module(&module).unwrap();
        let batches: Vec<usize> = optimized
            .stmts
            .iter()
            .map(|s| match s {
                HirStmt::VarBatch(b) => b.transfers.len(),
                _ => panic!("expected only VarBatch statements, got {s:?}"),
            })
            .collect();
        assert_eq!(batches, vec![64, 1]);

        let doc = HirDocument::new(module);
        let lir = lower_document(&doc).unwrap();
        let sim = simulate_trigger_loop(&lir).unwrap();
        // 130 pinned vars -> 3 pages, no cells; pages are the last items.
        let exec_count = lir.module.items.len() - 3;
        for k in 0..65 {
            let dst = 65 + k;
            let page_item = exec_count + dst / 64;
            assert_eq!(
                sim.read_page_slot_value(page_item, dst % 64),
                100 + k as u32,
                "destination {dst} (from source {k})"
            );
        }
    }

    #[test]
    fn multi_reloc_bit_cell_cond_is_a_loud_error() {
        // A BitCell var plus a DwordCell var in one condition: the
        // masked rewrite would silently apply to only one of them, so
        // lowering must fail loudly. DSL-valid programs never hit this:
        // `var_deaths` carries exactly one variable reloc.
        let mut cond = HirCond::var_deaths(0, 10, 1);
        cond.relocs.push(HirReloc::var(4, 1, 20, 1));
        let doc = HirDocument::new(HirModule {
            vars: vec![HirVarDecl::new(0, 0), HirVarDecl::new(1, 42)],
            stmts: vec![
                HirStmt::Act(HirAct::set_var_const(0, 7, 1, 0xFFFF_FFFF)),
                // One static action keeps the gate alive: the post-schedule
                // dead-gate pass drops a 0-act trigger, and the condition must
                // reach lowering to exercise the error.
                HirStmt::Trigger(
                    HirTrigger::new(
                        0,
                        vec![cond],
                        vec![HirAct::set_deaths(0, 7, 0, 0)],
                        true,
                    )
                    .unwrap(),
                ),
            ],
            strings: Vec::new(),
            files: Vec::new(),
            blobs: Vec::new(),
            funcs: Vec::new(),
        });
        assert!(matches!(
            lower_document(&doc),
            Err(LowerError::MultiRelocBitCell(0))
        ));
    }

    #[test]
    fn multi_reloc_bit_cell_act_is_a_loud_error() {
        // A BitCell var plus a TriggerPage var in one action: same loud
        // error as the condition case. `set_var_const` carries exactly
        // one variable reloc in DSL-valid programs.
        let mut act = HirAct::set_var_const(0, 7, 1, 0xFFFF_FFFF);
        act.relocs.push(HirReloc::var(16, 1, 20, 1));
        let doc = HirDocument::new(HirModule {
            vars: vec![HirVarDecl::new(0, 0), HirVarDecl::pinned(1, 0)],
            stmts: vec![
                HirStmt::Act(HirAct::set_var_const(0, 7, 1, 0xFFFF_FFFF)),
                HirStmt::Trigger(
                    HirTrigger::new(0, vec![HirCond::var_deaths(0, 10, 1)], vec![act], true)
                        .unwrap(),
                ),
            ],
            strings: Vec::new(),
            files: Vec::new(),
            blobs: Vec::new(),
            funcs: Vec::new(),
        });
        assert!(matches!(
            lower_document(&doc),
            Err(LowerError::MultiRelocBitCell(0))
        ));
    }

    #[test]
    fn non_value_cell_field_is_a_distinct_error() {
        // A cell-backed variable referenced at a non-value field offset
        // must fail with the distinct CellField error, not UnknownVarId.
        // The reachable case is a DwordCell: a BitCell referenced at a
        // non-value offset is poisoned by classification itself (the
        // cell tests pin that it demotes to DwordCell instead), so it
        // can never reach this branch as a BitCell.
        let mut cond = HirCond::var_deaths(0, 10, 42);
        cond.relocs[0].target_offset = 16;
        let doc = HirDocument::new(HirModule {
            vars: vec![HirVarDecl::new(0, 42)],
            // One static action keeps the gate alive past the post-schedule
            // dead-gate pass so the condition reaches lowering.
            stmts: vec![HirStmt::Trigger(
                HirTrigger::new(
                    0,
                    vec![cond],
                    vec![HirAct::set_deaths(0, 7, 0, 0)],
                    true,
                )
                .unwrap(),
            )],
            strings: Vec::new(),
            files: Vec::new(),
            blobs: Vec::new(),
            funcs: Vec::new(),
        });
        assert!(matches!(
            lower_document(&doc),
            Err(LowerError::CellField(0, 16))
        ));
    }

    #[test]
    fn cell_rewrite_cond_and_act_share_one_bit_semantics() {
        // The unified helper reproduces the old twins exactly for the
        // single-reloc cases: a single BitCell reloc rewrites, a DwordCell
        // or TriggerPage reloc does not. A bit among multiple variable
        // relocs is now a loud error (see the multi-reloc tests above).
        use super::{rewrite_cell_act, rewrite_cell_cond, single_bit_cell};
        use eudkit_opt::cell::analyze_storage;

        let module = HirModule {
            vars: vec![
                HirVarDecl::new(0, 0),
                HirVarDecl::new(1, 42),
                HirVarDecl::pinned(2, 0),
            ],
            stmts: vec![
                HirStmt::Act(HirAct::set_var_const(0, 7, 1, 0xFFFF_FFFF)),
                HirStmt::Trigger(
                    HirTrigger::new(0, vec![HirCond::var_deaths(0, 10, 1)], vec![], true)
                        .unwrap(),
                ),
            ],
            strings: Vec::new(),
            files: Vec::new(),
            blobs: Vec::new(),
            funcs: Vec::new(),
        };
        let opt = eudkit_opt::optimize_module(&module).unwrap();
        let plan = analyze_storage(&opt);
        assert!(matches!(
            plan.storage(0),
            Some(StorageClass::BitCell {
                dword_index: 1,
                bit_index: 0,
            })
        ));
        assert!(matches!(
            plan.storage(1),
            Some(StorageClass::DwordCell { .. })
        ));
        assert!(matches!(
            plan.storage(2),
            Some(StorageClass::TriggerPage { .. })
        ));

        assert!(matches!(
            single_bit_cell([0], &plan),
            Ok(Some((1, 0)))
        ));
        assert!(matches!(single_bit_cell([1], &plan), Ok(None)));
        assert!(matches!(single_bit_cell([2], &plan), Ok(None)));
        assert!(matches!(
            single_bit_cell([0, 1], &plan),
            Err(LowerError::MultiRelocBitCell(0))
        ));
        assert!(matches!(
            single_bit_cell([0, 2], &plan),
            Err(LowerError::MultiRelocBitCell(0))
        ));
        assert!(matches!(
            single_bit_cell(Vec::<usize>::new(), &plan),
            Ok(None)
        ));

        // End to end: the same bit rewrites both a condition and an
        // action through the unified helper, with identical masks.
        let mut cond = HirCond::var_deaths(0, 10, 1);
        rewrite_cell_cond(&mut cond, &plan).unwrap();
        assert_eq!(cond.locid, 1);
        assert_eq!(cond.eudx, EUDX_MAGIC);
        assert_eq!(cond.amount, 1);

        let mut act = HirAct::set_var_const(0, 7, 1, 0xFFFF_FFFF).to_ctrig_action();
        let relocs = vec![ExecReloc {
            field_offset: 16,
            target: ExecRelocTarget::Var {
                var_id: 0,
                slot_offset: 20,
            },
            mode: 1,
        }];
        rewrite_cell_act(&mut act, &relocs, &plan).unwrap();
        assert_eq!(act.mask, 1);
        assert_eq!(act.eudx, EUDX_MAGIC);
        assert_eq!(act.value, 1);

        // Value 0 stays 0 (not the mask) on both paths.
        let mut cond0 = HirCond::var_deaths(0, 10, 0);
        rewrite_cell_cond(&mut cond0, &plan).unwrap();
        assert_eq!(cond0.amount, 0);
        let mut act0 = HirAct::set_var_const(0, 7, 0, 0xFFFF_FFFF).to_ctrig_action();
        rewrite_cell_act(&mut act0, &relocs, &plan).unwrap();
        assert_eq!(act0.value, 0);
        assert_eq!(act0.mask, 1);
    }

    /// #99 probe: the unlimiter's `connectDList` shape. Four `Db` blobs are
    /// declared with the real element byte sizes and four `SetMemory`
    /// actions patch the game list heads with each blob's payload address
    /// (mode 4 pointer).
    #[test]
    fn lowers_hir_blobs_and_patches_four_game_list_heads() {
        let sizes = [112usize, 36, 64, 20];
        let heads = [0x64EED8u32, 0x63FE30, 0x57EB68, 0x64B2E0];
        let acts: Vec<HirAct> = heads
            .iter()
            .enumerate()
            .map(|(blob_idx, head)| HirAct {
                relocs: vec![HirReloc::blob(VALUE_FIELD_OFFSET, blob_idx, 0, 4)],
                ..HirAct::set_deaths(epd_u32(*head), 7, 0, 0)
            })
            .collect();
        let trig = HirTrigger::new(0, vec![HirCond::always()], acts, true).unwrap();
        let doc = HirDocument::new(HirModule {
            vars: Vec::new(),
            stmts: vec![HirStmt::Trigger(trig)],
            strings: Vec::new(),
            files: Vec::new(),
            blobs: sizes.iter().map(|&s| HirBlob::zeroed(s)).collect(),
            funcs: Vec::new(),
        });

        let lir = lower_document(&doc).unwrap();
        assert_eq!(lir.module.items.len(), 5, "1 trigger + 4 blobs");
        for (i, &size) in sizes.iter().enumerate() {
            match &lir.module.items[1 + i] {
                LirItem::Blob(b) => {
                    assert_eq!(b.size, size);
                    assert_eq!(b.data, "");
                    assert!(b.relocs.is_empty());
                }
                other => panic!("item {} must be a blob, got {other:?}", 1 + i),
            }
        }
        match &lir.module.items[0] {
            LirItem::Trigger(t) => {
                for (i, act) in t.acts.iter().enumerate() {
                    let reloc = &act.relocs[0];
                    assert_eq!(
                        (
                            reloc.field_offset,
                            reloc.target_item,
                            reloc.target_offset,
                            reloc.mode
                        ),
                        (VALUE_FIELD_OFFSET, 1 + i, 0, 4)
                    );
                }
            }
            other => panic!("item 0 must be the head trigger, got {other:?}"),
        }
        assert_eq!(static_cost(&lir).blob_bytes, sizes.iter().sum::<usize>());

        // Payload proof: each action's value field ends up holding the blob's
        // payload address, which is the exact `SetMemory(head, SetTo, db)`
        // patch the unlimiter needs.
        let payload = lir2payload(&lir).unwrap();
        for (i, _) in sizes.iter().enumerate() {
            let act_field =
                payload.addrs[0] as usize + act_field_byte_offset(i, VALUE_FIELD_OFFSET);
            assert_eq!(
                eudkit_lir::read_u32_le(&payload.data, act_field),
                payload.addrs[1 + i]
            );
        }
    }

    /// #120: the second HIR `Db` blob's base is dword-aligned, so a mode-1
    /// (EPD) `SetMemory(second_blob, …)` shape names its own first dword.
    /// Before the fix the second blob sat at 2411 (`%4 == 3`) and
    /// `epd_u32(2411) == epd_u32(2412)` resolved one byte late.
    #[test]
    fn lowers_second_hir_blob_with_a_dword_aligned_base() {
        // Real `SetMemory(second_blob, SetTo, x)` shape: the blob pointer
        // lands in the action's `dest` field (mode 1).
        let act = HirAct {
            relocs: vec![HirReloc::blob(DEST_FIELD_OFFSET, 1, 0, 1)],
            ..HirAct::set_deaths(0, 7, 0x1122_3344, 0)
        };
        let trig = HirTrigger::new(0, vec![HirCond::always()], vec![act], true).unwrap();
        let doc = HirDocument::new(HirModule {
            vars: Vec::new(),
            stmts: vec![HirStmt::Trigger(trig)],
            strings: Vec::new(),
            files: Vec::new(),
            blobs: vec![HirBlob::zeroed(3), HirBlob::zeroed(8)],
            funcs: Vec::new(),
        });

        let lir = lower_document(&doc).unwrap();
        let payload = lir2payload(&lir).unwrap();
        assert_eq!(payload.addrs, vec![0, 2408, 2412]);
        let second = payload.addrs[2];
        assert_eq!(second % 4, 0, "the second blob base must be dword-aligned");

        let raw = RawTrigger::decode(
            payload.data[..eudkit_lir::RAW_TRIGGER_BYTE_SIZE]
                .try_into()
                .unwrap(),
        );
        assert_eq!(
            raw.acts[0].dest,
            epd_u32(second),
            "the EPD relocation must encode the aligned base"
        );
        assert_eq!(raw.acts[0].value, 0x1122_3344);
        let act_field = payload.addrs[0] as usize + act_field_byte_offset(0, DEST_FIELD_OFFSET);
        assert_eq!(
            eudkit_lir::read_u32_le(&payload.data, act_field),
            epd_u32(second),
            "the payload's dest field must hold the aligned base's EPD"
        );
        // Corrupt-tree control: the unaligned base produced the *same* EPD
        // as the aligned one, so the relocation silently pointed 1 B late.
        assert_eq!(
            epd_u32(2411),
            epd_u32(second),
            "epd_u32 truncation is exactly why the unaligned base was silent"
        );
    }

    /// #99: blob-internal pointer fields relocate too. Two list nodes link
    /// `next` (offset 4) / `prev` (offset 0) to each other, and the blobs sit
    /// after the demoted Cell blob so the item-index arithmetic is proven for
    /// a module that also has cell storage.
    #[test]
    fn blob_internal_relocs_and_cell_blob_offsets_resolve() {
        let node0 = HirBlob::zeroed(8).with_relocs(vec![
            HirBlobReloc::blob(4, 1, 0, 4),
            HirBlobReloc::blob(0, 0, 4, 4),
        ]);
        let node1 = HirBlob::zeroed(8).with_relocs(vec![
            HirBlobReloc::blob(0, 0, 4, 4),
            HirBlobReloc::blob(4, 1, 0, 4),
        ]);
        let trig = HirTrigger::new(
            0,
            vec![HirCond::always()],
            vec![HirAct {
                relocs: vec![HirReloc::blob(VALUE_FIELD_OFFSET, 1, 0, 4)],
                ..HirAct::set_deaths(epd_u32(0x57EB68), 7, 0, 0)
            }],
            true,
        )
        .unwrap();
        let doc = HirDocument::new(HirModule {
            // A scalar that never needs a page is demoted to a 4-byte Cell blob,
            // which must stay ahead of the frontend blobs.
            vars: vec![HirVarDecl::new(0, 7)],
            stmts: vec![HirStmt::Trigger(trig)],
            strings: Vec::new(),
            files: Vec::new(),
            blobs: vec![node0, node1],
            funcs: Vec::new(),
        });

        let lir = lower_document(&doc).unwrap();
        assert_eq!(lir.module.items.len(), 4, "trigger + cell blob + 2 blobs");
        match &lir.module.items[1] {
            LirItem::Blob(cell) => assert_eq!(cell.size, 4),
            other => panic!("item 1 must be the cell blob, got {other:?}"),
        }
        let node_base = 2;
        match &lir.module.items[node_base] {
            LirItem::Blob(b) => {
                assert_eq!(b.size, 8);
                assert_eq!(b.relocs.len(), 2);
                assert_eq!(b.relocs[0].target_item, node_base + 1);
                assert_eq!(b.relocs[1].target_item, node_base);
            }
            other => panic!("item {node_base} must be a blob, got {other:?}"),
        }

        let payload = lir2payload(&lir).unwrap();
        let n0 = payload.addrs[node_base] as usize;
        let n1 = payload.addrs[node_base + 1] as usize;
        assert_eq!(eudkit_lir::read_u32_le(&payload.data, n0 + 4), n1 as u32);
        assert_eq!(eudkit_lir::read_u32_le(&payload.data, n0), (n0 + 4) as u32);
        assert_eq!(eudkit_lir::read_u32_le(&payload.data, n1), (n0 + 4) as u32);
        assert_eq!(eudkit_lir::read_u32_le(&payload.data, n1 + 4), n1 as u32);
        // The head patch points at node 1.
        let act_field = payload.addrs[0] as usize + act_field_byte_offset(0, VALUE_FIELD_OFFSET);
        assert_eq!(eudkit_lir::read_u32_le(&payload.data, act_field), n1 as u32);
    }
}
