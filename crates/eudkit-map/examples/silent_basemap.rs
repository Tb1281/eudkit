//! Build the silent fixture basemap used by smoke maps: disable every authored
//! classic trigger in a basemap (flag dword bit 3) and write a copy.
//!
//! Usage: cargo run -p eudkit-map --example silent_basemap -- <in.scx> <out.scx>
//!
//! `tests/outputmap/basemap/basemap_silent.scx` is generated from
//! `tests/outputmap/basemap/basemap.scx` with this helper.

use eudkit_map::chk::Chk;
use eudkit_map::mpq;

fn main() {
    let mut args = std::env::args().skip(1);
    let inp = args.next().expect("input map");
    let out = args.next().expect("output map");
    let mut ar = stormlib::Archive::open(&inp, stormlib::OpenArchiveFlags::empty()).unwrap();
    let bytes = ar
        .open_file("staredit\\scenario.chk")
        .unwrap()
        .read_all()
        .unwrap();
    drop(ar);
    let mut chk = Chk::new();
    assert!(chk.loadchk(&bytes));
    let mut trig = chk.getsection(b"TRIG").unwrap().to_vec();
    for t in trig.chunks_exact_mut(2400) {
        let flag = u32::from_le_bytes(t[2368..2372].try_into().unwrap());
        t[2368..2372].copy_from_slice(&(flag | 8).to_le_bytes());
    }
    chk.setsection(*b"TRIG", trig);
    let raw = chk.savechk(&mut eudkit_map::rng::CpRandom::seed_zero(), true);
    mpq::write_output_map(std::path::Path::new(&out), std::path::Path::new(&inp), &raw, None, &[])
        .unwrap();
    eprintln!("wrote {out}");
}
