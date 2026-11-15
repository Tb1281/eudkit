//! HIR 3.2 types, validation, and canonical JSON serialization for `eudkit`.
//!
//! `HirDocument` is the language-neutral high-level IR emitted identically by
//! both the TypeScript (`ts/`) and Python (`py/`) `buildModule` frontends.

use std::collections::HashSet;

use eudkit_lir::{ACT_COUNT, COND_COUNT, CTrigAction, CTrigCondition, EUDX_MAGIC};
use serde::{Deserialize, Serialize};
use thiserror::Error;

pub const HIR_VERSION: &str = "3.2";

#[derive(Debug, Error, PartialEq, Eq)]
pub enum HirError {
    #[error("Unsupported HIR version: {0} (expected {HIR_VERSION})")]
    BadVersion(String),
    #[error("EUDSL-BOUNDS: Trigger condition count {0} exceeds maximum {COND_COUNT}")]
    TooManyConditions(usize),
    #[error("EUDSL-BOUNDS: Trigger action count {0} exceeds maximum {ACT_COUNT}")]
    TooManyActions(usize),
    #[error("EUDSL-BOUNDS: counted form count must be a positive integer, got {0}")]
    NonPositiveCount(u32),
    #[error("Unknown variable ID {0} referenced in HIR module")]
    UnknownVarId(usize),
    #[error("EUDSL-BOUNDS: Unknown module string index {0} referenced in HIR")]
    UnknownStringId(usize),
    #[error("EUDSL-BOUNDS: init_string requires a page-pinned variable (id {0})")]
    StringInitRequiresPage(usize),
    #[error(
        "EUDSL-BOUNDS: string relocation field offset {0} does not fit a 4-byte window inside one {1}-byte record"
    )]
    InvalidStringReloc(usize, usize),
    #[error("Invalid HIR relocation: exactly one of target_trigger/target_var/target_string must be set")]
    InvalidReloc,
    #[error("EUDSL-BOUNDS: VarStore must set exactly one of dst_addr or dst_trigger")]
    InvalidVarStore,
    #[error("EUDSL-BOUNDS: embedded file name must not be empty")]
    EmptyEmbeddedFileName,
    #[error("EUDSL-BOUNDS: embedded file '{0}' must have a non-empty path")]
    EmptyEmbeddedFilePath(String),
    #[error("EUDSL-BOUNDS: duplicate embedded file name '{0}'")]
    DuplicateEmbeddedFileName(String),
    #[error("EUDSL-BOUNDS: unknown function ID {0} referenced by a call")]
    UnknownFuncId(usize),
    #[error("EUDSL-BOUNDS: function {0} is called with {1} arguments but declares {2}")]
    FuncArgCount(usize, usize, usize),
    #[error(
        "EUDSL-BOUNDS: function {0} is recursive (direct or indirect); callable subroutines have a single exit pointer and cannot re-enter"
    )]
    RecursiveFunc(usize),
    #[error("EUDSL-BOUNDS: duplicate function ID {0}")]
    DuplicateFuncId(usize),
    #[error("EUDSL-BOUNDS: blob {0} data is not valid base64 or is longer than its size")]
    InvalidBlobData(usize),
    #[error(
        "EUDSL-BOUNDS: invalid blob {0} relocation at offset {1}: need exactly one known blob/trigger target and a 4-byte aligned offset inside the blob"
    )]
    InvalidBlobReloc(usize, usize),
    #[error(
        "EUDSL-BOUNDS: relocation to blob {0} at field offset {1} needs a known blob id, a 4-byte aligned field, and a 4-byte aligned target offset"
    )]
    InvalidBlobTarget(usize, usize),
    #[error("Invalid HIR JSON: {0}")]
    Json(String),
    #[error("optimizer invariant violated: {0}")]
    OptimizerInvariant(&'static str),
}

/// Top-level HIR 3.2 document emitted by `buildModule`.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct HirDocument {
    pub version: String,
    pub module: HirModule,
}

impl HirDocument {
    pub fn new(module: HirModule) -> Self {
        Self {
            version: HIR_VERSION.to_string(),
            module,
        }
    }

    pub fn validate(&self) -> Result<(), HirError> {
        if self.version != HIR_VERSION {
            return Err(HirError::BadVersion(self.version.clone()));
        }
        self.module.validate()
    }
}

fn default_full_mask() -> u32 {
    0xFFFF_FFFF
}

fn is_full_mask(mask: &u32) -> bool {
    *mask == 0xFFFF_FFFF
}

/// Scalar variable declaration backed by a `64A` `TriggerPage` action slot.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct HirVarDecl {
    pub id: usize,
    pub init: u32,
    #[serde(default = "default_full_mask", skip_serializing_if = "is_full_mask")]
    pub mask: u32,
    /// Optional storage pin. `"page"` forces the variable onto a `TriggerPage`
    /// action slot (used by writable `FixedArray`/`TriggerStruct` backing and any
    /// variable whose address is consumed by container address math).
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub storage: Option<String>,
    /// Late-bound initial value: the EPD address of module string `index`
    /// plus `offset` bytes. Resolved by lowering against the basemap STR
    /// offset table (used by `FixedString` buffer address variables).
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub init_string: Option<HirStringInit>,
}

/// Late-bound module-string address initializer.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub struct HirStringInit {
    /// 0-based index into [`HirModule::strings`].
    pub index: usize,
    #[serde(default)]
    pub offset: usize,
}

impl HirVarDecl {
    pub const fn new(id: usize, init: u32) -> Self {
        Self {
            id,
            init,
            mask: 0xFFFF_FFFF,
            storage: None,
            init_string: None,
        }
    }

    pub const fn with_mask(id: usize, init: u32, mask: u32) -> Self {
        Self {
            id,
            init,
            mask,
            storage: None,
            init_string: None,
        }
    }

    /// Declare a variable permanently backed by a `TriggerPage` action slot.
    pub fn pinned(id: usize, init: u32) -> Self {
        Self {
            id,
            init,
            mask: 0xFFFF_FFFF,
            storage: Some("page".to_string()),
            init_string: None,
        }
    }

    /// Pinned variant of [`HirVarDecl::with_mask`].
    pub fn pinned_with_mask(id: usize, init: u32, mask: u32) -> Self {
        Self {
            id,
            init,
            mask,
            storage: Some("page".to_string()),
            init_string: None,
        }
    }
}

/// A single variable-source assignment (`dst = src`, `dst += src`, `dst -= src`).
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct HirVarTransfer {
    pub dst_var: usize,
    pub src_var: usize,
    pub modifier: u8,
}

/// A coalesced batch of variable-source assignments emitted by `eudkit-opt`.
///
/// A batch is one `TriggerPage` firing: every plan patches its own source
/// slot and one fire delivers all of them, so a batch costs one page fire
/// regardless of member count. Members are variable-to-variable transfers
/// (`transfers`) and variable-to-address/trigger-field stores (`stores`);
/// the legality rule that lets them share a fire is
/// [`eudkit_opt::transfer::can_coalesce_plan`]. A store's destination is not
/// a variable slot, so stores never affect the transfer hazard rules beyond
/// reading their `src_var`.
#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct HirVarBatch {
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub transfers: Vec<HirVarTransfer>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub stores: Vec<HirVarStore>,
}

impl HirVarBatch {
    /// A batch holding only transfers (the pre-#96 shape).
    pub fn transfers(transfers: Vec<HirVarTransfer>) -> Self {
        Self {
            transfers,
            stores: Vec::new(),
        }
    }

    /// Whether the batch has no members and would lower to nothing.
    pub fn is_empty(&self) -> bool {
        self.transfers.is_empty() && self.stores.is_empty()
    }

    /// Number of plan members (one page-firing slot each).
    pub fn len(&self) -> usize {
        self.transfers.len() + self.stores.len()
    }
}

/// Store a variable's value into an arbitrary 4-byte memory location.
///
/// Unlike [`HirVarTransfer`], the destination is not a scalar variable slot:
/// it is either a fixed byte address (`dst_addr`, e.g. the `CurrentPlayer`
/// slot) or a byte offset inside a trigger (`dst_trigger` + `dst_offset`,
/// e.g. a condition's `player` field). This is the runtime equivalent of
/// "write the value of `src_var` at `EPD(destination)`", used for dynamic
/// EPD indirection (unit-group pruning) and CP-scheduled block loops.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct HirVarStore {
    pub src_var: usize,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub dst_addr: Option<u32>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub dst_trigger: Option<usize>,
    #[serde(default)]
    pub dst_offset: usize,
    pub modifier: u8,
}

impl HirVarStore {
    /// Store `src_var`'s value at the 4-byte aligned `addr`.
    pub const fn to_addr(src_var: usize, addr: u32, modifier: u8) -> Self {
        Self {
            src_var,
            dst_addr: Some(addr),
            dst_trigger: None,
            dst_offset: 0,
            modifier,
        }
    }

    /// Store `src_var`'s value into `offset` bytes inside `trigger_id`.
    pub const fn to_trigger(
        src_var: usize,
        trigger_id: usize,
        offset: usize,
        modifier: u8,
    ) -> Self {
        Self {
            src_var,
            dst_addr: None,
            dst_trigger: Some(trigger_id),
            dst_offset: offset,
            modifier,
        }
    }
}

/// One argument passed to a [`HirCall`].
///
/// Constant arguments let call sites omit the transfer act that would
/// otherwise only load an immediate value into a scratch variable.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "lowercase")]
pub enum HirCallArg {
    Var { id: usize },
    Const { value: u32 },
}

impl HirCallArg {
    pub const fn var(id: usize) -> Self {
        Self::Var { id }
    }

    pub const fn constant(value: u32) -> Self {
        Self::Const { value }
    }
}

/// Invoke subroutine `func` with `args`, copying its result into `dst`.
///
/// `dst` is `None` when the subroutine's result is unused (or when the
/// subroutine has no result slot at all).
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct HirCall {
    pub func: usize,
    pub args: Vec<HirCallArg>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub dst: Option<usize>,
}

impl HirCall {
    pub fn new(func: usize, args: Vec<HirCallArg>, dst: Option<usize>) -> Self {
        Self { func, args, dst }
    }
}

/// A callable subroutine: a statement body emitted once per module.
///
/// Call sites transfer `args` into `params` in declaration order, jump to the
/// subroutine's single entry, and copy `result` back out on return. A body is
/// emitted once regardless of how many sites call it, so a shared kernel costs
/// O(K + M) triggers instead of O(K x M) inlined copies.
///
/// Subroutines cannot recurse: the return path is a single `next` pointer
/// patched by the call site, so a nested call would clobber the outer return
/// address. [`HirModule::validate`] rejects direct and indirect recursion.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct HirFunc {
    pub id: usize,
    /// Parameter variable IDs, bound positionally by each call's `args`.
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub params: Vec<usize>,
    /// Variable receiving the subroutine's result, if it has one.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub result: Option<usize>,
    pub body: Vec<HirStmt>,
}

impl HirFunc {
    pub fn new(id: usize, params: Vec<usize>, result: Option<usize>, body: Vec<HirStmt>) -> Self {
        Self {
            id,
            params,
            result,
            body,
        }
    }
}

/// A build-time file embedded into the output MPQ under `name`.
///
/// This is the general MPQ embed surface: `embed_file` / `embedFile` record
/// entries here and the build queues them as `MPQAddFile` (the `bgmplayer`
/// helper and the future dataDumper port both use it). `path` is recorded
/// exactly as the frontend authored it; the CLI resolves relative paths
/// against the compiling source's directory, so HIR artifacts stay
/// machine-independent.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct HirEmbeddedFile {
    /// Archived file name inside the MPQ (e.g. `"bgm"`).
    pub name: String,
    /// Source path as authored (relative paths are resolved by the CLI).
    pub path: String,
}

/// A relocatable raw data blob (`Db(bytes | size)`), lowered to one
/// `LirItem::Blob` appended after the execution triggers, `TriggerPage`s, and
/// the demoted Cell blob.
///
/// This is the writable data buffer seam #94 (unlimiter) and dataDumper need:
/// a frontend can reserve `size` bytes (`data` empty, zero-filled) or hand over
/// explicit bytes (`data`, base64; shorter payloads are zero-padded to `size`),
/// and every `SetMemory`/condition field can point at the blob (or an offset
/// inside it) through [`HirReloc::blob`]. Blob-internal dword fields can point
/// at another blob or a trigger through [`HirBlobReloc`].
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct HirBlob {
    /// Payload byte length written into the map payload.
    pub size: usize,
    /// Base64 payload bytes; may be shorter than `size` (zero-padded).
    #[serde(default, skip_serializing_if = "String::is_empty")]
    pub data: String,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub relocs: Vec<HirBlobReloc>,
}

impl HirBlob {
    /// A zero-filled blob of `size` bytes (`Db(size)`).
    pub const fn zeroed(size: usize) -> Self {
        Self {
            size,
            data: String::new(),
            relocs: Vec::new(),
        }
    }

    /// A blob holding `bytes` verbatim (`Db(bytes)`).
    pub fn from_bytes(bytes: &[u8]) -> Self {
        Self {
            size: bytes.len(),
            data: eudkit_lir::b64_encode(bytes),
            relocs: Vec::new(),
        }
    }

    pub fn with_relocs(mut self, relocs: Vec<HirBlobReloc>) -> Self {
        self.relocs = relocs;
        self
    }
}

/// A 4-byte pointer field inside a [`HirBlob`], relocated to another item.
///
/// Exactly one of `target_blob` / `target_trigger` is set; `target_offset` is
/// the byte offset inside that item (a single blob node can therefore link its
/// `next` pointer to the following node).
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct HirBlobReloc {
    /// Byte offset of the 4-byte field inside the blob.
    pub offset: usize,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub target_blob: Option<usize>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub target_trigger: Option<usize>,
    #[serde(default)]
    pub target_offset: usize,
    /// `1`: EPD relocation; `4`: byte-address pointer relocation.
    pub mode: i64,
}

impl HirBlobReloc {
    pub const fn blob(offset: usize, target_blob: usize, target_offset: usize, mode: i64) -> Self {
        Self {
            offset,
            target_blob: Some(target_blob),
            target_trigger: None,
            target_offset,
            mode,
        }
    }

    pub const fn trigger(
        offset: usize,
        target_trigger: usize,
        target_offset: usize,
        mode: i64,
    ) -> Self {
        Self {
            offset,
            target_blob: None,
            target_trigger: Some(target_trigger),
            target_offset,
            mode,
        }
    }
}

/// A compiled `buildModule` program containing declared variables and top-level
/// statements (`Trigger`s, unconditional `Act`s, and variable transfers/batches).
#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct HirModule {
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub vars: Vec<HirVarDecl>,
    pub stmts: Vec<HirStmt>,
    /// Callable subroutines. Each body is emitted once per module; see
    /// [`HirFunc`]. Order is declaration order; IDs are unique.
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub funcs: Vec<HirFunc>,
    /// Module-owned strings reserved in the map string table, 0-based.
    /// `FixedString(size)` force-appends a marker string here so the buffer has a
    /// stable string-table slot before lowering sees basemap STR offsets.
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub strings: Vec<String>,
    /// Files the map tail must add to the output MPQ (`MPQAddFile` equivalent).
    /// Order is declaration order; names are unique.
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub files: Vec<HirEmbeddedFile>,
    /// Raw data blobs (`Db`), appended to the payload in declaration order.
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub blobs: Vec<HirBlob>,
}

impl HirModule {
    pub fn new() -> Self {
        Self {
            vars: Vec::new(),
            stmts: Vec::new(),
            funcs: Vec::new(),
            strings: Vec::new(),
            files: Vec::new(),
            blobs: Vec::new(),
        }
    }

    pub fn validate(&self) -> Result<(), HirError> {
        let check_var = |var_id: usize| -> Result<(), HirError> {
            if self.vars.iter().any(|v| v.id == var_id) {
                Ok(())
            } else {
                Err(HirError::UnknownVarId(var_id))
            }
        };
        let check_string = |string_id: usize| -> Result<(), HirError> {
            if string_id < self.strings.len() {
                Ok(())
            } else {
                Err(HirError::UnknownStringId(string_id))
            }
        };
        let check_reloc = |r: &HirReloc, record_size: usize| -> Result<(), HirError> {
            let targets = usize::from(r.target_var.is_some())
                + usize::from(r.target_trigger.is_some())
                + usize::from(r.target_string.is_some())
                + usize::from(r.target_blob.is_some());
            if targets != 1 {
                return Err(HirError::InvalidReloc);
            }
            if let Some(vid) = r.target_var {
                check_var(vid)?;
            }
            if let Some(sid) = r.target_string {
                check_string(sid)?;
                // A string relocation patches an arbitrary 4-byte window
                // inside the record; only the window must fit. Sub-dword
                // offsets are allowed (issue #101).
                if r.field_offset
                    .checked_add(4)
                    .is_none_or(|end| end > record_size)
                {
                    return Err(HirError::InvalidStringReloc(r.field_offset, record_size));
                }
            }
            if let Some(bid) = r.target_blob {
                if bid >= self.blobs.len()
                    || r.field_offset % 4 != 0
                    || r.target_offset % 4 != 0
                {
                    return Err(HirError::InvalidBlobTarget(bid, r.field_offset));
                }
            }
            Ok(())
        };
        let check_act = |act: &HirAct| -> Result<(), HirError> {
            if let Some(sid) = act.str_index {
                check_string(sid)?;
            }
            for r in &act.relocs {
                check_reloc(r, eudkit_lir::ACT_BYTE_SIZE)?;
            }
            Ok(())
        };
        let check_cond = |cond: &HirCond| -> Result<(), HirError> {
            for r in &cond.relocs {
                check_reloc(r, eudkit_lir::COND_BYTE_SIZE)?;
            }
            Ok(())
        };
        let check_call = |call: &HirCall| -> Result<(), HirError> {
            let Some(func) = self.funcs.iter().find(|f| f.id == call.func) else {
                return Err(HirError::UnknownFuncId(call.func));
            };
            if call.args.len() != func.params.len() {
                return Err(HirError::FuncArgCount(
                    call.func,
                    call.args.len(),
                    func.params.len(),
                ));
            }
            for arg in &call.args {
                if let HirCallArg::Var { id } = arg {
                    check_var(*id)?;
                }
            }
            if let Some(dst) = call.dst {
                check_var(dst)?;
            }
            Ok(())
        };

        for v in &self.vars {
            if let Some(init) = &v.init_string {
                check_string(init.index)?;
                if v.storage.as_deref() != Some("page") {
                    return Err(HirError::StringInitRequiresPage(v.id));
                }
            }
        }

        let mut seen_files: std::collections::HashSet<&str> =
            std::collections::HashSet::with_capacity(self.files.len());
        for file in &self.files {
            if file.name.is_empty() {
                return Err(HirError::EmptyEmbeddedFileName);
            }
            if file.path.is_empty() {
                return Err(HirError::EmptyEmbeddedFilePath(file.name.clone()));
            }
            if !seen_files.insert(file.name.as_str()) {
                return Err(HirError::DuplicateEmbeddedFileName(file.name.clone()));
            }
        }

        for (index, blob) in self.blobs.iter().enumerate() {
            let decoded_len = match eudkit_lir::b64_decode(&blob.data) {
                Ok(bytes) => bytes.len(),
                Err(_) => return Err(HirError::InvalidBlobData(index)),
            };
            if decoded_len > blob.size {
                return Err(HirError::InvalidBlobData(index));
            }
            for reloc in &blob.relocs {
                let targets = usize::from(reloc.target_blob.is_some())
                    + usize::from(reloc.target_trigger.is_some());
                let aligned = reloc.offset % 4 == 0 && reloc.offset + 4 <= blob.size;
                let target_ok = match reloc.target_blob {
                    Some(bid) => bid < self.blobs.len(),
                    None => true,
                };
                if targets != 1 || !aligned || !target_ok {
                    return Err(HirError::InvalidBlobReloc(index, reloc.offset));
                }
            }
        }

        fn validate_stmt(
            stmt: &HirStmt,
            check_var: &impl Fn(usize) -> Result<(), HirError>,
            check_act: &impl Fn(&HirAct) -> Result<(), HirError>,
            check_cond: &impl Fn(&HirCond) -> Result<(), HirError>,
            check_call: &impl Fn(&HirCall) -> Result<(), HirError>,
        ) -> Result<(), HirError> {
            match stmt {
                HirStmt::Trigger(trig) => {
                    trig.validate()?;
                    for c in &trig.conds {
                        check_cond(c)?;
                    }
                    for a in &trig.acts {
                        check_act(a)?;
                    }
                }
                HirStmt::Act(act) => {
                    check_act(act)?;
                }
                HirStmt::VarTransfer(t) => {
                    check_var(t.dst_var)?;
                    check_var(t.src_var)?;
                }
                HirStmt::VarBatch(b) => {
                    for t in &b.transfers {
                        check_var(t.dst_var)?;
                        check_var(t.src_var)?;
                    }
                    for s in &b.stores {
                        check_var(s.src_var)?;
                        if s.dst_addr.is_some() == s.dst_trigger.is_some() {
                            return Err(HirError::InvalidVarStore);
                        }
                    }
                }
                HirStmt::VarStore(s) => {
                    check_var(s.src_var)?;
                    if s.dst_addr.is_some() == s.dst_trigger.is_some() {
                        return Err(HirError::InvalidVarStore);
                    }
                }
                HirStmt::Call(c) => {
                    check_call(c)?;
                }
                HirStmt::When(w) => {
                    for c in &w.conds {
                        check_cond(c)?;
                    }
                    for s in &w.body {
                        validate_stmt(s, check_var, check_act, check_cond, check_call)?;
                    }
                    for s in &w.else_body {
                        validate_stmt(s, check_var, check_act, check_cond, check_call)?;
                    }
                }
                HirStmt::Once(o) => {
                    for c in &o.conds {
                        check_cond(c)?;
                    }
                    for s in &o.body {
                        validate_stmt(s, check_var, check_act, check_cond, check_call)?;
                    }
                }
                HirStmt::While(w) => {
                    for c in &w.conds {
                        check_cond(c)?;
                    }
                    for s in &w.body {
                        validate_stmt(s, check_var, check_act, check_cond, check_call)?;
                    }
                }
                HirStmt::Until(u) => {
                    check_var(u.latch_var)?;
                    for c in &u.conds {
                        check_cond(c)?;
                    }
                    for s in &u.body {
                        validate_stmt(s, check_var, check_act, check_cond, check_call)?;
                    }
                }
                HirStmt::Edge(e) => {
                    check_var(e.curr_var)?;
                    check_var(e.prev_var)?;
                    for c in &e.conds {
                        check_cond(c)?;
                    }
                    for s in &e.body {
                        validate_stmt(s, check_var, check_act, check_cond, check_call)?;
                    }
                }
                HirStmt::Every(e) => {
                    // `n` is u32, so negatives already fail loud at serde
                    // parse; only `n = 0` needs this bounds-style check.
                    if e.n == 0 {
                        return Err(HirError::NonPositiveCount(e.n));
                    }
                    check_var(e.counter_var)?;
                    for s in &e.body {
                        validate_stmt(s, check_var, check_act, check_cond, check_call)?;
                    }
                }
                HirStmt::After(a) => {
                    if a.n == 0 {
                        return Err(HirError::NonPositiveCount(a.n));
                    }
                    check_var(a.counter_var)?;
                    for s in &a.body {
                        validate_stmt(s, check_var, check_act, check_cond, check_call)?;
                    }
                }
            }
            Ok(())
        }

        for stmt in &self.stmts {
            validate_stmt(stmt, &check_var, &check_act, &check_cond, &check_call)?;
        }

        let mut seen_funcs: std::collections::HashSet<usize> =
            std::collections::HashSet::with_capacity(self.funcs.len());
        for func in &self.funcs {
            if !seen_funcs.insert(func.id) {
                return Err(HirError::DuplicateFuncId(func.id));
            }
            for param in &func.params {
                check_var(*param)?;
            }
            if let Some(result) = func.result {
                check_var(result)?;
            }
            for stmt in &func.body {
                validate_stmt(stmt, &check_var, &check_act, &check_cond, &check_call)?;
            }
        }

        // Subroutines share one return pointer patched per call site, so a
        // recursive call would clobber its caller's return address. Reject
        // direct and indirect recursion up front instead of emitting a
        // subroutine that silently jumps back into its middle.
        fn collect_callees(stmt: &HirStmt, out: &mut Vec<usize>) {
            fn walk(stmts: &[HirStmt], out: &mut Vec<usize>) {
                for stmt in stmts {
                    collect_callees(stmt, out);
                }
            }
            match stmt {
                HirStmt::Call(c) => out.push(c.func),
                HirStmt::When(w) => {
                    walk(&w.body, out);
                    walk(&w.else_body, out);
                }
                HirStmt::Once(o) => walk(&o.body, out),
                HirStmt::While(w) => walk(&w.body, out),
                HirStmt::Until(u) => walk(&u.body, out),
                HirStmt::Edge(e) => walk(&e.body, out),
                HirStmt::Every(e) => walk(&e.body, out),
                HirStmt::After(a) => walk(&a.body, out),
                HirStmt::Trigger(_)
                | HirStmt::Act(_)
                | HirStmt::VarTransfer(_)
                | HirStmt::VarBatch(_)
                | HirStmt::VarStore(_) => {}
            }
        }

        let mut callees: std::collections::BTreeMap<usize, Vec<usize>> =
            std::collections::BTreeMap::new();
        for func in &self.funcs {
            let mut calls = Vec::new();
            for stmt in &func.body {
                collect_callees(stmt, &mut calls);
            }
            callees.insert(func.id, calls);
        }

        fn is_recursive(
            callees: &std::collections::BTreeMap<usize, Vec<usize>>,
            start: usize,
            node: usize,
            seen: &mut std::collections::HashSet<usize>,
        ) -> bool {
            if !seen.insert(node) {
                return false;
            }
            let empty: &[usize] = &[];
            for &next in callees.get(&node).map(Vec::as_slice).unwrap_or(empty) {
                if next == start || is_recursive(callees, start, next, seen) {
                    return true;
                }
            }
            false
        }

        for func in &self.funcs {
            let mut seen = std::collections::HashSet::new();
            if is_recursive(&callees, func.id, func.id, &mut seen) {
                return Err(HirError::RecursiveFunc(func.id));
            }
        }
        Ok(())
    }
}

/// A conditional execution block (`when`).
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct HirWhen {
    pub conds: Vec<HirCond>,
    pub body: Vec<HirStmt>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub else_body: Vec<HirStmt>,
}

impl HirWhen {
    pub fn new(conds: Vec<HirCond>, body: Vec<HirStmt>) -> Self {
        Self {
            conds,
            body,
            else_body: Vec::new(),
        }
    }

    pub fn with_else(conds: Vec<HirCond>, body: Vec<HirStmt>, else_body: Vec<HirStmt>) -> Self {
        Self {
            conds,
            body,
            else_body,
        }
    }
}

/// A one-shot conditional execution block (`once`).
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct HirOnce {
    pub conds: Vec<HirCond>,
    pub body: Vec<HirStmt>,
}

impl HirOnce {
    pub fn new(conds: Vec<HirCond>, body: Vec<HirStmt>) -> Self {
        Self { conds, body }
    }
}

/// A repeated execution loop (`whileLoop`).
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct HirWhile {
    pub conds: Vec<HirCond>,
    pub body: Vec<HirStmt>,
}

impl HirWhile {
    pub fn new(conds: Vec<HirCond>, body: Vec<HirStmt>) -> Self {
        Self { conds, body }
    }
}

/// A latch-off conditional execution block (`until`).
///
/// Runs `body` while `conds` are false and latches off permanently once
/// they hold. The opt desugar (C11-02) turns it into today's 2-`when`
/// shape, so lowering never sees it.
///
/// `latch_var` is the latch state variable, reserved by the DSL at
/// node-emission time: the DSL allocates it exactly where today's
/// desugar allocated `active`, so the declaration (and therefore the
/// StoragePlan rank, which is assigned in id order) is identical to
/// the DSL-desugar baseline. The opt desugar uses this id verbatim; it
/// never mints fresh ids. Minting in opt cannot preserve rank order:
/// opt's `next_id` is a trigger-id allocator (seeded from the max
/// trigger id), `optimize_stmts` has no channel to `module.vars`, and
/// appended-late ids would sort last in `analyze_storage`'s id-order
/// ranking, shifting ranks whenever a user variable is allocated after
/// the until call. The design doc's Q6 mechanism (`next_id` + appended
/// `HirVarDecl`s) is therefore corrected here, not followed literally;
/// the acceptance (byte-identical output) is what is preserved.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct HirUntil {
    pub conds: Vec<HirCond>,
    pub body: Vec<HirStmt>,
    /// State variable holding the latch (init 1, set to 0 once `conds` hold).
    pub latch_var: usize,
}

impl HirUntil {
    pub fn new(conds: Vec<HirCond>, body: Vec<HirStmt>, latch_var: usize) -> Self {
        Self {
            conds,
            body,
            latch_var,
        }
    }
}

/// Edge polarity for [`HirEdge`]: a false-to-true or true-to-false
/// transition of `conds` between consecutive loop passes.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum HirEdgeDirection {
    Rising,
    Falling,
}

/// A transition-triggered execution block (`onRising` / `onFalling`).
///
/// One node for both polarities: rising and falling share all logic
/// except comparison polarity. The opt desugar (C11-03) turns it into
/// today's 5-`when` shape.
///
/// `curr_var` / `prev_var` are the edge's two state variables, reserved
/// by the DSL at node-emission time: the DSL allocates them exactly
/// where today's desugar allocated `curr` / `prev`, so the declarations
/// (and therefore the StoragePlan ranks, which are assigned in id order)
/// are identical to the DSL-desugar baseline. The opt desugar uses these
/// ids verbatim; it never mints fresh ids. Minting in opt cannot preserve
/// rank order: opt's `next_id` is a trigger-id allocator (seeded from the
/// max trigger id), `optimize_stmts` has no channel to `module.vars`,
/// and appended-late ids would sort last in `analyze_storage`'s id-order
/// ranking, shifting ranks whenever a user variable is allocated after
/// the edge call. The design doc's Q6 mechanism (`next_id` + appended
/// `HirVarDecl`s) is therefore corrected here, not followed literally;
/// the acceptance (byte-identical output) is what is preserved.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct HirEdge {
    pub direction: HirEdgeDirection,
    pub conds: Vec<HirCond>,
    pub body: Vec<HirStmt>,
    /// State variable holding the current sampled value (0/1).
    pub curr_var: usize,
    /// State variable holding the previous loop's sampled value (0/1).
    pub prev_var: usize,
}

impl HirEdge {
    pub fn new(
        direction: HirEdgeDirection,
        conds: Vec<HirCond>,
        body: Vec<HirStmt>,
        curr_var: usize,
        prev_var: usize,
    ) -> Self {
        Self {
            direction,
            conds,
            body,
            curr_var,
            prev_var,
        }
    }
}

/// A periodic execution block (`every(n)`): runs `body` once every `n`
/// loop passes. `n` is a build-time positive integer, validated both by
/// the DSL at authoring time and here at HIR validation (`n = 0` is a
/// bounds-style rejection; negatives already fail loud at serde parse
/// since `n: u32`).
///
/// `counter_var` is the counter variable allocated by the DSL at node
/// emission time, in the old allocation order (C11-04, corrected
/// mechanism per the C11-03 finding: opt never mints var ids — see
/// `eudkit_opt::temporal::desugar_every`). It must name a declared
/// variable; validation enforces that reference integrity.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct HirEvery {
    pub n: u32,
    pub counter_var: usize,
    pub body: Vec<HirStmt>,
}

impl HirEvery {
    pub fn new(n: u32, counter_var: usize, body: Vec<HirStmt>) -> Self {
        Self {
            n,
            counter_var,
            body,
        }
    }
}

/// A delayed one-shot execution block (`after(n)`): runs `body` once,
/// after `n` loop passes. See [`HirEvery`] for the `n` validation and
/// `counter_var` notes.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct HirAfter {
    pub n: u32,
    pub counter_var: usize,
    pub body: Vec<HirStmt>,
}

impl HirAfter {
    pub fn new(n: u32, counter_var: usize, body: Vec<HirStmt>) -> Self {
        Self {
            n,
            counter_var,
            body,
        }
    }
}

/// Top-level statement in a `buildModule`.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "lowercase")]
pub enum HirStmt {
    Trigger(HirTrigger),
    Act(HirAct),
    VarTransfer(HirVarTransfer),
    VarBatch(HirVarBatch),
    VarStore(HirVarStore),
    Call(HirCall),
    When(HirWhen),
    Once(HirOnce),
    While(HirWhile),
    Until(HirUntil),
    Edge(HirEdge),
    Every(HirEvery),
    After(HirAfter),
}

/// Every trigger id a module names explicitly, plus the maximum id any trigger
/// statement declares.
///
/// One collector serves every pass that needs trigger identity: dead-gate
/// removal (which may not drop a referenced empty gate), the backward `when`
/// merge and call inlining (which may neither duplicate nor remint a
/// referenced id), and lowering's unpack predicate (which may not fold a
/// referenced trigger into a basic block). A single walk over `stmts`, every
/// function body, and every `blobs[].relocs` is what keeps those paths from
/// drifting apart: a reference form added to one call site must be visible to
/// all of them.
#[derive(Debug, Default)]
pub struct TriggerRefs {
    referenced: HashSet<usize>,
    max_id: Option<usize>,
}

impl TriggerRefs {
    /// Collect from a whole module: root statements, function bodies, and blob
    /// relocations.
    pub fn from_module(module: &HirModule) -> Self {
        let mut refs = Self::default();
        for stmt in &module.stmts {
            refs.collect_stmt(stmt);
        }
        for func in &module.funcs {
            for stmt in &func.body {
                refs.collect_stmt(stmt);
            }
        }
        for blob in &module.blobs {
            for reloc in &blob.relocs {
                if let Some(id) = reloc.target_trigger {
                    refs.referenced.insert(id);
                }
            }
        }
        refs
    }

    /// Collect from one statement list. Used by callers that hold only a
    /// block, not a whole [`HirModule`].
    pub fn from_stmts(stmts: &[HirStmt]) -> Self {
        let mut refs = Self::default();
        for stmt in stmts {
            refs.collect_stmt(stmt);
        }
        refs
    }

    /// Whether the module names `id` from a chain pointer, a condition/action
    /// relocation, a trigger-destination store, or a blob relocation.
    pub fn contains(&self, id: usize) -> bool {
        self.referenced.contains(&id)
    }

    /// One above the module-wide maximum trigger id *declared by a trigger
    /// statement*: the seed for reminting a later inline copy's retained ids.
    ///
    /// Reference targets that are not themselves declared as triggers (a blob
    /// relocation, for example) do not advance this value.
    pub fn next_id(&self) -> usize {
        self.max_id.map_or(0, |max_id| max_id + 1)
    }

    fn collect_relocs(&mut self, relocs: &[HirReloc]) {
        for reloc in relocs {
            if let Some(id) = reloc.target_trigger {
                self.referenced.insert(id);
            }
        }
    }

    fn collect_conds(&mut self, conds: &[HirCond]) {
        for cond in conds {
            self.collect_relocs(&cond.relocs);
        }
    }

    fn collect_trigger(&mut self, trig: &HirTrigger) {
        self.max_id = Some(self.max_id.map_or(trig.id, |max_id| max_id.max(trig.id)));
        for ptr in [&trig.prev, &trig.next].into_iter().flatten() {
            if let HirTriggerPtr::Trigger { id, .. } = ptr {
                self.referenced.insert(*id);
            }
        }
        self.collect_conds(&trig.conds);
        for act in &trig.acts {
            self.collect_relocs(&act.relocs);
        }
    }

    fn collect_store(&mut self, store: &HirVarStore) {
        if let Some(id) = store.dst_trigger {
            self.referenced.insert(id);
        }
    }

    fn collect_stmt(&mut self, stmt: &HirStmt) {
        match stmt {
            HirStmt::Trigger(trig) => self.collect_trigger(trig),
            HirStmt::Act(act) => self.collect_relocs(&act.relocs),
            HirStmt::VarStore(store) => self.collect_store(store),
            HirStmt::VarBatch(batch) => {
                for store in &batch.stores {
                    self.collect_store(store);
                }
            }
            HirStmt::When(w) => {
                self.collect_conds(&w.conds);
                for stmt in &w.body {
                    self.collect_stmt(stmt);
                }
                for stmt in &w.else_body {
                    self.collect_stmt(stmt);
                }
            }
            HirStmt::Once(o) => {
                self.collect_conds(&o.conds);
                for stmt in &o.body {
                    self.collect_stmt(stmt);
                }
            }
            HirStmt::While(w) => {
                self.collect_conds(&w.conds);
                for stmt in &w.body {
                    self.collect_stmt(stmt);
                }
            }
            HirStmt::Until(u) => {
                self.collect_conds(&u.conds);
                for stmt in &u.body {
                    self.collect_stmt(stmt);
                }
            }
            HirStmt::Edge(e) => {
                self.collect_conds(&e.conds);
                for stmt in &e.body {
                    self.collect_stmt(stmt);
                }
            }
            HirStmt::Every(e) => {
                for stmt in &e.body {
                    self.collect_stmt(stmt);
                }
            }
            HirStmt::After(a) => {
                for stmt in &a.body {
                    self.collect_stmt(stmt);
                }
            }
            HirStmt::VarTransfer(_) | HirStmt::Call(_) => {}
        }
    }
}

/// Symbolic or constant pointer stored in `Trigger.prev` or `Trigger.next`.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "lowercase")]
pub enum HirTriggerPtr {
    Const { value: i64 },
    Trigger { id: usize, offset: usize },
}

/// Symbolic relocation attached to a condition or action field.
///
/// `mode == 1`: EPD relocation (`(addr - 0x58A364) / 4`)
/// `mode == 4`: byte-address pointer relocation (`addr`)
/// `mode == 6`: 16-bit wire string id (`template_count + index + 1`), shifted
/// left by `target_offset` bits and written into the field. `target_offset`
/// is a **bit shift**, not a byte offset, for this mode only.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct HirReloc {
    pub field_offset: usize,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub target_trigger: Option<usize>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub target_var: Option<usize>,
    /// 0-based module string index; lowering folds it into the field as the
    /// absolute string address (`mode == 4`) or its EPD form (`mode == 1`).
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub target_string: Option<usize>,
    /// 0-based module blob index; lowering folds it into the field as the
    /// blob's payload address (`mode == 4`) or its EPD form (`mode == 1`).
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub target_blob: Option<usize>,
    #[serde(default)]
    pub target_offset: usize,
    pub mode: i64,
}

impl HirReloc {
    pub const fn trigger(
        field_offset: usize,
        target_trigger: usize,
        target_offset: usize,
        mode: i64,
    ) -> Self {
        Self {
            field_offset,
            target_trigger: Some(target_trigger),
            target_var: None,
            target_string: None,
            target_blob: None,
            target_offset,
            mode,
        }
    }

    pub const fn var(
        field_offset: usize,
        target_var: usize,
        target_offset: usize,
        mode: i64,
    ) -> Self {
        Self {
            field_offset,
            target_trigger: None,
            target_var: Some(target_var),
            target_string: None,
            target_blob: None,
            target_offset,
            mode,
        }
    }

    /// Relocate a 4-byte action/condition field to a module string buffer.
    pub const fn string(
        field_offset: usize,
        target_string: usize,
        target_offset: usize,
        mode: i64,
    ) -> Self {
        Self {
            field_offset,
            target_trigger: None,
            target_var: None,
            target_string: Some(target_string),
            target_blob: None,
            target_offset,
            mode,
        }
    }

    /// Relocate a 4-byte action/condition field to a module data blob.
    pub const fn blob(
        field_offset: usize,
        target_blob: usize,
        target_offset: usize,
        mode: i64,
    ) -> Self {
        Self {
            field_offset,
            target_trigger: None,
            target_var: None,
            target_string: None,
            target_blob: Some(target_blob),
            target_offset,
            mode,
        }
    }
}

/// First-class `Trigger (16C, 64A)` node in HIR 3.0.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct HirTrigger {
    pub id: usize,
    pub conds: Vec<HirCond>,
    pub acts: Vec<HirAct>,
    pub preserved: bool,
    pub flags: u32,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub prev: Option<HirTriggerPtr>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub next: Option<HirTriggerPtr>,
}

impl HirTrigger {
    pub fn new(
        id: usize,
        conds: Vec<HirCond>,
        acts: Vec<HirAct>,
        preserved: bool,
    ) -> Result<Self, HirError> {
        let flags = if preserved { 4 } else { 0 };
        let trig = Self {
            id,
            conds,
            acts,
            preserved,
            flags,
            prev: None,
            next: None,
        };
        trig.validate()?;
        Ok(trig)
    }

    pub fn validate(&self) -> Result<(), HirError> {
        if self.conds.len() > COND_COUNT {
            return Err(HirError::TooManyConditions(self.conds.len()));
        }
        if self.acts.len() > ACT_COUNT {
            return Err(HirError::TooManyActions(self.acts.len()));
        }
        Ok(())
    }

    /// The one shape half of "may this trigger fold into the surrounding basic
    /// block?": unconditional, chain-free, preserved, and exactly the
    /// preserved flag-`4` encoding.
    ///
    /// This is deliberately a trigger property, not a block- or module-level
    /// one: whether a `VarBatch`/`VarTransfer`/`VarStore` in the same statement
    /// list can receive the folded acts stays a lowering decision (the
    /// `has_var_batch` gate). Use [`Self::is_unpackable`] when the trigger id
    /// must also be unnamed.
    pub fn unpack_shape(&self) -> bool {
        self.conds.is_empty()
            && self.prev.is_none()
            && self.next.is_none()
            && self.preserved
            && self.flags == 4
    }

    /// The one reference-aware half of the unpack predicate: the
    /// [`Self::unpack_shape`] shape *and* an id no module name refers to.
    ///
    /// Folding a referenced trigger into a basic block would delete the named
    /// node and leave the chain pointer, relocation, store, or blob relocation
    /// dangling, so references are a hard barrier.
    pub fn is_unpackable(&self, refs: &TriggerRefs) -> bool {
        self.unpack_shape() && !refs.contains(self.id)
    }
}

/// A single condition (`20` bytes = `5` EPDs) in a `Trigger (16C, 64A)`.
#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct HirCond {
    pub locid: u32,
    pub player: u32,
    pub amount: u32,
    pub unit: u16,
    pub comparison: u8,
    pub condtype: u8,
    pub restype: u8,
    pub flags: u8,
    pub eudx: u16,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub relocs: Vec<HirReloc>,
}

impl HirCond {
    pub const fn always() -> Self {
        Self {
            locid: 0,
            player: 0,
            amount: 0,
            unit: 0,
            comparison: 0,
            condtype: 22,
            restype: 0,
            flags: 0,
            eudx: 0,
            relocs: Vec::new(),
        }
    }

    pub const fn never() -> Self {
        Self {
            locid: 0,
            player: 0,
            amount: 0,
            unit: 0,
            comparison: 0,
            condtype: 23,
            restype: 0,
            flags: 0,
            eudx: 0,
            relocs: Vec::new(),
        }
    }

    pub const fn deaths(player: u32, comparison: u8, amount: u32, unit: u16) -> Self {
        Self {
            locid: 0,
            player,
            amount,
            unit,
            comparison,
            condtype: 15,
            restype: 0,
            flags: 0,
            eudx: 0,
            relocs: Vec::new(),
        }
    }

    pub const fn deathsx(player: u32, comparison: u8, amount: u32, unit: u16, mask: u32) -> Self {
        Self {
            locid: mask,
            player,
            amount,
            unit,
            comparison,
            condtype: 15,
            restype: 0,
            flags: 0,
            eudx: EUDX_MAGIC,
            relocs: Vec::new(),
        }
    }

    pub fn var_deaths(var_id: usize, comparison: u8, amount: u32) -> Self {
        Self {
            locid: 0,
            player: 0,
            amount,
            unit: 0,
            comparison,
            condtype: 15,
            restype: 0,
            flags: 0,
            eudx: 0,
            relocs: vec![HirReloc::var(4, var_id, 20, 1)],
        }
    }

    pub fn var_deathsx(var_id: usize, comparison: u8, amount: u32, mask: u32) -> Self {
        Self {
            locid: mask,
            player: 0,
            amount,
            unit: 0,
            comparison,
            condtype: 15,
            restype: 0,
            flags: 0,
            eudx: EUDX_MAGIC,
            relocs: vec![HirReloc::var(4, var_id, 20, 1)],
        }
    }

    pub fn to_ctrig_condition(&self) -> CTrigCondition {
        CTrigCondition {
            locid: self.locid,
            player: self.player,
            amount: self.amount,
            unit: self.unit,
            comparison: self.comparison,
            condtype: self.condtype,
            restype: self.restype,
            flags: self.flags,
            eudx: self.eudx,
        }
    }
}

/// A single action (`32` bytes = `8` EPDs) in a `Trigger (16C, 64A)`.
#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct HirAct {
    pub mask: u32,
    pub strid: u32,
    pub wavid: u32,
    pub time: u32,
    pub dest: u32,
    pub value: u32,
    pub unit: u16,
    pub acttype: u8,
    pub modifier: u8,
    pub flags: u8,
    pub padding: u8,
    pub eudx: u16,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub text: Option<String>,
    /// Optional WAV sound name, resolved to the action's `wavid` field.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub wav: Option<String>,
    /// Normalized 20-byte UPRP record for a `CreateUnitWithProperties` action.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub uprp: Option<[u8; 20]>,
    /// 0-based module string index displayed by a `DisplayText` action.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub str_index: Option<usize>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub relocs: Vec<HirReloc>,
}

impl HirAct {
    pub const fn set_deaths(dest: u32, modifier: u8, value: u32, unit: u16) -> Self {
        Self {
            mask: 0,
            strid: 0,
            wavid: 0,
            time: 0,
            dest,
            value,
            unit,
            acttype: 45,
            modifier,
            flags: 20,
            padding: 0,
            eudx: 0,
            text: None,
            wav: None,
            uprp: None,
            str_index: None,
            relocs: Vec::new(),
        }
    }

    pub const fn set_deathsx(dest: u32, modifier: u8, value: u32, unit: u16, mask: u32) -> Self {
        Self {
            mask,
            strid: 0,
            wavid: 0,
            time: 0,
            dest,
            value,
            unit,
            acttype: 45,
            modifier,
            flags: 20,
            padding: 0,
            eudx: EUDX_MAGIC,
            text: None,
            wav: None,
            uprp: None,
            str_index: None,
            relocs: Vec::new(),
        }
    }

    pub fn display_text(text: impl Into<String>, always_display: u8) -> Self {
        Self {
            mask: 0,
            strid: 0,
            wavid: 0,
            time: 0,
            dest: 0,
            value: 0,
            unit: 0,
            acttype: 9,
            modifier: 0,
            flags: always_display,
            padding: 0,
            eudx: 0,
            text: Some(text.into()),
            wav: None,
            uprp: None,
            str_index: None,
            relocs: Vec::new(),
        }
    }

    pub fn set_var_const(var_id: usize, modifier: u8, value: u32, mask: u32) -> Self {
        Self {
            mask,
            strid: 0,
            wavid: 0,
            time: 0,
            dest: 0,
            value,
            unit: 0,
            acttype: 45,
            modifier,
            flags: 20,
            padding: 0,
            eudx: EUDX_MAGIC,
            text: None,
            wav: None,
            uprp: None,
            str_index: None,
            relocs: vec![HirReloc::var(16, var_id, 20, 1)],
        }
    }

    pub fn to_ctrig_action(&self) -> CTrigAction {
        CTrigAction {
            mask: self.mask,
            strid: self.strid,
            wavid: self.wavid,
            time: self.time,
            dest: self.dest,
            value: self.value,
            unit: self.unit,
            acttype: self.acttype,
            modifier: self.modifier,
            flags: self.flags,
            padding: self.padding,
            eudx: self.eudx,
        }
    }
}

/// Serialize a [`HirDocument`] to canonical compact JSON.
pub fn hir_to_json(doc: &HirDocument) -> Result<String, HirError> {
    doc.validate()?;
    serde_json::to_string(doc).map_err(|e| HirError::Json(e.to_string()))
}

/// Deserialize and validate a [`HirDocument`] from JSON.
pub fn hir_from_json(json: &str) -> Result<HirDocument, HirError> {
    let doc: HirDocument = serde_json::from_str(json).map_err(|e| HirError::Json(e.to_string()))?;
    doc.validate()?;
    Ok(doc)
}

#[cfg(test)]
mod tests {
    use super::*;

    /// The one shape predicate: an unconditional, chain-free, preserved
    /// flag-`4` trigger. Each field is flipped in isolation so a relaxation of
    /// any one of them is caught.
    #[test]
    fn unpack_shape_is_the_unconditional_preserved_flag4_block() {
        let mut trig = HirTrigger::new(
            7,
            Vec::new(),
            vec![HirAct::set_deaths(0, 7, 1, 0)],
            true,
        )
        .unwrap();
        assert!(trig.unpack_shape());

        trig.conds.push(HirCond::always());
        assert!(!trig.unpack_shape(), "a condition makes it conditional");
        trig.conds.clear();

        trig.prev = Some(HirTriggerPtr::Const { value: 0 });
        assert!(!trig.unpack_shape(), "an explicit prev is a chain pointer");
        trig.prev = None;

        trig.next = Some(HirTriggerPtr::Const { value: 0 });
        assert!(!trig.unpack_shape(), "an explicit next is a chain pointer");
        trig.next = None;

        trig.preserved = false;
        assert!(!trig.unpack_shape(), "a self-disabling block is not foldable");
        trig.preserved = true;

        trig.flags = 0;
        assert!(!trig.unpack_shape(), "only the preserved flag-4 shape folds");
        trig.flags = 4;
        assert!(trig.unpack_shape());
    }

    /// The one reference-aware predicate: the shared shape plus "no module
    /// name refers to this id".
    #[test]
    fn is_unpackable_requires_an_unnamed_id() {
        let trig = HirTrigger::new(
            7,
            Vec::new(),
            vec![HirAct::set_deaths(0, 7, 1, 0)],
            true,
        )
        .unwrap();
        let unnamed = TriggerRefs::from_stmts(&[]);
        assert!(trig.is_unpackable(&unnamed));

        let mut referrer = HirAct::set_deaths(0, 7, 1, 0);
        referrer.relocs.push(HirReloc::trigger(0, 7, 0, 1));
        let refs = TriggerRefs::from_stmts(&[HirStmt::Act(referrer)]);
        assert!(refs.contains(7));
        assert!(!trig.is_unpackable(&refs), "a named id must not vanish");

        let conditional = HirTrigger::new(8, vec![HirCond::always()], Vec::new(), true).unwrap();
        assert!(!conditional.is_unpackable(&unnamed));
    }

    #[test]
    fn hir_trigger_allows_up_to_16_conditions_and_64_actions() {
        let trig = HirTrigger::new(
            0,
            vec![HirCond::always(); 16],
            vec![HirAct::set_deaths(0, 7, 1, 0); 64],
            true,
        )
        .unwrap();
        let doc = HirDocument::new(HirModule {
            vars: Vec::new(),
            stmts: vec![HirStmt::Trigger(trig)],

            strings: Vec::new(),
            files: Vec::new(),
            funcs: Vec::new(),
            blobs: Vec::new(),
        });
        let json = hir_to_json(&doc).unwrap();
        let parsed = hir_from_json(&json).unwrap();
        assert_eq!(parsed, doc);
    }

    fn func_module(funcs: Vec<HirFunc>, stmts: Vec<HirStmt>) -> HirModule {
        HirModule {
            funcs,
            vars: (0..8).map(|id| HirVarDecl::new(id, 0)).collect(),
            stmts,
            strings: Vec::new(),
            files: Vec::new(),
            blobs: Vec::new(),
        }
    }

    fn body_copy(param: usize, result: usize) -> Vec<HirStmt> {
        vec![HirStmt::VarTransfer(HirVarTransfer {
            dst_var: result,
            src_var: param,
            modifier: 7,
        })]
    }

    #[test]
    fn func_calls_validate_and_round_trip_through_json() {
        let module = func_module(
            vec![HirFunc::new(100, vec![0], Some(1), body_copy(0, 1))],
            vec![
                HirStmt::Call(HirCall::new(100, vec![HirCallArg::constant(7)], Some(2))),
                HirStmt::Call(HirCall::new(100, vec![HirCallArg::var(3)], None)),
            ],
        );
        module.validate().unwrap();
        let doc = HirDocument::new(module);
        let json = hir_to_json(&doc).unwrap();
        assert!(json.contains("\"funcs\""), "{json}");
        assert!(json.contains("\"kind\":\"const\""), "{json}");
        let parsed = hir_from_json(&json).unwrap();
        assert_eq!(parsed, doc);
    }

    #[test]
    fn rejects_unknown_func_id() {
        let module = func_module(
            Vec::new(),
            vec![HirStmt::Call(HirCall::new(7, Vec::new(), None))],
        );
        assert_eq!(module.validate().unwrap_err(), HirError::UnknownFuncId(7));
    }

    #[test]
    fn rejects_wrong_argument_count() {
        let module = func_module(
            vec![HirFunc::new(0, vec![0, 1], Some(2), body_copy(0, 2))],
            vec![HirStmt::Call(HirCall::new(0, vec![HirCallArg::var(3)], Some(4)))],
        );
        assert_eq!(
            module.validate().unwrap_err(),
            HirError::FuncArgCount(0, 1, 2)
        );
    }

    #[test]
    fn rejects_duplicate_func_id() {
        let module = func_module(
            vec![
                HirFunc::new(0, Vec::new(), None, Vec::new()),
                HirFunc::new(0, Vec::new(), None, Vec::new()),
            ],
            Vec::new(),
        );
        assert_eq!(module.validate().unwrap_err(), HirError::DuplicateFuncId(0));
    }

    #[test]
    fn rejects_direct_and_indirect_recursion() {
        let direct = func_module(
            vec![HirFunc::new(
                0,
                Vec::new(),
                None,
                vec![HirStmt::Call(HirCall::new(0, Vec::new(), None))],
            )],
            Vec::new(),
        );
        assert_eq!(direct.validate().unwrap_err(), HirError::RecursiveFunc(0));

        // 1 -> 2 -> 3 -> 1 (nested inside a `when` body: the walk is recursive).
        let indirect = func_module(
            vec![
                HirFunc::new(
                    1,
                    Vec::new(),
                    None,
                    vec![HirStmt::Call(HirCall::new(2, Vec::new(), None))],
                ),
                HirFunc::new(
                    2,
                    Vec::new(),
                    None,
                    vec![HirStmt::Call(HirCall::new(3, Vec::new(), None))],
                ),
                HirFunc::new(
                    3,
                    Vec::new(),
                    None,
                    vec![HirStmt::When(HirWhen::new(
                        vec![HirCond::always()],
                        vec![HirStmt::Call(HirCall::new(1, Vec::new(), None))],
                    ))],
                ),
            ],
            Vec::new(),
        );
        assert_eq!(indirect.validate().unwrap_err(), HirError::RecursiveFunc(1));
    }

    #[test]
    fn non_recursive_call_chain_and_repeated_calls_validate() {
        // 1 -> 2, and 2 called from two sites: legal (no cycle).
        let module = func_module(
            vec![
                HirFunc::new(
                    1,
                    Vec::new(),
                    None,
                    vec![HirStmt::Call(HirCall::new(2, Vec::new(), None))],
                ),
                HirFunc::new(2, Vec::new(), Some(0), body_copy(1, 0)),
            ],
            vec![
                HirStmt::Call(HirCall::new(1, Vec::new(), None)),
                HirStmt::Call(HirCall::new(2, Vec::new(), Some(1))),
            ],
        );
        module.validate().unwrap();
    }

    #[test]
    fn hir_module_serializes_vars_and_var_transfers_and_validates_var_ids() {
        let doc = HirDocument::new(HirModule {
            vars: vec![HirVarDecl::new(0, 10), HirVarDecl::new(1, 20)],
            stmts: vec![
                HirStmt::Act(HirAct::set_var_const(0, 8, 5, 0xFFFF_FFFF)),
                HirStmt::VarTransfer(HirVarTransfer {
                    dst_var: 1,
                    src_var: 0,
                    modifier: 7,
                }),
            ],

            strings: Vec::new(),
            files: Vec::new(),
            funcs: Vec::new(),
            blobs: Vec::new(),
        });
        let json = hir_to_json(&doc).unwrap();
        let parsed = hir_from_json(&json).unwrap();
        assert_eq!(parsed, doc);

        let bad_doc = HirDocument::new(HirModule {
            vars: vec![HirVarDecl::new(0, 10)],
            stmts: vec![HirStmt::VarTransfer(HirVarTransfer {
                dst_var: 99,
                src_var: 0,
                modifier: 7,
            })],

            strings: Vec::new(),
            files: Vec::new(),
            funcs: Vec::new(),
            blobs: Vec::new(),
        });
        assert_eq!(bad_doc.validate().unwrap_err(), HirError::UnknownVarId(99));
    }

    #[test]
    fn hir_trigger_rejects_more_than_16_conditions_with_eudsl_bounds() {
        let err = HirTrigger::new(0, vec![HirCond::always(); 17], vec![], true).unwrap_err();
        assert_eq!(err, HirError::TooManyConditions(17));
        assert!(err.to_string().contains("EUDSL-BOUNDS"));
    }

    #[test]
    fn hir_trigger_rejects_more_than_64_actions_with_eudsl_bounds() {
        let err = HirTrigger::new(
            0,
            vec![HirCond::always()],
            vec![HirAct::set_deaths(0, 7, 1, 0); 65],
            true,
        )
        .unwrap_err();
        assert_eq!(err, HirError::TooManyActions(65));
        assert!(err.to_string().contains("EUDSL-BOUNDS"));
    }

    #[test]
    fn hir_validates_module_string_ids_on_acts_conds_and_vars() {
        let mut act = HirAct::set_deaths(0, 7, 0, 0);
        act.str_index = Some(0);
        let doc = HirDocument::new(HirModule {
            vars: Vec::new(),
            stmts: vec![HirStmt::Act(act)],
            strings: Vec::new(),
            files: Vec::new(),
            funcs: Vec::new(),
            blobs: Vec::new(),
        });
        assert_eq!(doc.validate().unwrap_err(), HirError::UnknownStringId(0));

        let mut cond = HirCond::always();
        cond.relocs.push(HirReloc::string(16, 1, 0, 1));
        let doc = HirDocument::new(HirModule {
            vars: Vec::new(),
            stmts: vec![HirStmt::Trigger(
                HirTrigger::new(0, vec![cond], Vec::new(), true).unwrap(),
            )],
            strings: vec!["present".to_string()],
            files: Vec::new(),
            funcs: Vec::new(),
            blobs: Vec::new(),
        });
        assert_eq!(doc.validate().unwrap_err(), HirError::UnknownStringId(1));

        let mut var = HirVarDecl::new(0, 0);
        var.init_string = Some(HirStringInit {
            index: 1,
            offset: 0,
        });
        var.storage = Some("page".to_string());
        let doc = HirDocument::new(HirModule {
            vars: vec![var],
            stmts: Vec::new(),
            strings: vec!["present".to_string()],
            files: Vec::new(),
            funcs: Vec::new(),
            blobs: Vec::new(),
        });
        assert_eq!(doc.validate().unwrap_err(), HirError::UnknownStringId(1));
    }

    fn embedded(name: &str, path: &str) -> HirEmbeddedFile {
        HirEmbeddedFile {
            name: name.to_string(),
            path: path.to_string(),
        }
    }

    #[test]
    fn hir_module_round_trips_embedded_files_and_omits_empty_lists() {
        let doc = HirDocument::new(HirModule {
            funcs: Vec::new(),
            files: vec![embedded("bgm", "assets/bgm.wav")],
            blobs: Vec::new(),
            ..HirModule::default()
        });
        let json = hir_to_json(&doc).unwrap();
        assert!(
            json.contains(r#""files":[{"name":"bgm","path":"assets/bgm.wav"}]"#),
            "embedded files must serialize in declaration order: {json}"
        );
        let parsed = hir_from_json(&json).unwrap();
        assert_eq!(parsed, doc);
        assert_eq!(parsed.module.files, doc.module.files);

        let empty = HirDocument::new(HirModule {
            funcs: Vec::new(),
            stmts: Vec::new(),
            ..HirModule::default()
        });
        let empty_json = hir_to_json(&empty).unwrap();
        assert!(
            !empty_json.contains("\"files\""),
            "an empty file list must not appear in HIR JSON: {empty_json}"
        );
        assert_eq!(hir_from_json(&empty_json).unwrap(), empty);
    }

    #[test]
    fn hir_rejects_invalid_embedded_files() {
        let doc = HirDocument::new(HirModule {
            funcs: Vec::new(),
            files: vec![embedded("", "bgm.wav")],
            blobs: Vec::new(),
            ..HirModule::default()
        });
        assert_eq!(doc.validate().unwrap_err(), HirError::EmptyEmbeddedFileName);

        let doc = HirDocument::new(HirModule {
            funcs: Vec::new(),
            files: vec![embedded("bgm", "")],
            blobs: Vec::new(),
            ..HirModule::default()
        });
        assert_eq!(
            doc.validate().unwrap_err(),
            HirError::EmptyEmbeddedFilePath("bgm".to_string())
        );

        let doc = HirDocument::new(HirModule {
            funcs: Vec::new(),
            files: vec![embedded("bgm", "a.wav"), embedded("bgm", "b.wav")],
            blobs: Vec::new(),
            ..HirModule::default()
        });
        assert_eq!(
            doc.validate().unwrap_err(),
            HirError::DuplicateEmbeddedFileName("bgm".to_string())
        );
    }

    #[test]
    fn hir_requires_page_storage_for_init_string() {
        let mut var = HirVarDecl::new(0, 0);
        var.init_string = Some(HirStringInit {
            index: 0,
            offset: 4,
        });
        let doc = HirDocument::new(HirModule {
            vars: vec![var],
            stmts: Vec::new(),
            strings: vec!["buffer".to_string()],
            files: Vec::new(),
            funcs: Vec::new(),
            blobs: Vec::new(),
        });
        assert_eq!(
            doc.validate().unwrap_err(),
            HirError::StringInitRequiresPage(0)
        );

        let mut pinned = HirVarDecl::pinned(0, 0);
        pinned.init_string = Some(HirStringInit {
            index: 0,
            offset: 4,
        });
        let doc = HirDocument::new(HirModule {
            vars: vec![pinned],
            stmts: Vec::new(),
            strings: vec!["buffer".to_string()],
            files: Vec::new(),
            funcs: Vec::new(),
            blobs: Vec::new(),
        });
        doc.validate().unwrap();
    }

    #[test]
    fn hir_string_relocs_require_a_fitting_window_and_one_target() {
        // Sub-dword offsets are legal as long as the 4-byte window fits
        // the 32-byte action record (issue #101); 28 is the last legal
        // action offset and 2 is legal too.
        for offset in [2usize, 16, 28] {
            let mut act = HirAct::set_deaths(0, 7, 0, 0);
            act.relocs.push(HirReloc::string(offset, 0, 0, 1));
            let doc = HirDocument::new(HirModule {
                vars: Vec::new(),
                stmts: vec![HirStmt::Act(act)],
                strings: vec!["buffer".to_string()],
                files: Vec::new(),
                blobs: Vec::new(),
                funcs: Vec::new(),
            });
            doc.validate()
                .unwrap_or_else(|e| panic!("offset {offset} must validate: {e}"));
        }
        // 29 has no 4-byte window inside 32 bytes.
        let mut act = HirAct::set_deaths(0, 7, 0, 0);
        act.relocs.push(HirReloc::string(29, 0, 0, 1));
        let doc = HirDocument::new(HirModule {
            vars: Vec::new(),
            stmts: vec![HirStmt::Act(act)],
            strings: vec!["buffer".to_string()],
            files: Vec::new(),
            funcs: Vec::new(),
            blobs: Vec::new(),
        });
        assert_eq!(
            doc.validate().unwrap_err(),
            HirError::InvalidStringReloc(29, eudkit_lir::ACT_BYTE_SIZE)
        );
        // A condition record is 20 bytes: 16 fits, 17 does not.
        let mut cond = HirCond::always();
        cond.relocs.push(HirReloc::string(17, 0, 0, 1));
        let trig = HirTrigger::new(0, vec![cond], vec![], true).unwrap();
        let doc = HirDocument::new(HirModule {
            vars: Vec::new(),
            stmts: vec![HirStmt::Trigger(trig)],
            strings: vec!["buffer".to_string()],
            files: Vec::new(),
            blobs: Vec::new(),
            funcs: Vec::new(),
        });
        assert_eq!(
            doc.validate().unwrap_err(),
            HirError::InvalidStringReloc(17, eudkit_lir::COND_BYTE_SIZE)
        );

        let mut both = HirReloc::string(16, 0, 0, 1);
        both.target_var = Some(0);
        let mut act = HirAct::set_deaths(0, 7, 0, 0);
        act.relocs.push(both);
        let doc = HirDocument::new(HirModule {
            vars: vec![HirVarDecl::new(0, 0)],
            stmts: vec![HirStmt::Act(act)],
            strings: vec!["buffer".to_string()],
            files: Vec::new(),
            funcs: Vec::new(),
            blobs: Vec::new(),
        });
        assert_eq!(doc.validate().unwrap_err(), HirError::InvalidReloc);
    }

    #[test]
    fn hir_string_fields_round_trip_through_json() {
        let mut var = HirVarDecl::pinned(0, 0);
        var.init_string = Some(HirStringInit {
            index: 1,
            offset: 8,
        });
        let mut act = HirAct::set_deaths(0, 7, 0, 0);
        act.str_index = Some(0);
        act.relocs.push(HirReloc::string(16, 1, 8, 1));
        let doc = HirDocument::new(HirModule {
            vars: vec![var],
            stmts: vec![HirStmt::Trigger(
                HirTrigger::new(0, vec![HirCond::always()], vec![act], true).unwrap(),
            )],
            strings: vec!["buffer".to_string(), "display".to_string()],
            files: Vec::new(),
            funcs: Vec::new(),
            blobs: Vec::new(),
        });
        let json = hir_to_json(&doc).unwrap();
        assert!(json.contains("\"init_string\""));
        assert!(json.contains("\"str_index\":0"));
        assert!(json.contains("\"target_string\":1"));
        assert_eq!(hir_from_json(&json).unwrap(), doc);
    }

    #[test]
    fn hir_version_is_3_2() {
        assert_eq!(HIR_VERSION, "3.2");
        let doc = HirDocument::new(HirModule::new());
        assert_eq!(doc.version, "3.2");
    }

    /// ADR 0021: the version is exact-checked, which is what keeps
    /// `HirModule.files` safe. A 3.1 reader would parse a 3.1-tagged document,
    /// silently drop the `#[serde(default)]` list, and lower a dangling
    /// `PlayWAV("bgm")`; tagging the document 3.2 makes that read fail clean.
    #[test]
    fn hir_documents_older_than_the_files_surface_fail_clean() {
        let json =
            r#"{"version":"3.1","module":{"stmts":[],"files":[{"name":"bgm","path":"a.wav"}]}}"#;
        assert_eq!(
            hir_from_json(json).unwrap_err(),
            HirError::BadVersion("3.1".to_string())
        );
    }

    #[test]
    fn new_temporal_kinds_round_trip_through_json() {
        let leaf = HirStmt::Act(HirAct::set_deaths(0, 7, 1, 0));
        let doc = HirDocument::new(HirModule {
            // The counted nodes carry counter_var 0, which must name a
            // declared variable for the parse-time validation to pass.
            vars: vec![HirVarDecl::new(0, 1), HirVarDecl::new(1, 0)],
            stmts: vec![
                HirStmt::Until(HirUntil::new(
                    vec![HirCond::always()],
                    vec![leaf.clone()],
                    0,
                )),
                HirStmt::Edge(HirEdge::new(
                    HirEdgeDirection::Rising,
                    vec![HirCond::always()],
                    vec![leaf.clone()],
                    0,
                    1,
                )),
                HirStmt::Edge(HirEdge::new(
                    HirEdgeDirection::Falling,
                    vec![HirCond::always()],
                    vec![leaf.clone()],
                    0,
                    1,
                )),
                HirStmt::Every(HirEvery::new(3, 0, vec![leaf.clone()])),
                HirStmt::After(HirAfter::new(2, 0, vec![leaf])),
            ],
            strings: Vec::new(),
            files: Vec::new(),
            funcs: Vec::new(),
            blobs: Vec::new(),
        });
        let json = hir_to_json(&doc).unwrap();
        assert!(json.contains("\"kind\":\"until\""));
        assert!(json.contains("\"latch_var\":0"));
        assert!(json.contains("\"kind\":\"edge\""));
        assert!(json.contains("\"direction\":\"rising\""));
        assert!(json.contains("\"direction\":\"falling\""));
        assert!(json.contains("\"kind\":\"every\""));
        assert!(json.contains("\"n\":3"));
        assert!(json.contains("\"counter_var\":0"));
        assert!(json.contains("\"kind\":\"after\""));
        assert_eq!(hir_from_json(&json).unwrap(), doc);
    }

    #[test]
    fn hir_rejects_non_current_versions_with_bad_version() {
        let stale = r#"{"version":"3.0","module":{"stmts":[]}}"#;
        assert_eq!(
            hir_from_json(stale).unwrap_err(),
            HirError::BadVersion("3.0".to_string())
        );
        let unknown = r#"{"version":"3.3","module":{"stmts":[]}}"#;
        assert_eq!(
            hir_from_json(unknown).unwrap_err(),
            HirError::BadVersion("3.3".to_string())
        );
    }

    #[test]
    fn new_temporal_kinds_validate_structurally() {
        // A body referencing an undeclared variable fails loud, proving
        // validation recurses into the new nodes' bodies.
        for stmt in [
            HirStmt::Until(HirUntil::new(
                vec![],
                vec![HirStmt::VarTransfer(HirVarTransfer {
                    dst_var: 0,
                    src_var: 99,
                    modifier: 7,
                })],
                0,
            )),
            HirStmt::Edge(HirEdge::new(
                HirEdgeDirection::Rising,
                vec![],
                vec![HirStmt::VarTransfer(HirVarTransfer {
                    dst_var: 0,
                    src_var: 99,
                    modifier: 7,
                })],
                0,
                1,
            )),
            HirStmt::Every(HirEvery::new(
                3,
                0,
                vec![HirStmt::VarTransfer(HirVarTransfer {
                    dst_var: 0,
                    src_var: 99,
                    modifier: 7,
                })],
            )),
            HirStmt::After(HirAfter::new(
                2,
                0,
                vec![HirStmt::VarTransfer(HirVarTransfer {
                    dst_var: 0,
                    src_var: 99,
                    modifier: 7,
                })],
            )),
        ] {
            let doc = HirDocument::new(HirModule {
                vars: vec![HirVarDecl::new(0, 0), HirVarDecl::new(1, 0)],
                stmts: vec![stmt],
                strings: Vec::new(),
                files: Vec::new(),
                funcs: Vec::new(),
                blobs: Vec::new(),
            });
            assert_eq!(doc.validate().unwrap_err(), HirError::UnknownVarId(99));
        }

        // C11-03: an edge whose state vars are not declared fails loud.
        let doc = HirDocument::new(HirModule {
            vars: vec![HirVarDecl::new(0, 0)],
            stmts: vec![HirStmt::Edge(HirEdge::new(
                HirEdgeDirection::Rising,
                vec![],
                vec![],
                0,
                7,
            ))],
            strings: Vec::new(),
            files: Vec::new(),
            funcs: Vec::new(),
            blobs: Vec::new(),
        });
        assert_eq!(doc.validate().unwrap_err(), HirError::UnknownVarId(7));

        // A well-formed nested new node validates clean.
        let doc = HirDocument::new(HirModule {
            vars: vec![HirVarDecl::new(0, 0)],
            stmts: vec![HirStmt::Until(HirUntil::new(
                vec![],
                vec![HirStmt::When(HirWhen::new(
                    vec![],
                    vec![HirStmt::Act(HirAct::set_deaths(0, 7, 1, 0))],
                ))],
                0,
            ))],
            strings: Vec::new(),
            files: Vec::new(),
            funcs: Vec::new(),
            blobs: Vec::new(),
        });
        doc.validate().unwrap();

        // C11-02: an until whose latch var is not declared fails loud.
        let doc = HirDocument::new(HirModule {
            vars: vec![HirVarDecl::new(0, 0)],
            stmts: vec![HirStmt::Until(HirUntil::new(vec![], vec![], 7))],
            strings: Vec::new(),
            files: Vec::new(),
            funcs: Vec::new(),
            blobs: Vec::new(),
        });
        assert_eq!(doc.validate().unwrap_err(), HirError::UnknownVarId(7));
    }

    #[test]
    fn edge_direction_rejects_unknown_polarity() {
        let json = r#"{"version":"3.2","module":{"stmts":[{"kind":"edge","direction":"sideways","conds":[],"body":[]}]}}"#;
        assert!(matches!(hir_from_json(json), Err(HirError::Json(_))));
    }

    /// C11-04: counted forms reject `n = 0` at HIR validation (DSL
    /// validation already rejects it at authoring time; hand-written
    /// HIR must fail loud too), with the exact `NonPositiveCount`
    /// variant and its `EUDSL-BOUNDS` message.
    #[test]
    fn counted_forms_reject_zero_count() {
        for stmt in [
            HirStmt::Every(HirEvery::new(0, 0, vec![])),
            HirStmt::After(HirAfter::new(0, 0, vec![])),
        ] {
            let doc = HirDocument::new(HirModule {
                vars: vec![HirVarDecl::new(0, 0)],
                stmts: vec![stmt],
                strings: Vec::new(),
                files: Vec::new(),
                funcs: Vec::new(),
                blobs: Vec::new(),
            });
            let err = doc.validate().unwrap_err();
            assert_eq!(err, HirError::NonPositiveCount(0));
            assert!(err.to_string().contains("EUDSL-BOUNDS"));
        }
    }

    /// C11-04: a counted node whose `counter_var` names no declared
    /// variable fails reference-integrity validation with the exact
    /// `UnknownVarId` error for the counter id.
    #[test]
    fn counted_node_with_undeclared_counter_rejected() {
        // hir_from_json validates on entry, so the undeclared counter
        // fails loud at parse time with the exact `UnknownVarId` error.
        let json = r#"{"version":"3.2","module":{"vars":[],"stmts":[{"kind":"every","n":3,"counter_var":99,"body":[]}]}}"#;
        assert_eq!(hir_from_json(json).unwrap_err(), HirError::UnknownVarId(99));
    }

    #[test]
    fn blobs_round_trip_through_json() {
        let blob = HirBlob::from_bytes(&[1, 2, 3, 4, 5, 6, 7, 8])
            .with_relocs(vec![HirBlobReloc::blob(0, 0, 4, 4)]);
        let doc = HirDocument::new(HirModule {
            vars: Vec::new(),
            stmts: vec![HirStmt::Act(HirAct {
                relocs: vec![HirReloc::blob(20, 0, 0, 4)],
                ..HirAct::set_deaths(0x64EED8, 7, 0, 0)
            })],
            strings: Vec::new(),
            files: Vec::new(),
            blobs: vec![blob, HirBlob::zeroed(16)],
            funcs: Vec::new(),
        });
        let json = hir_to_json(&doc).unwrap();
        assert!(json.contains("\"blobs\""), "blobs must serialize: {json}");
        let parsed = hir_from_json(&json).unwrap();
        assert_eq!(parsed, doc);
        assert_eq!(parsed.module.blobs[0].size, 8);
        assert_eq!(parsed.module.blobs[1], HirBlob::zeroed(16));
        assert_eq!(HirBlob::from_bytes(b"abcd").data, "YWJjZA==");
    }

    #[test]
    fn blob_validation_rejects_bad_payload_and_relocations() {
        let doc_with = |blobs: Vec<HirBlob>| {
            HirDocument::new(HirModule {
                vars: Vec::new(),
                stmts: Vec::new(),
                strings: Vec::new(),
                files: Vec::new(),
                blobs,
                funcs: Vec::new(),
            })
        };

        // Base64 payload longer than the declared size.
        let mut too_long = HirBlob::from_bytes(&[1, 2, 3, 4]);
        too_long.size = 2;
        assert_eq!(
            doc_with(vec![too_long]).validate().unwrap_err(),
            HirError::InvalidBlobData(0)
        );

        // Reloc field must be 4-byte aligned and inside the blob.
        for reloc in [
            HirBlobReloc::blob(2, 0, 0, 4),
            HirBlobReloc::blob(16, 0, 0, 4),
            HirBlobReloc::blob(0, 9, 0, 4),
        ] {
            let offset = reloc.offset;
            assert_eq!(
                doc_with(vec![HirBlob::zeroed(16).with_relocs(vec![reloc])])
                    .validate()
                    .unwrap_err(),
                HirError::InvalidBlobReloc(0, offset)
            );
        }

        // A blob reloc needs exactly one target.
        assert_eq!(
            doc_with(vec![HirBlob::zeroed(16).with_relocs(vec![HirBlobReloc {
                offset: 0,
                target_blob: Some(0),
                target_trigger: Some(0),
                target_offset: 0,
                mode: 4,
            }])])
            .validate()
            .unwrap_err(),
            HirError::InvalidBlobReloc(0, 0)
        );

        // A trigger/condition field reloc to an unknown blob fails loud.
        let doc = HirDocument::new(HirModule {
            vars: Vec::new(),
            stmts: vec![HirStmt::Act(HirAct {
                relocs: vec![HirReloc::blob(20, 3, 0, 4)],
                ..HirAct::set_deaths(0, 7, 0, 0)
            })],
            strings: Vec::new(),
            files: Vec::new(),
            blobs: vec![HirBlob::zeroed(4)],
            funcs: Vec::new(),
        });
        assert_eq!(
            doc.validate().unwrap_err(),
            HirError::InvalidBlobTarget(3, 20)
        );
    }
}
