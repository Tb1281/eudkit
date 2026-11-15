//! `eudc-map`: the native map tail (EUD IR Phase 5 §1.6).
//!
//! Ports, onto the pure `stormlib` crate (P9 write API — never the
//! PyO3-bound `eudplib-stormlib`):
//!
//! - [`basemap`] — [`BasemapContext`]: the basemap MPQ open + CHK read +
//!   tokenize + template string-table parse, shared by the lowering
//!   driver's MapInfo and STR-offset consumers (one parse per compile).
//! - [`chk`] — CHK section model (used subset of `chktok.py`).
//! - [`epmap`] — `.epmap` trace-file writer (pure function) + the
//!   `tracecrypt.mix` id scrambler (`savemap.py` tail, `tracecrypt.py`).
//! - [`fixmapdata`] — `_fix_map_data` + the `update_map_data` property-map
//!   half (`fixmapdata.py`, `proptable.py`).
//! - [`mapinfo`] — MapInfo extraction from the basemap (template
//!   strings, location names, switch names) for frontends/lowering.
//! - [`tbl`] — `TBL.save_tbl` port (pure function of the emit-time string
//!   table; see that module for the empty-string recycling rule).
//! - [`payload_init`] — `initialize_payload` port (prt/ort fixups, STR
//!   assembly, MRGN obfuscation, TRIG rebuild with three classic-2400
//!   trigger encodings).
//! - [`rng`] — CPython `random.Random` replication (MT19937 +
//!   `seed(0)`/`getrandbits`/`_randbelow` rejection logic) plus the logical
//!   draw-log replay that positions the stream for savechk's fake `ISOM`
//!   section.
//! - [`mpq`] — MPQ tail (`clone_with_sector_size` reimplementation +
//!   `_write_output_map` / `_update_mpq` logic).
//!
//! [`assemble_map`] is the top-level orchestration used by the `eudc`
//! binary: LIR → `lir2payload` → `initialize_payload` → `optimize` →
//! `savechk` → MPQ write.

pub mod basemap;
pub mod chk;
pub mod epmap;
pub mod layout;
pub mod mapinfo;
pub mod fixmapdata;
pub mod mpq;
pub mod payload_init;
pub mod rng;
#[doc(hidden)]
pub mod stage;
pub mod tbl;
pub mod tick_gate;
pub mod uprp;


use std::path::Path;

use crate::basemap::BasemapContext;

/// Errors for the map tail. Loud by design: every variant names the failed
/// step so the CLI can surface it without re-wrapping.
#[derive(Debug)]
pub enum MapTailError {
    Storm(stormlib::error::StormError),
    Io(std::io::Error),
    SectionName(String),
    MissingSection(&'static str),
    TruncatedSection(&'static str),
    FixupOob(usize),
    MrgnOob(usize),
    MrgnDataShort(usize),
    ListfileNotUtf8,
    TempPersist(String),
    Lir(eudkit_lir::LirError),
    BadStrings(String),
    TemplateTbl(String),
    /// Template TRIG length is not a multiple of 2400 (malformed; Python
    /// silently drops the partial tail — the native tail refuses).
    MalformedTrig(usize),
    /// CHK DIM section holds out-of-range tile dimensions (valid:
    /// 1..=256 per axis, StarCraft's max map size). The string holds
    /// the offending `WxH` pair.
    InvalidDim(String),
    /// All 64 UPRP slots are occupied, so a new module property record
    /// cannot be allocated (eudplib: "Unit property table overflow").
    UprpOverflow,
    /// Malformed module UPRP usage (record on a non-CUWP action, etc.).
    BadUprp(String),
    /// The LIR `module.entry` index does not name a payload item.
    EntryOutOfRange { entry: usize, item_count: usize },
}

impl std::fmt::Display for MapTailError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            MapTailError::Storm(e) => write!(f, "stormlib error: {e}"),
            MapTailError::Io(e) => write!(f, "I/O error: {e}"),
            MapTailError::SectionName(e) => write!(f, "bad section name: {e}"),
            MapTailError::MissingSection(s) => write!(f, "CHK missing section {s}"),
            MapTailError::TruncatedSection(s) => {
                write!(f, "CHK section {s} shorter than expected")
            }
            MapTailError::FixupOob(o) => write!(f, "reloc fixup out of bounds: {o}"),
            MapTailError::MrgnOob(i) => write!(f, "MRGN read out of bounds at {i}"),
            MapTailError::MrgnDataShort(i) => {
                write!(f, "mrgndata shorter than MRGN section at {i}")
            }
            MapTailError::ListfileNotUtf8 => write!(f, "(listfile) is not UTF-8"),
            MapTailError::TempPersist(e) => write!(f, "temp file persist failed: {e}"),
            MapTailError::Lir(e) => write!(f, "LIR error: {e:?}"),
            MapTailError::BadStrings(e) => write!(f, "bad LIR strings: {e}"),
            MapTailError::TemplateTbl(e) => write!(f, "template string table: {e}"),
            MapTailError::MalformedTrig(n) => write!(
                f,
                "template TRIG has {n} bytes, not a multiple of 2400 (malformed)"
            ),
            MapTailError::InvalidDim(wh) => write!(
                f,
                "CHK DIM section holds out-of-range tile dimensions: {wh} (valid: 1..=256 per axis)"
            ),
            MapTailError::UprpOverflow => write!(
                f,
                "unit property table overflow: all 64 UPRP slots are in use"
            ),
            MapTailError::BadUprp(e) => write!(f, "bad UPRP allocation: {e}"),
            MapTailError::EntryOutOfRange { entry, item_count } => write!(
                f,
                "LIR module.entry {entry} is out of range (payload has {item_count} item(s))"
            ),
        }
    }
}

impl std::error::Error for MapTailError {}

impl From<stormlib::error::StormError> for MapTailError {
    fn from(e: stormlib::error::StormError) -> Self {
        MapTailError::Storm(e)
    }
}

impl From<std::io::Error> for MapTailError {
    fn from(e: std::io::Error) -> Self {
        MapTailError::Io(e)
    }
}

impl From<eudkit_lir::LirError> for MapTailError {
    fn from(e: eudkit_lir::LirError) -> Self {
        MapTailError::Lir(e)
    }
}

/// Decode LIR 3.0 `module.strings` (base64) to the ordered string list.
pub fn decode_strings(b64: &[String]) -> Result<Vec<Vec<u8>>, MapTailError> {
    b64.iter()
        .map(|s| {
            eudkit_lir::b64_decode(s)
                .map_err(|_| MapTailError::BadStrings(format!("invalid base64: {s:.40}…")))
        })
        .collect()
}

/// `Option` adapter over the single [`eudkit_lir::b64_decode`]
/// implementation, for call sites that prefer absence over the typed
/// error. Accepts and rejects exactly the same corpus as LIR.
pub(crate) fn decode_b64(s: &str) -> Option<Vec<u8>> {
    eudkit_lir::b64_decode(s).ok()
}

/// Inputs for one map-tail run.
pub struct TailInputs<'a> {
    /// The lowered program, handed over typed: assembly never re-parses it.
    pub lir: &'a eudkit_lir::LirDocument,
    /// Template map path (`--basemap`).
    pub basemap: &'a Path,
    /// Output `.scx` path.
    pub out_path: &'a Path,
    /// MPQ sector size (`--sector-size`; `None` = byte-copy branch).
    pub sector_size: Option<u32>,
    /// MRGN obfuscation bytes (5100). `None` skips MRGN work.
    pub mrgndata: Option<&'a [u8]>,
    /// Recorded global-stream draw log (JSON wire format, see [`rng`]).
    /// Replayed verbatim before savechk's own draws. The recorder's log
    /// starts with the import-time prefix (`import_time_draws`), so when it
    /// is present the prefix is NOT replayed separately (no double replay).
    /// When absent, the fixed import-time prefix is replayed instead —
    /// Python's `build()` ALWAYS replays `_replay_import_time_draws()` after
    /// `seed(0)`, so skipping it would land savechk's draws 137 logical
    /// draws early.
    pub draw_log_json: Option<&'a str>,
    /// Extra files for `_update_mpq` (MPQAddFile queue; usually empty —
    /// the golden builds queue nothing with content).
    pub queued_files: Vec<mpq::QueuedFile>,
    /// Hidden debug hook (Tier-1 harness): write the assembled CHK here.
    pub debug_emit_chk: Option<&'a Path>,
    /// Skip savechk's fake `ISOM` trailer (`--no-fake-isom`); the
    /// trailer's negative length blocks editor-style CHK parsers, so
    /// skipping it yields a map those tools can open. Default off
    /// (trailer appended, legacy behavior).
    pub no_fake_isom: bool,
    /// Map-assembly policy, including whether to append the once-per-tick
    /// gate around the compiled payload. Defaults to
    /// [`AssemblyOptions::default`] (gate enabled).
    pub assembly: AssemblyOptions,
}

/// Position the savechk stream: `seed(0)` → import-time prefix → recorder log.
///
/// Python's `build()` ALWAYS replays `_replay_import_time_draws()` after
/// `seed(0)` (`hir/pure/build.py`), with or without a draw-log recorder.
/// The recorder's log starts with that prefix (its wrappers install before
/// `install_deterministic_pins`, which performs the replay), so the prefix
/// must only come from the fixed sequence when no log is present —
/// replaying both would double-replay the prefix.
fn positioned_rng(draw_log_json: Option<&str>) -> Result<rng::CpRandom, MapTailError> {
    let mut rng = rng::CpRandom::seed_zero();
    match draw_log_json {
        Some(log_json) => {
            let log = rng::parse_draw_log(log_json)
                .map_err(|e| MapTailError::BadStrings(format!("draw log: {e}")))?;
            rng::replay_draw_log(&mut rng, &log);
        }
        None => {
            rng::replay_draw_log(&mut rng, &rng::import_time_draws());
        }
    }
    Ok(rng)
}

/// What `assemble_map` did with the template.
pub struct TailSummary {
    /// Whether the template's TRIG triggers were inlined (live-default) or
    /// kept verbatim (rate-0 fallback, P6-D10).
    pub template_inlined: bool,
    /// Static cost of the assembled payload, including the once-per-tick
    /// gate `assemble_map` appends unless the caller opted out (ADR 0013).
    /// This is what `compile` reports.
    pub static_cost: eudkit_lir::StaticCost,
}

/// Map-assembly policy shared by the cost preflight and [`assemble_map`].
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct AssemblyOptions {
    /// Append the once-per-tick gate around the compiled payload.
    /// Default true; false is for maps with a single active chain (ADR 0013).
    pub enable_tick_gate: bool,
}

impl Default for AssemblyOptions {
    fn default() -> Self {
        Self {
            enable_tick_gate: true,
        }
    }
}

/// Static cost of the payload [`assemble_map`] writes: the program LIR plus
/// the 35-trigger once-per-tick gate.
///
/// `compile` reports the cost returned by [`assemble_map`] itself. This
/// helper applies the exact same gate to a cloned document so the plan
/// cost gate can enforce the same contract BEFORE writing a map.
pub fn assembled_static_cost(lir: &eudkit_lir::LirDocument) -> eudkit_lir::StaticCost {
    assembled_static_cost_with_options(lir, AssemblyOptions::default())
}

/// Static cost of the payload [`assemble_map`] writes with the specified options.
pub fn assembled_static_cost_with_options(
    lir: &eudkit_lir::LirDocument,
    options: AssemblyOptions,
) -> eudkit_lir::StaticCost {
    if options.enable_tick_gate {
        let mut doc = lir.clone();
        tick_gate::append_tick_gate(&mut doc);
        eudkit_lir::static_cost(&doc)
    } else {
        eudkit_lir::static_cost(lir)
    }
}

/// Run the native map tail: LIR → payload → CHK → MPQ → `.scx`.
///
/// Returns how the template's TRIG section was handled, so the caller can
/// report accurately (an inlined template matches a live default build;
/// a kept one differs by design).
pub fn assemble_map(inputs: TailInputs<'_>) -> Result<TailSummary, MapTailError> {
    assemble_map_inner(inputs, None)
}

/// Run the map tail with an already-parsed [`BasemapContext`].
///
/// The compile pipeline opens the basemap once during lowering
/// (`prepare_lowering`) and hands the context here, so assembly reuses its
/// tokenized CHK and parsed template string table instead of re-opening the
/// MPQ and re-running `TemplateStrings::parse` (measured ~11.4 ms). The
/// `.scx` output is byte-identical to [`assemble_map`]'s.
///
/// `ctx` must have been opened from `inputs.basemap`: the template CHK and
/// string table come from the context while the MPQ tail still copies
/// `inputs.basemap`.
///
/// Soundness: `E-STRADDR-VERIFY` still compares the driver-recorded offset
/// table against the table this call builds, and still fails loud on a
/// mismatch. Only the independent-parse half of the cross-check is removed —
/// see the module docs in [`crate::basemap`] for why that was accepted.
pub fn assemble_map_with_context(
    inputs: TailInputs<'_>,
    ctx: &BasemapContext,
) -> Result<TailSummary, MapTailError> {
    assemble_map_inner(inputs, Some(ctx))
}

fn assemble_map_inner(
    inputs: TailInputs<'_>,
    reused: Option<&BasemapContext>,
) -> Result<TailSummary, MapTailError> {
    // Load + tokenize the template CHK before building the payload: module
    // UPRP allocation needs the authored UPRP/UPUS tables, and the string
    // table below must still be parsed from the pristine template. A handed
    // off context already tokenized the same pristine bytes, so assembly
    // only needs an owned copy for its fixups.
    let stage = crate::stage::Stage::start("map.template");
    let mut chkt = match reused {
        Some(ctx) => ctx.chk().clone(),
        None => {
            let mut basemap_mpq = stormlib::Archive::open(
                inputs.basemap,
                stormlib::OpenArchiveFlags::empty(),
            )?;
            let chk_bytes = basemap_mpq
                .open_file("staredit\\scenario.chk")?
                .read_all()?;
            drop(basemap_mpq);
            let mut chkt = chk::Chk::new();
            if !chkt.loadchk(&chk_bytes) {
                return Err(MapTailError::BadStrings(
                    "template scenario.chk failed to tokenize".into(),
                ));
            }
            chkt
        }
    };
    stage.finish();

    // Stage ③: LIR → payload (native, in-process).
    // The once-per-tick gate is optional: for multiplayer maps (default),
    // it is appended to the LIR document here, at map-assembly time, so the
    // stage-1 chain enters the gate before the program's first item.
    // When disabled (single-player / --no-tick-gate), the gate is omitted
    // and stage-1 links directly to the program's original entry.
    let stage = crate::stage::Stage::start("map.lir");
    // The typed hand-off needs no parse; copy so the gate and UPRP patching
    // below cannot mutate the caller's document.
    let mut doc = inputs.lir.clone();
    // Validate the program entry against the program items BEFORE the gate
    // mutates `module.entry` and appends its own items. `append_tick_gate`
    // records a dangling original entry as `CHAIN_END_PTR` (its empty-program
    // semantics), so validating only after the gate would let an invalid
    // entry pass silently on the default path.
    if doc.module.entry >= doc.module.items.len() {
        return Err(MapTailError::EntryOutOfRange {
            entry: doc.module.entry,
            item_count: doc.module.items.len(),
        });
    }
    stage.finish();
    let stage = crate::stage::Stage::start("map.gate");
    let gate = if inputs.assembly.enable_tick_gate {
        Some(tick_gate::append_tick_gate(&mut doc))
    } else {
        None
    };
    stage.finish();
    // Allocate module UPRP slots against the authored tables and patch the
    // actions' 1-based property indices before serialization. The returned
    // bytes are installed after the pristine-template STR parse below.
    let stage = crate::stage::Stage::start("map.uprp");
    let uprp_update = uprp::allocate(&mut doc, &chkt)?;
    stage.finish();
    let stage = crate::stage::Stage::start("map.payload");
    let payload = eudkit_lir::lir2payload(&doc)?;
    stage.finish();
    // Report the cost of the exact document that produced `payload`, so
    // the compiled map and its reported static cost cannot disagree.
    let stage = crate::stage::Stage::start("map.cost");
    let static_cost = eudkit_lir::static_cost(&doc);
    stage.finish();

    // Stream positioning for savechk's fake ISOM section.
    let mut rng = positioned_rng(inputs.draw_log_json)?;

    // STR section, parsed and built FIRST, before any mutation of the
    // template CHK. LIR 3.0 strings → one ordered build; empty
    // (string-less LIR, which is what the native lowering always emits) →
    // re-serialize the template's own table as STRx
    // (`TemplateStrings::parse` + `save_as_strx`). This mirrors Python's
    // `load_entry=2` → `save_entry=4` conversion (`init_stringmaps` →
    // `save_tbl`). Copying the raw section bytes here would mislabel a
    // `STR ` (u16 offsets) section as `STRx` (u32 offsets) — a silently
    // corrupt string table at exit 0 (F43).
    // NOTE (port-09): the payload appended after the STR section
    // is compiled LIR `RawTrigger` items (2408 bytes, flags at 2376),
    // NOT the classic 2400-byte template triggers that payload_init
    // rebuilds (flag at 2368). The two encodings are never
    // transcoded into each other; ticket 14 owns the Raw layout.
    //
    // The table is built exactly once: the bytes emitted below and
    // the offsets the verification compares against come from the
    // same build, not from a second serialization.
    //
    // Ordering pin: this parse + build runs before `_preprocess_inline_code`,
    // `apply_property_map`, and `fix_map_data` mutate the template, so
    // the table always describes the pristine template (Python has the
    // same ordering via LoadMap → init_stringmaps long before
    // _fix_map_data). The ordering is pinned by position (this block
    // runs first), not by verification alone — and the distinction
    // matters. The verification below compares the offsets of the
    // table built here against the DRIVER-RECORDED StrAddr table
    // (`doc.module.str_addrs`), which the CLI computed from the
    // pristine basemap via `str_offsets_for_full_strings` and never
    // ran through `fix_map_data`. So for a StrAddr program, a parse
    // taken after a mutation that changed the table would fail LOUD
    // (recorded pristine != recomputed post-fix), not verify against
    // itself: a recomputation from the same wrong parse agrees with
    // that parse (see the parse-after-fix divergence test in tbl.rs).
    // The genuinely blind case is a str_addrs-EMPTY program, where the
    // verification block below is skipped entirely and a post-fix parse
    // would emit its (wrong) table unchecked. The hazard is real, not
    // hypothetical: `fix_map_data`'s seventh UNIx setting group writes
    // at UNIX_NAME_OFFSET + 2i (see layout::UNIX_NAME_OFFSET) — exactly
    // the reserved unit-name slots the TBL parse reads — so a parse
    // after the fix can yield a different table.
    //
    // With a handed-off `BasemapContext` the parse happened at driver
    // start, before any template mutation at all, so the ordering hazard
    // above cannot arise; what changes is that the recorded table and the
    // rebuilt table share one template parse (the independent-data half of
    // `E-STRADDR-VERIFY`). See `crate::basemap`'s module docs for the
    // accepted trade; the comparison itself is unchanged.
    let stage = crate::stage::Stage::start("map.strings");
    let ordered = decode_strings(&doc.module.strings)?;
    // A handed-off context already parsed the template table from the same
    // pristine CHK; parse the freshly read CHK only when assembly owns it.
    let parsed_tpl;
    let tpl: &tbl::TemplateStrings = match reused {
        Some(ctx) => ctx.template_strings(),
        None => {
            parsed_tpl = tbl::TemplateStrings::parse(&chkt)?;
            &parsed_tpl
        }
    };
    let built: tbl::BuiltStringTable = if ordered.is_empty() {
        tbl::BuiltStringTable::fallback(tpl)
    } else {
        tbl::BuiltStringTable::from_ordered(&ordered, tpl)?
    };

    // SCR print redesign (M1) soundness backstop: when lowering
    // recorded the StrAddr offset table it resolved against
    // (`module.str_addrs`), compare it against the offsets of the
    // table built above and FAIL LOUD on any mismatch. A wrong buffer
    // address would be a silent miscompile; this converts it into a
    // build error. No recorded table (hand-written LIR, old lowering,
    // every non-StrAddr program) means nothing to verify — const
    // addresses in such LIR are trusted exactly as today.
    if !doc.module.str_addrs.is_empty() {
        if ordered.is_empty() {
            return Err(MapTailError::BadStrings(
                "E-STRADDR-VERIFY: LIR records a StrAddr offset table \
                 but carries no strings to recompute it from"
                    .into(),
            ));
        }
        let recomputed = built.offsets.as_ref().ok_or_else(|| {
            MapTailError::BadStrings(
                "E-STRADDR-VERIFY: built string table carries no offsets"
                    .into(),
            )
        })?;
        // Full-equality comparison, deliberately NOT a prefix
        // comparison. Appending a string grows the STR header by 4
        // bytes per id (header = roundup4(4 * num_ids + 4)), so every
        // data offset shifts — a recorded table can never be a prefix
        // of a recomputed table with more ids, values included. More
        // importantly, appends can only arise from Str args the
        // frontend never interned (raw-builder HIR: both DSLs intern
        // every SymAct string arg via vocab _validated/internString,
        // so lowering's allocate() never appends for DSL programs).
        // In that case the StrAddr Consts folded during lowering from
        // the driver's table would be wrong by 4 bytes per appended
        // id in the final map — emitting would be a silent
        // miscompile. Refusing loud here is the sound behavior, not
        // a limitation; the fix for raw-builder HIR is to intern its
        // strings, as both DSLs do.
        if *recomputed != doc.module.str_addrs {
            let first_diff = recomputed
                .iter()
                .zip(doc.module.str_addrs.iter())
                .position(|(a, b)| a != b)
                .map(|i| format!("first differing id {}: recorded {} vs recomputed {}", i + 1, doc.module.str_addrs[i], recomputed[i]))
                .unwrap_or_else(|| {
                    format!(
                        "length differs: recorded {} ids vs recomputed {} ids",
                        doc.module.str_addrs.len(),
                        recomputed.len()
                    )
                });
            return Err(MapTailError::BadStrings(format!(
                "E-STRADDR-VERIFY: recorded StrAddr offset table does not \
                 match the recomputed STR layout ({first_diff}); refusing \
                 to emit a map with possibly wrong string-buffer addresses"
            )));
        }
    }

    stage.finish();

    // `_preprocess_inline_code` port (mirrors build()/SaveMap, which run it
    // before the injector tail): when the LIR payload carries the inline
    // bodies (dispatch wiring present), normal template triggers are
    // inlined to dispatchers (live-default); otherwise they are kept
    // verbatim (rate-0 semantics, P6-D10). A template that already carries
    // valid compiled inline code is preserved verbatim. One blank trigger
    // is appended either way — the "normal trigger at the last" the
    // stage-1 nextptr fixup needs, which `initialize_payload` later
    // flag-toggles into the final TRIG trigger.
    let stage = crate::stage::Stage::start("map.inline");
    let wired = payload_init::dispatch_wired_func_ids(&doc);
    let inline_mode = payload_init::preprocess_inline_code(&mut chkt, &wired)?;
    // The same wiring presence decides the old-trigger flag policy: with
    // stage-3 dispatch wiring in the payload, its runtime flip restores
    // every chain trigger (and enables the synthesized dispatchers), so
    // stage 1 keeps eudplib's blanket toggle. Without wiring nothing flips
    // the bit back, so authored classic triggers keep their own state.
    let authored_flags = if wired.is_empty() {
        payload_init::AuthoredTriggerFlags::Preserve
    } else {
        payload_init::AuthoredTriggerFlags::ToggleWithStage1
    };
    stage.finish();

    // `update_map_data()` + `_fix_map_data(chkt)` (mirrors build()/SaveMap,
    // which run both after `_preprocess_inline_code` and before the
    // injector tail). `apply_string_map`'s effect is already in `built`.
    let stage = crate::stage::Stage::start("map.fixups");
    if let Some(update) = uprp_update {
        chkt.setsection(*b"UPRP", update.uprp);
        chkt.setsection(*b"UPUS", update.upus);
    }
    fixmapdata::apply_property_map(&mut chkt)?;
    fixmapdata::fix_map_data(&mut chkt)?;
    stage.finish();

    // initialize_payload port.
    let stage = crate::stage::Stage::start("map.init");
    let entry_byte_offset = match gate {
        Some(info) => info.gate_byte_offset,
        None => payload
            .addrs
            .get(doc.module.entry)
            .copied()
            .ok_or(MapTailError::EntryOutOfRange {
                entry: doc.module.entry,
                item_count: payload.addrs.len(),
            })? as usize,
    };
    // Move the payload buffer into `initialize_payload` instead of cloning it
    // (`PayloadOut` is a local: at a 40 MB payload the clone costs ≈2 ms of
    // memcpy plus a fresh allocation). The fixup tables stay owned by this
    // frame, so they are borrowed from the destructured fields.
    let eudkit_lir::PayloadOut {
        mut data,
        prttable,
        orttable,
        ..
    } = payload;
    payload_init::initialize_payload(
        &mut chkt,
        &mut data,
        eudkit_lir::PayloadFixups::new(&prttable, &orttable),
        &built,
        inputs.mrgndata,
        entry_byte_offset,
        authored_flags,
    )?;
    stage.finish();

    let stage = crate::stage::Stage::start("map.savechk");
    chkt.optimize();
    let rawchk = chkt.savechk(&mut rng, !inputs.no_fake_isom);

    if let Some(p) = inputs.debug_emit_chk {
        std::fs::write(p, &rawchk)?;
    }
    stage.finish();

    // MPQ tail.
    mpq::write_output_map(
        inputs.out_path,
        inputs.basemap,
        &rawchk,
        inputs.sector_size,
        &inputs.queued_files,
    )?;
    Ok(TailSummary {
        template_inlined: inline_mode == payload_init::InlineMode::Inlined,
        static_cost,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn positioned_rng_without_log_replays_import_time_prefix() {
        // Regression (review FAIL 1): without --draw-log, the tail must
        // still replay the import-time prefix — Python's build() ALWAYS
        // replays _replay_import_time_draws() after seed(0). The first
        // draws must match a manual prefix replay.
        let mut rng = positioned_rng(None).unwrap();
        let mut reference = rng::CpRandom::seed_zero();
        rng::replay_draw_log(&mut reference, &rng::import_time_draws());
        assert_eq!(
            rng.randrange(58040099, 59652323),
            reference.randrange(58040099, 59652323)
        );
        assert_eq!(rng.randint(234, 65535), reference.randint(234, 65535));
        // And match the traced Python values (golden lock).
        let mut rng = positioned_rng(None).unwrap();
        assert_eq!(rng.randrange(58040099, 59652323), 59595293);
    }

    #[test]
    fn positioned_rng_with_log_does_not_double_replay_prefix() {
        // The recorder's log starts with the import-time prefix, so a log
        // is replayed verbatim — replaying the fixed prefix on top would
        // land the stream 137 draws late.
        let log_json = serde_json::to_string(&rng::import_time_draws()).unwrap();
        let mut rng = positioned_rng(Some(&log_json)).unwrap();
        let mut reference = rng::CpRandom::seed_zero();
        rng::replay_draw_log(&mut reference, &rng::import_time_draws());
        assert_eq!(
            rng.randrange(58040099, 59652323),
            reference.randrange(58040099, 59652323)
        );
        assert_eq!(rng.randint(234, 65535), reference.randint(234, 65535));
    }

    #[test]
    fn b64_roundtrip() {
        assert_eq!(decode_b64("aGVsbG8="), Some(b"hello".to_vec()));
        assert_eq!(decode_b64("YWI="), Some(b"ab".to_vec()));
        assert_eq!(decode_b64("YQ=="), Some(b"a".to_vec()));
        assert_eq!(decode_b64("AAAA"), Some(vec![0, 0, 0]));
        assert_eq!(decode_b64(""), Some(vec![]));
        assert!(decode_b64("!!!").is_none());
        assert!(decode_b64("abc").is_none());
        // The shared decoder enforces standard padding: at most two
        // `=` per chunk.
        assert!(decode_b64("====").is_none());
        assert!(decode_b64("A===").is_none());
    }

    /// Parse a STRx section (u32 offsets) into per-id (1-based) strings.
    /// Loud on any malformed offset — a corrupt table must never pass
    /// silently (this is the hole the F43 regression test closes).
    fn parse_strx_ids(section: &[u8]) -> Vec<Vec<u8>> {
        let n = u32::from_le_bytes(section[0..4].try_into().unwrap()) as usize;
        assert!(n < 1_000_000, "STRx string count garbage: {n}");
        let mut out = Vec::with_capacity(n);
        for i in 0..n {
            let o = 4 + 4 * i;
            let off = u32::from_le_bytes(section[o..o + 4].try_into().unwrap()) as usize;
            let end = section[off..]
                .iter()
                .position(|&b| b == 0)
                .unwrap_or_else(|| panic!("STRx id {}: unterminated string", i + 1));
            out.push(section[off..off + end].to_vec());
        }
        out
    }

    /// Extract a raw section from a CHK byte blob.
    fn chk_section<'a>(chk: &'a [u8], want: &[u8; 4]) -> Option<&'a [u8]> {
        let mut off = 0;
        while off + 8 <= chk.len() {
            let name = &chk[off..off + 4];
            let ln = i32::from_le_bytes(chk[off + 4..off + 8].try_into().unwrap());
            if ln < 0 || off + 8 + (ln as usize) > chk.len() {
                return None;
            }
            if name == want {
                return Some(&chk[off + 8..off + 8 + (ln as usize)]);
            }
            off += 8 + (ln as usize);
        }
        None
    }

    #[test]
    fn native_lane_str_fallback_produces_valid_strx() {
        // Regression (F43, HIGH): the v0.1 string-less-LIR fallback used
        // to copy the template's raw STR bytes and label them STRx —
        // silently corrupt on STR-only templates (u16 offsets read as
        // u32). This builds a real .scx from the repo's own STR-only
        // basemap through the native path and asserts the output STRx
        // parses and carries the template's strings. The T2 e2e only
        // checked exit 0, which is the hole this closes.
        use std::path::Path;

        let basemap = Path::new(concat!(
            env!("CARGO_MANIFEST_DIR"),
            "/../../tests/outputmap/basemap/basemap.scx"
        ));
        // A real native-lowering LIR: "strings": [] always.
        let lir_json = r#"{"version":"3.0","module":{"entry":0,"items":[{"kind":"trigger","size":2408,"prev":-1,"next":-2147483648,"prev_reloc":false,"next_reloc":false,"conds":[],"acts":[],"preserved":true}],"strings":[]}}"#.to_string();
        assert!(
            lir_json.contains("\"strings\":[]") || lir_json.contains("\"strings\": []"),
            "fixture must be a string-less native LIR"
        );
        let native_lir: eudkit_lir::LirDocument = serde_json::from_str(&lir_json).unwrap();

        // Template's own strings, as loaded (STR preferred-fallback logic
        // inside TemplateStrings::parse).
        let mut ar = stormlib::Archive::open(basemap, stormlib::OpenArchiveFlags::empty()).unwrap();
        let tpl_chk = ar
            .open_file("staredit\\scenario.chk")
            .unwrap()
            .read_all()
            .unwrap();
        drop(ar);
        let mut tpl_chkt = chk::Chk::new();
        assert!(tpl_chkt.loadchk(&tpl_chk));
        let tpl = tbl::TemplateStrings::parse(&tpl_chkt).unwrap();
        let expected: Vec<Vec<u8>> = (1..=tpl.count())
            .map(|id| tpl.string_by_id(id).unwrap().to_vec())
            .collect();
        assert!(
            expected.len() > 100,
            "basemap should have a real string table, got {}",
            expected.len()
        );

        let workdir = tempfile::tempdir().unwrap();
        let out = workdir.path().join("out.scx");
        assemble_map(TailInputs {
            lir: &native_lir,
            basemap,
            out_path: &out,
            sector_size: None,
            mrgndata: None,
            draw_log_json: None,
            queued_files: vec![],
            debug_emit_chk: None,
            no_fake_isom: false,
            assembly: AssemblyOptions::default(),
        })
        .unwrap();
        assert!(out.is_file());

        let mut ar = stormlib::Archive::open(&out, stormlib::OpenArchiveFlags::empty()).unwrap();
        let out_chk = ar
            .open_file("staredit\\scenario.chk")
            .unwrap()
            .read_all()
            .unwrap();
        drop(ar);
        let strx = chk_section(&out_chk, b"STRx").expect("output must carry STRx (not STR)");
        let got = parse_strx_ids(strx);
        assert_eq!(
            got.len(),
            expected.len(),
            "STRx id count must match the template's"
        );
        assert_eq!(
            got, expected,
            "every STRx id must resolve to the template's string"
        );
    }

    /// StrAddr fixture (SCR print redesign, M1): the LIR `eudc`
    /// produced from a one-statement HIR (`MemWrite{addr: StrAddr{8},
    /// value: 0x20006968, size: 4}`) against the test basemap.
    /// String id 8 of that basemap is "Hello World!".
    fn straddr_fixture_lir() -> String {
        use std::path::Path;
        let basemap = Path::new(concat!(
            env!("CARGO_MANIFEST_DIR"),
            "/../../tests/outputmap/basemap/basemap.scx"
        ));
        let info = mapinfo::extract_map_info(basemap).unwrap();
        let mut ar =
            stormlib::Archive::open(basemap, stormlib::OpenArchiveFlags::empty()).unwrap();
        let chk_bytes = ar
            .open_file("staredit\\scenario.chk")
            .unwrap()
            .read_all()
            .unwrap();
        drop(ar);
        let mut chkt = chk::Chk::new();
        assert!(chkt.loadchk(&chk_bytes));
        let tpl = tbl::TemplateStrings::parse(&chkt).unwrap();
        let ordered = decode_strings(&info.strings_b64).unwrap();
        let str_addrs = tbl::string_section_offsets(&ordered, &tpl).unwrap();
        let stroffset = str_addrs[7];
        let want_epd = ((0x191943C8u32 + stroffset - 0x58A364) / 4) as i64;
        let doc = eudkit_lir::LirDocument {
            version: eudkit_lir::LIR_VERSION.to_string(),
            module: eudkit_lir::LirModule {
                entry: 0,
                items: vec![eudkit_lir::LirItem::Trigger(eudkit_lir::TriggerItem {
                    size: 2408,
                    prev: Some(-1),
                    next: Some(-2147483648),
                    prev_reloc: false,
                    next_reloc: false,
                    conds: vec![],
                    acts: vec![eudkit_lir::ActData {
                        act: eudkit_lir::CTrigAction {
                            mask: 0xFFFF_FFFF,
                            strid: 0,
                            wavid: 0,
                            time: 0,
                            dest: want_epd as u32,
                            value: 0x20006968,
                            unit: 0,
                            acttype: 45,
                            modifier: 7,
                            flags: 20,
                            padding: 0,
                            eudx: 0x4353,
                        },
                        uprp: None,
                        relocs: vec![],
                    }],
                    preserved: true,
                    flag: None,
                })],
                strings: info.strings_b64,
                str_addrs,
            },
        };
        serde_json::to_string(&doc).unwrap()
    }

    fn assemble_fixture(
        lir_json: &str,
        basemap: &std::path::Path,
        out: &std::path::Path,
    ) -> Result<TailSummary, MapTailError> {
        let doc: eudkit_lir::LirDocument = serde_json::from_str(lir_json).unwrap();
        assemble_map(TailInputs {
            lir: &doc,
            basemap,
            out_path: out,
            sector_size: None,
            mrgndata: None,
            draw_log_json: None,
            queued_files: vec![],
            debug_emit_chk: None,
            no_fake_isom: false,
            assembly: AssemblyOptions::default(),
        })
    }

    /// [`assemble_fixture`] through the shared-context entry point the compile
    /// pipeline uses (one basemap parse for lowering and assembly).
    fn assemble_fixture_with_context(
        lir_json: &str,
        basemap: &std::path::Path,
        out: &std::path::Path,
    ) -> Result<TailSummary, MapTailError> {
        let doc: eudkit_lir::LirDocument = serde_json::from_str(lir_json).unwrap();
        let ctx = crate::basemap::BasemapContext::open(basemap)?;
        assemble_map_with_context(
            TailInputs {
                lir: &doc,
                basemap,
                out_path: out,
                sector_size: None,
                mrgndata: None,
                draw_log_json: None,
                queued_files: vec![],
                debug_emit_chk: None,
                no_fake_isom: false,
                assembly: AssemblyOptions::default(),
            },
            &ctx,
        )
    }

    #[test]
    fn assembled_cost_includes_the_tick_gate_and_matches_the_written_payload() {
        use std::path::Path;

        let basemap = Path::new(concat!(
            env!("CARGO_MANIFEST_DIR"),
            "/../../tests/outputmap/basemap/basemap.scx"
        ));
        // A string-less, one-trigger native LIR: the payload is pure
        // 2408-byte trigger items, so its trigger count is its size
        // divided by RAW_TRIGGER_BYTE_SIZE.
        let lir_json = r#"{"version":"3.0","module":{"entry":0,"items":[{"kind":"trigger","size":2408,"prev":-1,"next":-2147483648,"prev_reloc":false,"next_reloc":false,"conds":[],"acts":[],"preserved":true}],"strings":[]}}"#;
        let doc: eudkit_lir::LirDocument = serde_json::from_str(lir_json).unwrap();
        let program_triggers = eudkit_lir::static_cost(&doc).triggers;
        assert_eq!(program_triggers, 1, "fixture must be one trigger");

        // String-less LIR uses the template fallback table; its length
        // locates the payload tail inside the output STRx section.
        let mut ar =
            stormlib::Archive::open(basemap, stormlib::OpenArchiveFlags::empty()).unwrap();
        let tpl_chk = ar
            .open_file("staredit\\scenario.chk")
            .unwrap()
            .read_all()
            .unwrap();
        drop(ar);
        let mut tpl_chkt = chk::Chk::new();
        assert!(tpl_chkt.loadchk(&tpl_chk));
        let tpl = tbl::TemplateStrings::parse(&tpl_chkt).unwrap();
        let str_len = tbl::BuiltStringTable::fallback(&tpl).bytes.len();

        let workdir = tempfile::tempdir().unwrap();
        let out = workdir.path().join("out.scx");
        let chk_out = workdir.path().join("out.chk");
        let summary = assemble_map(TailInputs {
            lir: &doc,
            basemap,
            out_path: &out,
            sector_size: None,
            mrgndata: None,
            draw_log_json: None,
            queued_files: vec![],
            debug_emit_chk: Some(&chk_out),
            no_fake_isom: false,
            assembly: AssemblyOptions::default(),
        })
        .unwrap();

        assert_eq!(
            summary.static_cost.triggers,
            program_triggers + tick_gate::GATE_TRIGGER_COUNT,
            "reported cost must include the once-per-tick gate"
        );
        assert_eq!(
            summary.static_cost,
            assembled_static_cost(&doc),
            "the pre-write helper must match the cost reported by assembly"
        );

        let raw_chk = std::fs::read(&chk_out).unwrap();
        let strx = chk_section(&raw_chk, b"STRx").expect("output must carry STRx");
        let str_padding = (4 - str_len % 4) % 4;
        let payload = &strx[str_len + str_padding..];
        assert_eq!(
            payload.len(),
            summary.static_cost.total_bytes,
            "reported bytes must match the bytes written after the STR table"
        );
        assert_eq!(
            payload.len() % eudkit_lir::RAW_TRIGGER_BYTE_SIZE,
            0,
            "fixture payload must contain only full trigger items"
        );
        let payload_triggers = payload.len() / eudkit_lir::RAW_TRIGGER_BYTE_SIZE;
        assert_eq!(
            payload_triggers, summary.static_cost.triggers,
            "reported static cost must equal the assembled payload trigger count"
        );
        assert_eq!(payload_triggers, program_triggers + tick_gate::GATE_TRIGGER_COUNT);
    }

    #[test]
    fn assembled_cost_without_tick_gate_matches_the_written_payload() {
        use std::path::Path;

        let basemap = Path::new(concat!(
            env!("CARGO_MANIFEST_DIR"),
            "/../../tests/outputmap/basemap/basemap.scx"
        ));
        let lir_json = r#"{"version":"3.0","module":{"entry":0,"items":[{"kind":"trigger","size":2408,"prev":-1,"next":-2147483648,"prev_reloc":false,"next_reloc":false,"conds":[],"acts":[],"preserved":true}],"strings":[]}}"#;
        let doc: eudkit_lir::LirDocument = serde_json::from_str(lir_json).unwrap();
        let program_triggers = eudkit_lir::static_cost(&doc).triggers;
        assert_eq!(program_triggers, 1);

        let mut ar =
            stormlib::Archive::open(basemap, stormlib::OpenArchiveFlags::empty()).unwrap();
        let tpl_chk = ar
            .open_file("staredit\\scenario.chk")
            .unwrap()
            .read_all()
            .unwrap();
        drop(ar);
        let mut tpl_chkt = chk::Chk::new();
        assert!(tpl_chkt.loadchk(&tpl_chk));
        let tpl = tbl::TemplateStrings::parse(&tpl_chkt).unwrap();
        let str_len = tbl::BuiltStringTable::fallback(&tpl).bytes.len();

        let workdir = tempfile::tempdir().unwrap();
        let out = workdir.path().join("out.scx");
        let chk_out = workdir.path().join("out.chk");
        let summary = assemble_map(TailInputs {
            lir: &doc,
            basemap,
            out_path: &out,
            sector_size: None,
            mrgndata: None,
            draw_log_json: None,
            queued_files: vec![],
            debug_emit_chk: Some(&chk_out),
            no_fake_isom: false,
            assembly: AssemblyOptions {
                enable_tick_gate: false,
            },
        })
        .unwrap();

        assert_eq!(
            summary.static_cost.triggers, program_triggers,
            "reported cost must reflect payload without the 35 gate triggers"
        );
        assert_eq!(
            summary.static_cost,
            assembled_static_cost_with_options(
                &doc,
                AssemblyOptions { enable_tick_gate: false },
            ),
            "pre-write helper must match assembly static cost when tick gate is disabled"
        );

        let raw_chk = std::fs::read(&chk_out).unwrap();
        let strx = chk_section(&raw_chk, b"STRx").expect("output must carry STRx");
        let str_padding = (4 - str_len % 4) % 4;
        let payload = &strx[str_len + str_padding..];
        assert_eq!(
            payload.len(),
            summary.static_cost.total_bytes,
            "reported bytes must match payload bytes without gate"
        );
        let payload_triggers = payload.len() / eudkit_lir::RAW_TRIGGER_BYTE_SIZE;
        assert_eq!(payload_triggers, program_triggers);
    }

    #[test]
    fn dangling_module_entry_fails_loudly_on_gated_and_ungated_paths() {
        use std::path::Path;

        let basemap = Path::new(concat!(
            env!("CARGO_MANIFEST_DIR"),
            "/../../tests/outputmap/basemap/basemap.scx"
        ));
        // One payload item, so the only valid entry is 0. The gate must not
        // be able to launder the invalid entry: gated assembly records the
        // original entry before appending the gate, and the public contract
        // still has to reject it.
        let lir_json = r#"{"version":"3.0","module":{"entry":1,"items":[{"kind":"trigger","size":2408,"prev":-1,"next":-2147483648,"prev_reloc":false,"next_reloc":false,"conds":[],"acts":[],"preserved":true}],"strings":[]}}"#;
        let doc: eudkit_lir::LirDocument = serde_json::from_str(lir_json).unwrap();

        for enable_tick_gate in [true, false] {
            let workdir = tempfile::tempdir().unwrap();
            let out = workdir.path().join("out.scx");
            let err = assemble_map(TailInputs {
                lir: &doc,
                basemap,
                out_path: &out,
                sector_size: None,
                mrgndata: None,
                draw_log_json: None,
                queued_files: vec![],
                debug_emit_chk: None,
                no_fake_isom: false,
                assembly: AssemblyOptions { enable_tick_gate },
            })
            .err()
            .unwrap_or_else(|| {
                panic!(
                    "dangling module.entry must fail with enable_tick_gate={enable_tick_gate}, \
                     not link to payload offset 0"
                )
            });

            let message = err.to_string();
            assert!(
                message.contains("module.entry 1"),
                "error must name the offending entry (enable_tick_gate={enable_tick_gate}), \
                 got: {message}"
            );
            assert!(
                message.contains("out of range"),
                "error must explain the range violation (enable_tick_gate={enable_tick_gate}), \
                 got: {message}"
            );
            assert!(
                !out.exists(),
                "a failed assembly must not write a map (enable_tick_gate={enable_tick_gate})"
            );
        }
    }

    #[test]
    fn straddr_verification_mismatch_is_loud_and_emits_no_map() {
        use std::path::Path;

        let basemap = Path::new(concat!(
            env!("CARGO_MANIFEST_DIR"),
            "/../../tests/outputmap/basemap/basemap.scx"
        ));
        let mut doc: serde_json::Value =
            serde_json::from_str(&straddr_fixture_lir()).unwrap();
        // Corrupt one recorded offset (id 8, the buffer id): the
        // recomputation must disagree and assembly must refuse.
        let arr = doc["module"]["str_addrs"].as_array_mut().unwrap();
        assert!(arr.len() > 8, "fixture must carry the full offset table");
        let corrupted = arr[7].as_u64().unwrap() + 4;
        arr[7] = serde_json::json!(corrupted);
        let lir_json = serde_json::to_string(&doc).unwrap();

        let workdir = tempfile::tempdir().unwrap();
        let out = workdir.path().join("out.scx");
        let err = match assemble_fixture(&lir_json, basemap, &out) {
            Ok(_) => panic!("mismatched recorded offsets must fail"),
            Err(e) => e,
        };
        assert!(
            err.to_string().contains("E-STRADDR-VERIFY"),
            "unexpected error: {err}"
        );
        assert!(
            !out.is_file(),
            "a failed verification must never emit a map"
        );
    }

    /// The shared-context path (`eudkit compile`) must still refuse a
    /// corrupted recorded offset table. Removing the independent parse must
    /// not turn `E-STRADDR-VERIFY` into a comparison that can only agree.
    #[test]
    fn straddr_verification_is_loud_on_the_shared_context_path() {
        use std::path::Path;

        let basemap = Path::new(concat!(
            env!("CARGO_MANIFEST_DIR"),
            "/../../tests/outputmap/basemap/basemap.scx"
        ));
        let mut doc: serde_json::Value =
            serde_json::from_str(&straddr_fixture_lir()).unwrap();
        let arr = doc["module"]["str_addrs"].as_array_mut().unwrap();
        assert!(arr.len() > 8, "fixture must carry the full offset table");
        arr[7] = serde_json::json!(arr[7].as_u64().unwrap() + 4);
        let lir_json = serde_json::to_string(&doc).unwrap();

        let workdir = tempfile::tempdir().unwrap();
        let out = workdir.path().join("out.scx");
        let err = match assemble_fixture_with_context(&lir_json, basemap, &out) {
            Ok(_) => panic!("mismatched recorded offsets must fail on the context path"),
            Err(e) => e,
        };
        assert!(
            err.to_string().contains("E-STRADDR-VERIFY"),
            "unexpected error: {err}"
        );
        assert!(
            !out.is_file(),
            "a failed verification must never emit a map"
        );
    }

    /// Reusing the lowering parse must not change a single output byte.
    #[test]
    fn shared_context_assembly_is_byte_identical() {
        use std::path::Path;

        let basemap = Path::new(concat!(
            env!("CARGO_MANIFEST_DIR"),
            "/../../tests/outputmap/basemap/basemap.scx"
        ));
        let lir_json = straddr_fixture_lir();
        let workdir = tempfile::tempdir().unwrap();
        let own_parse = workdir.path().join("own.scx");
        let shared = workdir.path().join("shared.scx");
        assemble_fixture(&lir_json, basemap, &own_parse).unwrap();
        assemble_fixture_with_context(&lir_json, basemap, &shared).unwrap();
        assert_eq!(
            std::fs::read(&own_parse).unwrap(),
            std::fs::read(&shared).unwrap(),
            "assembly with and without a handed-off context must be byte-identical"
        );
    }

    #[test]
    fn straddr_verification_refuses_loud_when_strings_appended_past_recorded_table() {
        use std::path::Path;

        let basemap = Path::new(concat!(
            env!("CARGO_MANIFEST_DIR"),
            "/../../tests/outputmap/basemap/basemap.scx"
        ));
        let mut doc: serde_json::Value =
            serde_json::from_str(&straddr_fixture_lir()).unwrap();
        // Simulate a string list that grew past the recorded StrAddr
        // table (the only way this arises is a Str arg the frontend
        // never interned — raw-builder HIR, since both DSLs intern
        // every SymAct string arg). Appending grows the STR header by
        // 4 bytes per id, so every recorded offset would be wrong in
        // the final map — including the StrAddr Consts folded during
        // lowering. Assembly must REFUSE loud (never emit a map with
        // wrong buffer addresses), not silently accept a prefix.
        let n_before = doc["module"]["strings"].as_array().unwrap().len();
        assert_eq!(
            doc["module"]["str_addrs"].as_array().unwrap().len(),
            n_before,
            "fixture table must cover exactly the fixture strings"
        );
        // LIR strings are base64; "Victory!" is "VmljdG9yeSE=".
        doc["module"]["strings"]
            .as_array_mut()
            .unwrap()
            .push(serde_json::json!("VmljdG9yeSE="));
        let lir_json = serde_json::to_string(&doc).unwrap();

        let workdir = tempfile::tempdir().unwrap();
        let out = workdir.path().join("out.scx");
        let err = match assemble_fixture(&lir_json, basemap, &out) {
            Ok(_) => panic!("appended strings past the recorded table must fail"),
            Err(e) => e,
        };
        assert!(
            err.to_string().contains("E-STRADDR-VERIFY"),
            "unexpected error: {err}"
        );
        assert!(
            !out.is_file(),
            "a failed verification must never emit a map"
        );
    }

    #[test]
    fn straddr_end_to_end_scx_strx_and_setdeathsx_player() {
        use std::path::Path;

        let basemap = Path::new(concat!(
            env!("CARGO_MANIFEST_DIR"),
            "/../../tests/outputmap/basemap/basemap.scx"
        ));
        let lir_json = straddr_fixture_lir();
        let workdir = tempfile::tempdir().unwrap();
        let out = workdir.path().join("out.scx");
        // The recorded table verifies (this would fail loud on any
        // replay divergence) and a real .scx is produced.
        assemble_fixture(&lir_json, basemap, &out).unwrap();
        assert!(out.is_file());

        let mut ar =
            stormlib::Archive::open(&out, stormlib::OpenArchiveFlags::empty())
                .unwrap();
        let out_chk = ar
            .open_file("staredit\\scenario.chk")
            .unwrap()
            .read_all()
            .unwrap();
        drop(ar);
        let strx = chk_section(&out_chk, b"STRx").expect("output STRx");
        let ids = parse_strx_ids(strx);
        assert_eq!(ids.len(), 1024);
        assert_eq!(ids[7], b"Hello World!".to_vec());

        // The buffer's section offset, read from the assembled map's
        // own STRx (not from the fixture), gives the expected
        // SetDeathsX player field for the id-8 buffer write.
        let stroffset =
            u32::from_le_bytes(strx[4 + 4 * 7..4 + 4 * 8].try_into().unwrap());
        let want_epd = (0x191943C8u32 + stroffset - 0x58A364) / 4;
        let doc: serde_json::Value =
            serde_json::from_str(&lir_json).unwrap();
        let mut hits = 0;
        for item in doc["module"]["items"].as_array().unwrap() {
            if item["kind"] != "trigger" {
                continue;
            }
            let acts = item["acts"].as_array().unwrap();
            for act in acts {
                let is_deathsx = act["acttype"].as_i64() == Some(45);
                let player = act["dest"].as_i64().unwrap_or(-1);
                let number = act["value"].as_i64().unwrap_or(-1);
                if is_deathsx
                    && player == want_epd as i64
                    && number == 0x20006968
                    && acts.len() == 1
                {
                    hits += 1;
                }
            }
        }
        assert_eq!(
            hits, 1,
            "exactly one single-action SetDeathsX must write the dword              to buffer id 8 (player field {want_epd})"
        );
    }

    #[test]
    fn assemble_map_mrgn_partial_record_is_loud() {
        // Assemble_map-level form of the probe case: the real
        // basemap's MRGN is truncated to 38 bytes (one full entry +
        // 18 bytes of a second entry) and written back into a copy
        // of the basemap. assemble_map must fail loud at STR parse
        // (TemplateStrings::parse), before any mutation, rather than
        // silently building a map that lost a location.
        use std::path::Path;

        let basemap = Path::new(concat!(
            env!("CARGO_MANIFEST_DIR"),
            "/../../tests/outputmap/basemap/basemap.scx"
        ));
        let mut ar =
            stormlib::Archive::open(basemap, stormlib::OpenArchiveFlags::empty()).unwrap();
        let chk_bytes = ar
            .open_file("staredit\\scenario.chk")
            .unwrap()
            .read_all()
            .unwrap();
        drop(ar);
        let mut chkt = chk::Chk::new();
        assert!(chkt.loadchk(&chk_bytes));
        let mrgn = chkt.getsection(b"MRGN").unwrap().to_vec();
        assert_eq!(mrgn.len() % 20, 0);
        chkt.setsection(*b"MRGN", mrgn[..38].to_vec());
        let mut rng = rng::CpRandom::seed_zero();
        let rawchk = chkt.savechk(&mut rng, false);

        let workdir = tempfile::tempdir().unwrap();
        let malformed = workdir.path().join("malformed.scx");
        mpq::write_output_map(&malformed, basemap, &rawchk, None, &[]).unwrap();
        let out = workdir.path().join("out.scx");
        let lir_json = r#"{"version":"3.0","module":{"entry":0,"items":[{"kind":"trigger","size":2408,"prev":-1,"next":-2147483648,"prev_reloc":false,"next_reloc":false,"conds":[],"acts":[],"preserved":true}],"strings":[]}}"#.to_string();
        assert!(matches!(
            assemble_fixture(&lir_json, &malformed, &out),
            Err(MapTailError::TruncatedSection("MRGN"))
        ));
        assert!(!out.is_file());
    }

    #[test]
    fn string_and_cell_blob_coexist_through_assemble_map() {
        // Ticket 9's assemble_map tests assert output STRx ids but
        // their modules carry no cell-demoted variable, and ticket 8's
        // String + BitCell test covers String + cell coexistence only
        // through lower_document + sim, with no assemble_map call.
        // This test carries BOTH through assemble_map: a String
        // display write (LIR strings + recorded str_addrs, buffer id
        // 8 = "Hello World!") and a cell-demoted boolean (a Blob item
        // holding one dword with bit 0 set — the lowered form of a
        // BitCell set to 1) — and asserts both the output STRx ids
        // and the cell blob bytes in the output map.
        use std::path::Path;

        let basemap = Path::new(concat!(
            env!("CARGO_MANIFEST_DIR"),
            "/../../tests/outputmap/basemap/basemap.scx"
        ));
        let info = mapinfo::extract_map_info(basemap).unwrap();
        let mut ar =
            stormlib::Archive::open(basemap, stormlib::OpenArchiveFlags::empty()).unwrap();
        let chk_bytes = ar
            .open_file("staredit\\scenario.chk")
            .unwrap()
            .read_all()
            .unwrap();
        drop(ar);
        let mut chkt = chk::Chk::new();
        assert!(chkt.loadchk(&chk_bytes));
        let tpl = tbl::TemplateStrings::parse(&chkt).unwrap();
        let ordered = decode_strings(&info.strings_b64).unwrap();
        let str_addrs = tbl::string_section_offsets(&ordered, &tpl).unwrap();
        let built = tbl::BuiltStringTable::from_ordered(&ordered, &tpl).unwrap();
        let stroffset = str_addrs[7];
        let want_epd = payload_init::epd(payload_init::STR_SECTION_BASE + stroffset as i64);

        // One exec trigger writing to the id-8 String buffer, then the
        // cell blob: one dword, bit 0 set (a BitCell set to 1).
        let cell_dword = 1u32.to_le_bytes();
        let doc = eudkit_lir::LirDocument {
            version: eudkit_lir::LIR_VERSION.to_string(),
            module: eudkit_lir::LirModule {
                entry: 0,
                items: vec![
                    eudkit_lir::LirItem::Trigger(eudkit_lir::TriggerItem {
                        size: eudkit_lir::RAW_TRIGGER_BYTE_SIZE,
                        prev: Some(-1),
                        next: Some(-2147483648),
                        prev_reloc: false,
                        next_reloc: false,
                        conds: vec![],
                        acts: vec![eudkit_lir::ActData {
                            act: eudkit_lir::CTrigAction {
                                mask: 0xFFFF_FFFF,
                                strid: 0,
                                wavid: 0,
                                time: 0,
                                dest: want_epd as u32,
                                value: 0x20006968,
                                unit: 0,
                                acttype: 45,
                                modifier: 7,
                                flags: 20,
                                padding: 0,
                                eudx: 0x4353,
                            },
                            uprp: None,
                            relocs: vec![],
                        }],
                        preserved: true,
                        flag: None,
                    }),
                    eudkit_lir::LirItem::Blob(eudkit_lir::BlobItem {
                        size: cell_dword.len(),
                        data: eudkit_lir::b64_encode(&cell_dword),
                        relocs: vec![],
                    }),
                ],
                strings: info.strings_b64,
                str_addrs,
            },
        };
        let lir_json = serde_json::to_string(&doc).unwrap();

        let workdir = tempfile::tempdir().unwrap();
        let out = workdir.path().join("out.scx");
        assemble_fixture(&lir_json, basemap, &out).unwrap();
        assert!(out.is_file());

        let mut ar = stormlib::Archive::open(&out, stormlib::OpenArchiveFlags::empty()).unwrap();
        let out_chk = ar.open_file("staredit\\scenario.chk").unwrap().read_all().unwrap();
        drop(ar);
        let strx = chk_section(&out_chk, b"STRx").expect("output STRx");

        // String side: every output STRx id resolves to the template's
        // string, including id 8 = "Hello World!" (the display buffer).
        let expected: Vec<Vec<u8>> = (1..=tpl.count())
            .map(|id| tpl.string_by_id(id).unwrap().to_vec())
            .collect();
        let ids = parse_strx_ids(strx);
        assert_eq!(ids, expected, "output STRx ids must match the template's strings");
        assert_eq!(ids[7], b"Hello World!".to_vec());

        // Cell side: the output STRx section is table + padding +
        // payload (exec trigger, then cell blob). The blob sits at
        // exactly table_len + padding + trigger size and must carry
        // the demoted bit, byte-identical — the prt/ort path does not
        // touch it (the fixture has no relocs).
        // Issue #39: the once-per-tick gate appends after every original
        // item, so the blob position is unchanged and only the total
        // length grows by the gate size.
        let table_len = built.bytes.len();
        let padding = (4 - table_len % 4) % 4;
        let blob_at = table_len + padding + eudkit_lir::RAW_TRIGGER_BYTE_SIZE;
        assert_eq!(
            strx.len(),
            blob_at
                + cell_dword.len()
                + tick_gate::GATE_TRIGGER_COUNT * eudkit_lir::RAW_TRIGGER_BYTE_SIZE,
            "output STRx must be exactly table + padding + trigger + blob + gate"
        );
        assert_eq!(
            &strx[blob_at..blob_at + cell_dword.len()],
            &cell_dword,
            "cell blob dword must survive assembly intact"
        );
    }
}
