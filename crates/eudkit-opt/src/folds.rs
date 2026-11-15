//! Deterministic, opt-in reporting of optimizer-visible folds.
//!
//! [`FoldLog`] is a plain in-memory sink threaded through the temporal passes.
//! Production callers pass [`FoldLog::disabled`], which never allocates and
//! never advances the site counter; `dump-ir --explain-folds` passes
//! [`FoldLog::enabled`] and renders the recorded events.
//!
//! Event kinds mirror the temporal passes: condition fusion/dedup
//! ([`FoldKind::CondFold`]), constant-gate opening ([`FoldKind::GateOpen`]),
//! post-schedule dead-gate removal ([`FoldKind::GateDrop`]), adjacent flat
//! sibling merges ([`FoldKind::Merge`]), and ADR 0027 condition chunking
//! ([`FoldKind::Chunk`]).
//!
//! `site` is a deterministic ordinal, not a HIR id. Gate sites are assigned in
//! optimizer pre-order (`when#0`, `once#1`, ...). Dead gates discovered by the
//! post-schedule removal pass have already lost their pre-order ordinal, so
//! they receive the next ordinal at the moment the drop is reported; that
//! keeps the whole log stable across runs for a given input.

use eudkit_hir::HirCond;
use eudkit_lir::COND_COUNT;

/// The optimizer transformations a [`FoldEvent`] can describe.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum FoldKind {
    /// Conditions fused and/or deduplicated (including `Always`/`Never`).
    CondFold,
    /// A constant (`Always`/empty) gate was opened into its body.
    GateOpen,
    /// A dead gate (empty body, or statically `Never`) was removed.
    GateDrop,
    /// Two consecutive identically-gated flat siblings were merged.
    Merge,
    /// A gate with more than 16 conditions was split into chunks (ADR 0027).
    Chunk,
}

impl FoldKind {
    /// Stable, lowercase JSON/report spelling.
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::CondFold => "cond-fold",
            Self::GateOpen => "gate-open",
            Self::GateDrop => "gate-drop",
            Self::Merge => "merge",
            Self::Chunk => "chunk",
        }
    }
}

/// One recorded optimization step.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct FoldEvent {
    pub kind: FoldKind,
    /// Deterministic site label: `when#<n>`, `once#<n>`, or `while#<n>`.
    pub site: String,
    /// Compact condition summary of the input.
    pub before: String,
    /// Compact condition summary of the output, or `opened`/`dropped`/counts.
    pub after: String,
}

/// Opt-in fold sink threaded through the temporal optimizer.
///
/// The disabled log is a zero-allocation no-op: [`Self::next_site`] returns 0
/// without advancing and [`Self::push`] discards the event.
#[derive(Debug, Default, Clone, PartialEq, Eq)]
pub struct FoldLog {
    enabled: bool,
    next_site: u32,
    events: Vec<FoldEvent>,
}

impl FoldLog {
    /// A recording log.
    pub fn enabled() -> Self {
        Self {
            enabled: true,
            next_site: 0,
            events: Vec::new(),
        }
    }

    /// A no-op log for production optimization.
    pub fn disabled() -> Self {
        Self::default()
    }

    pub fn is_enabled(&self) -> bool {
        self.enabled
    }

    /// Allocate the next gate-site ordinal. Returns 0 and does not advance
    /// when disabled.
    pub fn next_site(&mut self) -> u32 {
        if !self.enabled {
            return 0;
        }
        let site = self.next_site;
        self.next_site += 1;
        site
    }

    /// Record an event. Discarded when disabled.
    pub fn push(&mut self, event: FoldEvent) {
        if self.enabled {
            self.events.push(event);
        }
    }

    pub fn events(&self) -> &[FoldEvent] {
        &self.events
    }

    pub fn into_events(self) -> Vec<FoldEvent> {
        self.events
    }
}

/// Render one condition in the compact, address-free report form.
///
/// `AtLeast`/`AtMost`/`Exactly` spell the three comparison codes of a
/// `Deaths`/`DeathsX` (condtype 15) read; `Always`/`Never` are condtypes 22/23.
/// Every other condition falls back to a deterministic field list. No pointer
/// addresses or `Debug` output is used, so the text is stable across runs.
pub fn render_cond(cond: &HirCond) -> String {
    match cond.condtype {
        22 => "Always".to_string(),
        23 => "Never".to_string(),
        15 => {
            let name = match cond.comparison {
                0 => "AtLeast",
                1 => "AtMost",
                10 => "Exactly",
                _ => "Deaths",
            };
            format!(
                "{name}(15,p{},u{},0x{:08x},{})",
                cond.player, cond.unit, cond.locid, cond.amount
            )
        }
        other => format!(
            "Cond({other},p{},u{},0x{:08x},{},cmp{})",
            cond.player, cond.unit, cond.locid, cond.amount, cond.comparison
        ),
    }
}

/// Render a condition list in source order, joined by ` & `.
///
/// An empty list is the unconditional gate, rendered `Always` (the same value
/// a lone `Always` condition renders).
pub fn render_conds(conds: &[HirCond]) -> String {
    if conds.is_empty() {
        return "Always".to_string();
    }
    let rendered: Vec<String> = conds.iter().map(render_cond).collect();
    rendered.join(" & ")
}

fn plural(count: usize, singular: &str, plural: &str) -> String {
    if count == 1 {
        format!("{count} {singular}")
    } else {
        format!("{count} {plural}")
    }
}

fn conditions_phrase(count: usize) -> String {
    plural(count, "condition", "conditions")
}

fn statements_phrase(count: usize) -> String {
    plural(count, "statement", "statements")
}

fn site_label(prefix: &str, site: u32) -> String {
    format!("{prefix}#{site}")
}

/// Record a condition fold when the fused output differs from the input.
pub fn record_cond_fold(
    log: &mut FoldLog,
    prefix: &str,
    site: u32,
    before: &[HirCond],
    after: &[HirCond],
) {
    if !log.is_enabled() || before == after {
        return;
    }
    log.push(FoldEvent {
        kind: FoldKind::CondFold,
        site: site_label(prefix, site),
        before: render_conds(before),
        after: render_conds(after),
    });
}

/// Record a constant gate opening into its body.
pub fn record_gate_open(log: &mut FoldLog, prefix: &str, site: u32, conds: &[HirCond]) {
    if !log.is_enabled() {
        return;
    }
    log.push(FoldEvent {
        kind: FoldKind::GateOpen,
        site: site_label(prefix, site),
        before: render_conds(conds),
        after: "opened".to_string(),
    });
}

/// Record a dead-gate removal.
pub fn record_gate_drop(log: &mut FoldLog, prefix: &str, site: u32, conds: &[HirCond]) {
    record_gate_drop_with(log, prefix, site, conds, "dropped");
}

/// Record a dead-gate outcome with a caller-chosen `after` reason.
///
/// Gate satisfiability (ADR 0031) distinguishes a plain drop from an
/// `else` promotion and from a `Never` body kept because it declares a
/// referenced trigger. The reason is text only: the event kind stays
/// [`FoldKind::GateDrop`].
pub fn record_gate_drop_with(
    log: &mut FoldLog,
    prefix: &str,
    site: u32,
    conds: &[HirCond],
    after: &str,
) {
    if !log.is_enabled() {
        return;
    }
    log.push(FoldEvent {
        kind: FoldKind::GateDrop,
        site: site_label(prefix, site),
        before: render_conds(conds),
        after: after.to_string(),
    });
}

/// Record an adjacent flat sibling merge: condition count in, condition and
/// statement counts out.
pub fn record_merge(
    log: &mut FoldLog,
    prefix: &str,
    site: u32,
    conditions: usize,
    statements: usize,
) {
    if !log.is_enabled() {
        return;
    }
    log.push(FoldEvent {
        kind: FoldKind::Merge,
        site: site_label(prefix, site),
        before: conditions_phrase(conditions),
        after: format!(
            "{}, {}",
            conditions_phrase(conditions),
            statements_phrase(statements)
        ),
    });
}

/// Record ADR 0027 condition chunking for a gate wider than one trigger chunk.
pub fn record_chunk(log: &mut FoldLog, prefix: &str, site: u32, conds: &[HirCond]) {
    if !log.is_enabled() || conds.len() <= COND_COUNT {
        return;
    }
    log.push(FoldEvent {
        kind: FoldKind::Chunk,
        site: site_label(prefix, site),
        before: conditions_phrase(conds.len()),
        after: format!(
            "{} chunks of <={COND_COUNT}",
            conds.len().div_ceil(COND_COUNT)
        ),
    });
}

/// Render a recorded fold log as the `dump-ir --explain-folds` text section.
pub fn render_folds(log: &FoldLog) -> String {
    render_events(log.events())
}

/// Render events as the `Folds:` text section.
pub fn render_events(events: &[FoldEvent]) -> String {
    let mut out = format!("Folds: {} event(s)\n", events.len());
    for event in events {
        out.push_str(&format!(
            "  [{}] {}: {} -> {}\n",
            event.kind.as_str(),
            event.site,
            event.before,
            event.after
        ));
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::optimize_module;
    use eudkit_hir::{HirAct, HirModule, HirStmt, HirWhen};

    fn deaths(amount: u32) -> HirCond {
        HirCond::deaths(0, 0, amount, 0)
    }

    fn act(value: u32) -> HirStmt {
        HirStmt::Act(HirAct::set_deaths(0, 7, value, 0))
    }

    /// Two independent gates, each with a pair of `AtLeast` conditions on the
    /// same target: the only optimizer-visible step is the amount fusion.
    fn two_gate_module() -> HirModule {
        HirModule {
            vars: Vec::new(),
            stmts: vec![
                HirStmt::When(HirWhen::new(vec![deaths(5), deaths(10)], vec![act(1)])),
                HirStmt::When(HirWhen::new(vec![deaths(7), deaths(9)], vec![act(2)])),
            ],
            strings: Vec::new(),
            files: Vec::new(),
            blobs: Vec::new(),
            funcs: Vec::new(),
        }
    }

    #[test]
    fn disabled_log_records_nothing_and_never_advances() {
        let mut log = FoldLog::disabled();
        assert!(!log.is_enabled());
        assert_eq!(log.next_site(), 0);
        assert_eq!(log.next_site(), 0);
        log.push(FoldEvent {
            kind: FoldKind::CondFold,
            site: "when#0".to_string(),
            before: "Always".to_string(),
            after: "Never".to_string(),
        });
        assert!(log.events().is_empty());
        assert!(log.into_events().is_empty());
    }

    #[test]
    fn enabled_log_records_two_cond_folds_in_deterministic_order() {
        let (_, log) = crate::optimize_module_with_folds(&two_gate_module()).unwrap();
        assert!(log.is_enabled());
        let kinds: Vec<FoldKind> = log.events().iter().map(|event| event.kind).collect();
        assert_eq!(kinds, vec![FoldKind::CondFold, FoldKind::CondFold]);
        assert_eq!(log.events()[0].site, "when#0");
        assert_eq!(log.events()[1].site, "when#1");
        assert_eq!(
            log.events()[0].before,
            "AtLeast(15,p0,u0,0x00000000,5) & AtLeast(15,p0,u0,0x00000000,10)"
        );
        assert_eq!(log.events()[0].after, "AtLeast(15,p0,u0,0x00000000,10)");
        assert_eq!(log.events()[1].after, "AtLeast(15,p0,u0,0x00000000,9)");

        // Identical input, identical log: site assignment is not a hash or an
        // address, so a second run must reproduce the event stream exactly.
        let (_, again) = crate::optimize_module_with_folds(&two_gate_module()).unwrap();
        assert_eq!(log.events(), again.events());
    }

    #[test]
    fn folded_and_unfolded_runs_produce_identical_hir() {
        let module = two_gate_module();
        let plain = optimize_module(&module).unwrap();
        let (folded, _) = crate::optimize_module_with_folds(&module).unwrap();
        assert_eq!(plain, folded);
    }

    #[test]
    fn render_cond_uses_compact_stable_spellings() {
        assert_eq!(render_cond(&HirCond::always()), "Always");
        assert_eq!(render_cond(&HirCond::never()), "Never");
        assert_eq!(
            render_cond(&HirCond::deaths(2, 1, 7, 3)),
            "AtMost(15,p2,u3,0x00000000,7)"
        );
        assert_eq!(
            render_cond(&HirCond::deaths(0, 10, 4, 0)),
            "Exactly(15,p0,u0,0x00000000,4)"
        );
        assert_eq!(render_conds(&[]), "Always");
    }
}
