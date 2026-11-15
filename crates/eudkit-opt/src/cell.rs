//! Automatic Cell storage demotion analysis for HIR 3.0 scalar variables.
//!
//! The pass classifies every scalar [`HirVarDecl`] into one of three storage
//! classes. The pin sets themselves live in [`StorageFacts`], which this pass
//! consumes:
//! - **TriggerPage slot** (legacy layout): retained for variables that are ever
//!   the source of a `VarTransfer`/`VarBatch`/`VarStore`, that are passed as a
//!   `HirCallArg::Var`, that are a subroutine parameter or result, that are
//!   explicitly pinned by a container (writable
//!   `FixedArray`/`TriggerStruct` backing), or whose storage address is used
//!   for anything other than a plain value read/write.
//! - **4B dword Cell**: non-boolean variables that are never VProc sources.
//! - **1-bit boolean Cell**: variables initialized to `0`/`1`, only mutated with
//!   `SetTo(0)` or `SetTo(1)`, and only tested in boolean-domain `Deaths`
//!   conditions. Up to 32 booleans share a single 4-byte dword.
//!
//! Cells are laid out contiguously in a dedicated data blob; every dword is
//! addressable with a normal EPD relocation, so demotion never requires a
//! `32T` `dwread` trap.
//!
//! TriggerPage ordering guarantee (Layer 2, option C): `analyze_storage`
//! assigns final TriggerPage ranks to page-pinned variables in id order
//! first, then to unpinned transfer/store sources in id order. For every
//! page-pinned variable, its final TriggerPage rank therefore equals its
//! DSL pinned rank (its rank among page-pinned variables only), so every
//! DSL page-aligned container starts at final slot 0 with its elements
//! consecutive in final order. This guarantee does not itself guarantee
//! DSL alignment (element 0 pinned rank % 64 == 0); that remains the
//! DSL-side guarantee. Programs with no unpinned TriggerPage variable
//! before any pinned variable are unaffected by the ordering and keep
//! their previous assignments.

use std::collections::HashMap;
use std::fmt::Write as _;

use eudkit_hir::{HirModule, HirStmt, HirVarDecl};
use eudkit_lir::{ACT_COUNT, VALUE_FIELD_OFFSET};

use crate::facts::StorageFacts;

/// Process-wide number of [`analyze_storage`] calls.
///
/// Instrumentation for the C2c acceptance test (one analysis per compile);
/// compiled in only with the `count-calls` feature.
#[cfg(feature = "count-calls")]
#[doc(hidden)]
pub static ANALYZE_STORAGE_CALLS: std::sync::atomic::AtomicUsize =
    std::sync::atomic::AtomicUsize::new(0);

/// Returns true when `offset` is the value-field offset of a `CTrigAction`
/// record, i.e. when a variable relocation at that offset is a plain value
/// access rather than an access to some other action field.
pub fn is_value_field_offset(offset: usize) -> bool {
    offset == VALUE_FIELD_OFFSET
}

/// Bit mask for one boolean Cell bit: bit `n` of its dword.
pub fn bit_mask(bit_index: u8) -> u32 {
    debug_assert!(bit_index < 32, "BitCell bit index is always 0..31");
    1u32 << bit_index
}

/// Extracts one boolean Cell bit from a dword value.
pub fn bit_value(bit_index: u8, value: u32) -> u32 {
    (value >> bit_index) & 1
}

/// Storage backing allocated for a scalar variable.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum StorageClass {
    /// Variable is backed by action slot `slot_index` (`0..63`) of TriggerPage `page_index`.
    TriggerPage {
        page_index: usize,
        slot_index: usize,
    },
    /// Variable is demoted to a 4-byte dword Cell at `dword_index` in the cell data block.
    DwordCell { dword_index: usize },
    /// Variable is demoted to bit `bit_index` (`0..31`) of `dword_index`.
    BitCell { dword_index: usize, bit_index: u8 },
}

impl StorageClass {
    /// Byte offset of this variable's backing storage inside the cell blob.
    pub fn cell_byte_offset(self) -> Option<usize> {
        match self {
            StorageClass::TriggerPage { .. } => None,
            StorageClass::DwordCell { dword_index } => Some(dword_index * 4),
            StorageClass::BitCell { dword_index, .. } => Some(dword_index * 4),
        }
    }

    pub fn is_cell(self) -> bool {
        !matches!(self, StorageClass::TriggerPage { .. })
    }
}

/// Storage allocation plan for all scalar variables in a [`HirModule`].
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct StoragePlan {
    pub allocations: HashMap<usize, StorageClass>,
    pub trigger_page_vars: usize,
    pub dword_cell_vars: usize,
    pub bit_cell_vars: usize,
    pub trigger_pages: usize,
    pub cell_dwords: usize,
}

impl StoragePlan {
    /// Returns true if all variables are placed in TriggerPages (legacy layout).
    pub fn is_all_trigger_pages(&self) -> bool {
        self.dword_cell_vars == 0 && self.bit_cell_vars == 0
    }

    pub fn storage(&self, var_id: usize) -> Option<StorageClass> {
        self.allocations.get(&var_id).copied()
    }
}

/// Analyze a [`HirModule`] without cell demotion, placing all declared variables
/// sequentially into 64-slot `TriggerPage` action slots (legacy layout).
pub fn plan_all_trigger_pages(vars: &[HirVarDecl]) -> StoragePlan {
    let mut allocations = HashMap::with_capacity(vars.len());
    let max_slot = vars
        .iter()
        .map(|v| v.id % ACT_COUNT)
        .max()
        .map(|s| s + 1)
        .unwrap_or(0);
    let _ = max_slot;
    // Pack deterministically by declaration order rather than by raw id so that
    // ids with gaps still fit in the minimum number of pages.
    for (i, v) in vars.iter().enumerate() {
        allocations.insert(
            v.id,
            StorageClass::TriggerPage {
                page_index: i / ACT_COUNT,
                slot_index: i % ACT_COUNT,
            },
        );
    }
    StoragePlan {
        allocations,
        trigger_page_vars: vars.len(),
        dword_cell_vars: 0,
        bit_cell_vars: 0,
        trigger_pages: if vars.is_empty() {
            0
        } else {
            (vars.len() - 1) / ACT_COUNT + 1
        },
        cell_dwords: 0,
    }
}

/// Whether any statement list still carries a high-level temporal node
/// (`until`/`edge`/`every`/`after`).
///
/// Only the post-desugar invariant in [`analyze_storage`] uses this: stage 1
/// of `eudkit-opt` expands every high-level node into `when` blocks, so a hit
/// here means the caller skipped optimisation.
#[cfg(debug_assertions)]
fn has_high_level_temporal(stmts: &[HirStmt]) -> bool {
    stmts.iter().any(|stmt| match stmt {
        HirStmt::Until(_) | HirStmt::Edge(_) | HirStmt::Every(_) | HirStmt::After(_) => true,
        HirStmt::When(w) => {
            has_high_level_temporal(&w.body) || has_high_level_temporal(&w.else_body)
        }
        HirStmt::Once(o) => has_high_level_temporal(&o.body),
        HirStmt::While(w) => has_high_level_temporal(&w.body),
        _ => false,
    })
}

/// Analyze each `Var` in `module` and generate an automatic Cell storage plan.
///
/// # Invariant: pass an optimized (post-desugar) module
///
/// Every production caller runs [`crate::optimize_module`] first
/// (`eudkit-lower`'s `prepare_lowering` and both optimize-then-lower wrappers),
/// which desugars every `until`/`edge`/`every`/`after` node into `when`
/// blocks. The storage ranking is computed from that shape: a high-level node
/// lowered without desugaring would rank its state variables against a body
/// the optimizer has not scheduled yet. A debug build asserts the invariant;
/// the facts walk still handles the high-level arms defensively so a release
/// build without optimisation degrades rather than panics.
pub fn analyze_storage(module: &HirModule) -> StoragePlan {
    #[cfg(feature = "count-calls")]
    ANALYZE_STORAGE_CALLS.fetch_add(1, std::sync::atomic::Ordering::Relaxed);
    #[cfg(debug_assertions)]
    debug_assert!(
        !has_high_level_temporal(&module.stmts),
        "analyze_storage expects a post-desugar (optimized) module; run optimize_module first"
    );
    // Every pin set below comes from the shared facts walk: declarations,
    // transfer/batch/store sources, `Call` arguments, subroutine params and
    // results, and every nested body. There is no second scan here.
    let facts = StorageFacts::from_module(module);

    let mut pinned_vars: Vec<usize> = Vec::new();
    let mut unpinned_tp_vars: Vec<usize> = Vec::new();
    let mut dw_vars: Vec<usize> = Vec::new();
    let mut bit_vars: Vec<usize> = Vec::new();

    let mut sorted_vars: Vec<&HirVarDecl> = module.vars.iter().collect();
    sorted_vars.sort_by_key(|v| v.id);

    for v in sorted_vars {
        if facts.is_pinned(v.id) {
            pinned_vars.push(v.id);
        } else if facts.is_vproc_source(v.id) {
            unpinned_tp_vars.push(v.id);
        } else if (v.init == 0 || v.init == 1)
            && (v.mask == 0xFFFF_FFFF || (v.mask & 1) != 0)
            && !facts.is_transfer_dst(v.id)
            && !facts.is_not_boolean(v.id)
        {
            bit_vars.push(v.id);
        } else {
            dw_vars.push(v.id);
        }
    }

    // Pinned-first ordering (Layer 2, option C): a stable partition of the
    // TriggerPage set. Classification is unchanged; only the rank order
    // within the TriggerPage set changes, and only when an unpinned source
    // precedes a pinned variable in id order.
    let mut tp_vars = pinned_vars;
    tp_vars.extend(unpinned_tp_vars);

    let mut allocations = HashMap::with_capacity(module.vars.len());
    for (i, &vid) in tp_vars.iter().enumerate() {
        allocations.insert(
            vid,
            StorageClass::TriggerPage {
                page_index: i / ACT_COUNT,
                slot_index: i % ACT_COUNT,
            },
        );
    }
    let trigger_pages = tp_vars.len().div_ceil(ACT_COUNT);

    for (i, &vid) in dw_vars.iter().enumerate() {
        allocations.insert(vid, StorageClass::DwordCell { dword_index: i });
    }
    let dword_cell_vars = dw_vars.len();

    for (i, &vid) in bit_vars.iter().enumerate() {
        allocations.insert(
            vid,
            StorageClass::BitCell {
                dword_index: dword_cell_vars + i / 32,
                bit_index: (i % 32) as u8,
            },
        );
    }
    let bit_cell_vars = bit_vars.len();
    let cell_dwords = dword_cell_vars + bit_cell_vars.div_ceil(32);

    StoragePlan {
        allocations,
        trigger_page_vars: tp_vars.len(),
        dword_cell_vars,
        bit_cell_vars,
        trigger_pages,
        cell_dwords,
    }
}

/// Render a human-readable diagnostic report of variable storage allocation for `dump-ir`.
pub fn render_dump_ir(module: &HirModule, plan: &StoragePlan) -> String {
    let mut out = String::new();
    writeln!(out, "=== Variable Storage Allocation Report ===").unwrap();
    writeln!(
        out,
        "Total Variables: {}\n  TriggerPage Slots: {} ({} TriggerPages, {} bytes)\n  4B Dword Cells:   {}\n  1-bit Bit Cells:   {} ({} dwords)\n  Total Cell Dwords: {} ({} bytes)",
        module.vars.len(),
        plan.trigger_page_vars,
        plan.trigger_pages,
        plan.trigger_pages * 2408,
        plan.dword_cell_vars,
        plan.bit_cell_vars,
        plan.bit_cell_vars.div_ceil(32),
        plan.cell_dwords,
        plan.cell_dwords * 4
    )
    .unwrap();

    out.push_str(&render_allocations(module, plan));
    out
}

/// Render the per-variable `Allocations:` block used by `dump-ir`.
pub fn render_allocations(module: &HirModule, plan: &StoragePlan) -> String {
    let mut out = String::new();
    writeln!(out, "\nAllocations:").unwrap();
    let mut sorted_vars: Vec<&HirVarDecl> = module.vars.iter().collect();
    sorted_vars.sort_by_key(|v| v.id);

    for v in sorted_vars {
        match plan.allocations.get(&v.id) {
            Some(StorageClass::TriggerPage {
                page_index,
                slot_index,
            }) => {
                writeln!(
                    out,
                    "  var[{}] init=0x{:x}: TriggerPage(page={}, slot={})",
                    v.id, v.init, page_index, slot_index
                )
                .unwrap();
            }
            Some(StorageClass::DwordCell { dword_index }) => {
                writeln!(
                    out,
                    "  var[{}] init=0x{:x}: DwordCell(dword={})",
                    v.id, v.init, dword_index
                )
                .unwrap();
            }
            Some(StorageClass::BitCell {
                dword_index,
                bit_index,
            }) => {
                writeln!(
                    out,
                    "  var[{}] init=0x{:x}: BitCell(dword={}, bit={})",
                    v.id, v.init, dword_index, bit_index
                )
                .unwrap();
            }
            None => {
                writeln!(out, "  var[{}] init=0x{:x}: Unallocated", v.id, v.init).unwrap();
            }
        }
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;
    use eudkit_hir::{
        HirAct, HirAfter, HirCond, HirEdge, HirEdgeDirection, HirEvery, HirStmt, HirTrigger,
        HirUntil, HirVarDecl, HirVarTransfer, HirWhen,
    };

    fn module(vars: Vec<HirVarDecl>, stmts: Vec<HirStmt>) -> HirModule {
        HirModule {
            vars,
            stmts,
            strings: Vec::new(),
            files: Vec::new(),
            blobs: Vec::new(),
            funcs: Vec::new(),
        }
    }

    #[test]
    fn vproc_sources_retained_in_trigger_pages() {
        let vars = vec![
            HirVarDecl::new(0, 10),
            HirVarDecl::new(1, 0),
            HirVarDecl::new(2, 0),
        ];
        let stmts = vec![
            HirStmt::VarTransfer(HirVarTransfer {
                dst_var: 1,
                src_var: 0,
                modifier: 7,
            }),
            HirStmt::Act(HirAct::set_var_const(2, 7, 1, 0xFFFF_FFFF)),
            HirStmt::Trigger(
                HirTrigger::new(0, vec![HirCond::var_deaths(2, 10, 1)], vec![], true).unwrap(),
            ),
        ];
        let plan = analyze_storage(&module(vars, stmts));
        assert_eq!(plan.trigger_page_vars, 1);
        assert_eq!(plan.dword_cell_vars, 1);
        assert_eq!(plan.bit_cell_vars, 1);
        assert_eq!(plan.trigger_pages, 1);
        assert_eq!(plan.cell_dwords, 2);
        assert_eq!(
            plan.allocations.get(&0),
            Some(&StorageClass::TriggerPage {
                page_index: 0,
                slot_index: 0,
            })
        );
        assert_eq!(
            plan.allocations.get(&1),
            Some(&StorageClass::DwordCell { dword_index: 0 })
        );
        assert_eq!(
            plan.allocations.get(&2),
            Some(&StorageClass::BitCell {
                dword_index: 1,
                bit_index: 0,
            })
        );
    }

    #[test]
    fn non_boolean_and_write_only_scalars_demoted_to_4b_dword_cells() {
        let vars = vec![
            HirVarDecl::new(0, 500),
            HirVarDecl::new(1, 0),
            HirVarDecl::new(2, 0),
        ];
        let stmts = vec![
            HirStmt::Act(HirAct::set_var_const(1, 8, 5, 0xFFFF_FFFF)),
            HirStmt::Trigger(
                HirTrigger::new(0, vec![HirCond::var_deaths(2, 0, 2)], vec![], true).unwrap(),
            ),
        ];
        let plan = analyze_storage(&module(vars, stmts));
        assert_eq!(plan.trigger_page_vars, 0);
        assert_eq!(plan.dword_cell_vars, 3);
        assert_eq!(plan.bit_cell_vars, 0);
        assert_eq!(plan.trigger_pages, 0);
        assert_eq!(plan.cell_dwords, 3);
    }

    #[test]
    fn func_params_and_result_are_page_backed() {
        // A call-site transfer copies the result slot out, and `VarTransfer`
        // sources are dereferenced through their TriggerPage slot. Both the
        // parameter (written by the call site, read by the body) and the
        // result (written by the body, read by every call site) must
        // therefore be TriggerPage-backed, even though neither appears in a
        // module-level transfer.
        use eudkit_hir::{HirCall, HirCallArg, HirFunc};
        let vars = vec![
            HirVarDecl::new(0, 0),
            HirVarDecl::new(1, 0),
            HirVarDecl::new(2, 0),
            HirVarDecl::new(3, 0),
        ];
        let func = HirFunc {
            id: 0,
            params: vec![1],
            result: Some(2),
            body: vec![HirStmt::VarTransfer(HirVarTransfer {
                dst_var: 2,
                src_var: 1,
                modifier: 7,
            })],
        };
        let stmts = vec![HirStmt::Call(HirCall::new(
            0,
            vec![HirCallArg::var(0)],
            Some(3),
        ))];
        let mut m = module(vars, stmts);
        m.funcs.push(func);
        let plan = analyze_storage(&m);
        assert!(matches!(
            plan.allocations.get(&1),
            Some(StorageClass::TriggerPage { .. })
        ));
        assert!(matches!(
            plan.allocations.get(&2),
            Some(StorageClass::TriggerPage { .. })
        ));
    }

    #[test]
    fn packs_up_to_32_booleans_per_dword_and_spills_to_next() {
        let vars: Vec<HirVarDecl> = (0..35)
            .map(|i| HirVarDecl::new(i, (i % 2) as u32))
            .collect();
        let mut stmts = Vec::new();
        for i in 0..35 {
            stmts.push(HirStmt::Act(HirAct::set_var_const(
                i,
                7,
                (i % 2) as u32,
                0xFFFF_FFFF,
            )));
            stmts.push(HirStmt::Trigger(
                HirTrigger::new(
                    i,
                    vec![HirCond::var_deaths(i, 10, (i % 2) as u32)],
                    vec![],
                    true,
                )
                .unwrap(),
            ));
        }
        let plan = analyze_storage(&module(vars, stmts));
        assert_eq!(plan.trigger_page_vars, 0);
        assert_eq!(plan.dword_cell_vars, 0);
        assert_eq!(plan.bit_cell_vars, 35);
        assert_eq!(plan.trigger_pages, 0);
        assert_eq!(plan.cell_dwords, 2);
        for i in 0..32 {
            assert_eq!(
                plan.allocations.get(&i),
                Some(&StorageClass::BitCell {
                    dword_index: 0,
                    bit_index: i as u8,
                })
            );
        }
        for i in 32..35 {
            assert_eq!(
                plan.allocations.get(&i),
                Some(&StorageClass::BitCell {
                    dword_index: 1,
                    bit_index: (i - 32) as u8,
                })
            );
        }
    }

    #[test]
    fn pinned_variables_stay_on_trigger_pages() {
        let vars = vec![HirVarDecl::pinned(0, 0), HirVarDecl::pinned(1, 0)];
        let plan = analyze_storage(&module(vars, Vec::new()));
        assert_eq!(plan.trigger_page_vars, 2);
        assert_eq!(plan.dword_cell_vars, 0);
        assert_eq!(plan.bit_cell_vars, 0);
        assert_eq!(plan.trigger_pages, 1);
    }

    #[test]
    fn pinned_vars_rank_before_unpinned_trigger_page_sources() {
        // Layer 2, option C: page-pinned variables take final TriggerPage
        // ranks in id order first, then unpinned transfer/store sources in
        // id order. This test FAILS before the fix: the unpinned source
        // with the lowest id otherwise takes slot 0 and shifts both pinned
        // variables by one.
        let vars = vec![
            HirVarDecl::new(0, 1234),
            HirVarDecl::pinned(1, 0),
            HirVarDecl::pinned(2, 0),
            HirVarDecl::new(3, 0),
        ];
        let stmts = vec![HirStmt::VarTransfer(HirVarTransfer {
            dst_var: 3,
            src_var: 0,
            modifier: 7,
        })];
        let plan = analyze_storage(&module(vars, stmts));
        assert_eq!(plan.trigger_page_vars, 3);
        assert_eq!(plan.trigger_pages, 1);
        assert_eq!(
            plan.allocations.get(&1),
            Some(&StorageClass::TriggerPage {
                page_index: 0,
                slot_index: 0,
            })
        );
        assert_eq!(
            plan.allocations.get(&2),
            Some(&StorageClass::TriggerPage {
                page_index: 0,
                slot_index: 1,
            })
        );
        assert_eq!(
            plan.allocations.get(&0),
            Some(&StorageClass::TriggerPage {
                page_index: 0,
                slot_index: 2,
            })
        );
    }

    #[test]
    fn all_pinned_trigger_page_order_is_identical_to_baseline() {
        // No unpinned source exists, so the pinned-first partition changes
        // nothing: this passes before and after the fix.
        let vars = vec![
            HirVarDecl::pinned(0, 0),
            HirVarDecl::pinned(1, 0),
            HirVarDecl::pinned(2, 0),
        ];
        let plan = analyze_storage(&module(vars, Vec::new()));
        assert_eq!(plan.trigger_page_vars, 3);
        assert_eq!(plan.trigger_pages, 1);
        for (id, slot) in [(0, 0), (1, 1), (2, 2)] {
            assert_eq!(
                plan.allocations.get(&id),
                Some(&StorageClass::TriggerPage {
                    page_index: 0,
                    slot_index: slot,
                })
            );
        }
    }

    #[test]
    fn all_unpinned_trigger_page_order_is_identical_to_baseline() {
        // The pinned list is empty, so unpinned sources keep id order:
        // this passes before and after the fix.
        let vars = vec![
            HirVarDecl::new(0, 0),
            HirVarDecl::new(1, 0),
            HirVarDecl::new(2, 0),
        ];
        let stmts = vec![
            HirStmt::VarTransfer(HirVarTransfer {
                dst_var: 2,
                src_var: 0,
                modifier: 7,
            }),
            HirStmt::VarTransfer(HirVarTransfer {
                dst_var: 2,
                src_var: 1,
                modifier: 7,
            }),
        ];
        let plan = analyze_storage(&module(vars, stmts));
        assert_eq!(plan.trigger_page_vars, 2);
        assert_eq!(plan.trigger_pages, 1);
        assert_eq!(
            plan.allocations.get(&0),
            Some(&StorageClass::TriggerPage {
                page_index: 0,
                slot_index: 0,
            })
        );
        assert_eq!(
            plan.allocations.get(&1),
            Some(&StorageClass::TriggerPage {
                page_index: 0,
                slot_index: 1,
            })
        );
    }

    #[test]
    fn nested_blocks_are_scanned_for_vproc_sources() {
        let vars = vec![HirVarDecl::new(0, 5), HirVarDecl::new(1, 0)];
        let stmts = vec![HirStmt::When(eudkit_hir::HirWhen::new(
            vec![HirCond::always()],
            vec![HirStmt::VarTransfer(HirVarTransfer {
                dst_var: 1,
                src_var: 0,
                modifier: 7,
            })],
        ))];
        let plan = analyze_storage(&module(vars, stmts));
        assert_eq!(
            plan.allocations.get(&0),
            Some(&StorageClass::TriggerPage {
                page_index: 0,
                slot_index: 0,
            })
        );
    }

    #[test]
    fn dump_ir_diagnostic_report_formats_summary() {
        let vars = vec![
            HirVarDecl::new(0, 1),
            HirVarDecl::new(1, 42),
            HirVarDecl::new(2, 99),
        ];
        let stmts = vec![
            HirStmt::VarTransfer(HirVarTransfer {
                dst_var: 1,
                src_var: 2,
                modifier: 7,
            }),
            HirStmt::Act(HirAct::set_var_const(0, 7, 0, 0xFFFF_FFFF)),
        ];
        let m = module(vars, stmts);
        let plan = analyze_storage(&m);
        let report = render_dump_ir(&m, &plan);
        assert!(report.contains("TriggerPage Slots: 1"));
        assert!(report.contains("4B Dword Cells:   1"));
        assert!(report.contains("1-bit Bit Cells:   1"));
        assert!(report.contains("Total Cell Dwords: 2"));
    }

    fn plan_for(vars: Vec<HirVarDecl>, stmts: Vec<HirStmt>) -> StoragePlan {
        analyze_storage(&module(vars, stmts))
    }

    fn boolean_stmts(id: usize) -> Vec<HirStmt> {
        vec![
            HirStmt::Act(HirAct::set_var_const(id, 7, 1, 0xFFFF_FFFF)),
            HirStmt::Trigger(
                HirTrigger::new(0, vec![HirCond::var_deaths(id, 10, 1)], vec![], true).unwrap(),
            ),
        ]
    }

    fn classify(var: HirVarDecl, stmts: Vec<HirStmt>) -> StorageClass {
        let id = var.id;
        *plan_for(vec![var], stmts)
            .allocations
            .get(&id)
            .expect("declared var must be classified")
    }

    #[test]
    fn bit_mask_and_bit_value_cover_every_bit_position() {
        assert_eq!(bit_mask(0), 1);
        assert_eq!(bit_mask(1), 2);
        assert_eq!(bit_mask(31), 0x8000_0000);
        assert_eq!(bit_value(0, 0b101), 1);
        assert_eq!(bit_value(1, 0b101), 0);
        assert_eq!(bit_value(2, 0b101), 1);
        assert_eq!(bit_value(31, 0x8000_0000), 1);
        assert_eq!(bit_value(5, 0), 0);
    }

    #[test]
    fn value_field_offset_is_the_action_value_field() {
        assert_eq!(VALUE_FIELD_OFFSET, 20);
        assert!(is_value_field_offset(20));
        assert!(!is_value_field_offset(16));
        assert!(!is_value_field_offset(0));
    }

    #[test]
    fn classification_init_and_decl_mask_branches() {
        // Init outside 0/1 can never be a boolean Cell.
        assert_eq!(
            classify(HirVarDecl::new(0, 2), boolean_stmts(0)),
            StorageClass::DwordCell { dword_index: 0 }
        );
        // Decl mask with bit 0 set (but not full) still qualifies.
        assert_eq!(
            classify(HirVarDecl::with_mask(0, 0, 0x3), boolean_stmts(0)),
            StorageClass::BitCell {
                dword_index: 0,
                bit_index: 0,
            }
        );
        // Decl mask with bit 0 clear and not full does not qualify.
        assert_eq!(
            classify(HirVarDecl::with_mask(0, 0, 0xFFFF_FFFE), boolean_stmts(0)),
            StorageClass::DwordCell { dword_index: 0 }
        );
        // Decl mask 0 has bit 0 clear, so it does not qualify either.
        assert_eq!(
            classify(HirVarDecl::with_mask(0, 0, 0), boolean_stmts(0)),
            StorageClass::DwordCell { dword_index: 0 }
        );
    }

    #[test]
    fn classification_transfer_dst_exclusion_and_vproc_source_precedence() {
        // A boolean-shaped transfer destination is a DwordCell, never a
        // BitCell, while the transfer source stays on a TriggerPage.
        let plan = plan_for(
            vec![HirVarDecl::new(0, 0), HirVarDecl::new(1, 0)],
            vec![
                HirStmt::VarTransfer(HirVarTransfer {
                    dst_var: 1,
                    src_var: 0,
                    modifier: 7,
                }),
                HirStmt::Act(HirAct::set_var_const(1, 7, 1, 0xFFFF_FFFF)),
                HirStmt::Trigger(
                    HirTrigger::new(0, vec![HirCond::var_deaths(1, 10, 1)], vec![], true).unwrap(),
                ),
            ],
        );
        assert_eq!(
            plan.allocations.get(&0),
            Some(&StorageClass::TriggerPage {
                page_index: 0,
                slot_index: 0,
            })
        );
        assert_eq!(
            plan.allocations.get(&1),
            Some(&StorageClass::DwordCell { dword_index: 0 })
        );
    }

    #[test]
    fn classification_write_shape_branches() {
        // Modifier other than SetTo (7) is not a boolean write.
        let mut stmts = boolean_stmts(0);
        stmts[0] = HirStmt::Act(HirAct::set_var_const(0, 8, 1, 0xFFFF_FFFF));
        assert_eq!(
            classify(HirVarDecl::new(0, 0), stmts),
            StorageClass::DwordCell { dword_index: 0 }
        );
        // Value other than 0/1 is not a boolean write.
        let mut stmts = boolean_stmts(0);
        stmts[0] = HirStmt::Act(HirAct::set_var_const(0, 7, 5, 0xFFFF_FFFF));
        assert_eq!(
            classify(HirVarDecl::new(0, 0), stmts),
            StorageClass::DwordCell { dword_index: 0 }
        );
        // A partial act mask (neither full nor 0) is not a boolean write.
        let mut stmts = boolean_stmts(0);
        stmts[0] = HirStmt::Act(HirAct::set_var_const(0, 7, 1, 0xFF));
        assert_eq!(
            classify(HirVarDecl::new(0, 0), stmts),
            StorageClass::DwordCell { dword_index: 0 }
        );
        // LIR ignores the mask for a plain SetDeaths (no EUDX mask), so mask 0
        // stays boolean; an unexpected partial non-EUDX mask is still rejected
        // conservatively as non-boolean.
        let mut stmts = boolean_stmts(0);
        let mut unmasked = HirAct::set_var_const(0, 7, 1, 0);
        unmasked.eudx = 0;
        stmts[0] = HirStmt::Act(unmasked);
        assert_eq!(
            classify(HirVarDecl::new(0, 0), stmts),
            StorageClass::BitCell {
                dword_index: 0,
                bit_index: 0,
            }
        );
        // A SetDeathsX (eudx == EUDX_MAGIC) with mask 0 is a 0-bit no-op in
        // LIR, not a full value write, so it must not be a boolean cell.
        let mut stmts = boolean_stmts(0);
        stmts[0] = HirStmt::Act(HirAct::set_var_const(0, 7, 1, 0));
        assert_eq!(
            classify(HirVarDecl::new(0, 0), stmts),
            StorageClass::DwordCell { dword_index: 0 }
        );
        // A non-value-field write target is not a boolean write.
        let mut act = HirAct::set_var_const(0, 7, 1, 0xFFFF_FFFF);
        act.relocs[0].target_offset = 16;
        let stmts = vec![
            HirStmt::Act(act),
            HirStmt::Trigger(
                HirTrigger::new(0, vec![HirCond::var_deaths(0, 10, 1)], vec![], true).unwrap(),
            ),
        ];
        assert_eq!(
            classify(HirVarDecl::new(0, 0), stmts),
            StorageClass::DwordCell { dword_index: 0 }
        );
    }

    #[test]
    fn classification_read_shape_branches() {
        // AtLeast 2 is outside the boolean domain.
        let read = |comparison: u8, amount: u32| {
            vec![
                HirStmt::Act(HirAct::set_var_const(0, 7, 1, 0xFFFF_FFFF)),
                HirStmt::Trigger(
                    HirTrigger::new(
                        0,
                        vec![HirCond::var_deaths(0, comparison, amount)],
                        vec![],
                        true,
                    )
                    .unwrap(),
                ),
            ]
        };
        assert_eq!(
            classify(HirVarDecl::new(0, 0), read(0, 2)),
            StorageClass::DwordCell { dword_index: 0 }
        );
        for (comparison, amount) in [(10, 0), (10, 1), (0, 1), (1, 0), (1, 1)] {
            assert_eq!(
                classify(HirVarDecl::new(0, 0), read(comparison, amount)),
                StorageClass::BitCell {
                    dword_index: 0,
                    bit_index: 0,
                },
                "boolean read ({comparison}, {amount}) must stay a BitCell"
            );
        }
        // A non-Deaths condition type is not a boolean read.
        let mut cond = HirCond::var_deaths(0, 10, 1);
        cond.condtype = 14;
        let stmts = vec![
            HirStmt::Act(HirAct::set_var_const(0, 7, 1, 0xFFFF_FFFF)),
            HirStmt::Trigger(HirTrigger::new(0, vec![cond], vec![], true).unwrap()),
        ];
        assert_eq!(
            classify(HirVarDecl::new(0, 0), stmts),
            StorageClass::DwordCell { dword_index: 0 }
        );
        // A masked DeathsX read (locid is a mask, eudx is set) is not a
        // boolean-domain read.
        let stmts = vec![
            HirStmt::Act(HirAct::set_var_const(0, 7, 1, 0xFFFF_FFFF)),
            HirStmt::Trigger(
                HirTrigger::new(0, vec![HirCond::var_deathsx(0, 10, 1, 0xFF)], vec![], true)
                    .unwrap(),
            ),
        ];
        assert_eq!(
            classify(HirVarDecl::new(0, 0), stmts),
            StorageClass::DwordCell { dword_index: 0 }
        );
        // A non-value-field read target is not a boolean read.
        let mut cond = HirCond::var_deaths(0, 10, 1);
        cond.relocs[0].target_offset = 16;
        let stmts = vec![
            HirStmt::Act(HirAct::set_var_const(0, 7, 1, 0xFFFF_FFFF)),
            HirStmt::Trigger(HirTrigger::new(0, vec![cond], vec![], true).unwrap()),
        ];
        assert_eq!(
            classify(HirVarDecl::new(0, 0), stmts),
            StorageClass::DwordCell { dword_index: 0 }
        );
    }

    #[test]
    fn classification_write_and_read_shapes_are_independent() {
        // Boolean write but non-boolean read: DwordCell.
        let stmts = vec![
            HirStmt::Act(HirAct::set_var_const(0, 7, 1, 0xFFFF_FFFF)),
            HirStmt::Trigger(
                HirTrigger::new(0, vec![HirCond::var_deaths(0, 0, 2)], vec![], true).unwrap(),
            ),
        ];
        assert_eq!(
            classify(HirVarDecl::new(0, 0), stmts),
            StorageClass::DwordCell { dword_index: 0 }
        );
        // Boolean read but non-boolean write: DwordCell.
        let stmts = vec![
            HirStmt::Act(HirAct::set_var_const(0, 7, 5, 0xFFFF_FFFF)),
            HirStmt::Trigger(
                HirTrigger::new(0, vec![HirCond::var_deaths(0, 10, 1)], vec![], true).unwrap(),
            ),
        ];
        assert_eq!(
            classify(HirVarDecl::new(0, 0), stmts),
            StorageClass::DwordCell { dword_index: 0 }
        );
    }

    #[test]
    fn storage_accessor_returns_plan_indices_and_none_on_missing_var() {
        // Main's plan accessor is `storage` (arch's `read_back`/`VarReadBack`
        // names were renamed on main): indices are copied from the plan,
        // never recomputed from the variable id, and a missing var yields
        // None so sim read-back can fail loudly instead of falling back
        // to a default TriggerPage slot.
        let plan = plan_for(
            vec![
                HirVarDecl::pinned(0, 0),
                HirVarDecl::new(1, 500),
                HirVarDecl::new(2, 0),
            ],
            vec![
                HirStmt::Act(HirAct::set_var_const(2, 7, 1, 0xFFFF_FFFF)),
                HirStmt::Trigger(
                    HirTrigger::new(0, vec![HirCond::var_deaths(2, 10, 1)], vec![], true).unwrap(),
                ),
            ],
        );
        assert_eq!(
            plan.storage(0),
            Some(StorageClass::TriggerPage {
                page_index: 0,
                slot_index: 0,
            })
        );
        assert_eq!(
            plan.storage(1),
            Some(StorageClass::DwordCell { dword_index: 0 })
        );
        assert_eq!(
            plan.storage(2),
            Some(StorageClass::BitCell {
                dword_index: 1,
                bit_index: 0,
            })
        );
        assert_eq!(plan.storage(99), None);
    }

    /// The post-desugar invariant helper sees high-level temporal nodes at any
    /// depth and ignores the already-desugared shapes.
    #[test]
    fn has_high_level_temporal_detects_un_desugared_nodes() {
        let act = HirStmt::Act(HirAct::set_var_const(2, 7, 1, 0xFFFF_FFFF));
        assert!(!has_high_level_temporal(&[act.clone()]));

        let high_level = [
            HirStmt::Until(HirUntil::new(vec![], vec![act.clone()], 9)),
            HirStmt::Edge(HirEdge::new(
                HirEdgeDirection::Rising,
                vec![],
                vec![act.clone()],
                1,
                2,
            )),
            HirStmt::Every(HirEvery::new(2, 8, vec![act.clone()])),
            HirStmt::After(HirAfter::new(2, 7, vec![act.clone()])),
        ];
        for node in high_level {
            assert!(
                has_high_level_temporal(&[node.clone()]),
                "a top-level high-level node is detected"
            );
            let nested = HirStmt::When(HirWhen::new(vec![], vec![node]));
            assert!(
                has_high_level_temporal(&[nested]),
                "a node nested in a when body is detected"
            );
        }
    }
}
