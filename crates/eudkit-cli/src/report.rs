//! `dump-ir` report: static/executed cost, storage allocation, packing density.

use eudkit_lir::{ExecutedCost, LirDocument, LirItem, StaticCost};
use eudkit_opt::cell::StoragePlan;
use eudkit_opt::folds::{FoldEvent, FoldLog};
use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct StorageSummary {
    pub total_vars: usize,
    pub trigger_page_slots: usize,
    pub trigger_pages: usize,
    pub dword_cells: usize,
    pub bit_cells: usize,
    pub cell_dwords: usize,
    pub cell_bytes: usize,
}

impl StorageSummary {
    pub fn new(total_vars: usize, plan: &StoragePlan) -> Self {
        Self {
            total_vars,
            trigger_page_slots: plan.trigger_page_vars,
            trigger_pages: plan.trigger_pages,
            dword_cells: plan.dword_cell_vars,
            bit_cells: plan.bit_cell_vars,
            cell_dwords: plan.cell_dwords,
            cell_bytes: plan.cell_dwords * 4,
        }
    }
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct PackingEntry {
    pub item: usize,
    pub conditions: usize,
    pub actions: usize,
    pub condition_density: f64,
    pub action_density: f64,
}

impl PackingEntry {
    fn from_trigger(item: usize, t: &eudkit_lir::TriggerItem) -> Self {
        Self {
            item,
            conditions: t.conds.len(),
            actions: t.acts.len(),
            condition_density: t.conds.len() as f64 / 16.0,
            action_density: t.acts.len() as f64 / 64.0,
        }
    }
}

/// One optimizer fold rendered for the `dump-ir` report.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct FoldEntry {
    pub kind: String,
    pub site: String,
    pub before: String,
    pub after: String,
}

impl FoldEntry {
    fn from_event(event: &FoldEvent) -> Self {
        Self {
            kind: event.kind.as_str().to_string(),
            site: event.site.clone(),
            before: event.before.clone(),
            after: event.after.clone(),
        }
    }
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct DumpReport {
    pub static_cost: StaticCost,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub executed_cost: Option<ExecutedCost>,
    /// `dump-ir` lowers but does not assemble a map, so its costs exclude
    /// the once-per-tick gate that map assembly appends. The flag makes
    /// that explicit for JSON consumers; text output labels it too.
    pub pre_assembly: bool,
    pub storage: StorageSummary,
    pub packing: Vec<PackingEntry>,
    /// Present only for `dump-ir --explain-folds`; absent (not just empty)
    /// otherwise, so default JSON stays byte-identical.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub folds: Option<Vec<FoldEntry>>,
}

impl DumpReport {
    pub fn new(
        static_cost: StaticCost,
        executed_cost: Option<ExecutedCost>,
        total_vars: usize,
        storage: &StoragePlan,
        lir: &LirDocument,
        folds: Option<&FoldLog>,
    ) -> Self {
        let mut packing = Vec::new();
        for (idx, item) in lir.module.items.iter().enumerate() {
            if let LirItem::Trigger(trig) = item {
                packing.push(PackingEntry::from_trigger(idx, trig));
            }
        }
        Self {
            static_cost,
            executed_cost,
            pre_assembly: true,
            storage: StorageSummary::new(total_vars, storage),
            packing,
            folds: folds.map(|log| {
                log.events()
                    .iter()
                    .map(FoldEntry::from_event)
                    .collect::<Vec<_>>()
            }),
        }
    }

    pub fn render_text(&self) -> String {
        let mut out = String::new();
        out.push_str(&format!(
            "Static cost: {}T static ({}C, {}A, {} bytes)\n",
            self.static_cost.triggers,
            self.static_cost.conditions,
            self.static_cost.actions,
            self.static_cost.total_bytes
        ));
        if self.pre_assembly {
            out.push_str(
                "Cost stage: pre-assembly (excludes the map-build once-per-tick gate)\n",
            );
        }
        if let Some(executed) = &self.executed_cost {
            out.push_str(&format!(
                "Executed cost: {}T executed ({}C, {}A per loop)\n",
                executed.triggers, executed.conditions, executed.actions
            ));
        }
        out.push_str(&format!(
            "Storage: {} TriggerPage slot(s) in {} page(s); {} dword cell(s); {} bit cell(s) in {} dword(s) ({} bytes)\n",
            self.storage.trigger_page_slots,
            self.storage.trigger_pages,
            self.storage.dword_cells,
            self.storage.bit_cells,
            self.storage.cell_dwords,
            self.storage.cell_bytes
        ));
        out.push_str("Packing density:\n");
        for entry in &self.packing {
            out.push_str(&format!(
                "  trigger[{idx}] {c}/16C ({cd:.1}%) {a}/64A ({ad:.1}%)\n",
                idx = entry.item,
                c = entry.conditions,
                cd = entry.condition_density * 100.0,
                a = entry.actions,
                ad = entry.action_density * 100.0
            ));
        }
        if let Some(folds) = &self.folds {
            out.push_str(&format!("Folds: {} event(s)\n", folds.len()));
            for entry in folds {
                out.push_str(&format!(
                    "  [{}] {}: {} -> {}\n",
                    entry.kind, entry.site, entry.before, entry.after
                ));
            }
        }
        out
    }
}
