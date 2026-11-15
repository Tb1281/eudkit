//! MPQ tail port: `clone_with_sector_size` (`src/rust/src/mpqapi.rs:69-98`)
//! plus the `_write_output_map` logic (`src/eudkit/maprw/savemap.py:42-130`)
//! and the `_update_mpq` growth rule (`src/eudkit/maprw/mpqadd.py`).
//!
//! Runs on the pure `stormlib` crate (P9 write API) — never on the
//! PyO3-bound `eudplib-stormlib`.
//!
//! Call/flag parity with the Python path (what the Tier-1 H2/H5 gate
//! checks):
//! - clone: `(listfile)` line order preserved; each file extracted to a
//!   temp file and re-added with `replace_existing = true`
//!   (`MPQ_FILE_COMPRESS | MPQ_FILE_ENCRYPTED` + `MPQ_COMPRESSION_ZLIB`).
//! - `set_file_locale(0)` → add empty `staredit\scenario.chk` →
//!   `set_file_locale(0x409)` (clone path only), then the real CHK,
//!   `set_file_locale(0)`, `_update_mpq`, `compact()`.

use crate::MapTailError;
use crate::stage::Stage;
use std::path::{Path, PathBuf};

/// Reimplementation of `mpqapi.MPQ.clone_with_sector_size`
/// (`src/rust/src/mpqapi.rs:69-98`): open the input map, create the output
/// with the requested sector size and the input's max file count, then copy
/// every `(listfile)` entry (in line order) through a temp file.
pub fn clone_with_sector_size(
    input_path: &Path,
    output_path: &Path,
    sector_size: u32,
) -> Result<stormlib::Archive, MapTailError> {
    use stormlib::OpenArchiveFlags;
    let mut input = stormlib::Archive::open(input_path, OpenArchiveFlags::empty())?;
    let file_count = input.get_max_file_count();
    let listfile = String::from_utf8(
        input
            .open_file("(listfile)")?
            .read_all()?,
    )
    .map_err(|_| MapTailError::ListfileNotUtf8)?;
    let mut output =
        stormlib::Archive::create(output_path, sector_size, file_count)?;
    for archived_name in listfile.lines() {
        let tmp = write_temp(&input.open_file(archived_name)?.read_all()?)?;
        let r = output.add_file(&tmp, archived_name, true);
        std::fs::remove_file(&tmp).ok();
        r?;
        // note: temp removed even when add_file fails, like `fs::remove_file`
        // after `?` in mpqapi.rs (there the `?` propagates before removal;
        // here removal happens first — temp hygiene only, no byte effect).
    }
    Ok(output)
}

/// Write `data` to a temp file; the caller unlinks it when done.
fn write_temp(data: &[u8]) -> Result<PathBuf, MapTailError> {
    use std::io::Write;
    let mut f = tempfile::NamedTempFile::new()?;
    f.write_all(data)?;
    let path = f.into_temp_path().keep().map_err(|e| {
        MapTailError::TempPersist(format!("persisting temp file failed: {e:?}"))
    })?;
    Ok(path)
}

/// A file queued for `_update_mpq` (mirrors `mpqadd._addedFiles` values):
/// the archived name plus either a filesystem path or in-memory bytes.
/// (`None` content entries are skipped, like the Python code.)
#[derive(Debug, Clone)]
pub struct QueuedFile {
    pub archived_name: String,
    pub content: QueuedContent,
}

#[derive(Debug, Clone)]
pub enum QueuedContent {
    /// Skip (mirrors `path_or_content=None`).
    Skip,
    /// Read from this filesystem path.
    Path(PathBuf),
    /// Write these bytes to a temp file first.
    Bytes(Vec<u8>),
}

/// Port of `mpqadd._update_mpq`: grow the max file count with
/// `max(1024, 1 << bit_length(n))` when needed, then add every queued file
/// (`replace_existing=true`, the `MPQ.add_file` default).
pub fn update_mpq(
    mpq: &mut stormlib::Archive,
    queued: &[QueuedFile],
) -> Result<(), MapTailError> {
    let count = mpq.get_max_file_count();
    if count < queued.len() as u32 {
        let n = queued.len() as u32;
        let max_count = 1024u32.max(1 << (32 - n.leading_zeros()));
        mpq.set_max_file_count(max_count)?;
    }
    for q in queued {
        let tmp: Option<PathBuf> = match &q.content {
            QueuedContent::Skip => continue,
            QueuedContent::Path(_) => None,
            QueuedContent::Bytes(b) => Some(write_temp(b)?),
        };
        let file_path: &Path = match &q.content {
            QueuedContent::Skip => unreachable!(),
            QueuedContent::Path(p) => p,
            QueuedContent::Bytes(_) => tmp.as_ref().unwrap(),
        };
        // replace_existing=true: the PyO3 `add_file` default.
        let r = mpq.add_file(file_path, &q.archived_name, true);
        if tmp.is_some() {
            std::fs::remove_file(tmp.as_ref().unwrap()).ok();
        }
        r?;
    }
    Ok(())
}

/// Port of `savemap._write_output_map(fname, rawchk, sector_size)`.
///
/// - `sector_size = Some(n)`: remove `out_path` if it exists, clone the
///   basemap with the new sector size, add an empty
///   `staredit\scenario.chk` under locale 0, switch to locale 0x409.
/// - `sector_size = None`: byte-copy the basemap to `out_path` and open it.
/// - Then: add the real `rawchk` as `staredit\scenario.chk`, reset locale
///   to 0, `_update_mpq(queued)`, `compact()`.
pub fn write_output_map(
    out_path: &Path,
    basemap_path: &Path,
    rawchk: &[u8],
    sector_size: Option<u32>,
    queued: &[QueuedFile],
) -> Result<(), MapTailError> {
    if let Some(ss) = sector_size {
        if out_path.is_file() {
            std::fs::remove_file(out_path)?;
        }
        let stage = Stage::start("mpq.copy");
        let mut mw = clone_with_sector_size(basemap_path, out_path, ss)?;
        stage.finish();
        // add fake staredit\scenario.chk
        let stage = Stage::start("mpq.add-chk");
        stormlib::Archive::set_file_locale(0);
        let chk_path = write_temp(b"")?;
        let r = mw.add_file(&chk_path, "staredit\\scenario.chk", true);
        std::fs::remove_file(&chk_path).ok();
        r?;
        stormlib::Archive::set_file_locale(0x409);

        add_real_chk(&mut mw, rawchk)?;
        stage.finish();
        let stage = Stage::start("mpq.update");
        update_mpq(&mut mw, queued)?;
        stage.finish();
        let stage = Stage::start("mpq.compact");
        mw.compact()?;
        stage.finish();
    } else {
        // Process by modifying existing mpqfile: raw byte copy, then open.
        let stage = Stage::start("mpq.copy");
        let bytes = std::fs::read(basemap_path)?;
        std::fs::write(out_path, &bytes)?;
        let mut mw = stormlib::Archive::open(
            out_path,
            stormlib::OpenArchiveFlags::empty(),
        )?;
        stage.finish();
        let stage = Stage::start("mpq.add-chk");
        add_real_chk(&mut mw, rawchk)?;
        stage.finish();
        let stage = Stage::start("mpq.update");
        update_mpq(&mut mw, queued)?;
        stage.finish();
        let stage = Stage::start("mpq.compact");
        mw.compact()?;
        stage.finish();
    }
    Ok(())
}

/// Add the real `staredit\scenario.chk` through a temp file, then reset the
/// locale to 0 (shared tail of both `_write_output_map` branches).
fn add_real_chk(
    mw: &mut stormlib::Archive,
    rawchk: &[u8],
) -> Result<(), MapTailError> {
    let chk_path = write_temp(rawchk)?;
    let r = mw.add_file(&chk_path, "staredit\\scenario.chk", true);
    std::fs::remove_file(&chk_path).ok();
    r?;
    stormlib::Archive::set_file_locale(0);
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn basemap() -> PathBuf {
        PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .join("../../tests/outputmap/basemap/basemap_strx.scx")
    }

    #[test]
    fn clone_roundtrip_file_list() {
        let dir = tempfile::tempdir().unwrap();
        let out = dir.path().join("clone.scx");
        // Read the original's listfile + a file's bytes first (single open).
        let mut orig = stormlib::Archive::open(
            &basemap(),
            stormlib::OpenArchiveFlags::empty(),
        )
        .unwrap();
        let orig_list = String::from_utf8(
            orig.open_file("(listfile)").unwrap().read_all().unwrap(),
        )
        .unwrap();
        let orig_chk = orig
            .open_file("staredit\\scenario.chk")
            .unwrap()
            .read_all()
            .unwrap();

        let mw = clone_with_sector_size(&basemap(), &out, 3).unwrap();
        drop(mw);

        let mut cloned = stormlib::Archive::open(
            &out,
            stormlib::OpenArchiveFlags::empty(),
        )
        .unwrap();
        // Every (listfile) entry of the original must open in the clone
        // with identical bytes (the clone's own (listfile) is a StormLib
        // internal; the pipeline never reads it back).
        assert!(!orig_list.lines().next().is_none());
        for name in orig_list.lines() {
            let mut f2 = cloned.open_file(name).unwrap();
            assert_eq!(
                f2.read_all().unwrap(),
                orig.open_file(name).unwrap().read_all().unwrap(),
                "file {name} differs after clone"
            );
        }
        let mut f2 = cloned.open_file("staredit\\scenario.chk").unwrap();
        assert_eq!(f2.read_all().unwrap(), orig_chk);
    }

    #[test]
    fn update_mpq_growth_rule() {
        // max(1024, 1 << bit_length(n)): 1500 queued → 2048.
        let n = 1500u32;
        let max_count = 1024u32.max(1 << (32 - n.leading_zeros()));
        assert_eq!(max_count, 2048);
        let n = 900u32;
        let max_count = 1024u32.max(1 << (32 - n.leading_zeros()));
        assert_eq!(max_count, 1024);
    }
}
