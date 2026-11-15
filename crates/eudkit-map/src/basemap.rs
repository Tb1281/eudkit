//! One parse of a basemap's template string table per compile.
//!
//! The lowering driver needs two things from the pristine basemap before
//! map assembly:
//!
//! 1. the [`MapInfo`] document (template strings in ordered-list order,
//!    location/switch names, DIM), and
//! 2. the STR-section offsets for the final ordered string list
//!    ([`BasemapContext::str_offsets_for_full_strings`]).
//!
//! Both used to open the MPQ, read `staredit\scenario.chk`, tokenize it
//! and run [`TemplateStrings::parse`] independently. That parse is a
//! fixed ~11.5 ms per pass (the archive open and read are ~0.2 ms
//! together; see `docs/spikes/c2-rescope/`), so [`BasemapContext`] owns
//! the open → read → tokenize → template parse sequence once and serves
//! both products from that single state.
//!
//! **Map assembly reuses this context too** (`assemble_map_with_context`):
//! the parsed [`TemplateStrings`] and the tokenized [`Chk`] are handed to the
//! tail, so the second `TemplateStrings::parse` — measured at ~11.4 ms — is
//! paid once per compile instead of twice.
//!
//! ## Soundness: the `E-STRADDR-VERIFY` trade
//!
//! Assembly still compares the driver-recorded offset table
//! (`module.str_addrs`) against the offsets of the table it builds, and still
//! fails loud (`E-STRADDR-VERIFY`) on any mismatch. What the hand-off removes
//! is the *independent-data* half of that cross-check: the rebuilt table and
//! the recorded table now derive from the same [`TemplateStrings`] parse, so
//! a bug in the parse itself would make both sides agree.
//!
//! That is accepted because:
//!
//! - the context parses the same pristine `scenario.chk` bytes assembly used
//!   to read (the hand-off removes the parse-after-`fix_map_data` ordering
//!   hazard rather than introducing it);
//! - parse equivalence against the standalone wrappers is test-pinned by
//!   `basemap::tests::context_products_match_the_standalone_wrappers`;
//! - the runtime comparison still covers builder/offset-derivation bugs
//!   (recorded table vs rebuilt table are still different code paths), and
//!   both fixtures produced byte-identical `.scx` output with and without
//!   the hand-off during the #100 spike and the #108 implementation.
//!
//! Callers that do not have a context (tests, standalone `assemble_map` uses)
//! still get the independent read + parse.

use std::path::Path;

use crate::chk::Chk;
use crate::mapinfo::{map_info_from_parts, MapInfo};
use crate::tbl::{string_section_offsets, TemplateStrings};
use crate::MapTailError;

/// A parsed basemap template: the tokenized CHK plus its
/// [`TemplateStrings`] load state, parsed once and shared by every
/// driver-side consumer.
#[derive(Debug, Clone)]
pub struct BasemapContext {
    chk: Chk,
    template: TemplateStrings,
}

impl BasemapContext {
    /// Open the basemap MPQ, read `staredit\scenario.chk`, tokenize it
    /// and parse the template string table.
    pub fn open(basemap: &Path) -> Result<Self, MapTailError> {
        let mut mpq = stormlib::Archive::open(basemap, stormlib::OpenArchiveFlags::empty())?;
        let chk_bytes = mpq.open_file("staredit\\scenario.chk")?.read_all()?;
        drop(mpq);
        Self::from_chk_bytes(&chk_bytes)
    }

    /// Build a context from raw `scenario.chk` bytes (same parse as
    /// [`Self::open`], minus the archive).
    pub fn from_chk_bytes(chk_bytes: &[u8]) -> Result<Self, MapTailError> {
        let mut chk = Chk::new();
        if !chk.loadchk(chk_bytes) {
            return Err(MapTailError::BadStrings(
                "template scenario.chk failed to tokenize".into(),
            ));
        }
        let template = TemplateStrings::parse(&chk)?;
        Ok(Self { chk, template })
    }

    /// The parsed template string table.
    pub fn template_strings(&self) -> &TemplateStrings {
        &self.template
    }

    /// The tokenized pristine template CHK.
    ///
    /// Map assembly needs an owned, mutable copy for its fixups
    /// ([`Chk::clone`] costs ~0.003 ms); the clone is taken from here instead
    /// of re-reading the MPQ and re-tokenizing `scenario.chk`.
    pub fn chk(&self) -> &Chk {
        &self.chk
    }

    /// Build the [`MapInfo`] document from this context's tokenized CHK
    /// and template strings.
    pub fn map_info(&self) -> Result<MapInfo, MapTailError> {
        map_info_from_parts(&self.chk, &self.template)
    }

    /// Per-string-id STR-section offsets for a complete ordered string
    /// list (see [`crate::tbl::str_offsets_for_full_strings`]).
    pub fn str_offsets_for_full_strings(
        &self,
        strings_b64: &[String],
    ) -> Result<Vec<u32>, MapTailError> {
        let ordered = crate::decode_strings(strings_b64)?;
        string_section_offsets(&ordered, &self.template)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::tbl::template_parse_count;

    fn basemap() -> &'static Path {
        Path::new(concat!(
            env!("CARGO_MANIFEST_DIR"),
            "/../../tests/outputmap/basemap/basemap.scx"
        ))
    }

    /// The whole point of the context: opening it once and asking for
    /// both products runs `TemplateStrings::parse` exactly once.
    ///
    /// The counter is thread-local, so a test thread's delta is its own
    /// even when the suite runs in parallel.
    #[test]
    fn one_template_parse_serves_map_info_and_offsets() {
        let before = template_parse_count();
        let ctx = BasemapContext::open(basemap()).unwrap();
        let info = ctx.map_info().unwrap();
        let offsets = ctx
            .str_offsets_for_full_strings(&info.strings_b64)
            .unwrap();
        let after = template_parse_count();
        assert_eq!(
            after - before,
            1,
            "one BasemapContext must parse the template string table once"
        );
        assert_eq!(offsets.len(), info.strings_b64.len());
    }

    /// The shared context must produce exactly what the standalone
    /// (own-parse) wrappers produce — byte-identical outputs for the
    /// same inputs.
    #[test]
    fn context_products_match_the_standalone_wrappers() {
        let ctx = BasemapContext::open(basemap()).unwrap();
        let info = ctx.map_info().unwrap();
        assert_eq!(info, crate::mapinfo::extract_map_info(basemap()).unwrap());

        let offsets = ctx
            .str_offsets_for_full_strings(&info.strings_b64)
            .unwrap();
        assert_eq!(
            offsets,
            crate::tbl::str_offsets_for_full_strings(basemap(), &info.strings_b64).unwrap()
        );
    }
}
