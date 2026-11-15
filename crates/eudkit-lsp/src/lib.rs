//! `eudkit-lsp`: completions, hover docs, and domain diagnostics for the
//! unified TypeScript/Python DSL surface.
//!
//! The server is a small JSON-RPC 2.0 stdio loop (see `server`). All editor
//! intelligence lives in pure functions over document text, so it is testable
//! without a running editor.
//!
//! Receiver guessing is a heuristic, not inference: a receiver is matched
//! by its identifier (`u`/`unit` -> CUnit, `s`/`sprite` -> CSprite, ...),
//! so `s.` for a FixedString variable named `s` returns CSprite members. The
//! server never tracks bindings (`x = CUnit(...)` inference would need a
//! typechecker) and never compiles: completions, hover, and diagnostics
//! are editor-surface only and cannot change compiled map bytes.
//!
//! Member tables for CUnit/CSprite are generated at build time from the
//! schema layouts (see `build.rs`); diagnostics run over the lexer in
//! `lexer`, which treats string literals as single tokens and skips
//! comments, so text inside literals and comments never triggers a
//! diagnostic. Comment and string handling is language-aware: Python
//! documents additionally skip `#` comments and triple-quoted strings,
//! while other documents keep exactly the previous behaviour, so
//! TypeScript `#private` fields are never hidden.
//!
//! Porting note (RESOLVED on main): the arch branch's open checklist
//! above is closed by this port — FixedArray/FixedString are already in
//! the vocabulary on main, the hand CUnit/CSprite table is deleted in
//! favour of the build-time generated tables, and the bound re-check is
//! the FixedString n<1 port itself (the DSL constructor throws for
//! size <= 0).

use std::sync::LazyLock;

use serde::Serialize;
use serde_json::{Value, json};

pub mod lexer;
pub mod server;

use lexer::{
    count_top_level_items, matching, split_top_level, tokenize_with_language, Language, Token,
};

#[derive(Debug, Clone, Copy)]
pub struct SymbolDoc {
    pub name: &'static str,
    pub detail: &'static str,
    pub doc: &'static str,
}

#[derive(Debug, Clone, Copy)]
pub struct MemberDoc {
    pub name: &'static str,
    pub detail: &'static str,
    pub doc: &'static str,
}

// Stock and extension helper entries are generated at build time from
// `schema/stock-rows.json` (see `build.rs` and `build/stock_vocabulary.rs`),
// the same spec the DSL goldens are frozen from. They used to be hand-copied
// here and drifted from the helpers; a row edit now moves the vocabulary with
// it. A missing/unparseable spec or a stale curated doc-overlay key fails the
// build.
include!(concat!(env!("OUT_DIR"), "/stock_vocabulary.rs"));

/// Hand-written DSL vocabulary; the stock and extension helper entries are
/// appended from [`STOCK_VOCABULARY`].
const CORE_VOCABULARY: &[SymbolDoc] = &[
    SymbolDoc { name: "Trigger", detail: "Trigger (16C, 64A)", doc: "First-class StarCraft trigger node with 16 condition slots, 64 action slots, execution flags, and prev/next links. Serves as both the batching unit and the backing store for TriggerPages/TriggerStructs." },
    SymbolDoc { name: "TriggerStruct", detail: "semantic struct container", doc: "Allocated mutable struct whose fields occupy SetDeathsX action slots of a Trigger. Fields support byte/word/dword widths; whole-struct copies are one executed trigger." },
    SymbolDoc { name: "MemView", detail: "typed memory layout view", doc: "Zero-storage typed layout over an EPD or CP cursor. Access fields directly as properties, e.g. `u.hp` on CUnit." },
    SymbolDoc { name: "MemCond", detail: "in-place memory comparison", doc: "Single-condition in-place memory comparison against an EPD without materializing into a variable (1C / 1T when the EPD is constant)." },
    SymbolDoc { name: "CondView", detail: "20-byte condition-slot view", doc: "MemView over a Trigger condition slot. Access via `trig.cond(0..15)` and fields such as `.player`, `.amount`, `.condtype`." },
    SymbolDoc { name: "ActView", detail: "32-byte action-slot view", doc: "MemView over a Trigger action slot. Access via `trig.act(0..63)` and fields such as `.dest`, `.value`, `.mask`, `.modifier`." },
    SymbolDoc { name: "FixedArray", detail: "fixed-capacity array", doc: "Writable arrays live in page-aligned TriggerPage slots; `FixedArray.const([...])` is a zero-page build-time payload table. Dynamic indices use carry-free PageSlotEPD math." },
    SymbolDoc { name: "Queue", detail: "fixed-capacity FIFO", doc: "Fixed-capacity FIFO with a shared out box. `append` enqueues and `popleft` dequeues through one statically bound slot trigger per element." },
    SymbolDoc { name: "Deque", detail: "fixed-capacity double-ended queue", doc: "Fixed-capacity deque with `append`, `appendleft`, `pop`, and `popleft` over statically bound slot triggers." },
    SymbolDoc { name: "Stack", detail: "fixed-capacity LIFO", doc: "Fixed-capacity LIFO. `push` appends and `pop` returns the shared out box." },
    SymbolDoc { name: "UnitGroup", detail: "fixed-capacity unit set", doc: "Fixed-capacity unit collection with add/remove/prune and automatic dead-unit pruning via a placeholder Trigger probe." },
    SymbolDoc { name: "FixedString", detail: "STR-backed text buffer", doc: "Fixed-capacity text buffer backed by the map STR table. `write` starts at byte 0, `append` continues from lastpos, and `writeFrom` starts at an explicit offset." },
    SymbolDoc { name: "buildModule", detail: "HIR construction entry", doc: "Runs the module body once at compile time and returns a CompiledModule. The root source must export the result as `module` (`export const module = buildModule(...)`). Helpers may build modules, but only the root source provides the CLI entry." },
    SymbolDoc { name: "build_module", detail: "Python HIR construction entry", doc: "Python spelling of `buildModule`. The root source must assign the result to `module` (`module = build_module(body)`); helpers may build modules, but only the root source provides the CLI entry." },
    SymbolDoc { name: "Var", detail: "scalar variable", doc: "Scalar with a build-time initial value. The optimizer places it on a TriggerPage or demotes it to a dword/bit Cell." },
    SymbolDoc { name: "when", detail: "loop-cadence block", doc: "Runs `body` every trigger loop while `cond` holds." },
    SymbolDoc { name: "once", detail: "one-shot block", doc: "Runs `body` on the first trigger loop where `cond` holds, then permanently disables." },
    SymbolDoc { name: "until", detail: "run-until block", doc: "Runs `body` every loop while `cond` is false, then permanently disables." },
    SymbolDoc { name: "onRising", detail: "rising-edge block", doc: "Runs on the trigger loop where `cond` transitions false -> true." },
    SymbolDoc { name: "on_rising", detail: "Python rising-edge block", doc: "Python spelling of `onRising`." },
    SymbolDoc { name: "onFalling", detail: "falling-edge block", doc: "Runs on the trigger loop where `cond` transitions true -> false." },
    SymbolDoc { name: "on_falling", detail: "Python falling-edge block", doc: "Python spelling of `onFalling`." },
    SymbolDoc { name: "every", detail: "modulo cadence", doc: "Runs `body` every N trigger loops." },
    SymbolDoc { name: "after", detail: "countdown cadence", doc: "Arms an N-loop countdown when reached and fires once at zero." },
    SymbolDoc { name: "whileLoop", detail: "structured intra-frame loop", doc: "Structured intra-frame loop repeating `body` as long as `conds` hold." },
    SymbolDoc { name: "while_loop", detail: "Python structured loop", doc: "Python spelling of `whileLoop`." },
    SymbolDoc { name: "condAll", detail: "condition conjunction", doc: "Conjoin condition entries (`condAll(...conds)`) at zero trigger overhead." },
    SymbolDoc { name: "cond_all", detail: "Python condition conjunction", doc: "Conjoin condition entries (`cond_all(...conds)`) at zero trigger overhead." },
    SymbolDoc { name: "condAny", detail: "condition disjunction", doc: "Disjoin condition clauses (`condAny(...conds)`) via a shared flag gate." },
    SymbolDoc { name: "cond_any", detail: "Python condition disjunction", doc: "Disjoin condition clauses (`cond_any(...conds)`) via a shared flag gate." },
    SymbolDoc { name: "condNot", detail: "condition negation", doc: "Negate a condition (`condNot(cond)`) via De Morgan, `complement()` rewrite, or flag gate fallback." },
    SymbolDoc { name: "cond_not", detail: "Python condition negation", doc: "Negate a condition (`cond_not(cond)`) via De Morgan, `complement()` rewrite, or flag gate fallback." },
    SymbolDoc { name: "Func", detail: "reusable procedure", doc: "Define a reusable procedure." },
    SymbolDoc { name: "func", detail: "Python reusable procedure", doc: "Python spelling of `Func`." },
    SymbolDoc { name: "withCP", detail: "bind CurrentPlayer", doc: "Binds CurrentPlayer (`0x6509B0`) to an EPD for CP-relative memory access." },
    SymbolDoc { name: "with_cp", detail: "Python bind CurrentPlayer", doc: "Python spelling of `withCP`." },
    SymbolDoc { name: "memcpyEPD", detail: "block copy", doc: "Copies `count` dwords from `srcEPD` to `dstEPD`; a constant count of 1 emits the copy body once with no loop, and constant zero folds to 0T." },
    SymbolDoc { name: "memcpy_epd", detail: "Python block copy", doc: "Python spelling of `memcpyEPD`." },
    SymbolDoc { name: "memsetEPD", detail: "block fill", doc: "Fills `count` dwords at `dstEPD` with a 32-bit value; a constant `(dstEPD, value, count)` fill with `ceil(count/64) <= 512` (`MAX_FOLD_CHUNKS`, a static byte budget) emits packed `SetDeaths` triggers instead of the runtime loop, constant zero folds to 0T, and a constant count above `0xFFFFFFFF` is rejected with `EUDSL-RANGE`." },
    SymbolDoc { name: "memset_epd", detail: "Python block fill", doc: "Python spelling of `memsetEPD`." },
];

/// The complete editor vocabulary: hand-written core entries followed by the
/// stock/extension entries generated from `schema/stock-rows.json`.
pub static VOCABULARY: LazyLock<Vec<SymbolDoc>> = LazyLock::new(|| {
    let mut vocabulary = CORE_VOCABULARY.to_vec();
    vocabulary.extend_from_slice(STOCK_VOCABULARY);
    vocabulary
});

const TRIGGER: &[MemberDoc] = &[
    MemberDoc { name: "act", detail: "ActView", doc: "Return the `ActView` for action slot 0..63." },
    MemberDoc { name: "cond", detail: "CondView", doc: "Return the `CondView` for condition slot 0..15." },
    MemberDoc { name: "id", detail: "number", doc: "Build-time trigger id, used by self-relocations." },
    MemberDoc { name: "conds", detail: "HirCond[]", doc: "The trigger's condition slots (max 16)." },
    MemberDoc { name: "acts", detail: "HirAct[]", doc: "The trigger's action slots (max 64)." },
    MemberDoc { name: "preserved", detail: "boolean", doc: "Whether the trigger is preserved across loops." },
    MemberDoc { name: "flags", detail: "u32", doc: "Raw execution flags." },
    MemberDoc { name: "next", detail: "TriggerPtr", doc: "Explicit next trigger pointer." },
    MemberDoc { name: "prev", detail: "TriggerPtr", doc: "Explicit previous trigger pointer." },
];

const ACT_VIEW: &[MemberDoc] = &[
    MemberDoc { name: "mask", detail: "u32", doc: "SetDeathsX bit mask for the action slot." },
    MemberDoc { name: "dest", detail: "EPD", doc: "Destination EPD (or relocated pointer field)." },
    MemberDoc { name: "value", detail: "u32", doc: "Action value / pointer field." },
    MemberDoc { name: "unit", detail: "u16", doc: "Unit id field." },
    MemberDoc { name: "modifier", detail: "u8", doc: "Action modifier (SetTo/Add/Subtract...)." },
    MemberDoc { name: "acttype", detail: "u8", doc: "Raw StarCraft action type (45 = SetDeaths/SetDeathsX)." },
    MemberDoc { name: "flags", detail: "u8", doc: "Action flags." },
    MemberDoc { name: "pad", detail: "u8", doc: "Padding byte." },
    MemberDoc { name: "eudx", detail: "u16", doc: "EUD extension magic (`0x4353`) used by masked writes." },
    MemberDoc { name: "strid", detail: "u32", doc: "String id field for text actions." },
    MemberDoc { name: "wavid", detail: "u32", doc: "WAV id field for sound actions." },
    MemberDoc { name: "time", detail: "u32", doc: "Time field for timed actions." },
];

const COND_VIEW: &[MemberDoc] = &[
    MemberDoc { name: "mask", detail: "u32", doc: "Condition mask field (DeathsX mask)." },
    MemberDoc { name: "locid", detail: "u32", doc: "Location/amount mask field (DeathsX mask)." },
    MemberDoc { name: "player", detail: "u32", doc: "Player or EPD field." },
    MemberDoc { name: "amount", detail: "u32", doc: "Comparison amount." },
    MemberDoc { name: "unit", detail: "u16", doc: "Unit id field." },
    MemberDoc { name: "comparison", detail: "u8", doc: "Comparison operator (AtLeast/AtMost/Exactly)." },
    MemberDoc { name: "condtype", detail: "u8", doc: "Raw condition type." },
    MemberDoc { name: "restype", detail: "u8", doc: "Resource type field." },
    MemberDoc { name: "flags", detail: "u8", doc: "Condition flags." },
    MemberDoc { name: "eudx", detail: "u16", doc: "EUD extension magic." },
];

// CUnit/CSprite member tables are generated at build time from
// `schema/layouts/cunit.json` and `csprite.json` (see `build.rs`), the
// same files the DSL reads. They used to be hand-copied here and drifted:
// the hand table suggested `player`, `positionX`, and `positionY` on
// CUnit, all rejected by the DSL. The generated tables contain every
// size>0 schema field with its schema aliases, in offset order.
include!(concat!(env!("OUT_DIR"), "/generated.rs"));

const STRING: &[MemberDoc] = &[
    MemberDoc { name: "write", detail: "(...args) => void", doc: "Overwrite from byte offset 0 and write a trailing NUL." },
    MemberDoc { name: "append", detail: "(...args) => void", doc: "Continue from lastpos, overwriting the previous trailing NUL." },
    MemberDoc { name: "writeFrom", detail: "(pos, ...args) => void", doc: "Write from an explicit constant or game-time byte offset." },
    MemberDoc { name: "print", detail: "(...args) => void", doc: "Write from offset 0, then display the string in chat." },
    MemberDoc { name: "printAt", detail: "(line, ...args) => void", doc: "Write from offset 0 and display on a chat line 0..10." },
    MemberDoc { name: "display", detail: "() => void", doc: "Display the reserved STR entry." },
    MemberDoc { name: "displayAt", detail: "(line) => void", doc: "Display on a chat line 0..10 and restore the previous line." },
    MemberDoc { name: "clear", detail: "() => void", doc: "Clear the first dword and restart writing at offset 0." },
];

const ARRAY: &[MemberDoc] = &[
    MemberDoc { name: "length", detail: "number", doc: "Fixed build-time capacity." },
    MemberDoc { name: "elemType", detail: "FixedArrayElemType", doc: "Element type: byte/word/dword/var or a TriggerStruct schema." },
    MemberDoc { name: "vars", detail: "VarImpl[]", doc: "Per-element backing variables for primitive arrays." },
    MemberDoc { name: "pages", detail: "number", doc: "TriggerPage count used by a writable array." },
];

fn all_members() -> Vec<MemberDoc> {
    let mut out = Vec::new();
    out.extend_from_slice(TRIGGER);
    out.extend_from_slice(ACT_VIEW);
    out.extend_from_slice(COND_VIEW);
    out.extend_from_slice(CUNIT_GENERATED);
    out.extend_from_slice(CSPRITE_GENERATED);
    out.extend_from_slice(STRING);
    out.extend_from_slice(ARRAY);
    out
}

fn receiver_members(receiver: &str) -> Vec<MemberDoc> {
    let r = receiver.trim_end();
    if r.ends_with(')') {
        if r.contains(".act(") {
            return ACT_VIEW.to_vec();
        }
        if r.contains(".cond(") {
            return COND_VIEW.to_vec();
        }
    }
    let ident = r
        .rsplit(|c: char| !(c.is_alphanumeric() || c == '_'))
        .next()
        .unwrap_or("");
    let lower = ident.to_ascii_lowercase();
    if lower == "trig" || lower == "trigger" || lower == "t" {
        TRIGGER.to_vec()
    } else if lower == "unit" || lower == "u" || lower.starts_with("unit") {
        CUNIT_GENERATED.to_vec()
    } else if lower == "sprite" || lower == "s" || lower.starts_with("sprite") {
        CSPRITE_GENERATED.to_vec()
    } else if lower == "str" || lower == "msg" || lower.starts_with("str") {
        STRING.to_vec()
    } else if lower == "arr" || lower.starts_with("arr") {
        ARRAY.to_vec()
    } else {
        all_members()
    }
}

fn doc_for_member(name: &str, context: Option<&str>) -> Option<MemberDoc> {
    let pool = match context {
        Some(ctx) => receiver_members(ctx),
        None => all_members(),
    };
    pool.into_iter().find(|m| m.name == name)
}

fn vocab_doc(name: &str) -> Option<SymbolDoc> {
    VOCABULARY.iter().copied().find(|s| s.name == name)
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct CompletionItem {
    pub label: String,
    pub kind: u8,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub detail: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub documentation: Option<Value>,
    #[serde(rename = "insertText", skip_serializing_if = "Option::is_none")]
    pub insert_text: Option<String>,
}

pub fn completions(text: &str, line: usize, character: usize) -> Vec<CompletionItem> {
    let offset = offset_at(text, line, character);
    let before = &text[..offset];
    let (start, prefix) = word_before(before);
    let receiver = receiver_before_dot(&before[..start])
        .or_else(|| receiver_before_dot(before));
    let members = match &receiver {
        Some(r) => receiver_members(r),
        None => Vec::new(),
    };
    let items: Vec<CompletionItem> = if !members.is_empty() {
        members
            .into_iter()
            .filter(|m| m.name.to_ascii_lowercase().starts_with(&prefix.to_ascii_lowercase()))
            .map(|m| CompletionItem {
                label: m.name.to_string(),
                kind: 10,
                detail: Some(m.detail.to_string()),
                documentation: Some(json!({"kind": "markdown", "value": m.doc})),
                insert_text: None,
            })
            .collect()
    } else {
        VOCABULARY
            .iter()
            .filter(|s| s.name.to_ascii_lowercase().starts_with(&prefix.to_ascii_lowercase()))
            .map(|s| CompletionItem {
                label: s.name.to_string(),
                kind: 3,
                detail: Some(s.detail.to_string()),
                documentation: Some(json!({"kind": "markdown", "value": s.doc})),
                insert_text: None,
            })
            .collect()
    };
    items
}

pub fn hover(text: &str, line: usize, character: usize) -> Option<String> {
    let offset = offset_at(text, line, character);
    let (start, word) = word_at(text, offset)?;
    let context = context_before(text, start);
    let doc = doc_for_member(&word, context.as_deref())
        .map(|m| format!("**{}** — `{}`\n\n{}", m.name, m.detail, m.doc))
        .or_else(|| {
            vocab_doc(&word).map(|s| format!("**{}** — {}\n\n{}", s.name, s.detail, s.doc))
        })?;
    Some(doc)
}

fn receiver_before_dot(before: &str) -> Option<String> {
    let trimmed = before.trim_end();
    if !trimmed.ends_with('.') {
        return None;
    }
    let prefix = &trimmed[..trimmed.len() - 1];
    let start = prefix
        .rfind(|c: char| c == '\n' || c == ';' || c == '{' || c == '}')
        .map(|i| i + 1)
        .unwrap_or(0);
    let receiver = prefix[start..].trim();
    if receiver.is_empty() {
        None
    } else {
        Some(receiver.to_string())
    }
}

fn context_before(text: &str, word_start: usize) -> Option<String> {
    let before = &text[..word_start];
    receiver_before_dot(before)
}

/// Find the identifier immediately before the cursor (used as completion prefix).
fn word_before(before: &str) -> (usize, String) {
    let mut start = before.len();
    while start > 0 {
        let ch = before[..start].chars().next_back().unwrap();
        if ch.is_alphanumeric() || ch == '_' {
            start -= ch.len_utf8();
        } else {
            break;
        }
    }
    (start, before[start..].to_string())
}

/// Identifier at a byte offset, with its byte start.
fn word_at(text: &str, offset: usize) -> Option<(usize, String)> {
    let mut start = offset.min(text.len());
    while start > 0 {
        let ch = text[..start].chars().next_back()?;
        if ch.is_alphanumeric() || ch == '_' {
            start -= ch.len_utf8();
        } else {
            break;
        }
    }
    let mut end = offset.min(text.len());
    while end < text.len() {
        let ch = text[end..].chars().next()?;
        if ch.is_alphanumeric() || ch == '_' {
            end += ch.len_utf8();
        } else {
            break;
        }
    }
    if start == end {
        return None;
    }
    Some((start, text[start..end].to_string()))
}

pub fn offset_at(text: &str, line: usize, character: usize) -> usize {
    let mut offset = 0usize;
    for (idx, line_text) in text.split_inclusive('\n').enumerate() {
        if idx == line {
            let mut units = 0usize;
            for (byte_idx, ch) in line_text.char_indices() {
                if units >= character || ch == '\n' || ch == '\r' {
                    return offset + byte_idx;
                }
                units += ch.len_utf16();
            }
            return offset + line_text.len();
        }
        offset += line_text.len();
    }
    text.len()
}

pub fn position_at(text: &str, offset: usize) -> (usize, usize) {
    let offset = offset.min(text.len());
    let mut line = 0usize;
    let mut line_start = 0usize;
    for (idx, ch) in text[..offset].char_indices() {
        if ch == '\n' {
            line += 1;
            line_start = idx + 1;
        }
    }
    let character = text[line_start..offset].chars().map(char::len_utf16).sum();
    (line, character)
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct Diagnostic {
    pub range: Value,
    pub severity: u8,
    pub source: String,
    pub code: String,
    pub message: String,
}

pub fn diagnostics(text: &str) -> Vec<Diagnostic> {
    diagnostics_for(text, Language::Other)
}

/// Diagnostics for a document of the given language.
///
/// `Language::Other` behaves exactly like [`diagnostics`]; `Language::Python`
/// additionally skips `#` comments and triple-quoted strings when scanning,
/// so Python-native comment and string styles never produce false positives.
pub fn diagnostics_for(text: &str, language: Language) -> Vec<Diagnostic> {
    let tokens = tokenize_with_language(text, language);
    let mut out = Vec::new();
    for call in find_calls(&tokens, text, "FixedString") {
        if let Some(first) = call.args.first() {
            if let Some(n) = parse_int(first) {
                // The DSL constructor throws for size <= 0; the old check
                // only caught 0 and > 65535, so negative sizes were missed.
                if n < 1 || n > 65_535 {
                    out.push(diag(
                        text,
                        call.start,
                        call.end,
                        "EUDSL-BOUNDS",
                        format!("FixedString size must be in 1..=65535, got {n}"),
                    ));
                }
            }
        }
    }
    for (name, index) in [
        ("FixedArray", 1usize),
        ("Queue", 1),
        ("Deque", 1),
        ("Stack", 0),
        ("UnitGroup", 0),
    ] {
        for call in find_calls(&tokens, text, name) {
            if let Some(arg) = call.args.get(index) {
                if let Some(n) = parse_int(arg) {
                    if n <= 0 {
                        out.push(diag(
                            text,
                            call.start,
                            call.end,
                            "EUDSL-BOUNDS",
                            format!("{name} capacity must be a positive integer, got {n}"),
                        ));
                    }
                }
            }
        }
    }
    out.extend(trigger_capacity_diagnostics(&tokens, text));
    out
}

struct Call {
    start: usize,
    end: usize,
    args: Vec<String>,
}

/// Calls to `name` found in the token stream, excluding method calls
/// (`x.String(...)`) and text inside string literals or comments (which the
/// lexer never emits as identifier tokens).
fn find_calls(tokens: &[Token], text: &str, name: &str) -> Vec<Call> {
    let mut out = Vec::new();
    for (idx, tok) in tokens.iter().enumerate() {
        if !tok.is_ident(name) {
            continue;
        }
        if idx > 0 && tokens[idx - 1].is_punct('.') {
            continue;
        }
        let Some(open) = tokens.get(idx + 1) else {
            continue;
        };
        if !open.is_punct('(') {
            continue;
        }
        let Some(close) = matching(tokens, idx + 1, '(', ')') else {
            continue;
        };
        let args = split_top_level(text, tokens, idx + 2, close);
        out.push(Call {
            start: tok.start,
            end: tokens[close].end,
            args,
        });
    }
    out
}

/// Trigger capacity diagnostics over the token stream, covering both DSL
/// spellings: the TypeScript object form (`Trigger({ conds: [...] })`,
/// including `new Trigger({ ... })`) and the Python kwargs form
/// (`Trigger(conds=[...], acts=[...])`). The old text scanner required a
/// `{` after `Trigger` and therefore never flagged the Python form; it also
/// counted commas and closed brackets inside string literals.
fn trigger_capacity_diagnostics(tokens: &[Token], text: &str) -> Vec<Diagnostic> {
    let mut out = Vec::new();
    for (idx, tok) in tokens.iter().enumerate() {
        if !tok.is_ident("Trigger") {
            continue;
        }
        if idx > 0 && tokens[idx - 1].is_punct('.') {
            continue;
        }
        let Some(next) = tokens.get(idx + 1) else {
            continue;
        };
        if next.is_punct('{') {
            if let Some(close) = matching(tokens, idx + 1, '{', '}') {
                check_trigger_body(tokens, text, idx + 1, close, &mut out);
            }
        } else if next.is_punct('(') {
            let Some(close) = matching(tokens, idx + 1, '(', ')') else {
                continue;
            };
            if tokens.get(idx + 2).is_some_and(|t| t.is_punct('{')) {
                if let Some(bclose) = matching(tokens, idx + 2, '{', '}') {
                    check_trigger_body(tokens, text, idx + 2, bclose, &mut out);
                }
            } else {
                check_trigger_kwargs(tokens, text, idx + 2, close, &mut out);
            }
        }
    }
    out
}

/// First `key: [...]` (or `key = [...]`) inside a `{ ... }` body, per key.
fn check_trigger_body(
    tokens: &[Token],
    text: &str,
    open: usize,
    close: usize,
    out: &mut Vec<Diagnostic>,
) {
    for (key, limit) in [("conds", 16usize), ("acts", 64)] {
        let mut i = open + 1;
        while i + 2 < close {
            if tokens[i].is_ident(key)
                && (tokens[i + 1].is_punct(':') || tokens[i + 1].is_punct('='))
                && tokens[i + 2].is_punct('[')
            {
                if let Some(bclose) = matching(tokens, i + 2, '[', ']') {
                    let count = count_top_level_items(tokens, i + 3, bclose);
                    if count > limit {
                        out.push(diag(
                            text,
                            tokens[i].start,
                            tokens[bclose].end,
                            "EUDSL-BOUNDS",
                            format!("Trigger {key} has {count} entries; the maximum is {limit}"),
                        ));
                    }
                }
                break;
            }
            i += 1;
        }
    }
}

/// First top-level `key=[...]` inside a `( ... )` argument list, per key.
fn check_trigger_kwargs(
    tokens: &[Token],
    text: &str,
    open: usize,
    close: usize,
    out: &mut Vec<Diagnostic>,
) {
    for (key, limit) in [("conds", 16usize), ("acts", 64)] {
        let mut depth = 0i32;
        let mut i = open;
        while i + 2 < close {
            let tok = &tokens[i];
            match tok.kind {
                lexer::TokenKind::Punct('(')
                | lexer::TokenKind::Punct('[')
                | lexer::TokenKind::Punct('{') => depth += 1,
                lexer::TokenKind::Punct(')')
                | lexer::TokenKind::Punct(']')
                | lexer::TokenKind::Punct('}') => depth -= 1,
                _ => {}
            }
            if depth == 0
                && tok.is_ident(key)
                && tokens[i + 1].is_punct('=')
                && tokens[i + 2].is_punct('[')
            {
                if let Some(bclose) = matching(tokens, i + 2, '[', ']') {
                    let count = count_top_level_items(tokens, i + 3, bclose);
                    if count > limit {
                        out.push(diag(
                            text,
                            tokens[i].start,
                            tokens[bclose].end,
                            "EUDSL-BOUNDS",
                            format!("Trigger {key} has {count} entries; the maximum is {limit}"),
                        ));
                    }
                }
                break;
            }
            i += 1;
        }
    }
}

fn parse_int(text: &str) -> Option<i64> {
    let t = text.trim().replace('_', "");
    let t = t.strip_prefix('+').unwrap_or(&t);
    if let Some(hex) = t.strip_prefix("0x").or_else(|| t.strip_prefix("0X")) {
        i64::from_str_radix(hex, 16).ok()
    } else {
        t.parse().ok()
    }
}

fn diag(text: &str, start: usize, end: usize, code: &str, message: String) -> Diagnostic {
    let (sl, sc) = position_at(text, start);
    let (el, ec) = position_at(text, end);
    Diagnostic {
        range: json!({
            "start": {"line": sl, "character": sc},
            "end": {"line": el, "character": ec}
        }),
        severity: 1,
        source: "eudkit-lsp".to_string(),
        code: code.to_string(),
        message,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn vocabulary_completion_filters_by_prefix() {
        let items = completions("Trig", 0, 4);
        assert!(items.iter().any(|i| i.label == "Trigger"));
        assert!(items.iter().any(|i| i.label == "TriggerStruct"));
        assert_eq!(items.iter().filter(|i| i.label == "FixedArray").count(), 0);
    }

    #[test]
    fn member_completion_uses_direct_view_properties() {
        let text = "u.";
        let items = completions(text, 0, text.len());
        assert!(items.iter().any(|i| i.label == "hp"));

        let text = "u.h";
        let items = completions(text, 0, text.len());
        assert!(items.iter().any(|i| i.label == "hp"));

        let text = "trig.act(0).";
        let items = completions(text, 0, text.len());
        assert!(items.iter().any(|i| i.label == "value"));
        assert!(items.iter().any(|i| i.label == "dest"));

        let text = "trig.act(0).v";
        let items = completions(text, 0, text.len());
        assert!(items.iter().any(|i| i.label == "value"));
        assert_eq!(items.iter().filter(|i| i.label == "dest").count(), 0);
    }

    #[test]
    fn hover_resolves_vocabulary_and_view_members() {
        let text = "const t = Trigger";
        assert!(hover(text, 0, 12).unwrap().contains("16 condition slots"));

        let text = "u.hp";
        assert!(hover(text, 0, 3).unwrap().contains("CUnit hit points"));

        let text = "trig.act(0).value";
        assert!(hover(text, 0, 15).unwrap().contains("Action value"));
    }

    #[test]
    fn diagnostics_flag_out_of_range_capacities() {
        let text = "new FixedString(0);\nnew FixedString(70000);\nnew FixedArray(\"dword\", 0);\nnew UnitGroup(0);";
        let diags = diagnostics(text);
        assert_eq!(diags.len(), 4);
        assert!(diags.iter().all(|d| d.code == "EUDSL-BOUNDS"));
    }

    #[test]
    fn diagnostics_flag_trigger_capacity_overflows() {
        let conds = (0..17).map(|_| "Always()").collect::<Vec<_>>().join(",");
        let text = format!("const t = new Trigger({{ conds: [{conds}], acts: [] }});");
        let diags = diagnostics(&text);
        assert_eq!(diags.len(), 1);
        assert!(diags[0].message.contains("conds has 17 entries"));
    }

    #[test]
    fn capacity_diagnostics_ignore_structured_gate_forms() {
        // The 16-condition cap is a raw `Trigger` storage bound (ADR 0001,
        // ADR 0027). Structured forms carry one logical gate and are
        // auto-chunked by lowering, so an editor must never report
        // `EUDSL-BOUNDS` for them, even well above 16 conditions.
        let conds = (0..40).map(|_| "Always()").collect::<Vec<_>>().join(", ");
        for (open, close) in [
            ("when([", "], () => {})"),
            ("once([", "], () => {})"),
            ("until([", "], () => {})"),
            ("whileLoop([", "], () => {})"),
            ("onRising([", "], () => {})"),
            ("onFalling([", "], () => {})"),
        ] {
            let text = format!("{open}{conds}{close}");
            assert!(diagnostics(&text).is_empty(), "{text}");
            assert!(diagnostics_for(&text, Language::Python).is_empty(), "{text}");
        }
    }

    #[test]
    fn capacity_diagnostics_ignore_conditions_spliced_by_cond_all() {
        // `condAll` splices at build time, so a raw `Trigger` sees one
        // logical condition no matter how many leaves it wraps: the editor
        // must not count the leaves against the 16-slot storage cap.
        let conds = (0..17).map(|_| "Always()").collect::<Vec<_>>().join(", ");
        let ts = format!("const t = new Trigger({{ conds: [condAll({conds})], acts: [] }});");
        assert!(diagnostics(&ts).is_empty(), "{ts}");
        let py = format!("t = Trigger(conds=[cond_all({conds})], acts=[])");
        assert!(diagnostics_for(&py, Language::Python).is_empty(), "{py}");

        // The bare list still trips the raw storage cap.
        let bare = format!("const t = new Trigger({{ conds: [{conds}], acts: [] }});");
        assert_eq!(diagnostics(&bare).len(), 1, "{bare}");
    }

    #[test]
    fn diagnostics_ignore_string_literals_and_comments() {
        assert!(diagnostics("const s = \"new FixedString(0)\";").is_empty());
        assert!(diagnostics("// new FixedString(70000)\n").is_empty());
        assert!(diagnostics("/* new FixedString(70000) */\n").is_empty());
        let conds = (0..17).map(|_| "Always()").collect::<Vec<_>>().join(",");
        assert!(diagnostics(&format!("// Trigger({{ conds: [{conds}], acts: [] }})")).is_empty());
    }

    #[test]
    fn diagnostics_do_not_overcount_commas_inside_strings() {
        let mut conds: Vec<String> = (0..15).map(|_| "Always()".to_string()).collect();
        conds.push("\"a,b\"".to_string());
        let text = format!("const t = new Trigger({{ conds: [{}], acts: [] }});", conds.join(","));
        assert!(diagnostics(&text).is_empty());
    }

    #[test]
    fn diagnostics_handle_string_internal_bracket_and_comma_args() {
        let mut conds: Vec<String> = (0..16).map(|_| "Always()".to_string()).collect();
        conds.push("\"a]b\"".to_string());
        let text = format!("const t = new Trigger({{ conds: [{}], acts: [] }});", conds.join(","));
        let diags = diagnostics(&text);
        assert_eq!(diags.len(), 1);
        assert!(diags[0].message.contains("conds has 17 entries"));

        let diags = diagnostics("new FixedArray(\"a,b\", 0);");
        assert_eq!(diags.len(), 1);
        assert!(diags[0].message.contains("FixedArray capacity"));
    }

    #[test]
    fn diagnostics_flag_negative_string_sizes() {
        assert_eq!(diagnostics("new FixedString(-1);").len(), 1);
        assert_eq!(diagnostics("new FixedString(-5);").len(), 1);
        assert!(diagnostics("new FixedString(8);").is_empty());
    }

    #[test]
    fn diagnostics_flag_python_trigger_capacity_overflows() {
        let conds = (0..17).map(|_| "Always()").collect::<Vec<_>>().join(", ");
        let text = format!("t = Trigger(conds=[{conds}], acts=[])");
        let diags = diagnostics(&text);
        assert_eq!(diags.len(), 1);
        assert!(diags[0].message.contains("conds has 17 entries"));
    }

    #[test]
    fn invalid_cunit_members_are_not_completed() {
        let text = "u.";
        let items = completions(text, 0, text.len());
        for bad in ["player", "positionX", "positionY"] {
            assert_eq!(
                items.iter().filter(|i| i.label == bad).count(),
                0,
                "{bad} must not be suggested on CUnit"
            );
        }
        // The real members survive: canonical names, schema aliases, and
        // the DSL's common aliases.
        for good in ["hp", "owner", "playerID", "posX", "unitID", "moveTargetUnit"] {
            assert!(
                items.iter().any(|i| i.label == good),
                "{good} must be suggested on CUnit"
            );
        }
        // Hover on an invalid member resolves to nothing.
        assert!(hover("u.player", 0, 4).is_none());
        assert!(hover("u.positionX", 0, 4).is_none());
    }

    #[test]
    fn cond_and_act_views_include_previously_missing_fields() {
        let text = "trig.cond(0).";
        let items = completions(text, 0, text.len());
        assert!(items.iter().any(|i| i.label == "mask"));
        assert!(items.iter().any(|i| i.label == "locid"));

        let text = "trig.act(0).";
        let items = completions(text, 0, text.len());
        assert!(items.iter().any(|i| i.label == "unit"));
        assert!(items.iter().any(|i| i.label == "pad"));
    }

    #[test]
    fn vocabulary_docs_cover_stock_catalog_and_extensions() {
        for (name, expected) in [
            ("Bring", "Stock condition 3"),
            ("CreateUnit", "Stock action 44"),
            ("Transmission", "Stock action 7"),
            ("LeaderBoardControl", "Stock action 17"),
            ("CreateUnitWithProperties", "Stock action 11"),
        ] {
            let items = completions(name, 0, name.len());
            let item = items
                .iter()
                .find(|item| item.label == name)
                .unwrap_or_else(|| panic!("{name} must be suggested"));
            let doc = item
                .documentation
                .as_ref()
                .and_then(|value| value["value"].as_str())
                .unwrap_or_else(|| panic!("{name} must carry documentation"));
            assert!(doc.contains(expected), "{name}: {doc}");
        }

        for (name, expected) in [
            ("SetDeathsX", "action 45 with the `eudx` mask"),
            ("Memory", "condition 15 at `EPD(addr)`"),
            ("SetNextPtr", "trigger `next` pointer"),
            ("DisplayTextIndex", "module string at `index`"),
        ] {
            let items = completions(name, 0, name.len());
            let item = items
                .iter()
                .find(|item| item.label == name)
                .unwrap_or_else(|| panic!("{name} must be suggested"));
            let doc = item
                .documentation
                .as_ref()
                .and_then(|value| value["value"].as_str())
                .unwrap_or_else(|| panic!("{name} must carry documentation"));
            assert!(doc.contains("eudkit extension"), "{name}: {doc}");
            assert!(doc.contains(expected), "{name}: {doc}");
        }
    }

    #[test]
    fn display_text_docs_name_current_player_target() {
        for name in ["DisplayText", "DisplayTextIndex"] {
            let items = completions(name, 0, name.len());
            let item = items
                .iter()
                .find(|item| item.label == name)
                .unwrap_or_else(|| panic!("{name} must be suggested"));
            let doc = item
                .documentation
                .as_ref()
                .and_then(|value| value["value"].as_str())
                .unwrap_or_else(|| panic!("{name} must carry documentation"));
            assert!(doc.contains("CurrentPlayer"), "{name}: {doc}");
            assert!(doc.contains("0x6509B0"), "{name}: {doc}");
            assert!(doc.contains("0x512684"), "{name}: {doc}");
        }
    }

    #[test]
    fn vocabulary_docs_distinguish_extension_target_forms() {
        for (name, dynamic) in [("Deaths", "amount"), ("SetDeaths", "value")] {
            let items = completions(name, 0, name.len());
            let item = items
                .iter()
                .find(|item| item.label == name)
                .unwrap_or_else(|| panic!("{name} must be suggested"));
            let doc = item
                .documentation
                .as_ref()
                .and_then(|value| value["value"].as_str())
                .unwrap_or_else(|| panic!("{name} must carry documentation"));
            assert!(doc.contains("eudkit extension"), "{name}: {doc}");
            assert!(doc.contains(dynamic), "{name}: {doc}");
            assert!(!doc.contains("Constants only."), "{name}: {doc}");
        }

        // The exception is limited to the two Deaths-table rows.
        let items = completions("Bring", 0, "Bring".len());
        let item = items
            .iter()
            .find(|item| item.label == "Bring")
            .expect("Bring must be suggested");
        let doc = item
            .documentation
            .as_ref()
            .and_then(|value| value["value"].as_str())
            .expect("Bring must carry documentation");
        assert!(doc.contains("Constants only."), "{doc}");
    }

    #[test]
    fn vocabulary_completion_includes_python_spellings_and_new_exports() {
        for name in [
            "memcpy_epd",
            "memset_epd",
            "on_falling",
            "on_rising",
            "while_loop",
            "with_cp",
            "MemCond",
            "whileLoop",
            "Func",
            "func",
        ] {
            let items = completions(name, 0, name.len());
            assert!(
                items.iter().any(|i| i.label == name),
                "{name} must be suggested"
            );
        }
        // `while_` as a prefix must now find `while_loop`, where it
        // previously completed to nothing.
        let items = completions("while_", 0, 6);
        assert!(items.iter().any(|i| i.label == "while_loop"));
    }

    #[test]
    fn vocabulary_completes_and_documents_condition_combinators() {
        let items = completions("cond", 0, 4);
        for name in [
            "condAll",
            "cond_all",
            "condAny",
            "cond_any",
            "condNot",
            "cond_not",
        ] {
            assert!(
                items.iter().any(|i| i.label == name),
                "{name} must be suggested for the `cond` prefix"
            );
        }

        for (name, needles) in [
            ("condAll", ["Conjoin", "condAll(...conds)"]),
            ("cond_all", ["Conjoin", "cond_all(...conds)"]),
            ("condAny", ["Disjoin", "condAny(...conds)"]),
            ("cond_any", ["Disjoin", "cond_any(...conds)"]),
            ("condNot", ["Negate", "condNot(cond)"]),
            ("cond_not", ["Negate", "cond_not(cond)"]),
        ] {
            let text = format!("{name}");
            let doc = hover(&text, 0, name.len())
                .unwrap_or_else(|| panic!("{name} must carry hover documentation"));
            for needle in needles {
                assert!(doc.contains(needle), "{name}: {doc}");
            }
        }
    }

    #[test]
    fn vocabulary_completion_includes_renamed_names_on_main() {
        // The inverse of the arch branch's exclusion test: on main the
        // DSL has FixedArray/FixedString, so the editor MUST suggest them.
        // Porting the arch exclusion verbatim would fail here.
        for name in ["FixedArray", "FixedString"] {
            let items = completions(name, 0, name.len());
            assert_eq!(
                items.iter().filter(|i| i.label == name).count(),
                1,
                "{name} must be suggested on main"
            );
        }
    }

    #[test]
    fn diagnostics_skip_python_hash_comments() {
        // Same defect class as the `//`/`/* */` cases, but for Python's
        // native comment style, which the old lexer never skipped.
        assert!(diagnostics_for("# new FixedString(0)\n", Language::Python).is_empty());
        assert!(diagnostics_for("x = 1 # new FixedString(0)\n", Language::Python).is_empty());
        let conds = (0..17).map(|_| "Always()").collect::<Vec<_>>().join(", ");
        let text = format!("# Trigger(conds=[{conds}], acts=[])");
        assert!(diagnostics_for(&text, Language::Python).is_empty());
        // A real violation on its own line is still flagged.
        assert_eq!(diagnostics_for("s = FixedString(0)\n", Language::Python).len(), 1);
    }

    #[test]
    fn diagnostics_handle_python_triple_quoted_strings() {
        // A `"` inside a triple-quoted string must not end it early and
        // leak the following lines as code tokens.
        let text = "s = \"\"\"a \" b\nnew FixedString(0)\n\"\"\"\n";
        assert!(diagnostics_for(text, Language::Python).is_empty());
        let text = "s = '''a \" b\nnew FixedString(0)\n'''\n";
        assert!(diagnostics_for(text, Language::Python).is_empty());
        // Code after the closing triple quotes is scanned again.
        let text = "s = \"\"\"a \" b\"\"\"\nt = FixedString(0)\n";
        assert_eq!(diagnostics_for(text, Language::Python).len(), 1);
    }

    #[test]
    fn diagnostics_do_not_skip_hash_comments_outside_python() {
        // In TypeScript `#` starts a private field name, not a comment:
        // a violation after `#x =` on the same line must still be flagged.
        // A blanket `#` skip would hide exactly this line.
        let text = "class C { #x = new FixedString(0); }";
        assert_eq!(diagnostics(text).len(), 1);
        assert_eq!(diagnostics_for(text, Language::Other).len(), 1);
        // The same text in Python mode is a comment, by contrast.
        assert!(diagnostics_for(text, Language::Python).is_empty());
    }

    #[test]
    fn diagnostics_do_not_treat_floor_division_as_comment_in_python() {
        // `//` is floor division in Python, not a comment: a call after
        // it on the same line is still code and must be flagged.
        assert_eq!(
            diagnostics_for("f(a // FixedString(0))", Language::Python).len(),
            1
        );
    }

    #[test]
    fn diagnostics_do_not_treat_block_comment_markers_as_comments_in_python() {
        // `/* */` is likewise not a comment in Python: the markers are
        // operators, so a call between them is still code.
        assert_eq!(
            diagnostics_for("f(a /* FixedString(0) */)", Language::Python).len(),
            1
        );
    }

    #[test]
    fn diagnostics_handle_python_triple_quote_edges() {
        // Empty triple-quoted string, then a real call: flagged.
        assert_eq!(
            diagnostics_for("s = \"\"\"\"\"\"\nt = FixedString(0)\n", Language::Python).len(),
            1
        );
        // Unterminated triple-quoted string consumes the rest of the
        // input, so nothing inside it leaks out as code.
        assert!(
            diagnostics_for("s = \"\"\"abc\nt = FixedString(0)\n", Language::Python).is_empty()
        );
        // An escaped quote before the closing triple does not close it
        // early: the string ends at the real triple, and the call after
        // it is still flagged.
        assert_eq!(
            diagnostics_for("s = \"\"\"a \\\" \"\"\"\nt = FixedString(0)\n", Language::Python).len(),
            1
        );
    }

    #[test]
    fn lexer_treats_python_triple_quoted_strings_as_single_tokens() {
        let tokens =
            tokenize_with_language("s = \"\"\"a \" b\"\"\" + x", Language::Python);
        assert_eq!(tokens.len(), 5);
        assert_eq!(tokens[2].kind, lexer::TokenKind::Str);
        assert_eq!(tokens[2].text, "a \" b");
    }

    #[test]
    fn generated_tables_match_schema_fields_and_aliases() {
        use std::collections::BTreeSet;
        let common: &[(&str, &[&str])] = &[
            ("hp", &["hitPoints", "hit_points"]),
            ("shield", &["shields"]),
            ("order", &["orderID", "order_id"]),
            ("owner", &["playerID", "player_id"]),
            ("posX", &["pos_x"]),
            ("posY", &["pos_y"]),
        ];
        for (table, file) in [
            (CUNIT_GENERATED, "cunit"),
            (CSPRITE_GENERATED, "csprite"),
        ] {
            let path = std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
                .join("../../schema/layouts")
                .join(format!("{file}.json"));
            let data = std::fs::read_to_string(&path).unwrap();
            let schema: Value = serde_json::from_str(&data).unwrap();
            let fields = schema["fields"].as_object().unwrap();
            let mut expected = BTreeSet::new();
            for (name, spec) in fields {
                if spec["size"].as_u64().unwrap_or(0) == 0 {
                    continue;
                }
                expected.insert(name.clone());
                if let Some(aliases) = spec["aliases"].as_array() {
                    for alias in aliases {
                        expected.insert(alias.as_str().unwrap().to_string());
                    }
                }
            }
            for (canonical, aliases) in common {
                if fields.contains_key(*canonical) {
                    for alias in *aliases {
                        expected.insert((*alias).to_string());
                    }
                }
            }
            let got: BTreeSet<String> =
                table.iter().map(|m| m.name.to_string()).collect();
            assert_eq!(got, expected, "generated table for {file} drifted from schema");
            assert_eq!(table.len(), got.len(), "duplicate labels in {file} table");
            assert!(
                table.iter().all(|m| !m.doc.is_empty() && !m.detail.is_empty()),
                "generated {file} members must carry detail and docs"
            );
        }
    }

    #[test]
    fn generated_members_carry_curated_docs_where_overlaid() {
        let hp = CUNIT_GENERATED.iter().find(|m| m.name == "hp").unwrap();
        assert_eq!(hp.detail, "dword");
        assert!(hp.doc.contains("CUnit hit points"));
        let owner = CUNIT_GENERATED.iter().find(|m| m.name == "owner").unwrap();
        assert_eq!(owner.detail, "byte");
        assert!(owner.doc.contains("Owning player"));
        let sprite = CSPRITE_GENERATED.iter().find(|m| m.name == "player").unwrap();
        assert_eq!(sprite.detail, "byte");
        assert!(sprite.doc.contains("Owning player"));
    }
}
