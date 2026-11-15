//! Deterministic differential fuzz for structured control flow.
//!
//! The test builds HIR programs out of the same control forms the DSL lowers
//! (`when`/`else`, `once`, `whileLoop`, `until`, `onRising`, `onFalling`) plus
//! plain variable actions. For every program it:
//!
//! 1. runs the production pipeline (`prepare_lowering`: optimize + storage
//!    analysis + lowering),
//! 2. asserts the LIR seam invariants (`<= 16` conditions and `<= 64` actions
//!    per trigger), and
//! 3. simulates five trigger loops, comparing every declared variable against
//!    a hand-written HIR reference oracle.
//!
//! The oracle is intentionally written against the *source* HIR, not against
//! any optimized or lowered form. It models the variable relocations the
//! lowering passes resolve:
//!
//! * condition relocations at field offset 4 (`player`), and
//! * action relocations at field offset 16 (`dest`),
//!
//! which covers page-pinned `TriggerPage` slots, dword-packed cells, and
//! bit-packed boolean cells. Storage classes only change *where* a value
//! lives, never the value-level semantics the oracle executes.
//!
//! Generation is deterministic (fixed seeds), the programs are small, and the
//! extreme boundary cases are deliberately shallow so the whole file stays
//! well under the debug-mode runtime budget.

use std::collections::{HashMap, HashSet};

use eudkit_hir::{
    HirAct, HirCond, HirDocument, HirEdge, HirEdgeDirection, HirModule, HirOnce, HirStmt, HirUntil,
    HirVarDecl, HirWhen, HirWhile,
};
use eudkit_lir::{
    ACT_COUNT, COND_COUNT, COND_PLAYER_FIELD_OFFSET, DEST_FIELD_OFFSET, EUDX_MAGIC, LirDocument,
    LirItem, SimulationState, VALUE_FIELD_OFFSET, epd_u32, init_simulation,
};
use eudkit_lower::prepare_lowering;
use eudkit_opt::cell::{StorageClass, StoragePlan};

/// Loop passes simulated (and oracle frames compared) per program.
const FRAMES: usize = 5;

/// Number of deterministic, seeded nested programs.
const SEEDED_PROGRAMS: u64 = 120;

/// Maximum control-flow nesting depth (root control is depth 1).
const MAX_DEPTH: usize = 3;

/// Full value mask used by every generated variable write.
const FULL_MASK: u32 = 0xFFFF_FFFF;

/// Allocation start for compiler-internal state variables (edge curr/prev,
/// `until` latches, `while` termination counters). It is far above the user
/// palette so generated programs never accidentally mutate internal state.
const INTERNAL_VAR_BASE: usize = 64;

// ---------------------------------------------------------------------------
// Variable palette: page-pinned, dword-packed, and bit-packed scalars.
// ---------------------------------------------------------------------------

const PALETTE_LEN: usize = 8;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Role {
    /// Forced onto a `TriggerPage` action slot by the declaration.
    Page,
    /// Integer scalar demoted to a 4-byte dword cell.
    Dword,
    /// Boolean scalar demoted to a single bit in a shared dword.
    Bit,
}

/// User variable ids `0..PALETTE_LEN` and their storage roles.
///
/// `Page` variables are pinned explicitly. `Dword` variables have non-boolean
/// initial values and are written by integer arithmetic, so the cell analysis
/// keeps them as 4-byte dwords. `Bit` variables start at 0/1, are only written
/// with `SetTo(0|1)`, and are only tested with boolean-domain `Deaths`
/// comparisons, so they pack 32-to-a-dword.
const PALETTE_ROLES: [Role; PALETTE_LEN] = [
    Role::Page,
    Role::Page,
    Role::Dword,
    Role::Dword,
    Role::Bit,
    Role::Bit,
    Role::Bit,
    Role::Bit,
];

const PALETTE_INITS: [u32; PALETTE_LEN] = [3, 5, 7, 11, 0, 1, 0, 1];

fn palette_vars() -> Vec<HirVarDecl> {
    vec![
        HirVarDecl::pinned(0, PALETTE_INITS[0]),
        HirVarDecl::pinned(1, PALETTE_INITS[1]),
        HirVarDecl::new(2, PALETTE_INITS[2]),
        HirVarDecl::new(3, PALETTE_INITS[3]),
        HirVarDecl::new(4, PALETTE_INITS[4]),
        HirVarDecl::new(5, PALETTE_INITS[5]),
        HirVarDecl::new(6, PALETTE_INITS[6]),
        HirVarDecl::new(7, PALETTE_INITS[7]),
    ]
}

fn role_of(var_id: usize) -> Role {
    PALETTE_ROLES[var_id % PALETTE_LEN]
}

fn init_of(var_id: usize) -> u32 {
    PALETTE_INITS[var_id % PALETTE_LEN]
}

// ---------------------------------------------------------------------------
// Program builder and deterministic generator.
// ---------------------------------------------------------------------------

struct Builder {
    vars: Vec<HirVarDecl>,
    next_internal: usize,
}

impl Builder {
    fn new() -> Self {
        Self {
            vars: palette_vars(),
            next_internal: INTERNAL_VAR_BASE,
        }
    }

    fn alloc_internal(&mut self, init: u32) -> usize {
        let id = self.next_internal;
        self.next_internal += 1;
        self.vars.push(HirVarDecl::new(id, init));
        id
    }

    fn module(&self, stmts: Vec<HirStmt>) -> HirModule {
        let mut module = HirModule::new();
        module.vars = self.vars.clone();
        module.stmts = stmts;
        module
    }
}

/// Small deterministic xorshift generator. No external crates, no wall clock.
struct Rng(u64);

impl Rng {
    fn new(seed: u64) -> Self {
        // Avoid the all-zero state.
        Self(seed.wrapping_mul(0x9E37_79B9_7F4A_7C15) | 1)
    }

    fn next_u64(&mut self) -> u64 {
        let mut x = self.0;
        x ^= x << 13;
        x ^= x >> 7;
        x ^= x << 17;
        self.0 = x;
        x
    }

    fn below(&mut self, n: usize) -> usize {
        if n == 0 {
            0
        } else {
            (self.next_u64() % n as u64) as usize
        }
    }
}

/// One generated control form. Boundary coverage iterates all of them.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Form {
    When,
    WhenElse,
    Once,
    While,
    Until,
    EdgeRising,
    EdgeFalling,
}

const ALL_FORMS: [Form; 7] = [
    Form::When,
    Form::WhenElse,
    Form::Once,
    Form::While,
    Form::Until,
    Form::EdgeRising,
    Form::EdgeFalling,
];

const COND_BOUNDARIES: [usize; 11] = [0, 1, 15, 16, 17, 32, 33, 64, 65, 130, 1025];
const BODY_BOUNDARIES: [usize; 7] = [0, 1, 63, 64, 65, 128, 129];

/// A condition on a palette variable, deliberately made true for every
/// palette variable at frame 0. `unit` is unique so `fuse_and_dedup_conditions`
/// cannot collapse the list below the requested count.
fn boundary_cond(i: usize) -> HirCond {
    let var = i % PALETTE_LEN;
    let mut cond = HirCond::var_deaths(var, 10, init_of(var)); // Exactly init
    cond.unit = (i + 1) as u16;
    cond
}

/// A plain action on a palette variable. Boolean variables only ever receive
/// `SetTo(0|1)`; integer variables cycle through `SetTo`, `Add`, `Subtract`.
fn palette_act(var: usize, salt: usize) -> HirStmt {
    match role_of(var) {
        Role::Bit => HirStmt::Act(HirAct::set_var_const(var, 7, (salt as u32) & 1, FULL_MASK)),
        Role::Page | Role::Dword => {
            let modifier = match salt % 3 {
                0 => 7, // SetTo
                1 => 8, // Add
                _ => 9, // Subtract
            };
            let value = ((salt as u32) % 4) + 1;
            HirStmt::Act(HirAct::set_var_const(var, modifier, value, FULL_MASK))
        }
    }
}

fn boundary_acts(count: usize, salt_base: usize) -> Vec<HirStmt> {
    (0..count)
        .map(|i| palette_act((salt_base + i) % PALETTE_LEN, salt_base + i))
        .collect()
}

/// Build one boundary-matrix program for `form`, with exactly `n_conds`
/// conditions in the control node (for `while`, the terminating guard is one
/// of them) and `n_acts` user body actions.
///
/// A truly condition-free `while` would spin forever, so its `0` entry gets
/// the single termination guard; every other count is exact.
fn boundary_program(form: Form, n_conds: usize, n_acts: usize) -> HirModule {
    let mut builder = Builder::new();

    // Internal state nodes are allocated up front so the formal condition
    // count can include the `while` termination guard without changing n.
    let mut while_term = None;
    let conds = match form {
        Form::While => {
            let term = builder.alloc_internal(3); // three body passes at most
            while_term = Some(term);
            let mut conds = Vec::with_capacity(n_conds.max(1));
            if n_conds == 0 {
                conds.push(HirCond::var_deaths(term, 0, 1)); // term >= 1
            } else {
                conds.push(HirCond::var_deaths(term, 0, 1));
                for i in 0..n_conds - 1 {
                    conds.push(boundary_cond(i));
                }
            }
            conds
        }
        _ => (0..n_conds).map(boundary_cond).collect(),
    };

    let mut body = boundary_acts(n_acts, 0);
    if let Some(term) = while_term {
        // The decrement is unconditional and first: every loop iteration
        // makes progress even when the rest of the body is a control node.
        body.insert(
            0,
            HirStmt::Act(HirAct::set_var_const(term, 9, 1, FULL_MASK)),
        );
    }
    let else_body = if form == Form::WhenElse {
        boundary_acts(n_acts, n_acts + 1)
    } else {
        Vec::new()
    };

    let stmt = match form {
        Form::When => HirStmt::When(HirWhen::new(conds, body)),
        Form::WhenElse => HirStmt::When(HirWhen::with_else(conds, body, else_body)),
        Form::Once => HirStmt::Once(HirOnce::new(conds, body)),
        Form::While => HirStmt::While(HirWhile::new(conds, body)),
        Form::Until => {
            let latch = builder.alloc_internal(1);
            HirStmt::Until(HirUntil::new(conds, body, latch))
        }
        Form::EdgeRising | Form::EdgeFalling => {
            let curr = builder.alloc_internal(0);
            let prev = builder.alloc_internal(0);
            let direction = match form {
                Form::EdgeRising => HirEdgeDirection::Rising,
                Form::EdgeFalling => HirEdgeDirection::Falling,
                _ => unreachable!(),
            };
            HirStmt::Edge(HirEdge::new(direction, conds, body, curr, prev))
        }
    };

    builder.module(vec![stmt])
}

fn random_cond(rng: &mut Rng) -> HirCond {
    let var = rng.below(PALETTE_LEN);
    match role_of(var) {
        Role::Bit => {
            // Boolean-domain comparisons only, so the variable stays packed.
            let amount = rng.below(2) as u32;
            let mut cond = HirCond::var_deaths(var, 10, amount); // Exactly 0/1
            cond.unit = rng.below(u16::MAX as usize) as u16;
            cond
        }
        Role::Page | Role::Dword => {
            let comparison = match rng.below(3) {
                0 => 0,  // AtLeast
                1 => 1,  // AtMost
                _ => 10, // Exactly
            };
            let amount = rng.below(8) as u32;
            let mut cond = HirCond::var_deaths(var, comparison, amount);
            cond.unit = rng.below(u16::MAX as usize) as u16;
            cond
        }
    }
}

fn randomized_act(rng: &mut Rng) -> HirStmt {
    let var = rng.below(PALETTE_LEN);
    let salt = rng.below(64);
    palette_act(var, salt)
}

/// The condition variables named by a list, so a body can deliberately mutate
/// the values that gate its enclosing control node.
fn cond_vars(conds: &[HirCond]) -> Vec<usize> {
    let mut out = Vec::new();
    for cond in conds {
        for reloc in &cond.relocs {
            if reloc.field_offset == COND_PLAYER_FIELD_OFFSET {
                if let Some(var) = reloc.target_var {
                    if !out.contains(&var) {
                        out.push(var);
                    }
                }
            }
        }
    }
    out
}

/// Build one control node. `While` gets a fresh internal termination counter
/// and an unconditional decrement as its first body statement, so every
/// generated loop provably terminates. `Until`/`Edge` also allocate their
/// internal state variables here.
fn make_node(
    builder: &mut Builder,
    form: Form,
    conds: Vec<HirCond>,
    mut body: Vec<HirStmt>,
    else_body: Vec<HirStmt>,
) -> HirStmt {
    match form {
        Form::When => HirStmt::When(HirWhen::new(conds, body)),
        Form::WhenElse => HirStmt::When(HirWhen::with_else(conds, body, else_body)),
        Form::Once => HirStmt::Once(HirOnce::new(conds, body)),
        Form::While => {
            let term = builder.alloc_internal(3);
            // The terminating guard is part of the user-facing condition
            // list: `term >= 1`. The body's first statement decrements it, so
            // the loop is bounded no matter what the other conditions do.
            let mut conds = conds;
            conds.insert(0, HirCond::var_deaths(term, 0, 1));
            body.insert(
                0,
                HirStmt::Act(HirAct::set_var_const(term, 9, 1, FULL_MASK)),
            );
            HirStmt::While(HirWhile::new(conds, body))
        }
        Form::Until => {
            let latch = builder.alloc_internal(1);
            HirStmt::Until(HirUntil::new(conds, body, latch))
        }
        Form::EdgeRising | Form::EdgeFalling => {
            let curr = builder.alloc_internal(0);
            let prev = builder.alloc_internal(0);
            let direction = match form {
                Form::EdgeRising => HirEdgeDirection::Rising,
                Form::EdgeFalling => HirEdgeDirection::Falling,
                _ => unreachable!(),
            };
            HirStmt::Edge(HirEdge::new(direction, conds, body, curr, prev))
        }
    }
}

fn make_body(
    builder: &mut Builder,
    rng: &mut Rng,
    depth: usize,
    conds: &[HirCond],
) -> Vec<HirStmt> {
    let mut body = gen_stmts(builder, rng, depth);

    // Self-mutating condition variables: at least one body action writes a
    // variable the enclosing gate reads. The `while` termination variable is
    // internal and never in the palette, so it cannot be selected here.
    let vars = cond_vars(conds);
    if !vars.is_empty() {
        let var = vars[rng.below(vars.len())];
        body.push(palette_act(var, rng.below(64)));
    }
    body
}

fn gen_control(builder: &mut Builder, rng: &mut Rng, depth: usize, form: Form) -> Vec<HirStmt> {
    let n_conds = match rng.below(10) {
        0..=5 => rng.below(2),
        6..=8 => rng.below(4),
        _ => rng.below(6),
    };
    let conds: Vec<HirCond> = (0..n_conds).map(|_| random_cond(rng)).collect();

    let body = make_body(builder, rng, depth + 1, &conds);
    let else_body = if form == Form::WhenElse {
        make_body(builder, rng, depth + 1, &conds)
    } else {
        Vec::new()
    };
    vec![make_node(builder, form, conds, body, else_body)]
}

fn gen_stmts(builder: &mut Builder, rng: &mut Rng, depth: usize) -> Vec<HirStmt> {
    let count = rng.below(3);
    let mut out = Vec::new();
    for _ in 0..count {
        let make_control = depth < MAX_DEPTH && rng.below(100) < 60;
        if make_control {
            let form = ALL_FORMS[rng.below(ALL_FORMS.len())];
            out.extend(gen_control(builder, rng, depth, form));
        } else {
            out.push(randomized_act(rng));
        }
    }
    out
}

/// One deliberately nested chain: a control node whose body is the next
/// control node, down to [`MAX_DEPTH`]. Used for seeds divisible by 10 so the
/// seeded corpus provably reaches full depth.
fn seeded_chain(builder: &mut Builder, rng: &mut Rng, seed: u64, depth: usize) -> HirStmt {
    let form = ALL_FORMS[(seed as usize + depth) % ALL_FORMS.len()];
    let n_conds = if depth == 0 {
        (seed as usize) % 5 // 0..=4 conditions at the root
    } else {
        rng.below(4)
    };
    let conds: Vec<HirCond> = (0..n_conds).map(|_| random_cond(rng)).collect();

    let mut body = if depth + 1 < MAX_DEPTH {
        vec![seeded_chain(builder, rng, seed, depth + 1)]
    } else {
        make_body(builder, rng, depth + 1, &conds)
    };
    if let Some(&var) = cond_vars(&conds).first() {
        body.push(palette_act(var, (seed as usize + depth) & 63));
    }
    let else_body = if form == Form::WhenElse {
        make_body(builder, rng, depth + 1, &conds)
    } else {
        Vec::new()
    };
    make_node(builder, form, conds, body, else_body)
}

/// Deterministically build one seeded program. The top-level form cycles
/// through every supported control node, so each one is exercised even for
/// small seeds; nested forms are random but bounded by [`MAX_DEPTH`].
fn seeded_program(seed: u64) -> HirModule {
    let mut builder = Builder::new();
    let mut rng = Rng::new(seed);
    let form = ALL_FORMS[(seed as usize) % ALL_FORMS.len()];

    let stmt = if seed % 10 == 0 {
        seeded_chain(&mut builder, &mut rng, seed, 0)
    } else {
        gen_control(&mut builder, &mut rng, 0, form)
            .pop()
            .expect("gen_control always emits one node")
    };
    builder.module(vec![stmt])
}

// ---------------------------------------------------------------------------
// HIR reference oracle.
// ---------------------------------------------------------------------------

struct Oracle<'a> {
    module: &'a HirModule,
    vars: HashMap<usize, u32>,
    once_fired: HashSet<usize>,
}

impl<'a> Oracle<'a> {
    fn new(module: &'a HirModule) -> Self {
        Self {
            module,
            vars: module.vars.iter().map(|v| (v.id, v.init)).collect(),
            once_fired: HashSet::new(),
        }
    }

    fn value(&self, var: usize) -> u32 {
        *self
            .vars
            .get(&var)
            .unwrap_or_else(|| panic!("oracle read of undeclared variable {var}"))
    }

    fn set_value(&mut self, var: usize, value: u32) {
        self.vars.insert(var, value);
    }

    fn frame(&mut self) {
        // Copy the shared reference out first: the recursive walk mutates
        // `vars`, so it may not hold a borrow of `self` for the statements.
        let module = self.module;
        self.exec_stmts(&module.stmts);
    }

    fn exec_stmts(&mut self, stmts: &[HirStmt]) {
        for stmt in stmts {
            self.exec_stmt(stmt);
        }
    }

    fn all_true(&self, conds: &[HirCond]) -> bool {
        conds.iter().all(|cond| self.eval_cond(cond))
    }

    fn exec_stmt(&mut self, stmt: &HirStmt) {
        match stmt {
            HirStmt::Act(act) => self.exec_act(act),
            HirStmt::When(when) => {
                if self.all_true(&when.conds) {
                    self.exec_stmts(&when.body);
                } else {
                    self.exec_stmts(&when.else_body);
                }
            }
            HirStmt::Once(once) => {
                let node = once as *const HirOnce as usize;
                if self.once_fired.contains(&node) {
                    return;
                }
                if self.all_true(&once.conds) {
                    self.once_fired.insert(node);
                    self.exec_stmts(&once.body);
                }
            }
            HirStmt::While(wh) => {
                // Condition re-evaluation is intra-frame: every body pass is
                // followed by a fresh gate evaluation in the same loop.
                let mut guard = 0usize;
                while self.all_true(&wh.conds) {
                    self.exec_stmts(&wh.body);
                    guard += 1;
                    assert!(
                        guard <= 1024,
                        "oracle while loop did not terminate after 1024 body passes"
                    );
                }
            }
            HirStmt::Until(until) => {
                // Trip-before-body latch semantics: when the latch is armed
                // and the gate holds, the latch is cleared and the body is
                // skipped for this and every later frame.
                if self.value(until.latch_var) == 1 {
                    if self.all_true(&until.conds) {
                        self.set_value(until.latch_var, 0);
                    } else {
                        self.exec_stmts(&until.body);
                    }
                }
            }
            HirStmt::Edge(edge) => {
                // curr = 0; sample conds into curr; fire on (curr, prev);
                // run the body; then latch prev to the live curr value.
                self.set_value(edge.curr_var, 0);
                if self.all_true(&edge.conds) {
                    self.set_value(edge.curr_var, 1);
                }
                let curr = self.value(edge.curr_var);
                let prev = self.value(edge.prev_var);
                let fire = match edge.direction {
                    HirEdgeDirection::Rising => curr == 1 && prev == 0,
                    HirEdgeDirection::Falling => curr == 0 && prev == 1,
                };
                if fire {
                    self.exec_stmts(&edge.body);
                }
                let curr = self.value(edge.curr_var);
                self.set_value(edge.prev_var, curr);
            }
            HirStmt::Trigger(_) => {
                panic!("the fuzz generator never emits raw Trigger statements")
            }
            other => panic!("the fuzz generator never emits {other:?}"),
        }
    }

    fn eval_cond(&self, cond: &HirCond) -> bool {
        if cond.condtype == 22 {
            return true;
        }
        if cond.condtype == 23 {
            return false;
        }

        // The generator only emits variable-backed Deaths conditions. Lowering
        // resolves the field-4 (`player`) relocation to the variable's storage
        // address; the oracle resolves it to the variable's value instead.
        let var = cond
            .relocs
            .iter()
            .find(|reloc| {
                reloc.field_offset == COND_PLAYER_FIELD_OFFSET && reloc.target_var.is_some()
            })
            .and_then(|reloc| reloc.target_var);
        let Some(var) = var else {
            // No variable relocation: treat the literal player field as an
            // absolute external EPD whose value starts at zero, matching the
            // simulator's default external memory.
            let lhs = 0u32;
            return compare(lhs, cond.comparison, cond.amount);
        };

        let raw = self.value(var);
        let lhs = if cond.eudx == EUDX_MAGIC {
            raw & cond.locid
        } else {
            raw
        };
        compare(lhs, cond.comparison, cond.amount)
    }

    fn exec_act(&mut self, act: &HirAct) {
        // Actions write the variable named by the field-16 (`dest`)
        // relocation. Other reloc targets are control-flow machinery and are
        // not emitted by the generator.
        let Some(var) = act
            .relocs
            .iter()
            .find(|reloc| {
                reloc.field_offset == DEST_FIELD_OFFSET
                    && reloc.target_var.is_some()
                    && reloc.target_offset == VALUE_FIELD_OFFSET
            })
            .and_then(|reloc| reloc.target_var)
        else {
            return;
        };

        let old = self.value(var);
        let eff_mask = if act.eudx == EUDX_MAGIC {
            act.mask
        } else {
            FULL_MASK
        };
        let new = match act.modifier {
            7 => {
                // SetTo
                (old & !eff_mask) | (act.value & eff_mask)
            }
            8 => {
                // Add
                let sum = (old & eff_mask).wrapping_add(act.value & eff_mask);
                (old & !eff_mask) | (sum & eff_mask)
            }
            9 => {
                // Subtract (saturating inside the masked bits)
                let cur = old & eff_mask;
                let sub = act.value & eff_mask;
                (old & !eff_mask) | (cur.saturating_sub(sub) & eff_mask)
            }
            _ => old,
        };
        self.set_value(var, new);
    }
}

fn compare(lhs: u32, comparison: u8, amount: u32) -> bool {
    match comparison {
        0 => lhs >= amount,  // AtLeast
        1 => lhs <= amount,  // AtMost
        10 => lhs == amount, // Exactly
        _ => false,
    }
}

// ---------------------------------------------------------------------------
// Lowered-LIR reader and assertion harness.
// ---------------------------------------------------------------------------

struct VarReader<'a> {
    plan: &'a StoragePlan,
    num_exec: usize,
    num_pages: usize,
}

impl<'a> VarReader<'a> {
    fn new(lir: &LirDocument, plan: &'a StoragePlan) -> Self {
        let num_pages = plan.trigger_pages;
        let num_exec = lir
            .module
            .items
            .len()
            .checked_sub(num_pages + usize::from(plan.cell_dwords > 0))
            .expect("LIR must contain its exec items, pages, and optional cell blob");
        Self {
            plan,
            num_exec,
            num_pages,
        }
    }

    fn read(&self, sim: &SimulationState, var: usize) -> u32 {
        match self
            .plan
            .storage(var)
            .unwrap_or_else(|| panic!("variable {var} has no storage allocation"))
        {
            StorageClass::TriggerPage {
                page_index,
                slot_index,
            } => sim.read_page_slot_value(self.num_exec + page_index, slot_index),
            StorageClass::DwordCell { dword_index } => {
                let addr =
                    sim.payload.addrs[self.num_exec + self.num_pages] + (dword_index * 4) as u32;
                sim.read_epd(epd_u32(addr))
            }
            StorageClass::BitCell {
                dword_index,
                bit_index,
            } => {
                let addr =
                    sim.payload.addrs[self.num_exec + self.num_pages] + (dword_index * 4) as u32;
                (sim.read_epd(epd_u32(addr)) >> bit_index) & 1
            }
        }
    }
}

fn assert_lir_invariants(lir: &LirDocument, label: &str) {
    for (idx, item) in lir.module.items.iter().enumerate() {
        if let LirItem::Trigger(trigger) = item {
            assert!(
                trigger.conds.len() <= COND_COUNT,
                "{label}: LIR item {idx} has {} conditions (max {COND_COUNT})",
                trigger.conds.len()
            );
            assert!(
                trigger.acts.len() <= ACT_COUNT,
                "{label}: LIR item {idx} has {} actions (max {ACT_COUNT})",
                trigger.acts.len()
            );
        }
    }
}

fn assert_var_parity(
    label: &str,
    frame: usize,
    module: &HirModule,
    oracle: &Oracle<'_>,
    reader: &VarReader<'_>,
    sim: &SimulationState,
) {
    for decl in &module.vars {
        let got = reader.read(sim, decl.id);
        let want = oracle.value(decl.id);
        assert_eq!(
            got,
            want,
            "{label}: frame {frame}, var[{}] (storage {:?}) diverged: LIR={got} oracle={want}",
            decl.id,
            reader.plan.storage(decl.id)
        );
    }
}

/// Lower one program through the production `opt + storage + lowering`
/// pipeline, then compare five simulated frames against the HIR oracle.
fn run_program(label: &str, module: &HirModule) {
    let doc = HirDocument::new(module.clone());
    let prepared = prepare_lowering(&doc, None)
        .unwrap_or_else(|err| panic!("{label}: prepare_lowering failed: {err}"));

    assert_lir_invariants(&prepared.lir, label);

    for decl in &module.vars {
        assert!(
            prepared.storage.storage(decl.id).is_some(),
            "{label}: var[{}] has no storage class after analysis",
            decl.id
        );
    }

    let reader = VarReader::new(&prepared.lir, &prepared.storage);
    let mut oracle = Oracle::new(module);
    let mut sim = init_simulation(&prepared.lir)
        .unwrap_or_else(|err| panic!("{label}: init_simulation failed: {err}"));

    // Initial values come from the same declarations; comparing them first
    // catches storage-init bugs before any trigger executes.
    assert_var_parity(label, 0, module, &oracle, &reader, &sim);

    for frame in 1..=FRAMES {
        oracle.frame();
        sim.step_loop();
        assert_var_parity(label, frame, module, &oracle, &reader, &sim);
    }
}

// ---------------------------------------------------------------------------
// Tests.
// ---------------------------------------------------------------------------

#[test]
fn seeded_nested_programs_match_the_hir_oracle() {
    let mut max_depth = 0usize;
    for seed in 0..SEEDED_PROGRAMS {
        let module = seeded_program(seed);
        let depth = deepest_control_depth(&module.stmts);
        max_depth = max_depth.max(depth);
        run_program(&format!("seed {seed}"), &module);
    }
    assert_eq!(
        max_depth, MAX_DEPTH,
        "the seeded corpus must exercise the full nesting depth"
    );
}

#[test]
fn boundary_condition_counts_match_the_hir_oracle() {
    for form in ALL_FORMS {
        for &n in &COND_BOUNDARIES {
            let module = boundary_program(form, n, 1);
            run_program(&format!("conds {form:?} n={n}"), &module);
        }
    }
}

#[test]
fn boundary_body_action_counts_match_the_hir_oracle() {
    for form in ALL_FORMS {
        for &n in &BODY_BOUNDARIES {
            let module = boundary_program(form, 2, n);
            run_program(&format!("acts {form:?} n={n}"), &module);
        }
    }
}

#[test]
fn palette_exercises_page_dword_and_bit_storage() {
    let module = boundary_program(Form::When, 1, 1);
    let doc = HirDocument::new(module);
    let prepared = prepare_lowering(&doc, None).expect("palette program must lower");

    let mut saw_page = false;
    let mut saw_dword = false;
    let mut saw_bit = false;
    for decl in &prepared.optimized.vars {
        match prepared.storage.storage(decl.id) {
            Some(StorageClass::TriggerPage { .. }) => saw_page = true,
            Some(StorageClass::DwordCell { .. }) => saw_dword = true,
            Some(StorageClass::BitCell { .. }) => saw_bit = true,
            None => panic!("var[{}] has no storage class", decl.id),
        }
    }
    assert!(saw_page, "palette must include a page-pinned variable");
    assert!(saw_dword, "palette must include a dword-packed variable");
    assert!(saw_bit, "palette must include a bit-packed variable");
}

/// Maximum nesting depth of any control statement.
fn deepest_control_depth(stmts: &[HirStmt]) -> usize {
    fn walk(stmts: &[HirStmt]) -> usize {
        stmts
            .iter()
            .map(|stmt| match stmt {
                HirStmt::Act(_) => 0,
                HirStmt::When(w) => 1 + walk(&w.body).max(walk(&w.else_body)),
                HirStmt::Once(o) => 1 + walk(&o.body),
                HirStmt::While(w) => 1 + walk(&w.body),
                HirStmt::Until(u) => 1 + walk(&u.body),
                HirStmt::Edge(e) => 1 + walk(&e.body),
                _ => 0,
            })
            .max()
            .unwrap_or(0)
    }
    walk(stmts)
}
