//! Structured and temporal control flow lowering (`when`, `once`, `while`)
//! using SetNextPtr conditional branching around complex bodies.
//!
//! Path-complete restore guarantee: every `when`/`once`/`while` gate's next
//! pointer is restored to its default target on every path from body entry
//! to join.
//! A restore act is appended to the last body trigger only when that trigger
//! is unconditional (no conditions, next is DefaultChain, preserved);
//! otherwise a separate restore trigger is pushed. A conditional,
//! self-disabling, full, or already-assigned epilogue trigger must never
//! swallow the restore, or the gate stays stale on the next loop and the
//! body re-enters on a gate-false loop (or a `while` loop never exits).
//! Gate, restore, and join edges here are ExecTrigger indices only, never
//! var ids; variable ranks come from StoragePlan, never from a var id.

use eudkit_hir::{HirCond, HirOnce, HirStmt, HirWhen, HirWhile};
use eudkit_lir::{ACT_COUNT, COND_COUNT, NEXT_BYTE_OFFSET, set_next_ptr};

use crate::{ExecAct, ExecNext, ExecReloc, ExecRelocTarget, ExecTrigger, LowerError};

/// Number of `Trigger (16C, 64A)` chunks a structured gate needs.
///
/// `k == 1` (including an empty condition list) keeps the single-gate code
/// path verbatim; `k >= 2` switches the gate chain to ADR 0027 chunking.
fn chunk_count(conds: &[HirCond]) -> usize {
    conds.len().div_ceil(COND_COUNT).max(1)
}

/// Number of restore triggers needed to hold `k` `SetNextPtr` acts.
fn restore_trigger_count(k: usize) -> usize {
    k.div_ceil(ACT_COUNT).max(1)
}

/// Whether `needed_acts` restore acts may be appended to `trigger`.
///
/// Appending is safe only when the trigger is guaranteed to execute on
/// every path that reaches the end of the body: no conditions, a
/// DefaultChain next (so it is not itself a gate or an inner epilogue
/// whose next is already assigned), preserved (so it does not
/// self-disable like a `once` leaf), and room for all the acts.
/// Anything else gets a separate restore trigger, whose index is then
/// the inner join target for any conditional branch that skipped the body.
fn can_append_restore(trigger: &ExecTrigger, needed_acts: usize) -> bool {
    trigger.conds.is_empty()
        && matches!(trigger.next, ExecNext::DefaultChain)
        && trigger.preserved
        && trigger.acts.len() + needed_acts <= ACT_COUNT
}

/// Push the `k` condition chunks as a gate chain and return `k`.
///
/// Each gate's act rewrites its own next pointer to the following gate, and
/// the last gate targets `last_act_target` (the body, or the failure restore
/// when the body is empty). The static `next` defaults are patched by the
/// caller once the join and restore targets are known. Gates use explicit
/// `ExecItem` next edges, never `DefaultChain`, so a later chunk cannot fall
/// through into the next physical trigger by accident.
fn push_chunk_gates(
    exec_triggers: &mut Vec<ExecTrigger>,
    conds: &[HirCond],
    last_act_target: usize,
    last_preserved: bool,
) -> usize {
    let start = exec_triggers.len();
    let k = conds.len().div_ceil(COND_COUNT);
    debug_assert!(k >= 2, "chunked gates require at least two chunks");
    for (i, chunk) in conds.chunks(COND_COUNT).enumerate() {
        let idx = start + i;
        let is_last = i + 1 == k;
        let act_target = if is_last { last_act_target } else { idx + 1 };
        exec_triggers.push(ExecTrigger {
            hir_id: None,
            conds: chunk.to_vec(),
            acts: vec![make_exec_set_next_ptr_act(
                idx,
                ExecRelocTarget::ExecItem {
                    exec_idx: act_target,
                    offset: 0,
                },
            )],
            preserved: !(is_last && !last_preserved),
            flags: if is_last && !last_preserved { 0 } else { 4 },
            prev: None,
            next: ExecNext::ExecItem(0), // patched by the caller
        });
    }
    k
}

/// Restore acts for every chunk gate, in gate order.
///
/// Gate 0's target is a placeholder patched by the caller; every later gate
/// targets the failure restore (`R`/`S`) so the chain's static defaults are
/// correct. If `k > ACT_COUNT` the acts are split across chained
/// unconditional preserved triggers by [`push_gate_restore_block`].
fn make_gate_restore_acts(
    gate_start: usize,
    k: usize,
    nonfirst_target: usize,
) -> Vec<ExecAct> {
    (0..k)
        .map(|i| {
            let target = if i == 0 { 0 } else { nonfirst_target };
            make_exec_set_next_ptr_act(
                gate_start + i,
                ExecRelocTarget::ExecItem {
                    exec_idx: target,
                    offset: 0,
                },
            )
        })
        .collect()
}

/// Push an unconditional restore block resetting all `k` gates.
///
/// Returns `(first_trigger, last_trigger)`. Blocks after the first chain via
/// `DefaultChain`, mirroring the `chunks(ACT_COUNT)` split used for ordinary
/// action runs, so `k > ACT_COUNT` still restores every gate in one pass.
fn push_gate_restore_block(
    exec_triggers: &mut Vec<ExecTrigger>,
    gate_start: usize,
    k: usize,
    nonfirst_target: usize,
) -> (usize, usize) {
    debug_assert!(k > 0, "a restore block needs at least one gate act");
    let acts = make_gate_restore_acts(gate_start, k, nonfirst_target);
    let start = exec_triggers.len();
    let mut last = start;
    for chunk in acts.chunks(ACT_COUNT) {
        last = exec_triggers.len();
        exec_triggers.push(ExecTrigger {
            hir_id: None,
            conds: Vec::new(),
            acts: chunk.to_vec(),
            preserved: true,
            flags: 4,
            prev: None,
            next: ExecNext::DefaultChain,
        });
    }
    (start, last)
}

/// Point a `SetNextPtr` act's target at `target`.
fn patch_set_next_target(act: &mut ExecAct, target: usize) {
    debug_assert_eq!(
        act.relocs.len(),
        2,
        "SetNextPtr act must carry gate + target relocs"
    );
    act.relocs[1].target = ExecRelocTarget::ExecItem {
        exec_idx: target,
        offset: 0,
    };
}

/// Patch gate 0's target inside a restore block.
fn patch_first_restore_target(
    exec_triggers: &mut [ExecTrigger],
    block_start: usize,
    target: usize,
) {
    patch_set_next_target(&mut exec_triggers[block_start].acts[0], target);
}

/// Point every chunk gate at its static default.
///
/// `first_target` is chunk 0's fail target (the join, or `else_begin` for a
/// `when` with an else arm). Later gates jump to the failure restore so a
/// failed chunk cannot leave an earlier mutated gate behind (ADR 0027 I1-I3).
fn patch_chunk_gate_defaults(
    exec_triggers: &mut [ExecTrigger],
    gate_start: usize,
    k: usize,
    first_target: usize,
    nonfirst_target: usize,
) {
    for i in 0..k {
        exec_triggers[gate_start + i].next = ExecNext::ExecItem(if i == 0 {
            first_target
        } else {
            nonfirst_target
        });
    }
}

pub(crate) fn make_exec_set_next_ptr_act(exec_idx: usize, target: ExecRelocTarget) -> ExecAct {
    let (ctrig, relocs) = set_next_ptr(
        ExecRelocTarget::ExecItem {
            exec_idx,
            offset: NEXT_BYTE_OFFSET,
        },
        target,
    );
    ExecAct::new(
        ctrig,
        relocs
            .into_iter()
            .map(|(field_offset, target, mode)| ExecReloc {
                field_offset,
                target,
                mode,
            })
            .collect(),
    )
}

/// Where the success-restore acts landed.
#[derive(Debug, Clone, Copy)]
enum RestorePlacement {
    /// Appended to an existing unconditional body trigger. `first_act` is the
    /// index of the first appended act.
    Appended { trigger: usize, first_act: usize },
    /// Pushed as a fresh restore block spanning `start..=last`.
    Block { start: usize, last: usize },
}

impl RestorePlacement {
    /// The block start when the restore was pushed separately.
    fn block_start(self) -> Option<usize> {
        match self {
            Self::Block { start, .. } => Some(start),
            Self::Appended { .. } => None,
        }
    }
}

/// Append the `k` gate-restore acts to the last body trigger when `can_append`,
/// otherwise push a fresh restore block.
///
/// `nonfirst_target` is the failure restore carried by later gate acts. The
/// first act's target is patched by [`finish_restore`] once the join (or
/// `else_begin`) is known.
fn append_or_push_restore(
    exec_triggers: &mut Vec<ExecTrigger>,
    gate_start: usize,
    k: usize,
    nonfirst_target: usize,
    can_append: bool,
) -> RestorePlacement {
    if can_append {
        let trigger = exec_triggers.len() - 1;
        let first_act = exec_triggers[trigger].acts.len();
        exec_triggers[trigger]
            .acts
            .extend(make_gate_restore_acts(gate_start, k, nonfirst_target));
        RestorePlacement::Appended { trigger, first_act }
    } else {
        let (start, last) =
            push_gate_restore_block(exec_triggers, gate_start, k, nonfirst_target);
        RestorePlacement::Block { start, last }
    }
}

/// Patch the success restore to target `first_target` and exit to `last_next`
/// (the join, or `g0` for a `while` loop-back).
fn finish_restore(
    exec_triggers: &mut [ExecTrigger],
    placement: RestorePlacement,
    first_target: usize,
    last_next: usize,
) {
    match placement {
        RestorePlacement::Appended { trigger, first_act } => {
            patch_set_next_target(&mut exec_triggers[trigger].acts[first_act], first_target);
            exec_triggers[trigger].next = ExecNext::ExecItem(last_next);
        }
        RestorePlacement::Block { start, last } => {
            patch_first_restore_target(exec_triggers, start, first_target);
            exec_triggers[last].next = ExecNext::ExecItem(last_next);
        }
    }
}

pub(crate) fn lower_when<F>(
    when: &HirWhen,
    exec_triggers: &mut Vec<ExecTrigger>,
    mut lower_sub_stmts: F,
) -> Result<(), LowerError>
where
    F: FnMut(&[HirStmt], &mut Vec<ExecTrigger>) -> Result<(), LowerError>,
{
    if chunk_count(&when.conds) > 1 {
        return lower_when_chunked(when, exec_triggers, &mut lower_sub_stmts);
    }

    let gate_idx = exec_triggers.len();
    let then_start_idx = gate_idx + 1;

    let gate_act = make_exec_set_next_ptr_act(
        gate_idx,
        ExecRelocTarget::ExecItem {
            exec_idx: then_start_idx,
            offset: 0,
        },
    );

    exec_triggers.push(ExecTrigger {
        hir_id: None,
        conds: when.conds.clone(),
        acts: vec![gate_act],
        preserved: true,
        flags: 4,
        prev: None,
        next: ExecNext::DefaultChain, // patched below
    });

    let then_begin = exec_triggers.len();
    lower_sub_stmts(&when.body, exec_triggers)?;
    let then_end = exec_triggers.len();

    let has_else = !when.else_body.is_empty();

    // Placeholder restore trigger if then body cannot accept another action
    let restore_act = make_exec_set_next_ptr_act(
        gate_idx,
        ExecRelocTarget::ExecItem {
            exec_idx: 0, // will patch once default_target is known
            offset: 0,
        },
    );

    if then_end > then_begin && can_append_restore(&exec_triggers[then_end - 1], 1) {
        exec_triggers[then_end - 1].acts.push(restore_act);
    } else {
        exec_triggers.push(ExecTrigger {
            hir_id: None,
            conds: Vec::new(),
            acts: vec![restore_act],
            preserved: true,
            flags: 4,
            prev: None,
            next: ExecNext::DefaultChain,
        });
    }
    let then_epilogue_idx = exec_triggers.len() - 1;

    let else_begin = exec_triggers.len();
    if has_else {
        lower_sub_stmts(&when.else_body, exec_triggers)?;
    }
    let join_idx = exec_triggers.len();

    let default_target = if has_else { else_begin } else { join_idx };
    exec_triggers[gate_idx].next = ExecNext::ExecItem(default_target);

    // Patch restore act target
    let restore_act_idx = exec_triggers[then_epilogue_idx].acts.len() - 1;
    patch_set_next_target(
        &mut exec_triggers[then_epilogue_idx].acts[restore_act_idx],
        default_target,
    );

    exec_triggers[then_epilogue_idx].next = ExecNext::ExecItem(join_idx);

    if has_else && exec_triggers.len() > else_begin {
        let last_else_idx = exec_triggers.len() - 1;
        // Only move an else trigger that still falls through. A trailing
        // while-loop owns an explicit loop-back edge, a call owns its next
        // edge, and an explicit raw trigger next is already deliberate; none
        // of those may be redirected to the join.
        if matches!(
            exec_triggers[last_else_idx].next,
            ExecNext::DefaultChain
        ) {
            exec_triggers[last_else_idx].next = ExecNext::ExecItem(join_idx);
        }
    }

    Ok(())
}

/// `>16`-condition `when`: split the gate into `k` chunks.
///
/// Topology (`R` = failure restore before the body, `S` = success restore
/// after it):
///
/// ```text
/// when-no-else:  g0 -> g1 -> ... -> g(k-1) -> body -> S -> join
///                g0.next = join, gi.next = S
/// when+else:     g0 -> ... -> g(k-1) -> body -> S -> join
///                                     \-> R -> else_body -> join
///                g0.next = else_begin, gi.next = R
/// ```
///
/// A failing later gate jumps to the restore, which resets every chunk gate
/// in the same pass (I3); a successful chain reaches the body and then the
/// success restore. Without an else arm, `S` is also the failure target, so
/// it must be a separate trigger: a failing `gi` jumps over the body straight
/// to it. With an else arm, `S` may ride on an unconditional last then
/// trigger because only the success path falls into it.
fn lower_when_chunked<F>(
    when: &HirWhen,
    exec_triggers: &mut Vec<ExecTrigger>,
    lower_sub_stmts: &mut F,
) -> Result<(), LowerError>
where
    F: FnMut(&[HirStmt], &mut Vec<ExecTrigger>) -> Result<(), LowerError>,
{
    let k = chunk_count(&when.conds);
    let has_else = !when.else_body.is_empty();
    let gate_start = exec_triggers.len();
    let restore_blocks = restore_trigger_count(k);
    let fail_restore_start = gate_start + k;
    let then_start = if has_else {
        fail_restore_start + restore_blocks
    } else {
        gate_start + k
    };

    debug_assert_eq!(
        push_chunk_gates(exec_triggers, &when.conds, then_start, true),
        k
    );

    let r_block = has_else
        .then(|| push_gate_restore_block(exec_triggers, gate_start, k, fail_restore_start));
    debug_assert_eq!(exec_triggers.len(), then_start);

    let then_begin = exec_triggers.len();
    lower_sub_stmts(&when.body, exec_triggers)?;
    let then_end = exec_triggers.len();

    // Success restore. In the else form it may append to an unconditional
    // last then trigger; the no-else form always needs a standalone trigger
    // because failing later gates target it directly.
    let s_start = exec_triggers.len();
    let s_can_append =
        has_else && then_end > then_begin && can_append_restore(&exec_triggers[then_end - 1], k);
    let s_nonfirst = if has_else { fail_restore_start } else { s_start };
    let s_placement =
        append_or_push_restore(exec_triggers, gate_start, k, s_nonfirst, s_can_append);

    let else_begin = exec_triggers.len();
    if has_else {
        lower_sub_stmts(&when.else_body, exec_triggers)?;
    }
    let join_idx = exec_triggers.len();

    let first_default = if has_else { else_begin } else { join_idx };
    let nonfirst_default = if has_else {
        fail_restore_start
    } else {
        s_placement
            .block_start()
            .expect("no-else chunked when always pushes a success restore")
    };
    patch_chunk_gate_defaults(exec_triggers, gate_start, k, first_default, nonfirst_default);

    if let Some((r_start, r_last)) = r_block {
        patch_first_restore_target(exec_triggers, r_start, else_begin);
        exec_triggers[r_last].next = ExecNext::ExecItem(else_begin);
    }
    finish_restore(exec_triggers, s_placement, first_default, join_idx);
    if has_else && exec_triggers.len() > else_begin {
        let last_else_idx = exec_triggers.len() - 1;
        // Preserve a trailing while-loop's loop-back edge, a call's next edge,
        // or an explicit raw trigger next. Only fall-through else triggers
        // move to the join.
        if matches!(
            exec_triggers[last_else_idx].next,
            ExecNext::DefaultChain
        ) {
            exec_triggers[last_else_idx].next = ExecNext::ExecItem(join_idx);
        }
    }

    Ok(())
}

pub(crate) fn lower_once<F>(
    once: &HirOnce,
    exec_triggers: &mut Vec<ExecTrigger>,
    mut lower_sub_stmts: F,
) -> Result<(), LowerError>
where
    F: FnMut(&[HirStmt], &mut Vec<ExecTrigger>) -> Result<(), LowerError>,
{
    if chunk_count(&once.conds) > 1 {
        return lower_once_chunked(once, exec_triggers, &mut lower_sub_stmts);
    }

    let gate_idx = exec_triggers.len();
    let body_start_idx = gate_idx + 1;

    let gate_act = make_exec_set_next_ptr_act(
        gate_idx,
        ExecRelocTarget::ExecItem {
            exec_idx: body_start_idx,
            offset: 0,
        },
    );

    exec_triggers.push(ExecTrigger {
        hir_id: None,
        conds: once.conds.clone(),
        acts: vec![gate_act],
        preserved: false,
        flags: 0,
        prev: None,
        next: ExecNext::DefaultChain, // patched to join_idx below
    });

    let body_begin = exec_triggers.len();
    lower_sub_stmts(&once.body, exec_triggers)?;
    let body_end = exec_triggers.len();

    // Path-complete restore: the gate is self-disabling (preserved: false)
    // and its act rewrites its own next pointer to the body start. Without
    // a restore, later loops walk through the disabled gate via the stale
    // pointer and re-enter the body every loop. Mirror lower_when and
    // lower_while: append the restore to the last body trigger when it
    // can accept one, else push a separate unconditional preserved
    // restore trigger. Empty body: the gate act already targets the join,
    // so there is no stale pointer to restore.
    let join_idx;
    if body_end > body_begin {
        let restore_act = make_exec_set_next_ptr_act(
            gate_idx,
            ExecRelocTarget::ExecItem {
                exec_idx: 0, // patched to join_idx below
                offset: 0,
            },
        );

        if can_append_restore(&exec_triggers[body_end - 1], 1) {
            exec_triggers[body_end - 1].acts.push(restore_act);
        } else {
            exec_triggers.push(ExecTrigger {
                hir_id: None,
                conds: Vec::new(),
                acts: vec![restore_act],
                preserved: true,
                flags: 4,
                prev: None,
                next: ExecNext::DefaultChain,
            });
        }
        let body_epilogue_idx = exec_triggers.len() - 1;
        join_idx = exec_triggers.len();

        let restore_act_idx = exec_triggers[body_epilogue_idx].acts.len() - 1;
        patch_set_next_target(
            &mut exec_triggers[body_epilogue_idx].acts[restore_act_idx],
            join_idx,
        );

        exec_triggers[body_epilogue_idx].next = ExecNext::ExecItem(join_idx);
    } else {
        join_idx = exec_triggers.len();
    }
    exec_triggers[gate_idx].next = ExecNext::ExecItem(join_idx);

    Ok(())
}

/// `>16`-condition `once`: split the gate into `k` chunks.
///
/// The last chunk gate is the only self-disabling one (`preserved: false`,
/// `flags: 0`); it fires exactly once after every earlier chunk passed. The
/// success restore `S` sits after the body and is also the failure target of
/// later chunks, so it is always a separate trigger. `S` rewrites every gate's
/// `next` pointer, including the disabled last gate's, before the join; it
/// does not re-enable `preserved`, so the body stays off while the following
/// chain keeps running once per pass.
fn lower_once_chunked<F>(
    once: &HirOnce,
    exec_triggers: &mut Vec<ExecTrigger>,
    lower_sub_stmts: &mut F,
) -> Result<(), LowerError>
where
    F: FnMut(&[HirStmt], &mut Vec<ExecTrigger>) -> Result<(), LowerError>,
{
    let k = chunk_count(&once.conds);
    let gate_start = exec_triggers.len();
    let body_start = gate_start + k;

    debug_assert_eq!(
        push_chunk_gates(exec_triggers, &once.conds, body_start, false),
        k
    );

    lower_sub_stmts(&once.body, exec_triggers)?;

    // The success restore is the shared success/failure target, so it is a
    // standalone trigger even when the body is empty (empty body: the last
    // gate targets this trigger, which then joins).
    let s_start = exec_triggers.len();
    let (_, s_last) = push_gate_restore_block(exec_triggers, gate_start, k, s_start);
    let join_idx = exec_triggers.len();

    patch_chunk_gate_defaults(exec_triggers, gate_start, k, join_idx, s_start);
    finish_restore(
        exec_triggers,
        RestorePlacement::Block {
            start: s_start,
            last: s_last,
        },
        join_idx,
        join_idx,
    );

    Ok(())
}

pub(crate) fn lower_while<F>(
    wh: &HirWhile,
    exec_triggers: &mut Vec<ExecTrigger>,
    mut lower_sub_stmts: F,
) -> Result<(), LowerError>
where
    F: FnMut(&[HirStmt], &mut Vec<ExecTrigger>) -> Result<(), LowerError>,
{
    if chunk_count(&wh.conds) > 1 {
        return lower_while_chunked(wh, exec_triggers, &mut lower_sub_stmts);
    }

    let check_idx = exec_triggers.len();
    let body_start_idx = check_idx + 1;

    let gate_act = make_exec_set_next_ptr_act(
        check_idx,
        ExecRelocTarget::ExecItem {
            exec_idx: body_start_idx,
            offset: 0,
        },
    );

    exec_triggers.push(ExecTrigger {
        hir_id: None,
        conds: wh.conds.clone(),
        acts: vec![gate_act],
        preserved: true,
        flags: 4,
        prev: None,
        next: ExecNext::DefaultChain,
    });

    let body_begin = exec_triggers.len();
    lower_sub_stmts(&wh.body, exec_triggers)?;
    let body_end = exec_triggers.len();

    let restore_act = make_exec_set_next_ptr_act(
        check_idx,
        ExecRelocTarget::ExecItem {
            exec_idx: 0, // patched to join_idx below
            offset: 0,
        },
    );

    if body_end > body_begin && can_append_restore(&exec_triggers[body_end - 1], 1) {
        exec_triggers[body_end - 1].acts.push(restore_act);
    } else {
        exec_triggers.push(ExecTrigger {
            hir_id: None,
            conds: Vec::new(),
            acts: vec![restore_act],
            preserved: true,
            flags: 4,
            prev: None,
            next: ExecNext::DefaultChain,
        });
    }
    let body_epilogue_idx = exec_triggers.len() - 1;
    let join_idx = exec_triggers.len();

    let restore_act_idx = exec_triggers[body_epilogue_idx].acts.len() - 1;
    patch_set_next_target(
        &mut exec_triggers[body_epilogue_idx].acts[restore_act_idx],
        join_idx,
    );

    exec_triggers[body_epilogue_idx].next = ExecNext::ExecItem(check_idx);
    exec_triggers[check_idx].next = ExecNext::ExecItem(join_idx);

    Ok(())
}

/// `>16`-condition `while`: split the check into `k` chunks.
///
/// Topology:
///
/// ```text
/// g0 -> g1 -> ... -> g(k-1) -> body -> E -> g0
///                  \-> R -> join
/// ```
///
/// `R` sits before the body and is the failure target of later chunks; it
/// restores every gate and exits to the join. `E` sits after the body and
/// restores every gate before looping back to `g0`; it may ride on an
/// unconditional last body trigger. Gate 0's own static default is the join,
/// so a false first chunk exits before evaluating the rest.
fn lower_while_chunked<F>(
    wh: &HirWhile,
    exec_triggers: &mut Vec<ExecTrigger>,
    lower_sub_stmts: &mut F,
) -> Result<(), LowerError>
where
    F: FnMut(&[HirStmt], &mut Vec<ExecTrigger>) -> Result<(), LowerError>,
{
    let k = chunk_count(&wh.conds);
    let gate_start = exec_triggers.len();
    let r_start = gate_start + k;
    let restore_blocks = restore_trigger_count(k);
    let body_start = r_start + restore_blocks;

    debug_assert_eq!(
        push_chunk_gates(exec_triggers, &wh.conds, body_start, true),
        k
    );

    let (_, r_last) = push_gate_restore_block(exec_triggers, gate_start, k, r_start);
    debug_assert_eq!(exec_triggers.len(), body_start);

    let body_begin = exec_triggers.len();
    lower_sub_stmts(&wh.body, exec_triggers)?;
    let body_end = exec_triggers.len();

    // Success restore / loop-back. It may append to an unconditional last
    // body trigger; otherwise it is a separate trigger after the body.
    let e_can_append =
        body_end > body_begin && can_append_restore(&exec_triggers[body_end - 1], k);
    let e_placement = append_or_push_restore(exec_triggers, gate_start, k, r_start, e_can_append);

    let join_idx = exec_triggers.len();
    patch_chunk_gate_defaults(exec_triggers, gate_start, k, join_idx, r_start);
    patch_first_restore_target(exec_triggers, r_start, join_idx);
    exec_triggers[r_last].next = ExecNext::ExecItem(join_idx);
    finish_restore(exec_triggers, e_placement, join_idx, gate_start);

    Ok(())
}
#[cfg(test)]
mod tests {
    use super::*;
    use eudkit_hir::{HirCond, HirOnce, HirWhen, HirWhile};
    use eudkit_lir::CTrigAction;

    fn dummy_act() -> ExecAct {
        ExecAct::from_ctrig(CTrigAction::set_deaths(0, 7, 0, 0))
    }

    fn make_trigger(
        conds: Vec<HirCond>,
        preserved: bool,
        flags: u32,
        next: ExecNext,
        act_count: usize,
    ) -> ExecTrigger {
        ExecTrigger {
            hir_id: None,
            conds,
            acts: (0..act_count).map(|_| dummy_act()).collect(),
            preserved,
            flags,
            prev: None,
            next,
        }
    }

    fn unconditional() -> ExecTrigger {
        make_trigger(Vec::new(), true, 4, ExecNext::DefaultChain, 1)
    }

    fn next_item(trigger: &ExecTrigger) -> Option<usize> {
        match trigger.next {
            ExecNext::ExecItem(idx) => Some(idx),
            _ => None,
        }
    }

    fn act_target(act: &ExecAct, reloc_idx: usize) -> Option<usize> {
        match act.relocs.get(reloc_idx)?.target {
            ExecRelocTarget::ExecItem { exec_idx, .. } => Some(exec_idx),
            _ => None,
        }
    }

    fn gate_act_target(trigger: &ExecTrigger) -> Option<usize> {
        act_target(&trigger.acts[0], 1)
    }

    fn has_restore_act(trigger: &ExecTrigger, gate_idx: usize, target: usize) -> bool {
        trigger.acts.iter().any(|act| {
            act.relocs.len() == 2
                && matches!(
                    act.relocs[0].target,
                    ExecRelocTarget::ExecItem {
                        exec_idx,
                        offset,
                    } if exec_idx == gate_idx && offset == NEXT_BYTE_OFFSET
                )
                && act_target(act, 1) == Some(target)
        })
    }

    fn cond() -> Vec<HirCond> {
        vec![HirCond::deaths(0, 10, 1, 0)]
    }

    #[test]
    fn when_appends_restore_to_unconditional_last_trigger() {
        let mut out = Vec::new();
        lower_when(
            &HirWhen::new(cond(), Vec::new()),
            &mut out,
            |_, o| {
                o.push(unconditional());
                Ok(())
            },
        )
        .unwrap();
        // Gate + body trigger (restore appended), join is virtual index 2.
        assert_eq!(out.len(), 2);
        assert_eq!(gate_act_target(&out[0]), Some(1));
        assert_eq!(next_item(&out[0]), Some(2));
        assert_eq!(out[1].acts.len(), 2);
        assert!(has_restore_act(&out[1], 0, 2));
        assert_eq!(next_item(&out[1]), Some(2));
    }

    #[test]
    fn when_pushes_separate_restore_for_conditional_last_trigger() {
        let mut out = Vec::new();
        lower_when(
            &HirWhen::new(cond(), Vec::new()),
            &mut out,
            |_, o| {
                o.push(unconditional());
                o.push(make_trigger(
                    cond(),
                    true,
                    4,
                    ExecNext::ExecItem(999),
                    1,
                ));
                Ok(())
            },
        )
        .unwrap();
        // Gate + unconditional + conditional + separate restore; join = 4.
        assert_eq!(out.len(), 4);
        assert_eq!(gate_act_target(&out[0]), Some(1));
        assert_eq!(next_item(&out[0]), Some(4));
        // Conditional trigger is untouched: no restore appended, next kept.
        assert_eq!(out[2].acts.len(), 1);
        assert_eq!(next_item(&out[2]), Some(999));
        // Separate restore trigger, unconditional, next = join.
        assert_eq!(out[3].conds.len(), 0);
        assert!(out[3].preserved);
        assert!(has_restore_act(&out[3], 0, 4));
        assert_eq!(next_item(&out[3]), Some(4));
    }

    #[test]
    fn when_pushes_separate_restore_for_preserved_false_last_trigger() {
        let mut out = Vec::new();
        lower_when(
            &HirWhen::new(cond(), Vec::new()),
            &mut out,
            |_, o| {
                o.push(make_trigger(
                    Vec::new(),
                    false,
                    0,
                    ExecNext::DefaultChain,
                    1,
                ));
                Ok(())
            },
        )
        .unwrap();
        assert_eq!(out.len(), 3);
        assert_eq!(out[1].acts.len(), 1);
        assert!(matches!(out[1].next, ExecNext::DefaultChain));
        assert!(has_restore_act(&out[2], 0, 3));
        assert_eq!(next_item(&out[2]), Some(3));
        assert_eq!(next_item(&out[0]), Some(3));
    }

    #[test]
    fn when_pushes_separate_restore_for_epilogue_next_last_trigger() {
        let mut out = Vec::new();
        lower_when(
            &HirWhen::new(cond(), Vec::new()),
            &mut out,
            |_, o| {
                o.push(make_trigger(
                    Vec::new(),
                    true,
                    4,
                    ExecNext::ExecItem(999),
                    1,
                ));
                Ok(())
            },
        )
        .unwrap();
        // A last trigger whose next is already assigned (an inner epilogue)
        // must not accept an appended restore; its next is preserved.
        assert_eq!(out.len(), 3);
        assert_eq!(out[1].acts.len(), 1);
        assert_eq!(next_item(&out[1]), Some(999));
        assert!(has_restore_act(&out[2], 0, 3));
        assert_eq!(next_item(&out[2]), Some(3));
    }

    #[test]
    fn when_pushes_separate_restore_for_full_last_trigger() {
        let mut out = Vec::new();
        lower_when(
            &HirWhen::new(cond(), Vec::new()),
            &mut out,
            |_, o| {
                o.push(make_trigger(
                    Vec::new(),
                    true,
                    4,
                    ExecNext::DefaultChain,
                    ACT_COUNT,
                ));
                Ok(())
            },
        )
        .unwrap();
        assert_eq!(out.len(), 3);
        assert_eq!(out[1].acts.len(), ACT_COUNT);
        assert!(has_restore_act(&out[2], 0, 3));
        assert_eq!(next_item(&out[2]), Some(3));
    }

    #[test]
    fn when_empty_body_pushes_separate_restore() {
        let mut out = Vec::new();
        lower_when(&HirWhen::new(cond(), Vec::new()), &mut out, |_, _| Ok(())).unwrap();
        assert_eq!(out.len(), 2);
        assert!(has_restore_act(&out[1], 0, 2));
        assert_eq!(next_item(&out[0]), Some(2));
        assert_eq!(next_item(&out[1]), Some(2));
    }

    #[test]
    fn when_else_branch_restore_targets_else_and_join() {
        use eudkit_hir::{HirAct, HirStmt};
        let mut out = Vec::new();
        // The body closure is invoked once for the body and once for the
        // else body; it pushes one unconditional trigger per invocation.
        // The statement lists themselves are only used by lower_when to
        // decide has_else, so a single dummy act per list suffices.
        let stmt = HirStmt::Act(HirAct::set_deaths(0, 7, 1, 0));
        lower_when(
            &HirWhen::with_else(cond(), vec![stmt.clone()], vec![stmt]),
            &mut out,
            |_, o| {
                o.push(unconditional());
                Ok(())
            },
        )
        .unwrap();
        // Gate 0, body trigger (restore appended) 1, else trigger 2,
        // join 3. Gate default = else, restore target = else, body
        // epilogue next = join, else last next = join.
        assert_eq!(out.len(), 3);
        assert_eq!(next_item(&out[0]), Some(2));
        assert!(has_restore_act(&out[1], 0, 2));
        assert_eq!(next_item(&out[1]), Some(3));
        assert_eq!(next_item(&out[2]), Some(3));
    }

    #[test]
    fn when_nested_when_as_last_pushes_separate_restore() {
        let mut out = Vec::new();
        lower_when(
            &HirWhen::new(cond(), Vec::new()),
            &mut out,
            |_, o| {
                lower_when(
                    &HirWhen::new(cond(), Vec::new()),
                    o,
                    |_, o2| {
                        o2.push(unconditional());
                        Ok(())
                    },
                )?;
                Ok(())
            },
        )
        .unwrap();
        // Outer gate 0, inner gate 1, inner body (restore appended) 2,
        // outer separate restore 3, outer join 4.
        assert_eq!(out.len(), 4);
        assert_eq!(gate_act_target(&out[0]), Some(1));
        assert_eq!(next_item(&out[0]), Some(4));
        assert_eq!(gate_act_target(&out[1]), Some(2));
        // Inner gate's default target is the outer restore trigger.
        assert_eq!(next_item(&out[1]), Some(3));
        // Inner epilogue keeps its own join (the outer restore index).
        assert_eq!(next_item(&out[2]), Some(3));
        assert!(has_restore_act(&out[2], 1, 3));
        // Outer restore: unconditional, targets outer join, next = join.
        assert_eq!(out[3].conds.len(), 0);
        assert!(has_restore_act(&out[3], 0, 4));
        assert_eq!(next_item(&out[3]), Some(4));
    }

    #[test]
    fn while_appends_restore_to_unconditional_last_trigger() {
        let mut out = Vec::new();
        lower_while(
            &HirWhile::new(cond(), Vec::new()),
            &mut out,
            |_, o| {
                o.push(unconditional());
                Ok(())
            },
        )
        .unwrap();
        // Check 0, body trigger (restore appended) 1, join 2.
        assert_eq!(out.len(), 2);
        assert_eq!(gate_act_target(&out[0]), Some(1));
        assert_eq!(next_item(&out[0]), Some(2));
        assert!(has_restore_act(&out[1], 0, 2));
        // Body epilogue loops back to the check.
        assert_eq!(next_item(&out[1]), Some(0));
    }

    #[test]
    fn while_empty_body_pushes_separate_restore() {
        let mut out = Vec::new();
        lower_while(&HirWhile::new(cond(), Vec::new()), &mut out, |_, _| Ok(())).unwrap();
        assert_eq!(out.len(), 2);
        assert!(has_restore_act(&out[1], 0, 2));
        assert_eq!(next_item(&out[0]), Some(2));
        assert_eq!(next_item(&out[1]), Some(0));
    }

    #[test]
    fn while_with_inner_when_as_last_pushes_separate_restore() {
        let mut out = Vec::new();
        lower_while(
            &HirWhile::new(cond(), Vec::new()),
            &mut out,
            |_, o| {
                lower_when(
                    &HirWhen::new(cond(), Vec::new()),
                    o,
                    |_, o2| {
                        o2.push(unconditional());
                        Ok(())
                    },
                )?;
                Ok(())
            },
        )
        .unwrap();
        // Check 0, inner gate 1, inner body (restore appended) 2,
        // while separate restore 3, while join 4.
        assert_eq!(out.len(), 4);
        assert_eq!(gate_act_target(&out[0]), Some(1));
        assert_eq!(next_item(&out[0]), Some(4));
        assert_eq!(next_item(&out[1]), Some(3));
        assert_eq!(next_item(&out[2]), Some(3));
        assert!(has_restore_act(&out[2], 1, 3));
        assert!(has_restore_act(&out[3], 0, 4));
        assert_eq!(next_item(&out[3]), Some(0));
    }

    #[test]
    fn once_appends_restore_to_unconditional_last_trigger() {
        let mut out = Vec::new();
        lower_once(
            &HirOnce::new(cond(), Vec::new()),
            &mut out,
            |_, o| {
                o.push(unconditional());
                Ok(())
            },
        )
        .unwrap();
        // Gate 0 (preserved false), body 1 (restore appended), join 2.
        // The restore returns the self-disabling gate's next pointer to
        // the join, so later loops skip the body instead of re-entering it.
        assert_eq!(out.len(), 2);
        assert!(!out[0].preserved);
        assert_eq!(gate_act_target(&out[0]), Some(1));
        assert_eq!(next_item(&out[0]), Some(2));
        assert_eq!(out[1].acts.len(), 2);
        assert!(has_restore_act(&out[1], 0, 2));
        assert_eq!(next_item(&out[1]), Some(2));
    }

    #[test]
    fn once_with_inner_when_as_last_pushes_separate_restore() {
        let mut out = Vec::new();
        lower_once(
            &HirOnce::new(cond(), Vec::new()),
            &mut out,
            |_, o| {
                lower_when(
                    &HirWhen::new(cond(), Vec::new()),
                    o,
                    |_, o2| {
                        o2.push(unconditional());
                        Ok(())
                    },
                )?;
                Ok(())
            },
        )
        .unwrap();
        // Once gate 0, inner gate 1, inner body (restore appended) 2,
        // outer separate restore 3, once join 4. The inner body's next
        // is already assigned (inner join 3), so the outer restore cannot
        // append to it.
        assert_eq!(out.len(), 4);
        assert!(!out[0].preserved);
        assert_eq!(gate_act_target(&out[0]), Some(1));
        assert_eq!(next_item(&out[0]), Some(4));
        assert_eq!(next_item(&out[1]), Some(3));
        assert_eq!(next_item(&out[2]), Some(3));
        assert!(has_restore_act(&out[2], 1, 3));
        // Separate outer restore trigger, unconditional, next = join.
        assert_eq!(out[3].conds.len(), 0);
        assert!(out[3].preserved);
        assert!(has_restore_act(&out[3], 0, 4));
        assert_eq!(next_item(&out[3]), Some(4));
    }

    #[test]
    fn once_empty_body_skips_restore() {
        let mut out = Vec::new();
        lower_once(&HirOnce::new(cond(), Vec::new()), &mut out, |_, _| Ok(())).unwrap();
        // Gate 0 only; the gate act already targets the join (index 1),
        // so there is no stale pointer to restore.
        assert_eq!(out.len(), 1);
        assert!(!out[0].preserved);
        assert_eq!(gate_act_target(&out[0]), Some(1));
        assert_eq!(next_item(&out[0]), Some(1));
    }

    #[test]
    fn when_nested_while_as_last_pushes_separate_restore_and_preserves_loopback() {
        let mut out = Vec::new();
        lower_when(
            &HirWhen::new(cond(), Vec::new()),
            &mut out,
            |_, o| {
                lower_while(
                    &HirWhile::new(cond(), Vec::new()),
                    o,
                    |_, o2| {
                        o2.push(unconditional());
                        Ok(())
                    },
                )?;
                Ok(())
            },
        )
        .unwrap();
        // Outer gate 0, inner check 1, inner body (restore appended) 2,
        // outer separate restore 3, outer join 4.
        assert_eq!(out.len(), 4);
        assert_eq!(next_item(&out[0]), Some(4));
        assert_eq!(next_item(&out[1]), Some(3));
        // Inner loopback is preserved: body epilogue still targets check.
        assert_eq!(next_item(&out[2]), Some(1));
        assert!(has_restore_act(&out[2], 1, 3));
        assert!(has_restore_act(&out[3], 0, 4));
        assert_eq!(next_item(&out[3]), Some(4));
    }

    // ---- ADR 0027: >16-condition auto-chunking ---------------------------
    //
    // Structured forms must split their condition list into `Trigger (16C)`
    // chunks. A raw `Trigger` stays capped at 16 (`EUDSL-BOUNDS`); only the
    // lowering desugar chunks `when`/`once`/`while` (and, after opt desugar,
    // `until` / `onRising` / `onFalling`).
    //
    // The model below mirrors the game's trigger walk: a trigger's `next`
    // pointer is followed whether its conditions pass or fail; actions run
    // only when the conditions pass; a non-preserved trigger disables itself
    // after it executes. That is what makes a stale gate `next` observable:
    // on a later frame the gate is false, but its pointer still jumps into
    // the next chunk.

    use eudkit_hir::HirAct;
    use eudkit_lir::COND_COUNT;

    const BODY_MARK: u32 = 0xB0D1;
    const ELSE_MARK: u32 = 0xE15E;
    const AFTER_MARK: u32 = 0xA47E;

    /// Distinct conditions, keyed by `amount` (1-based) in the simulator.
    /// `unit` is varied too so the opt condition-fusion pass (which matches
    /// on unit) can never collapse them into fewer than `n` conditions.
    fn chunk_conds(n: usize) -> Vec<HirCond> {
        (0..n)
            .map(|i| HirCond::deaths(0, 10, (i + 1) as u32, i as u16))
            .collect()
    }

    fn marker(value: u32) -> ExecTrigger {
        ExecTrigger {
            hir_id: None,
            conds: Vec::new(),
            acts: vec![ExecAct::from_ctrig(CTrigAction::set_deaths(0, 7, value, 0))],
            preserved: true,
            flags: 4,
            prev: None,
            next: ExecNext::DefaultChain,
        }
    }

    fn marker_index(out: &[ExecTrigger], value: u32) -> usize {
        out.iter()
            .position(|t| {
                t.acts
                    .iter()
                    .any(|a| a.relocs.is_empty() && a.ctrig.value == value)
            })
            .unwrap_or_else(|| panic!("marker 0x{value:X} missing from lowered triggers"))
    }

    fn assert_cond_chunks(out: &[ExecTrigger], n: usize) {
        for (idx, trig) in out.iter().enumerate() {
            assert!(
                trig.conds.len() <= COND_COUNT,
                "trigger {idx} carries {} conditions; ADR 0027 requires <= {COND_COUNT} (n={n})",
                trig.conds.len()
            );
        }
        let total: usize = out.iter().map(|t| t.conds.len()).sum();
        assert!(
            total >= n,
            "all {n} conditions must stay represented across chunks; found {total}"
        );
    }

    fn all_true(n: usize) -> Vec<bool> {
        vec![true; n]
    }

    /// The static `next` target of an `ExecTrigger`, as a simulator index.
    fn static_next(triggers: &[ExecTrigger], idx: usize) -> Option<usize> {
        match &triggers[idx].next {
            ExecNext::ExecItem(target) => (*target < triggers.len()).then_some(*target),
            ExecNext::DefaultChain => (idx + 1 < triggers.len()).then_some(idx + 1),
            ExecNext::ChainEnd => None,
            other => panic!("chunk simulator does not model static next {other:?}"),
        }
    }

    /// The `(gate, target)` pair written by a `SetNextPtr` action.
    fn set_next_patch(act: &ExecAct) -> Option<(usize, usize)> {
        if act.relocs.len() != 2 {
            return None;
        }
        let gate = match act.relocs[0].target {
            ExecRelocTarget::ExecItem { exec_idx, offset } if offset == NEXT_BYTE_OFFSET => {
                exec_idx
            }
            _ => return None,
        };
        let target = match act.relocs[1].target {
            ExecRelocTarget::ExecItem {
                exec_idx,
                offset: 0,
            } => exec_idx,
            ExecRelocTarget::ChainEnd => usize::MAX,
            _ => return None,
        };
        Some((gate, target))
    }

    /// A tiny trigger-loop interpreter over `ExecTrigger`s.
    ///
    /// State that must persist across frames (patched `next` pointers and the
    /// enabled/disabled bit) lives in the simulator; conditions are abstract
    /// booleans keyed by `HirCond::amount` (1-based, i.e. `trunk_conds`).
    struct ChunkSim<'a> {
        triggers: &'a [ExecTrigger],
        next: Vec<Option<usize>>,
        enabled: Vec<bool>,
        truth: Vec<bool>,
    }

    impl<'a> ChunkSim<'a> {
        fn new(triggers: &'a [ExecTrigger]) -> Self {
            let next = (0..triggers.len())
                .map(|idx| static_next(triggers, idx))
                .collect();
            Self {
                triggers,
                next,
                enabled: vec![true; triggers.len()],
                truth: Vec::new(),
            }
        }

        fn set_truth(&mut self, truth: &[bool]) {
            self.truth = truth.to_vec();
        }

        /// Walk one trigger loop from the chain entry.
        ///
        /// `on_pass(idx, truth)` runs when trigger `idx` passes its
        /// conditions, before its actions are applied. Tests use it to model
        /// a body act that changes the condition inputs for the next
        /// intra-frame while iteration.
        fn frame<F>(&mut self, mut on_pass: F) -> Vec<usize>
        where
            F: FnMut(usize, &mut Vec<bool>),
        {
            let mut visited = Vec::new();
            let mut idx = 0usize;
            let mut steps = 0usize;
            while idx < self.triggers.len() {
                steps += 1;
                assert!(steps <= 100_000, "chunk simulator did not terminate");
                visited.push(idx);

                let trig = &self.triggers[idx];
                if self.enabled[idx] {
                    let passes = trig.conds.iter().all(|c| {
                        let amount = c.amount as usize;
                        amount >= 1 && amount <= self.truth.len() && self.truth[amount - 1]
                    });
                    if passes {
                        on_pass(idx, &mut self.truth);
                        let patches: Vec<(usize, usize)> =
                            trig.acts.iter().filter_map(set_next_patch).collect();
                        for (gate, target) in patches {
                            self.next[gate] = (target < self.triggers.len()).then_some(target);
                        }
                        if !trig.preserved {
                            self.enabled[idx] = false;
                        }
                    }
                }

                idx = match self.next[idx] {
                    Some(next) => next,
                    None => break,
                };
            }
            visited
        }
    }

    fn count_visits(visited: &[usize], marker: usize) -> usize {
        visited.iter().filter(|&&idx| idx == marker).count()
    }

    #[derive(Debug, Clone, Copy)]
    enum ChunkForm {
        When,
        Once,
        While,
    }

    fn body_marker(_: &[HirStmt], out: &mut Vec<ExecTrigger>) -> Result<(), LowerError> {
        out.push(marker(BODY_MARK));
        Ok(())
    }

    fn lower_chunk_form(form: ChunkForm, n: usize) -> Vec<ExecTrigger> {
        let mut out = Vec::new();
        match form {
            ChunkForm::When => lower_when(
                &HirWhen::new(chunk_conds(n), Vec::new()),
                &mut out,
                body_marker,
            ),
            ChunkForm::Once => lower_once(
                &HirOnce::new(chunk_conds(n), Vec::new()),
                &mut out,
                body_marker,
            ),
            ChunkForm::While => lower_while(
                &HirWhile::new(chunk_conds(n), Vec::new()),
                &mut out,
                body_marker,
            ),
        }
        .unwrap();
        out
    }

    /// Lower nested test statements so `when`/`while` in an else arm behave
    /// exactly like they do in a real program.
    fn lower_test_stmts(stmts: &[HirStmt], out: &mut Vec<ExecTrigger>) -> Result<(), LowerError> {
        for stmt in stmts {
            match stmt {
                HirStmt::Act(act) => out.push(marker(act.value)),
                HirStmt::When(w) => lower_when(w, out, lower_test_stmts)?,
                HirStmt::Once(o) => lower_once(o, out, lower_test_stmts)?,
                HirStmt::While(w) => lower_while(w, out, lower_test_stmts)?,
                _ => unreachable!("test lowerer only handles act/when/once/while"),
            }
        }
        Ok(())
    }

    /// `(gate, target)` pairs carried by a restore block, in act order.
    fn restore_acts(out: &[ExecTrigger], start: usize, blocks: usize) -> Vec<(usize, usize)> {
        let mut acts = Vec::new();
        for block in 0..blocks {
            for act in &out[start + block].acts {
                assert_eq!(
                    act.relocs.len(),
                    2,
                    "restore act must carry gate + target relocs"
                );
                let gate = match act.relocs[0].target {
                    ExecRelocTarget::ExecItem {
                        exec_idx,
                        offset: NEXT_BYTE_OFFSET,
                    } => exec_idx,
                    ref other => panic!("unexpected restore gate reloc: {other:?}"),
                };
                let target = act_target(act, 1).expect("restore act must target an ExecItem");
                acts.push((gate, target));
            }
        }
        acts
    }

    struct RestoreExpect {
        first_target: usize,
        nonfirst_target: usize,
        last_next: usize,
    }

    fn assert_restore_block(
        out: &[ExecTrigger],
        start: usize,
        blocks: usize,
        k: usize,
        expect: RestoreExpect,
        label: &str,
    ) {
        let acts = restore_acts(out, start, blocks);
        assert_eq!(acts.len(), k, "{label}: one restore act per gate");
        let mut gates: Vec<usize> = acts.iter().map(|(gate, _)| *gate).collect();
        gates.sort_unstable();
        assert_eq!(
            gates,
            (0..k).collect::<Vec<_>>(),
            "{label}: every mutated gate must be reset by the same restore path (I3)"
        );
        for (gate, target) in acts {
            let expected = if gate == 0 {
                expect.first_target
            } else {
                expect.nonfirst_target
            };
            assert_eq!(
                target, expected,
                "{label}: gate {gate} restore target must match the static default"
            );
        }
        for block in 0..blocks {
            let remaining = k.saturating_sub(block * ACT_COUNT);
            assert_eq!(
                out[start + block].acts.len(),
                ACT_COUNT.min(remaining),
                "{label}: restore block {block} act count"
            );
            if block + 1 == blocks {
                assert_eq!(
                    next_item(&out[start + block]),
                    Some(expect.last_next),
                    "{label}: last restore trigger must exit to its target"
                );
            } else {
                assert!(
                    matches!(out[start + block].next, ExecNext::DefaultChain),
                    "{label}: restore block {block} must chain with DefaultChain"
                );
            }
        }
    }

    fn assert_gate_defaults(out: &[ExecTrigger], sim: &ChunkSim, k: usize) {
        for (gate, actual) in sim.next.iter().enumerate().take(k) {
            assert_eq!(
                *actual,
                static_next(out, gate),
                "gate {gate} must be restored to its default at pass end (I1)"
            );
        }
    }

    #[test]
    fn when_else_while_loop_keeps_its_loop_back_and_iterates_completely() {
        for n in [1usize, 17, 33] {
            let loop_amount = (n + 1) as u32;
            let then_stmt = HirStmt::Act(HirAct::set_deaths(0, 7, BODY_MARK, 0));
            let else_stmt = HirStmt::Act(HirAct::set_deaths(0, 7, ELSE_MARK, 0));
            let else_while = HirStmt::While(HirWhile::new(
                vec![HirCond::deaths(0, 10, loop_amount, 0)],
                vec![else_stmt],
            ));
            let mut out = Vec::new();
            lower_when(
                &HirWhen::with_else(chunk_conds(n), vec![then_stmt], vec![else_while]),
                &mut out,
                lower_test_stmts,
            )
            .unwrap();
            assert_cond_chunks(&out, n);

            let then_body = marker_index(&out, BODY_MARK);
            let else_body = marker_index(&out, ELSE_MARK);
            let mut sim = ChunkSim::new(&out);
            let mut truth = all_true(n);
            truth[0] = false; // take the else arm
            truth.resize(n + 1, false);
            truth[n] = true; // else-while condition
            sim.set_truth(&truth);

            let mut iterations = 0usize;
            let visited = sim.frame(|idx, truth| {
                if idx == else_body {
                    iterations += 1;
                    truth[n] = iterations < 3;
                }
            });
            assert_eq!(
                count_visits(&visited, then_body),
                0,
                "n={n}: the false when gate must not run the then body"
            );
            assert_eq!(
                count_visits(&visited, else_body),
                3,
                "n={n}: when(else: while) must iterate the else-while loop completely, not jump to the join after one pass"
            );
        }
    }

    #[test]
    fn k_above_64_chains_restores_and_keeps_i1_i2_i3() {
        for n in [1025usize, 1056] {
            let k = n.div_ceil(COND_COUNT);
            let blocks = restore_trigger_count(k);
            assert!(k > ACT_COUNT, "n={n} must cross the 64-act restore split");
            assert!(blocks >= 2);

            for form in [ChunkForm::When, ChunkForm::Once, ChunkForm::While] {
                let out = lower_chunk_form(form, n);
                assert_cond_chunks(&out, n);
                let body = marker_index(&out, BODY_MARK);
                let (restore_start, e_start, join) = match form {
                    ChunkForm::When | ChunkForm::Once => {
                        let s_start = k + 1;
                        assert_eq!(body, k, "n={n} {form:?}: body follows the gates");
                        (s_start, None, s_start + blocks)
                    }
                    ChunkForm::While => {
                        let r_start = k;
                        let body_start = r_start + blocks;
                        assert_eq!(body, body_start, "n={n} {form:?}: body follows R");
                        let e_start = body + 1;
                        (r_start, Some(e_start), e_start + blocks)
                    }
                };

                for (gate, trigger) in out.iter().take(k).enumerate() {
                    let expected_conds = COND_COUNT.min(n - gate * COND_COUNT);
                    assert_eq!(
                        trigger.conds.len(),
                        expected_conds,
                        "n={n} {form:?}: gate {gate} chunk width"
                    );
                    assert!(
                        !matches!(trigger.next, ExecNext::DefaultChain),
                        "n={n} {form:?}: gate {gate} must use an explicit ExecItem edge"
                    );
                    let expected_default = if gate == 0 { join } else { restore_start };
                    assert_eq!(
                        next_item(trigger),
                        Some(expected_default),
                        "n={n} {form:?}: gate {gate} static default"
                    );
                    assert_eq!(trigger.acts[0].relocs.len(), 2);
                    let expected_act = if gate + 1 == k { body } else { gate + 1 };
                    assert_eq!(
                        gate_act_target(trigger),
                        Some(expected_act),
                        "n={n} {form:?}: gate {gate} act target"
                    );
                }

                match form {
                    ChunkForm::When | ChunkForm::Once => {
                        assert_restore_block(
                            &out,
                            restore_start,
                            blocks,
                            k,
                            RestoreExpect {
                                first_target: join,
                                nonfirst_target: restore_start,
                                last_next: join,
                            },
                            &format!("n={n} {form:?} S"),
                        );
                    }
                    ChunkForm::While => {
                        assert_restore_block(
                            &out,
                            restore_start,
                            blocks,
                            k,
                            RestoreExpect {
                                first_target: join,
                                nonfirst_target: restore_start,
                                last_next: join,
                            },
                            &format!("n={n} {form:?} R"),
                        );
                        assert_restore_block(
                            &out,
                            e_start.unwrap(),
                            blocks,
                            k,
                            RestoreExpect {
                                first_target: join,
                                nonfirst_target: restore_start,
                                last_next: 0,
                            },
                            &format!("n={n} {form:?} E"),
                        );
                    }
                }

                let mut sim = ChunkSim::new(&out);

                // I2 / 1T: a false first chunk is the only trigger visited.
                sim.set_truth(&vec![false; n]);
                let visited = sim.frame(|_, _| {});
                assert_eq!(
                    visited,
                    vec![0],
                    "n={n} {form:?}: an all-false pass must visit exactly g0"
                );
                assert_gate_defaults(&out, &sim, k);

                // Later-chunk failures, including index 64, exercise the
                // second chained restore trigger and reset every gate (I3).
                for fail_idx in [64usize, k - 1] {
                    let mut truth = all_true(n);
                    truth[fail_idx] = false;
                    sim.set_truth(&truth);
                    let visited = sim.frame(|_, _| {});
                    assert_eq!(
                        count_visits(&visited, body),
                        0,
                        "n={n} {form:?}: failing chunk {fail_idx} must not enter the body"
                    );
                    assert_gate_defaults(&out, &sim, k);
                }

                // A success after those failures still enters the body (I1/I3).
                sim.set_truth(&all_true(n));
                let mut remaining = 1usize;
                let visited = sim.frame(|idx, truth| {
                    if idx == body {
                        remaining = remaining.saturating_sub(1);
                        truth[0] = remaining > 0;
                    }
                });
                assert_eq!(
                    count_visits(&visited, body),
                    1,
                    "n={n} {form:?}: a restored all-true pass must run the body"
                );
                assert_gate_defaults(&out, &sim, k);

                // Another all-true pass: when/while run again, once stays off.
                sim.set_truth(&all_true(n));
                let mut remaining = 1usize;
                let visited = sim.frame(|idx, truth| {
                    if idx == body {
                        remaining = remaining.saturating_sub(1);
                        truth[0] = remaining > 0;
                    }
                });
                let expected_body = if matches!(form, ChunkForm::Once) { 0 } else { 1 };
                assert_eq!(
                    count_visits(&visited, body),
                    expected_body,
                    "n={n} {form:?}: second all-true pass"
                );
                assert_gate_defaults(&out, &sim, k);
            }
        }
    }

    #[test]
    fn when_zero_one_and_sixteen_conditions_keep_todays_shape() {
        for n in [0usize, 1, 16] {
            let mut out = Vec::new();
            lower_when(
                &HirWhen::new(chunk_conds(n), Vec::new()),
                &mut out,
                |_, o| {
                    o.push(unconditional());
                    Ok(())
                },
            )
            .unwrap();
            // Gate + one unconditional body trigger with the restore appended.
            assert_eq!(out.len(), 2, "n={n}");
            assert_eq!(out[0].conds.len(), n, "n={n}");
            assert_eq!(gate_act_target(&out[0]), Some(1), "n={n}");
            assert_eq!(next_item(&out[0]), Some(2), "n={n}");
            assert!(has_restore_act(&out[1], 0, 2), "n={n}");
            assert_eq!(next_item(&out[1]), Some(2), "n={n}");
        }
    }

    #[test]
    fn once_zero_one_and_sixteen_conditions_keep_todays_shape() {
        for n in [0usize, 1, 16] {
            let mut out = Vec::new();
            lower_once(
                &HirOnce::new(chunk_conds(n), Vec::new()),
                &mut out,
                |_, o| {
                    o.push(unconditional());
                    Ok(())
                },
            )
            .unwrap();
            assert_eq!(out.len(), 2, "n={n}");
            assert_eq!(out[0].conds.len(), n, "n={n}");
            assert!(!out[0].preserved, "n={n}");
            assert_eq!(gate_act_target(&out[0]), Some(1), "n={n}");
            assert_eq!(next_item(&out[0]), Some(2), "n={n}");
            assert!(has_restore_act(&out[1], 0, 2), "n={n}");
            assert_eq!(next_item(&out[1]), Some(2), "n={n}");
        }
    }

    #[test]
    fn while_zero_one_and_sixteen_conditions_keep_todays_shape() {
        for n in [0usize, 1, 16] {
            let mut out = Vec::new();
            lower_while(
                &HirWhile::new(chunk_conds(n), Vec::new()),
                &mut out,
                |_, o| {
                    o.push(unconditional());
                    Ok(())
                },
            )
            .unwrap();
            assert_eq!(out.len(), 2, "n={n}");
            assert_eq!(out[0].conds.len(), n, "n={n}");
            assert_eq!(gate_act_target(&out[0]), Some(1), "n={n}");
            assert_eq!(next_item(&out[0]), Some(2), "n={n}");
            assert!(has_restore_act(&out[1], 0, 2), "n={n}");
            assert_eq!(next_item(&out[1]), Some(0), "n={n}");
        }
    }

    #[test]
    fn when_chunks_17_32_33_conditions_and_keeps_body_gated() {
        for n in [17usize, 32, 33] {
            let mut out = Vec::new();
            lower_when(
                &HirWhen::new(chunk_conds(n), Vec::new()),
                &mut out,
                |_, o| {
                    o.push(marker(BODY_MARK));
                    Ok(())
                },
            )
            .unwrap();
            assert_cond_chunks(&out, n);
            let body = marker_index(&out, BODY_MARK);

            let mut sim = ChunkSim::new(&out);
            sim.set_truth(&all_true(n));
            let visited = sim.frame(|_, _| {});
            assert!(
                count_visits(&visited, body) >= 1,
                "n={n}: an all-true frame must enter the body"
            );

            sim.set_truth(&vec![false; n]);
            let visited = sim.frame(|_, _| {});
            assert!(
                count_visits(&visited, body) == 0,
                "n={n}: an all-false frame must not enter the body"
            );
        }
    }

    #[test]
    fn when_tf_then_ft_frames_never_fire_the_body() {
        // g0 passes on a T/F frame, then g0 fails on the next F/T frame.
        // If the success restore (S) or failure restore (R) left g0.next
        // pointing at g1, the F/T frame would walk into the body.
        for n in [17usize, 32, 33] {
            let mut out = Vec::new();
            lower_when(
                &HirWhen::new(chunk_conds(n), Vec::new()),
                &mut out,
                |_, o| {
                    o.push(marker(BODY_MARK));
                    Ok(())
                },
            )
            .unwrap();
            assert_cond_chunks(&out, n);
            let body = marker_index(&out, BODY_MARK);
            let mut sim = ChunkSim::new(&out);

            let mut tf = all_true(n);
            tf[n - 1] = false;
            sim.set_truth(&tf);
            let visited = sim.frame(|_, _| {});
            assert!(
                count_visits(&visited, body) == 0,
                "n={n}: T/F frame must not run the body"
            );

            let mut ft = all_true(n);
            ft[0] = false;
            sim.set_truth(&ft);
            let visited = sim.frame(|_, _| {});
            assert!(
                count_visits(&visited, body) == 0,
                "n={n}: F/T frame must not run the body; a stale g0.next misfires here"
            );

            sim.set_truth(&all_true(n));
            let visited = sim.frame(|_, _| {});
            assert!(
                count_visits(&visited, body) >= 1,
                "n={n}: T/T frame must run the body"
            );
            sim.set_truth(&all_true(n));
            let visited = sim.frame(|_, _| {});
            assert!(
                count_visits(&visited, body) >= 1,
                "n={n}: a later T/T frame must still run the body"
            );
        }
    }

    #[test]
    fn when_else_body_runs_cleanly_after_a_later_chunk_fails() {
        for n in [17usize, 32, 33] {
            let then = HirStmt::Act(HirAct::set_deaths(0, 7, BODY_MARK, 0));
            let otherwise = HirStmt::Act(HirAct::set_deaths(0, 7, ELSE_MARK, 0));
            let mut out = Vec::new();
            lower_when(
                &HirWhen::with_else(chunk_conds(n), vec![then], vec![otherwise]),
                &mut out,
                |stmts, o| {
                    for stmt in stmts {
                        if let HirStmt::Act(act) = stmt {
                            o.push(marker(act.value));
                        }
                    }
                    Ok(())
                },
            )
            .unwrap();
            assert_cond_chunks(&out, n);
            let body = marker_index(&out, BODY_MARK);
            let else_body = marker_index(&out, ELSE_MARK);
            let mut sim = ChunkSim::new(&out);

            let mut tf = all_true(n);
            tf[n - 1] = false;
            sim.set_truth(&tf);
            let visited = sim.frame(|_, _| {});
            assert_eq!(
                count_visits(&visited, body),
                0,
                "n={n}: T/F frame must skip the then body"
            );
            assert_eq!(
                count_visits(&visited, else_body),
                1,
                "n={n}: T/F frame must reach else_begin through the restore"
            );

            let mut ft = all_true(n);
            ft[0] = false;
            sim.set_truth(&ft);
            let visited = sim.frame(|_, _| {});
            assert_eq!(
                count_visits(&visited, body),
                0,
                "n={n}: F/T frame must skip the then body"
            );
            assert_eq!(
                count_visits(&visited, else_body),
                1,
                "n={n}: F/T frame must still reach the else body after a earlier T/F restore"
            );

            sim.set_truth(&all_true(n));
            let visited = sim.frame(|_, _| {});
            assert_eq!(
                count_visits(&visited, body),
                1,
                "n={n}: T/T frame must run the then body"
            );
            assert_eq!(
                count_visits(&visited, else_body),
                0,
                "n={n}: T/T frame must not run the else body"
            );
        }
    }

    #[test]
    fn when_empty_then_body_with_else_keeps_both_restores() {
        for n in [17usize, 32, 33] {
            let otherwise = HirStmt::Act(HirAct::set_deaths(0, 7, ELSE_MARK, 0));
            let mut out = Vec::new();
            lower_when(
                &HirWhen::with_else(chunk_conds(n), Vec::new(), vec![otherwise]),
                &mut out,
                |stmts, o| {
                    for stmt in stmts {
                        if let HirStmt::Act(act) = stmt {
                            o.push(marker(act.value));
                        }
                    }
                    Ok(())
                },
            )
            .unwrap();
            assert_cond_chunks(&out, n);
            let else_body = marker_index(&out, ELSE_MARK);
            let mut sim = ChunkSim::new(&out);

            // All chunks pass: the success restore (S) must restore the
            // gates before the join, even though the then body is empty.
            sim.set_truth(&all_true(n));
            let visited = sim.frame(|_, _| {});
            assert_eq!(
                count_visits(&visited, else_body),
                0,
                "n={n}: T/T frame must not run the else body"
            );

            // Next frame F/T: the else body must run. A missing S leaves
            // g0.next at g1, so this frame would misfire into the (empty)
            // then path and skip else_begin.
            let mut ft = all_true(n);
            ft[0] = false;
            sim.set_truth(&ft);
            let visited = sim.frame(|_, _| {});
            assert_eq!(
                count_visits(&visited, else_body),
                1,
                "n={n}: F/T after T/T must still reach the else body"
            );
        }
    }

    #[test]
    fn once_chunks_and_fires_only_on_the_first_all_true_frame() {
        for n in [17usize, 32, 33] {
            let mut out = Vec::new();
            lower_once(
                &HirOnce::new(chunk_conds(n), Vec::new()),
                &mut out,
                |_, o| {
                    o.push(marker(BODY_MARK));
                    Ok(())
                },
            )
            .unwrap();
            assert_cond_chunks(&out, n);
            let body = marker_index(&out, BODY_MARK);
            let mut sim = ChunkSim::new(&out);

            // A partial match (chunk 0 true, last chunk false) must not
            // self-disable any chunk gate.
            let mut tf = all_true(n);
            tf[n - 1] = false;
            sim.set_truth(&tf);
            let visited = sim.frame(|_, _| {});
            assert_eq!(
                count_visits(&visited, body),
                0,
                "n={n}: once must not fire on a partial T/F frame"
            );

            sim.set_truth(&vec![false; n]);
            let visited = sim.frame(|_, _| {});
            assert_eq!(
                count_visits(&visited, body),
                0,
                "n={n}: once must not fire on an F/F frame"
            );

            sim.set_truth(&all_true(n));
            let visited = sim.frame(|_, _| {});
            assert_eq!(
                count_visits(&visited, body),
                1,
                "n={n}: once fires on the first T/T frame"
            );

            sim.set_truth(&all_true(n));
            let visited = sim.frame(|_, _| {});
            assert_eq!(
                count_visits(&visited, body),
                0,
                "n={n}: once must stay disabled on later T/T frames"
            );
        }
    }

    #[test]
    fn once_with_empty_body_chunks_and_keeps_the_following_chain_live() {
        for n in [17usize, 32, 33] {
            let mut out = Vec::new();
            lower_once(
                &HirOnce::new(chunk_conds(n), Vec::new()),
                &mut out,
                |_, _| Ok(()),
            )
            .unwrap();
            assert_cond_chunks(&out, n);
            out.push(marker(AFTER_MARK));
            let after = marker_index(&out, AFTER_MARK);
            let mut sim = ChunkSim::new(&out);

            for (label, truth) in [
                ("T/T", all_true(n)),
                ("T/T again", all_true(n)),
                ("T/F", {
                    let mut tf = all_true(n);
                    tf[n - 1] = false;
                    tf
                }),
            ] {
                sim.set_truth(&truth);
                let visited = sim.frame(|_, _| {});
                assert_eq!(
                    count_visits(&visited, after),
                    1,
                    "n={n}: the trigger after an empty once must execute once per frame ({label})"
                );
            }
        }
    }

    #[test]
    fn while_chunks_iterate_while_true_and_exit_on_a_later_chunk_failure() {
        for n in [17usize, 32, 33] {
            let mut out = Vec::new();
            lower_while(
                &HirWhile::new(chunk_conds(n), Vec::new()),
                &mut out,
                |_, o| {
                    o.push(marker(BODY_MARK));
                    Ok(())
                },
            )
            .unwrap();
            assert_cond_chunks(&out, n);
            let body = marker_index(&out, BODY_MARK);

            // All chunks true for three intra-frame iterations; the body act
            // makes chunk 0 false on the fourth check, so the loop exits.
            let mut sim = ChunkSim::new(&out);
            sim.set_truth(&all_true(n));
            let mut remaining = 3usize;
            let visited = sim.frame(|idx, truth| {
                if idx == body {
                    remaining = remaining.saturating_sub(1);
                    truth[0] = remaining > 0;
                }
            });
            assert_eq!(
                count_visits(&visited, body),
                3,
                "n={n}: while must iterate once per body-driven chunk-0 pass"
            );

            // Next frame: chunk 0 already false, so the loop exits without
            // entering the body.
            sim.set_truth(&{
                let mut t = all_true(n);
                t[0] = false;
                t
            });
            let visited = sim.frame(|_, _| {});
            assert_eq!(
                count_visits(&visited, body),
                0,
                "n={n}: while must exit when g0 is false on the next frame"
            );

            // A failing later chunk exits immediately (T/F).
            let mut sim = ChunkSim::new(&out);
            let mut tf = all_true(n);
            tf[n - 1] = false;
            sim.set_truth(&tf);
            let visited = sim.frame(|_, _| {});
            assert_eq!(
                count_visits(&visited, body),
                0,
                "n={n}: while must exit immediately on a later-chunk failure"
            );

            if n == 33 {
                // Middle-chunk failure (chunk 1): g0 passes, g1 fails, and R
                // restores the already-mutated g0 before exiting.
                let mut sim = ChunkSim::new(&out);
                let mut ft = all_true(n);
                ft[16] = false;
                sim.set_truth(&ft);
                let visited = sim.frame(|_, _| {});
                assert_eq!(
                    count_visits(&visited, body),
                    0,
                    "n={n}: while must exit when middle chunk 1 fails"
                );
                assert!(
                    visited.contains(&0) && visited.contains(&1) && !visited.contains(&2),
                    "n={n}: middle-chunk failure must stop before g2"
                );
            }

            // A failing g0 exits immediately too (1T check path).
            let mut sim = ChunkSim::new(&out);
            let mut ft = all_true(n);
            ft[0] = false;
            sim.set_truth(&ft);
            let visited = sim.frame(|_, _| {});
            assert_eq!(
                count_visits(&visited, body),
                0,
                "n={n}: while must exit immediately when g0 fails"
            );
        }
    }

    #[test]
    fn while_conditional_last_body_trigger_still_loops_back_through_a_separate_restore() {
        let n = 17usize;
        let mut out = Vec::new();
        lower_while(
            &HirWhile::new(chunk_conds(n), Vec::new()),
            &mut out,
            |_, o| {
                o.push(marker(BODY_MARK));
                // Conditional last body trigger: the while restore cannot
                // append to it, so a separate E restore trigger must loop
                // back to the chunked check.
                o.push(make_trigger(
                    vec![HirCond::deaths(0, 10, 999, 0)],
                    true,
                    4,
                    ExecNext::DefaultChain,
                    1,
                ));
                Ok(())
            },
        )
        .unwrap();
        assert_cond_chunks(&out, n);
        let body = marker_index(&out, BODY_MARK);

        let mut sim = ChunkSim::new(&out);
        sim.set_truth(&all_true(n));
        let visited = sim.frame(|idx, truth| {
            if idx == body {
                truth[0] = false;
            }
        });
        assert_eq!(
            count_visits(&visited, body),
            1,
            "the conditional last body trigger must not swallow the loop-back restore"
        );

        // After the restore, g0.next must point at the join. On a frame where
        // g0 is already false, a stale pointer would still enter the body.
        sim.set_truth(&{
            let mut t = all_true(n);
            t[0] = false;
            t
        });
        let visited = sim.frame(|_, _| {});
        assert_eq!(
            count_visits(&visited, body),
            0,
            "a conditional last body trigger must still leave a restored g0.next"
        );
    }
}
