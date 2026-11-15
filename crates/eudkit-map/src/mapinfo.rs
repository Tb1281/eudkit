//! MapInfo extraction: the basemap facts the frontends and the native
//! lowering need *before* a build (EUD IR string/location/switch
//! resolution).
//!
//! `eudc` writes this as JSON to a temp file and passes its path to the
//! frontend processes in `$EUDC_MAPINFO`:
//!
//! ```json
//! {"mapinfo_version": 1, "strings_b64": ["…"], "locations": ["…"],
//!  "switches": ["…"]}
//! ```
//!
//! - `strings_b64` — base64 of the template string bytes, in the
//!   **ordered-list convention** (the same convention as the LIR
//!   ordered list / `TemplateStrings` per-id list): element `k` holds
//!   the content of game string id `k + 1`, for
//!   `k in 0..TemplateStrings::count`. There is NO leading placeholder
//!   for id 0 (SC string ids are 1-based and dense; there is no string
//!   0), so `strings_b64.len() == TemplateStrings::count`. The bytes
//!   are the *load-time* contents from
//!   [`crate::tbl::TemplateStrings`] (per-id content of id `k + 1` =
//!   `datatb[dataindextb[k]]`), so a string table seeded from this list
//!   round-trips exactly through `save_tbl_from_ordered` — location /
//!   switch name slots are culled to empty at load (see `tbl.rs`),
//!   which is why the names below are resolved from the *raw* string
//!   section instead.
//! - `locations` — display name per MRGN entry, in MRGN entry order
//!   (index = location index; the trigger location id is index + 1,
//!   mirroring legacy `GetLocationIndex`). Empty when MRGN is absent.
//!   Entries whose string id is 0 (or out of range) get `""`.
//! - `switches` — display name per SWNM entry, in SWNM order (index =
//!   switch index, mirroring legacy `GetSwitchIndex`'s `swmap`, which
//!   is built from SWNM in `tblformat.py: load_tbl_with_chk`). SWNM is
//!   the reliable in-CHK source for switch names: 256 slots of u32
//!   string id. Empty when SWNM is absent; slots with string id 0 get
//!   `""` (unnamed switches fall back to the default `Switch N` names
//!   in the legacy/frontend dictionaries, not to a map name).
//!
//! Display names are the raw CHK bytes decoded as CP949, falling back
//! to latin1 *per string* when CP949 decoding fails (contract).

use std::path::Path;

use serde::Serialize;

use crate::chk::Chk;
use crate::layout::{
    checked_records, MrgnEntry, DIM_MAX_TILES, DIM_MIN_LEN, DIM_MIN_TILES, MRGN_RECORD_SIZE,
    STR_LOAD_WIDTH,
    STR_SAVE_WIDTH, SWNM_COUNT, SWNM_SECTION_SIZE, SWNM_STRID_SIZE,
};
use crate::tbl::TemplateStrings;
use crate::MapTailError;

/// The only MapInfo schema version written today.
pub const MAPINFO_VERSION: u32 = 1;

/// The MapInfo document (wire form = the JSON above).
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct MapInfo {
    pub mapinfo_version: u32,
    /// Base64 template strings in ordered-list order: element `k` is
    /// game string id `k + 1` (see module docs). No id-0 placeholder.
    pub strings_b64: Vec<String>,
    /// Location display names in MRGN entry order.
    pub locations: Vec<String>,
    /// Switch display names in SWNM entry order.
    pub switches: Vec<String>,
    /// Basemap dimensions in TILES from the CHK DIM section, as
    /// `[tiles_w, tiles_h]`. Optional (absent = unknown): old writers
    /// predate it, and a basemap without a DIM section yields `None`
    /// (frontends then treat position domains as open, silent).
    /// Coordinates are pixels = tiles * 32, so the PositionX domain
    /// for a map is `[0, tiles_w * 32 - 1]` (same for Y). Validated
    /// 1..=256 per axis at extraction (StarCraft's max map size);
    /// out-of-range or truncated DIM is a loud extraction error.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub dim_tiles: Option<[u16; 2]>,
}

/// Standard base64 (with padding). Re-exported from the single LIR
/// implementation so the alphabet and padding rules cannot drift.
pub use eudkit_lir::b64_encode as encode_b64;

/// Decode raw CHK name bytes for display: CP949 first; when the bytes
/// are not valid CP949, fall back to latin1 (byte `b` → char U+00b),
/// per string. (`encoding_rs`'s EUC-KR is the WHATWG unified
/// CP949/Windows-949 decoder.)
pub fn decode_display_name(bytes: &[u8]) -> String {
    let (text, _encoding, had_errors) = encoding_rs::EUC_KR.decode(bytes);
    if had_errors {
        bytes.iter().map(|&b| b as char).collect()
    } else {
        text.into_owned()
    }
}

fn u16le(b: &[u8], off: usize) -> u16 {
    u16::from_le_bytes([b[off], b[off + 1]])
}

fn u32le(b: &[u8], off: usize) -> u32 {
    u32::from_le_bytes([b[off], b[off + 1], b[off + 2], b[off + 3]])
}

/// Raw per-id string contents from the template's STRx/STR section,
/// indexed by `id - 1`. Unlike [`TemplateStrings`], this does *not*
/// cull location/switch name slots — names must resolve to the bytes
/// actually in the map. Invalid offsets yield an empty entry (keeps
/// id alignment; the load-time table skips such ids entirely).
fn raw_strings(chkt: &Chk) -> Result<Vec<Vec<u8>>, MapTailError> {
    let (section, bytes, entry): (&'static str, &[u8], usize) = match chkt.getsection(b"STRx") {
        Some(b) => ("STRx", b, STR_SAVE_WIDTH),
        None => match chkt.getsection(b"STR ") {
            Some(b) => ("STR ", b, STR_LOAD_WIDTH),
            None => return Err(MapTailError::MissingSection("STRx/STR ")),
        },
    };
    let read_entry = |off: usize| -> Result<usize, MapTailError> {
        if off + entry > bytes.len() {
            return Err(MapTailError::TruncatedSection(section));
        }
        Ok(if entry == STR_SAVE_WIDTH {
            u32le(bytes, off) as usize
        } else {
            u16le(bytes, off) as usize
        })
    };
    let count = read_entry(0)?;
    let mut out = Vec::with_capacity(count);
    for i in 1..=count {
        let off = read_entry(i * entry)?;
        if off >= bytes.len() {
            out.push(Vec::new());
            continue;
        }
        let mut end = off;
        while end < bytes.len() && bytes[end] != 0 {
            end += 1;
        }
        out.push(bytes[off..end].to_vec());
    }
    Ok(out)
}

fn display_name(raw: &[Vec<u8>], strid: usize) -> String {
    if strid == 0 {
        return String::new();
    }
    // MRGN (+16) and SWNM strids are 1-based game ids, and `raw` is
    // the ordered per-id list (`raw[k]` = game id `k + 1`), so a
    // name's content is `raw[strid - 1]` — NOT `raw[strid]`.
    // Hand-verified on the basemap: MRGN entry 63 carries strid 3,
    // `raw[2] == "Anywhere"`, `raw[3] == "Human"` (the `[strid]` value).
    match raw.get(strid - 1) {
        Some(bytes) => decode_display_name(bytes),
        None => String::new(),
    }
}

/// Build the [`MapInfo`] from a tokenized template CHK (the existing
/// `chk` model — no second CHK parser). The template string table is
/// parsed with [`TemplateStrings::parse`], exactly as the map tail
/// parses it later in `assemble_map`.
pub fn map_info_from_chk(chkt: &Chk) -> Result<MapInfo, MapTailError> {
    let tpl = TemplateStrings::parse(chkt)?;
    map_info_from_parts(chkt, &tpl)
}

/// Build the [`MapInfo`] from an already parsed template CHK and string
/// table. This is the body of [`map_info_from_chk`], split out so a
/// [`crate::basemap::BasemapContext`] can serve MapInfo without a second
/// `TemplateStrings::parse`.
pub(crate) fn map_info_from_parts(
    chkt: &Chk,
    tpl: &TemplateStrings,
) -> Result<MapInfo, MapTailError> {
    // Ordered-list convention: element k is game string id k + 1.
    // No leading id-0 placeholder (len == tpl.count()).
    let mut strings_b64 = Vec::with_capacity(tpl.count());
    for id in 1..=tpl.count() {
        let bytes = tpl
            .string_by_id(id)
            .ok_or_else(|| MapTailError::TemplateTbl(format!("template string id {id} missing")))?;
        strings_b64.push(encode_b64(bytes));
    }

    // Names resolve against the raw table (the load-time table culls
    // location/switch slots to empty — see module docs).
    let raw = raw_strings(chkt)?;

    // Locations: MRGN entries are 20 bytes (rect 4×u32, string id u16
    // at +16, elevation u16) — the same layout `tbl.rs` and legacy
    // `load_tbl_with_chk` read the string ids from.
    let mut locations = Vec::new();
    if let Some(mrgn) = chkt.getsection(b"MRGN") {
        // A trailing partial record is loud here too, matching tbl
        // parse (see the tbl probe-pinned test).
        let mrgn = checked_records(Some(mrgn), "MRGN", MRGN_RECORD_SIZE)?;
        for entry in MrgnEntry::entries(mrgn) {
            locations.push(display_name(&raw, entry.strid() as usize));
        }
    }

    // Switches: SWNM is 256 u32 string ids (legacy `swnmdict` source).
    let mut switches = Vec::new();
    if let Some(swnm) = chkt.getsection(b"SWNM") {
        if swnm.len() < SWNM_SECTION_SIZE {
            return Err(MapTailError::TruncatedSection("SWNM"));
        }
        for i in 0..SWNM_COUNT {
            let strid = u32le(swnm, SWNM_STRID_SIZE * i) as usize;
            switches.push(display_name(&raw, strid));
        }
    }

    // DIM: 2x u16LE tile dimensions (same layout fixmapdata.rs reads
    // for null-tile reporting). Missing section -> None (open domains
    // downstream, silent); truncated or out-of-range -> loud, so a
    // corrupt DIM can never silently degrade to open domains.
    let dim_tiles = match chkt.getsection(b"DIM ") {
        None => None,
        Some(dim) => {
            if dim.len() < DIM_MIN_LEN {
                return Err(MapTailError::TruncatedSection("DIM "));
            }
            let w = u16le(dim, 0);
            let h = u16le(dim, 2);
            if !(DIM_MIN_TILES..=DIM_MAX_TILES).contains(&w)
                || !(DIM_MIN_TILES..=DIM_MAX_TILES).contains(&h)
            {
                return Err(MapTailError::InvalidDim(format!("{w}x{h}")));
            }
            Some([w, h])
        }
    };

    Ok(MapInfo {
        mapinfo_version: MAPINFO_VERSION,
        strings_b64,
        locations,
        switches,
        dim_tiles,
    })
}

/// Load a basemap `.scx` and extract its [`MapInfo`]. Uses the same
/// MPQ + CHK path as `assemble_map` (stormlib `scenario.chk` →
/// [`Chk::loadchk`]), just earlier: at frontend/lowering time rather
/// than at map assembly.
///
/// This is the one-shot wrapper; a driver that also needs STR offsets
/// should open a [`crate::basemap::BasemapContext`] once and call
/// [`crate::basemap::BasemapContext::map_info`] on it so the template
/// string table is parsed only once per compile.
pub fn extract_map_info(basemap: &Path) -> Result<MapInfo, MapTailError> {
    crate::basemap::BasemapContext::open(basemap)?.map_info()
}

#[cfg(test)]
mod tests {
    use super::*;

    fn b64_decode(s: &str) -> Vec<u8> {
        eudkit_lir::b64_decode(s).expect("valid base64")
    }

    /// Load the repo's test basemap CHK through stormlib, the same way
    /// `extract_map_info` does.
    fn basemap_chk() -> Chk {
        let path = concat!(
            env!("CARGO_MANIFEST_DIR"),
            "/../../tests/outputmap/basemap/basemap.scx"
        );
        let mut ar =
            stormlib::Archive::open(Path::new(path), stormlib::OpenArchiveFlags::empty()).unwrap();
        let raw = ar
            .open_file("staredit\\scenario.chk")
            .unwrap()
            .read_all()
            .unwrap();
        drop(ar);
        let mut chkt = Chk::new();
        assert!(chkt.loadchk(&raw));
        chkt
    }

    #[test]
    fn encode_b64_vectors() {
        assert_eq!(encode_b64(b""), "");
        assert_eq!(encode_b64(b"a"), "YQ==");
        assert_eq!(encode_b64(b"ab"), "YWI=");
        assert_eq!(encode_b64(b"hello"), "aGVsbG8=");
        assert_eq!(encode_b64(b"Hello World!"), "SGVsbG8gV29ybGQh");
    }

    #[test]
    fn display_name_cp949_and_latin1_fallback() {
        // "한글" in CP949.
        assert_eq!(decode_display_name(&[0xC7, 0xD1, 0xB1, 0xDB]), "한글");
        assert_eq!(decode_display_name(b"Anywhere"), "Anywhere");
        // 0xFF is never valid CP949 -> whole-string latin1 fallback.
        assert_eq!(decode_display_name(&[0xFF, b'A']), "ÿA");
    }

    #[test]
    fn map_info_real_basemap() {
        let chkt = basemap_chk();
        let info = map_info_from_chk(&chkt).unwrap();
        assert_eq!(info.mapinfo_version, 1);
        // Hand-computed from the basemap's raw STR (1024 entries,
        // ordered list: raw[k] = game id k + 1):
        //   raw[0] = "eudtrg basemap"       (game id 1)
        //   raw[1] = "Base map for eudtrg"  (game id 2)
        //   raw[2] = "Anywhere"             (game id 3)
        //   raw[7] = "Hello World!"         (game id 8 — the working
        //                                    DisplayText Const-8 fixture)
        // strings_b64 uses the SAME ordered convention with NO id-0
        // placeholder, but carries load-time contents: the location
        // slot (id 3) is culled to "" at load, the others above are not.
        assert_eq!(info.strings_b64.len(), 1024);
        assert_eq!(b64_decode(&info.strings_b64[0]), b"eudtrg basemap");
        assert_eq!(b64_decode(&info.strings_b64[1]), b"Base map for eudtrg");
        assert_eq!(b64_decode(&info.strings_b64[2]), b"");
        assert_eq!(b64_decode(&info.strings_b64[7]), b"Hello World!");
        // MRGN strid evidence for "Anywhere" (hand-verified):
        // raw per-id list (not the culled load-time table) —
        let raw = raw_strings(&chkt).unwrap();
        assert_eq!(raw.len(), 1024);
        assert_eq!(raw[0], b"eudtrg basemap");
        assert_eq!(raw[2], b"Anywhere");
        assert_eq!(raw[7], b"Hello World!");
        // MRGN entry 63: u16 string id at +16 must be 3 (1-based), so
        // the name resolves via raw[strid - 1] == raw[2]; raw[strid]
        // would be raw[3] == "Human", a different string.
        let mrgn = chkt.getsection(b"MRGN").unwrap();
        let strid_63 = u16le(mrgn, 20 * 63 + 16) as usize;
        assert_eq!(strid_63, 3);
        assert_eq!(raw[strid_63 - 1], b"Anywhere");
        assert_eq!(raw[strid_63], b"Human");
        // Locations: 255 MRGN entries; only entry 63 is named
        // ("Anywhere", whole-map rect) in this basemap.
        assert_eq!(info.locations.len(), 255);
        assert_eq!(info.locations[63], "Anywhere");
        assert_eq!(info.locations.iter().filter(|n| !n.is_empty()).count(), 1);
        // Switches: SWNM is present (256 slots) but every slot has
        // string id 0 in this basemap -> all unnamed.
        assert_eq!(info.switches.len(), 256);
        assert!(info.switches.iter().all(|n| n.is_empty()));
    }

    #[test]
    fn extract_map_info_from_basemap_file() {
        let path = Path::new(concat!(
            env!("CARGO_MANIFEST_DIR"),
            "/../../tests/outputmap/basemap/basemap.scx"
        ));
        let info = extract_map_info(path).unwrap();
        // Ordered convention: [0] = game id 1, [7] = game id 8.
        assert_eq!(info.strings_b64.len(), 1024);
        assert_eq!(b64_decode(&info.strings_b64[0]), b"eudtrg basemap");
        assert_eq!(b64_decode(&info.strings_b64[7]), b"Hello World!");
        assert_eq!(info.locations[63], "Anywhere");
        assert_eq!(info.switches.len(), 256);
        // DIM (map-contextual domains): the test basemap is 64x64
        // tiles, so position domains derived from it are [0, 2047].
        assert_eq!(info.dim_tiles, Some([64, 64]));
    }

    /// Synthetic CHK: strings ["장소" (CP949), "sw-alpha", ""], one
    /// named location (id 1), a second location with id 0, SWNM slot 0
    /// naming string id 2, and the TBL sections TemplateStrings needs.
    fn synthetic_chk(with_mrgn_swnm: bool) -> Chk {
        // STR (load_entry=2): strings start at roundup-header 2*(3+1)=8.
        // "장소" CP949 = C0 E5 BC D2 (4 bytes) @8, "sw-alpha" @14, "" @24.
        let jangso: [u8; 4] = [0xC0, 0xE5, 0xBC, 0xD2];
        let mut strsec = Vec::new();
        strsec.extend_from_slice(&3u16.to_le_bytes());
        for off in [8u16, 14, 24] {
            strsec.extend_from_slice(&off.to_le_bytes());
        }
        assert_eq!(strsec.len(), 8);
        strsec.extend_from_slice(&jangso);
        strsec.extend_from_slice(b"\0\0"); // pad to 14
        strsec.extend_from_slice(b"sw-alpha\0\0"); // 14..24
        strsec.extend_from_slice(b"\0\0"); // 24..26
        let mut chkt = Chk::new();
        chkt.setsection(*b"STR ", strsec);
        chkt.setsection(*b"UNIx", vec![0u8; 3648]);
        chkt.setsection(*b"SPRP", vec![0u8; 4]);
        chkt.setsection(*b"FORC", vec![0u8; 16]);
        if with_mrgn_swnm {
            // MRGN: two entries; entry 0 rect + strid 1, entry 1 strid 0.
            let mut mrgn = vec![0u8; 40];
            mrgn[0..4].copy_from_slice(&32u32.to_le_bytes());
            mrgn[16..18].copy_from_slice(&1u16.to_le_bytes());
            chkt.setsection(*b"MRGN", mrgn);
            // SWNM: slot 0 -> string id 2, the rest 0.
            let mut swnm = vec![0u8; SWNM_SECTION_SIZE];
            swnm[0..4].copy_from_slice(&2u32.to_le_bytes());
            chkt.setsection(*b"SWNM", swnm);
        }
        chkt
    }

    #[test]
    fn map_info_synthetic_names_cp949() {
        let chkt = synthetic_chk(true);
        let info = map_info_from_chk(&chkt).unwrap();
        assert_eq!(info.locations, vec!["장소".to_string(), String::new()]);
        assert_eq!(info.switches.len(), 256);
        assert_eq!(info.switches[0], "sw-alpha");
        assert!(info.switches[1..].iter().all(|n| n.is_empty()));
        // Load-time strings cull the location/switch slots to empty:
        // strings_b64 carries the culled table (round-trip contract),
        // while the names above come from the raw section.
        // Ordered convention (no placeholder): synthetic ids are
        // 1="장소" (location, culled), 2="sw-alpha" (switch, culled),
        // 3="" — so the list is exactly 3 entries, all empty at load.
        assert_eq!(info.strings_b64.len(), 3);
        assert_eq!(b64_decode(&info.strings_b64[0]), b"");
        assert_eq!(b64_decode(&info.strings_b64[1]), b"");
        assert_eq!(b64_decode(&info.strings_b64[2]), b"");
    }

    #[test]
    fn map_info_mrgn_partial_record_is_loud() {
        let mut chkt = synthetic_chk(true);
        let mut mrgn = vec![0u8; 38];
        mrgn[36..38].copy_from_slice(&3u16.to_le_bytes());
        chkt.setsection(*b"MRGN", mrgn);
        assert!(matches!(
            map_info_from_chk(&chkt),
            Err(MapTailError::TruncatedSection("MRGN"))
        ));
    }

    #[test]
    fn map_info_swnm_short_section_is_loud() {
        let mut chkt = synthetic_chk(true);
        chkt.setsection(*b"SWNM", vec![0u8; 1023]);
        assert!(matches!(
            map_info_from_chk(&chkt),
            Err(MapTailError::TruncatedSection("SWNM"))
        ));
    }

    #[test]
    fn map_info_absent_mrgn_swnm_gives_empty_names() {
        let chkt = synthetic_chk(false);
        let info = map_info_from_chk(&chkt).unwrap();
        assert!(info.locations.is_empty());
        assert!(info.switches.is_empty());
    }

    #[test]
    fn map_info_dim_tiles_variants() {
        // No DIM section -> None (open domains downstream, silent).
        let chkt = synthetic_chk(true);
        assert_eq!(map_info_from_chk(&chkt).unwrap().dim_tiles, None);
        // Valid DIM -> tiles as stored.
        let mut chkt = synthetic_chk(true);
        let mut dim = Vec::new();
        dim.extend_from_slice(&128u16.to_le_bytes());
        dim.extend_from_slice(&64u16.to_le_bytes());
        chkt.setsection(*b"DIM ", dim);
        assert_eq!(
            map_info_from_chk(&chkt).unwrap().dim_tiles,
            Some([128, 64])
        );
        // Truncated DIM (< 4 bytes) -> loud, never silent-open.
        let mut chkt = synthetic_chk(true);
        chkt.setsection(*b"DIM ", vec![64u8, 0]);
        assert!(matches!(
            map_info_from_chk(&chkt),
            Err(MapTailError::TruncatedSection("DIM "))
        ));
        // Out-of-range DIM (0 or > 256 per axis) -> loud.
        for (w, h) in [(0u16, 128u16), (128, 0), (257, 128), (128, 257)] {
            let mut chkt = synthetic_chk(true);
            let mut dim = Vec::new();
            dim.extend_from_slice(&w.to_le_bytes());
            dim.extend_from_slice(&h.to_le_bytes());
            chkt.setsection(*b"DIM ", dim);
            assert!(matches!(
                map_info_from_chk(&chkt),
                Err(MapTailError::InvalidDim(_))
            ));
        }
    }
}
