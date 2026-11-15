//! Shared scalar-storage facts for `eudkit-opt`.
//!
//! [`StorageFacts`] is the single home for the pin sets that decide whether a
//! variable is retained on a `TriggerPage` slot. Both consumers read it:
//! [`crate::cell::analyze_storage`] turns it into the final layout, and
//! [`crate::scheduler::CellMap`] feeds it to the act-ordering rules. There is
//! exactly one walk over the module, so the two can never disagree again:
//!
//! - a declaration with a page pin,
//! - a `VarTransfer`/`VarBatch`/`VarStore` source (its value is dereferenced
//!   through its page slot),
//! - a `HirCallArg::Var` argument (copied out of its slot by the call
//!   machinery), and
//! - a subroutine parameter or result (the call-site transfer writes/reads
//!   that slot).
//!
//! The walk covers every nested body and every subroutine body. Because
//! scheduling only regroups statements, the declaration, pin, transfer-source,
//! transfer-destination, and page-backing sets are identical before and after
//! scheduling. One set is deliberately not invariant: `not_boolean` also
//! records the counter arithmetic that `every`/`after` desugaring introduces,
//! so it can grow once optimization runs (see the `not_boolean` field docs).
//!
//! [`ActEffects`] is the per-act query over those facts. [`StorageFacts`] is
//! the only place page backing is decided; the effect classification and the
//! layout pass both consume it rather than re-walking declarations.

use std::collections::{BTreeSet, HashMap, HashSet};

use eudkit_hir::{HirAct, HirCallArg, HirCond, HirModule, HirReloc, HirStmt, HirVarDecl};
use eudkit_lir::{DEST_FIELD_OFFSET, EUDX_MAGIC, VALUE_FIELD_OFFSET};

use crate::cell::is_value_field_offset;

/// `SetDeaths`/`SetDeathsX` action type.
const ACT_SET_DEATHS: u8 = 45;
/// `SetTo` action modifier.
pub(crate) const MOD_SET_TO: u8 = 7;
/// All 32 bits of a scalar value word.
pub(crate) const FULL_MASK: u32 = 0xFFFF_FFFF;

/// Whether `act` writes the whole value word under the optimizer's conservative
/// mask model.
///
/// LIR applies the declared mask only to `SetDeathsX` (`eudx == EUDX_MAGIC`),
/// where `mask == 0` is a 0-bit no-op and only `FULL_MASK` replaces every bit.
/// For every other act LIR uses `eff_mask == FULL_MASK` regardless of the
/// `mask` field. This predicate still requires `mask == 0 || mask == FULL_MASK`
/// there, so an unexpected non-EUDX partial mask is treated as a read instead
/// of being assumed equivalent to LIR's full write.
fn act_has_full_mask(act: &HirAct) -> bool {
    if act.eudx == EUDX_MAGIC {
        act.mask == FULL_MASK
    } else {
        act.mask == 0 || act.mask == FULL_MASK
    }
}

/// Whether `act` writes `reloc`'s target value word in full without reading it.
///
/// Only a full-mask `SetTo` into the target's value field qualifies. A
/// `DEST_FIELD_OFFSET` reloc into any other target field patches machinery and
/// leaves the value word untouched, so it does not overwrite the value.
pub(crate) fn is_full_value_dest_write(act: &HirAct, reloc: &HirReloc) -> bool {
    reloc.field_offset == DEST_FIELD_OFFSET
        && is_value_field_offset(reloc.target_offset)
        && act.acttype == ACT_SET_DEATHS
        && act.modifier == MOD_SET_TO
        && act_has_full_mask(act)
}

/// Whether a declaration pins the variable onto a `TriggerPage` action slot.
fn is_pinned_decl(decl: &HirVarDecl) -> bool {
    matches!(
        decl.storage.as_deref(),
        Some("page") | Some("triggerpage") | Some("trigger_page")
    )
}

/// Scalar-storage pin sets, masks, and the derived page-backed set.
#[derive(Debug, Default, Clone)]
pub(crate) struct StorageFacts {
    masks: HashMap<usize, u32>,
    /// Variables pinned by their declaration.
    pinned: HashSet<usize>,
    /// Transfer/store/call sources plus subroutine params and results.
    vproc_sources: HashSet<usize>,
    /// Variables written by a transfer or call result copy-back.
    transfer_dsts: HashSet<usize>,
    /// Variables named by a non-boolean act or condition.
    ///
    /// This is the one set that may grow when [`StorageFacts::from_module`]
    /// runs after optimization: an `every`/`after` node names no variable
    /// itself, but its desugaring emits `MOD_ADD`/`MOD_SUB` counter acts,
    /// which are non-boolean writes. Every layout-relevant set above is stable
    /// across scheduling.
    not_boolean: HashSet<usize>,
    /// `pinned` union `vproc_sources`: the retained `TriggerPage` set.
    page_backed: HashSet<usize>,
}

impl StorageFacts {
    /// Collect every storage fact from the module in one walk.
    pub(crate) fn from_module(module: &HirModule) -> Self {
        let mut facts = Self {
            masks: module.vars.iter().map(|v| (v.id, v.mask)).collect(),
            pinned: module
                .vars
                .iter()
                .filter(|v| is_pinned_decl(v))
                .map(|v| v.id)
                .collect(),
            ..Self::default()
        };
        for stmt in &module.stmts {
            facts.visit(stmt);
        }
        for func in &module.funcs {
            // The call-site transfer writes every parameter and reads the
            // result, so both sides must stay page-backed.
            facts.vproc_sources.extend(func.params.iter().copied());
            if let Some(result) = func.result {
                facts.vproc_sources.insert(result);
            }
            for stmt in &func.body {
                facts.visit(stmt);
            }
        }
        facts.finish();
        facts
    }

    /// Facts for a bare statement list: no declarations, so no pins and every
    /// mask full.
    ///
    /// Only the unit tests that drive [`super::temporal::optimize_stmts`]
    /// without a module need this; production code always goes through
    /// [`StorageFacts::from_module`].
    #[cfg(test)]
    pub(crate) fn from_stmts(stmts: &[HirStmt]) -> Self {
        let mut facts = Self::default();
        for stmt in stmts {
            facts.visit(stmt);
        }
        facts.finish();
        facts
    }

    fn finish(&mut self) {
        self.page_backed = self.pinned.union(&self.vproc_sources).copied().collect();
    }

    /// Declared mask of `var_id`; unrecorded means full, as lowering assumes.
    pub(crate) fn mask(&self, var_id: usize) -> u32 {
        *self.masks.get(&var_id).unwrap_or(&FULL_MASK)
    }

    /// Whether the declaration pins `var_id` onto a `TriggerPage`.
    pub(crate) fn is_pinned(&self, var_id: usize) -> bool {
        self.pinned.contains(&var_id)
    }

    /// Whether `var_id` is a transfer/store/call source or a subroutine
    /// param/result, i.e. page-backed even without a declaration pin.
    pub(crate) fn is_vproc_source(&self, var_id: usize) -> bool {
        self.vproc_sources.contains(&var_id)
    }

    /// Whether `var_id` is retained in a `TriggerPage` action slot.
    pub(crate) fn is_page_backed(&self, var_id: usize) -> bool {
        self.page_backed.contains(&var_id)
    }

    /// Whether a transfer or call result copy-back writes `var_id`.
    pub(crate) fn is_transfer_dst(&self, var_id: usize) -> bool {
        self.transfer_dsts.contains(&var_id)
    }

    /// Whether an act or condition names `var_id` outside the boolean domain.
    pub(crate) fn is_not_boolean(&self, var_id: usize) -> bool {
        self.not_boolean.contains(&var_id)
    }

    fn visit(&mut self, stmt: &HirStmt) {
        match stmt {
            HirStmt::VarTransfer(t) => self.visit_transfer(t),
            HirStmt::VarBatch(b) => {
                for t in &b.transfers {
                    self.visit_transfer(t);
                }
                for s in &b.stores {
                    self.visit_store(s);
                }
            }
            HirStmt::VarStore(s) => self.visit_store(s),
            HirStmt::Call(c) => {
                // Arguments are copied out of their slots by the call
                // machinery, so var arguments must stay page-backed.
                for arg in &c.args {
                    if let HirCallArg::Var { id } = arg {
                        self.vproc_sources.insert(*id);
                    }
                }
                // The result is copied back into `dst` by a transfer, so it
                // cannot be a bit-cell.
                if let Some(dst) = c.dst {
                    self.transfer_dsts.insert(dst);
                }
            }
            HirStmt::Act(a) => self.inspect_act(a),
            HirStmt::Trigger(t) => {
                for c in &t.conds {
                    self.inspect_cond(c);
                }
                for a in &t.acts {
                    self.inspect_act(a);
                }
            }
            HirStmt::When(w) => {
                for c in &w.conds {
                    self.inspect_cond(c);
                }
                for s in w.body.iter().chain(w.else_body.iter()) {
                    self.visit(s);
                }
            }
            HirStmt::Once(o) => {
                for c in &o.conds {
                    self.inspect_cond(c);
                }
                for s in &o.body {
                    self.visit(s);
                }
            }
            HirStmt::While(w) => {
                for c in &w.conds {
                    self.inspect_cond(c);
                }
                for s in &w.body {
                    self.visit(s);
                }
            }
            HirStmt::Until(u) => {
                for c in &u.conds {
                    self.inspect_cond(c);
                }
                for s in &u.body {
                    self.visit(s);
                }
            }
            HirStmt::Edge(e) => {
                for c in &e.conds {
                    self.inspect_cond(c);
                }
                for s in &e.body {
                    self.visit(s);
                }
            }
            HirStmt::Every(e) => {
                // The pre-desugar node names no variable: `counter_var` is not
                // a page source here, and it only becomes non-boolean once the
                // desugar emits its `MOD_ADD`/`MOD_SET_TO` counter acts.
                for s in &e.body {
                    self.visit(s);
                }
            }
            HirStmt::After(a) => {
                // As with `Every`: the counter is unnamed until the desugar
                // emits its `MOD_SET_TO`/`MOD_SUB` acts.
                for s in &a.body {
                    self.visit(s);
                }
            }
        }
    }

    fn visit_transfer(&mut self, transfer: &eudkit_hir::HirVarTransfer) {
        self.vproc_sources.insert(transfer.src_var);
        self.transfer_dsts.insert(transfer.dst_var);
    }

    fn visit_store(&mut self, store: &eudkit_hir::HirVarStore) {
        // The source is dereferenced through its TriggerPage slot to write an
        // arbitrary destination, so it must stay page-backed.
        self.vproc_sources.insert(store.src_var);
    }

    fn inspect_act(&mut self, act: &HirAct) {
        for r in &act.relocs {
            if let Some(vid) = r.target_var {
                // Boolean-domain writes must be plain `SetTo` of 0/1 into the
                // variable value, with a full effective mask. LIR applies an
                // explicit `SetDeathsX` mask literally (0 is a 0-bit no-op),
                // while a plain `SetDeaths` mask is ignored; the optimizer is
                // stricter there, conservatively treating an unexpected
                // partial non-EUDX mask as a non-boolean read.
                if !is_value_field_offset(r.target_offset)
                    || act.acttype != ACT_SET_DEATHS
                    || act.modifier != MOD_SET_TO
                    || (act.value != 0 && act.value != 1)
                    || !act_has_full_mask(act)
                {
                    self.not_boolean.insert(vid);
                }
            }
        }
    }

    fn inspect_cond(&mut self, cond: &HirCond) {
        for r in &cond.relocs {
            if let Some(vid) = r.target_var {
                let valid = cond.condtype == 15
                    && is_value_field_offset(r.target_offset)
                    && cond.locid == 0
                    && cond.eudx == 0
                    && match (cond.comparison, cond.amount) {
                        (10, 0) | (10, 1) => true, // Exactly 0 / 1
                        (0, 1) => true,            // AtLeast 1 (>= 1)
                        (1, 0) | (1, 1) => true,   // AtMost 0 / 1
                        _ => false,
                    };
                if !valid {
                    self.not_boolean.insert(vid);
                }
            }
        }
    }
}

/// What one act does to the scalar variables it names.
#[derive(Debug, Default, PartialEq, Eq)]
pub(crate) struct ActEffects {
    /// Variables whose value field the act reads (its `value` operand).
    pub(crate) reads: BTreeSet<usize>,
    /// Variables whose value field the act writes (its `.dest` field).
    pub(crate) writes: BTreeSet<usize>,
    /// Variables whose non-value page fields the act names. These are the
    /// fields a firing patches, so an act touching one can never be folded
    /// into a batch's firing window.
    pub(crate) machinery: BTreeSet<usize>,
    /// Whether the act's destination is *not* a scalar variable (a constant
    /// EPD, an absolute word, or a trigger field). Such an act may alias a
    /// pending `VarStore`'s destination, which the scheduler cannot compare.
    pub(crate) opaque_dest: bool,
}

impl ActEffects {
    /// Classify `act` against the module's page-backed set.
    pub(crate) fn of(act: &HirAct, facts: &StorageFacts) -> Self {
        let mut effects = Self::default();
        for reloc in &act.relocs {
            let Some(var_id) = reloc.target_var else {
                continue;
            };
            let dest_write = reloc.field_offset == DEST_FIELD_OFFSET
                && is_value_field_offset(reloc.target_offset);
            let value_read = reloc.field_offset == VALUE_FIELD_OFFSET
                && is_value_field_offset(reloc.target_offset);
            if dest_write {
                effects.writes.insert(var_id);
                if !is_full_value_dest_write(act, reloc) {
                    effects.reads.insert(var_id);
                }
            } else if value_read {
                effects.reads.insert(var_id);
            } else if facts.is_page_backed(var_id) {
                effects.machinery.insert(var_id);
            } else {
                // Non-page scalar: a field offset other than the value word is
                // still a value access on its one dword.
                effects.reads.insert(var_id);
                effects.writes.insert(var_id);
            }
        }
        effects.opaque_dest = !act
            .relocs
            .iter()
            .any(|r| r.field_offset == DEST_FIELD_OFFSET && r.target_var.is_some());
        effects
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::scheduler::CellMap;
    use eudkit_hir::{
        HirAfter, HirCall, HirCallArg, HirCond, HirEvery, HirFunc, HirReloc, HirVarStore,
        HirVarTransfer, HirWhen,
    };
    use eudkit_lir::EUDX_MAGIC;

    /// A module whose subjects are page-backed only through a `Call` argument
    /// or a subroutine param/result: the exact pins the scheduler used to miss.
    fn call_and_func_pins() -> HirModule {
        let vars = (0..7).map(|id| HirVarDecl::new(id, 500)).collect();
        let func = HirFunc::new(
            0,
            vec![1],
            Some(2),
            vec![HirStmt::VarTransfer(HirVarTransfer {
                dst_var: 6,
                src_var: 5,
                modifier: 7,
            })],
        );
        HirModule {
            vars,
            stmts: vec![
                HirStmt::Call(HirCall::new(0, vec![HirCallArg::var(0)], Some(3))),
                HirStmt::Call(HirCall::new(0, vec![HirCallArg::var(0)], Some(4))),
            ],
            funcs: vec![func],
            strings: Vec::new(),
            files: Vec::new(),
            blobs: Vec::new(),
        }
    }

    #[test]
    fn call_and_subroutine_pins_classify_a_page_field_touch_as_machinery() {
        let cells = CellMap::from_module(&call_and_func_pins());
        for subject in [0, 1, 2] {
            let mut act = HirAct::set_deaths(0, 7, 0xDEAD, 0);
            act.relocs.push(HirReloc::var(
                VALUE_FIELD_OFFSET,
                subject,
                DEST_FIELD_OFFSET,
                1,
            ));
            let touches = ActEffects::of(&act, cells.facts());
            assert!(
                touches.machinery.contains(&subject),
                "var {subject} is page-backed only through the call site/function pins"
            );
        }
    }

    /// Scheduling only regroups statements: it turns adjacent transfers into
    /// batches and moves acts around a firing, but it never changes the
    /// declaration, pin, transfer-source/destination, page-backing, or
    /// `not_boolean` sets for a shape without counted loops.
    #[test]
    fn facts_are_identical_before_and_after_scheduling() {
        let module = HirModule {
            vars: (0..6).map(|id| HirVarDecl::new(id, 500)).collect(),
            stmts: vec![
                HirStmt::VarTransfer(HirVarTransfer {
                    dst_var: 1,
                    src_var: 0,
                    modifier: 7,
                }),
                HirStmt::VarStore(HirVarStore::to_addr(2, 0x640B58, 7)),
                HirStmt::When(HirWhen::new(
                    vec![HirCond::always()],
                    vec![HirStmt::VarTransfer(HirVarTransfer {
                        dst_var: 4,
                        src_var: 3,
                        modifier: 7,
                    })],
                )),
            ],
            funcs: Vec::new(),
            strings: Vec::new(),
            files: Vec::new(),
            blobs: Vec::new(),
        };
        let before = StorageFacts::from_module(&module);
        let optimized = crate::optimize_module(&module).expect("fixture optimizes");
        let after = StorageFacts::from_module(&optimized);

        assert_eq!(before.masks, after.masks);
        assert_eq!(before.pinned, after.pinned);
        assert_eq!(before.vproc_sources, after.vproc_sources);
        assert_eq!(before.transfer_dsts, after.transfer_dsts);
        assert_eq!(before.not_boolean, after.not_boolean);
        assert_eq!(before.page_backed, after.page_backed);
    }

    /// `every`/`after` name no counter variable before desugaring, but their
    /// `MOD_ADD`/`MOD_SUB` desugar acts are non-boolean writes. Only
    /// `not_boolean` may grow; the layout-relevant sets stay identical.
    #[test]
    fn not_boolean_grows_with_every_and_after_desugaring() {
        let body = || {
            vec![HirStmt::VarTransfer(HirVarTransfer {
                dst_var: 3,
                src_var: 2,
                modifier: 7,
            })]
        };
        let module = HirModule {
            vars: (0..4).map(|id| HirVarDecl::new(id, 500)).collect(),
            stmts: vec![
                HirStmt::Every(HirEvery::new(2, 1, body())),
                HirStmt::After(HirAfter::new(2, 0, body())),
            ],
            funcs: Vec::new(),
            strings: Vec::new(),
            files: Vec::new(),
            blobs: Vec::new(),
        };

        let before = StorageFacts::from_module(&module);
        let optimized = crate::optimize_module(&module).expect("fixture optimizes");
        let after = StorageFacts::from_module(&optimized);

        assert!(
            !before.is_not_boolean(0),
            "the `after` counter is unnamed until desugaring"
        );
        assert!(
            !before.is_not_boolean(1),
            "the `every` counter is unnamed until desugaring"
        );
        assert!(
            after.is_not_boolean(0),
            "the `after` counter's MOD_SUB act is non-boolean after desugaring"
        );
        assert!(
            after.is_not_boolean(1),
            "the `every` counter's MOD_ADD act is non-boolean after desugaring"
        );

        assert_eq!(before.masks, after.masks);
        assert_eq!(before.pinned, after.pinned);
        assert_eq!(before.vproc_sources, after.vproc_sources);
        assert_eq!(before.transfer_dsts, after.transfer_dsts);
        assert_eq!(before.page_backed, after.page_backed);
    }

    #[test]
    fn act_effects_of_rmw_and_partial_mask_dest_writes_record_reads_and_writes() {
        let mut add_act = HirAct::set_deaths(0, 8, 5, 0);
        add_act
            .relocs
            .push(HirReloc::var(DEST_FIELD_OFFSET, 0, VALUE_FIELD_OFFSET, 7));

        let mut masked_set = HirAct::set_deaths(0, 7, 1, 0);
        masked_set.mask = 0x00FF;
        masked_set
            .relocs
            .push(HirReloc::var(DEST_FIELD_OFFSET, 0, VALUE_FIELD_OFFSET, 7));

        let mut full_set = HirAct::set_deaths(0, 7, 1, 0);
        full_set
            .relocs
            .push(HirReloc::var(DEST_FIELD_OFFSET, 0, VALUE_FIELD_OFFSET, 7));

        let facts = StorageFacts::from_module(&HirModule {
            vars: vec![HirVarDecl::new(0, 0)],
            stmts: vec![],
            funcs: vec![],
            strings: vec![],
            files: vec![],
            blobs: vec![],
        });

        let full_effects = ActEffects::of(&full_set, &facts);
        assert!(full_effects.writes.contains(&0));
        assert!(!full_effects.reads.contains(&0));

        let add_effects = ActEffects::of(&add_act, &facts);
        assert!(add_effects.writes.contains(&0));
        assert!(add_effects.reads.contains(&0));

        let masked_effects = ActEffects::of(&masked_set, &facts);
        assert!(masked_effects.writes.contains(&0));
        assert!(masked_effects.reads.contains(&0));
    }

    /// A `SetDeathsX` value-dest act with the given value and mask.
    fn eudx_value_dest_act(value: u32, mask: u32) -> HirAct {
        let mut act = HirAct::set_deaths(0, 7, value, 0);
        act.eudx = EUDX_MAGIC;
        act.mask = mask;
        act.relocs
            .push(HirReloc::var(DEST_FIELD_OFFSET, 0, VALUE_FIELD_OFFSET, 7));
        act
    }

    /// Facts for a module whose only statement is `act` on var 0.
    fn scalar_facts_with(act: &HirAct) -> StorageFacts {
        StorageFacts::from_module(&HirModule {
            vars: vec![HirVarDecl::new(0, 0)],
            stmts: vec![HirStmt::Act(act.clone())],
            funcs: vec![],
            strings: vec![],
            files: vec![],
            blobs: vec![],
        })
    }

    /// A `SetDeathsX` (`eudx == EUDX_MAGIC`) with `mask == 0` lowers to a
    /// 0-bit no-op, not a full write: it is not boolean-domain safe and it
    /// depends on the target's pre-existing value.
    #[test]
    fn eudx_zero_mask_dest_write_is_not_boolean_and_reads_target() {
        let act = eudx_value_dest_act(1, 0);
        let facts = scalar_facts_with(&act);

        assert!(
            facts.is_not_boolean(0),
            "a 0-bit masked SetDeathsX is not a plain boolean SetTo"
        );
        let effects = ActEffects::of(&act, &facts);
        assert!(effects.writes.contains(&0));
        assert!(
            effects.reads.contains(&0),
            "a 0-bit masked SetDeathsX depends on the existing value word"
        );
    }

    /// A full-mask `SetDeathsX` is the boolean-safe full value write and does
    /// not read the target: LIR replaces the whole value word.
    #[test]
    fn eudx_full_mask_dest_write_is_boolean_and_does_not_read_target() {
        for value in [0, 1] {
            let act = eudx_value_dest_act(value, FULL_MASK);
            let facts = scalar_facts_with(&act);

            assert!(
                !facts.is_not_boolean(0),
                "full-mask SetDeathsX value {value} is a boolean-safe write"
            );
            let effects = ActEffects::of(&act, &facts);
            assert!(effects.writes.contains(&0));
            assert!(
                !effects.reads.contains(&0),
                "full-mask SetDeathsX value {value} does not read the target"
            );
        }
    }
}
