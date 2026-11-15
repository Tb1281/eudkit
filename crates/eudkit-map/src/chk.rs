//! CHK section model — the used subset of
//! `src/eudkit/core/mapdata/chktok.py`.
//!
//! Section order is insertion order (Python `dict` semantics): `loadchk`
//! inserts in file order, `setsection` on an existing name keeps its
//! position, and `savechk` serializes in that order.

use crate::rng::CpRandom;
use crate::rng::draw_savechk_fake_section;

/// Format a section name to 4 ASCII bytes (pad with spaces; error when
/// longer than 4) — mirrors `chktok.sectionname_format`.
pub fn section_name(name: &str) -> Result<[u8; 4], String> {
    let b = name.as_bytes();
    if b.len() > 4 {
        return Err(format!(
            "Length of section name cannot be longer than 4: {name:?}"
        ));
    }
    if !b.is_ascii() {
        return Err(format!("section name is not ASCII: {name:?}"));
    }
    let mut out = *b"    ";
    out[..b.len()].copy_from_slice(b);
    Ok(out)
}

/// Tokenized CHK: ordered map of 4-byte section name → section bytes.
#[derive(Debug, Clone, Default)]
pub struct Chk {
    sections: Vec<([u8; 4], Vec<u8>)>,
}

impl Chk {
    pub fn new() -> Self {
        Chk {
            sections: Vec::new(),
        }
    }

    fn find(&self, name: &[u8; 4]) -> Option<usize> {
        self.sections.iter().position(|(n, _)| n == name)
    }

    /// Parse raw CHK bytes into sections (mirrors `CHK.loadchk`).
    /// Returns `false` on protection-style negative section length
    /// (restores the previous sections, like the Python code).
    pub fn loadchk(&mut self, b: &[u8]) -> bool {
        let prev = std::mem::take(&mut self.sections);
        let mut sections = Vec::new();
        let mut index = 0usize;
        while index < b.len() {
            if index + 8 > b.len() {
                break;
            }
            let mut name = [0u8; 4];
            name.copy_from_slice(&b[index..index + 4]);
            // b2i4 is an *unsigned* read, so the `< 0` guard in the Python
            // code is dead; kept structurally for fidelity.
            let sectionlength = u32::from_le_bytes(
                b[index + 4..index + 8].try_into().unwrap(),
            ) as usize;
            // Python slice `b[i+8:i+8+len]` silently truncates at the end.
            let next = index.saturating_add(8).saturating_add(sectionlength);
            let end = next.min(b.len());
            sections.push((name, b[index + 8..end].to_vec()));
            index = next;
        }
        self.sections = sections;
        let _ = prev;
        true
    }

    /// Raw section bytes, if present.
    pub fn getsection(&self, name: &[u8; 4]) -> Option<&[u8]> {
        self.find(name).map(|i| self.sections[i].1.as_slice())
    }

    /// Replace (or append) a section, keeping insertion order.
    pub fn setsection(&mut self, name: [u8; 4], data: Vec<u8>) {
        match self.find(&name) {
            Some(i) => self.sections[i].1 = data,
            None => self.sections.push((name, data)),
        }
    }

    /// Iterate section names in order.
    pub fn enumsection(&self) -> Vec<[u8; 4]> {
        self.sections.iter().map(|(n, _)| *n).collect()
    }

    /// Drop sections the game does not use (mirrors `CHK.optimize`;
    /// the MASK/MTXM trimming stays omitted, like the Python code).
    pub fn optimize(&mut self) {
        const USED: [&[u8; 4]; 26] = [
            b"VER ", b"VCOD", b"OWNR", b"ERA ", b"DIM ", b"SIDE", b"MTXM",
            b"UNIT", b"THG2", b"MASK", b"STRx", b"UPRP", b"UPUS", b"MRGN", b"TRIG",
            b"MBRF", b"SPRP", b"FORC", b"COLR", b"PUNI", b"PUPx", b"PTEx",
            b"UNIx", b"UPGx", b"TECx", b"CRGB",
        ];
        self.sections.retain(|(n, _)| USED.contains(&n));
    }

    /// Serialize sections plus, unless `fake_isom` is false, the fake
    /// `ISOM` section header (mirrors `CHK.savechk`; the fake section's
    /// draws come from `rng`). The fake section's negative length
    /// blocks editor-style sequential CHK parsers, so skipping it
    /// yields a map those tools can open.
    pub fn savechk(&self, rng: &mut CpRandom, fake_isom: bool) -> Vec<u8> {
        let mut out = Vec::new();
        for (name, data) in &self.sections {
            out.extend_from_slice(name);
            out.extend_from_slice(&(data.len() as u32).to_le_bytes());
            out.extend_from_slice(data);
        }
        if fake_isom {
            out.extend_from_slice(&draw_savechk_fake_section(rng));
        }
        out
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn load_save_roundtrip() {
        let raw = b"VER \x04\x00\x00\x00\x3d\x00\x00\x00TRIG\x08\x00\x00\x00\x01\x02\x03\x04\x05\x06\x07\x08";
        let mut chk = Chk::new();
        assert!(chk.loadchk(raw));
        assert_eq!(chk.getsection(b"VER "), Some(b"\x3d\x00\x00\x00".as_slice()));
        assert_eq!(chk.getsection(b"TRIG"), Some(b"\x01\x02\x03\x04\x05\x06\x07\x08".as_slice()));
        assert_eq!(chk.getsection(b"XXXX"), None);
        // savechk appends the 8-byte fake trailer; draw it on a fresh rng
        // and check the prefix round-trips.
        let mut rng = CpRandom::seed_zero();
        let out = chk.savechk(&mut rng, true);
        assert_eq!(&out[..raw.len()], raw);
        assert_eq!(&out[raw.len()..raw.len() + 4], b"ISOM");
    }

    #[test]
    fn savechk_no_fake_isom_omits_trailer() {
        let raw = b"VER \x04\x00\x00\x00\x3d\x00\x00\x00";
        let mut chk = Chk::new();
        assert!(chk.loadchk(raw));
        let mut rng = CpRandom::seed_zero();
        let out = chk.savechk(&mut rng, false);
        assert_eq!(out, raw);
    }

    #[test]
    fn setsection_keeps_order() {
        let mut chk = Chk::new();
        chk.setsection(*b"AAAA", vec![1]);
        chk.setsection(*b"BBBB", vec![2]);
        chk.setsection(*b"AAAA", vec![3]); // replace: position kept
        assert_eq!(chk.enumsection(), vec![*b"AAAA", *b"BBBB"]);
        assert_eq!(chk.getsection(b"AAAA"), Some([3].as_slice()));
    }

    #[test]
    fn optimize_drops_unused() {
        let mut chk = Chk::new();
        chk.setsection(*b"VER ", vec![]);
        chk.setsection(*b"ISOM", vec![]);
        chk.setsection(*b"STRx", vec![]);
        chk.optimize();
        assert_eq!(chk.enumsection(), vec![*b"VER ", *b"STRx"]);
    }

    #[test]
    fn section_name_format() {
        assert_eq!(section_name("TRIG").unwrap(), *b"TRIG");
        assert_eq!(section_name("VER").unwrap(), *b"VER ");
        assert!(section_name("TOOLONG").is_err());
    }
}
