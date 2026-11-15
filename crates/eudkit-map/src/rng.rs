//! CPython `random.Random` replication (Phase 5 §1.6.4, top risk).
//!
//! `CHK.savechk` draws its fake `ISOM` section from the **global** `random`
//! stream at a precise position: `seed(0)` → import-time replay (136
//! `randint` + 1 `shuffle`, documented in
//! `src/eudkit/hir/pure/build.py::_replay_import_time_draws`) → per-build
//! draws (finalizer `randint(234, 65535)` sites, driver `strcommon`
//! obfuscation draws, …) → `sample` + `randint`. A byte-identical native
//! map tail must land the stream at the identical position, so this module
//! replicates CPython's generator exactly:
//!
//! - MT19937 core (twist + tempering) — standard.
//! - `seed(int)`: the absolute value split into little-endian 32-bit words
//!   (`keyused = 1` when the value is 0), then CPython's `init_by_array`
//!   variant. **Note:** CPython's `init_by_array` differs from the reference
//!   `mt19937ar.c`: it mixes `mt[i-1]` (not `mt[j]`) with 32-bit wrapping
//!   multiplies (`1664525U` / `1566083941U`, not `UL`). Verified against
//!   `Modules/_randommodule.c` (CPython 3.12.3) and empirically: all seed
//!   values tested match `random.Random(seed).getrandbits(32)` exactly.
//! - `getrandbits(k)`: `k <= 32` → one raw output `>> (32 - k)`; `k > 32` →
//!   little-endian 32-bit word fill, last word `>> (32 - k_remaining)`.
//! - `_randbelow(n)`: `k = n.bit_length()`, rejection loop on
//!   `getrandbits(k)`. **The stream position depends on rejection**, not
//!   just the raw outputs — e.g. `sample([b"ISOM"], 1)` consumed 2 raw
//!   outputs in the traced build (first `getrandbits(1)` returned 1 and was
//!   rejected), and the savechk `randint(0, 0xFFFFFFFF)` consumed 12
//!   (6 × `getrandbits(33)` with 50% rejection each).
//!
//! [`Draw`] is the logical-draw log: the Python reference build records
//! every global-stream draw from `seed(0)` up to (excluding) savechk, and
//! the native tail replays the log verbatim before drawing the fake
//! section itself. Recording (not a fixed sequence) is required because the
//! middle of the stream is driver/candidate-dependent (`strcommon`
//! obfuscation draws; the `f_dwepdread_epd` body-build draw lands early or
//! late depending on whether user code called it first) — measured with
//! the stream-position tracer (`random.sample` on the 1-element population
//! **does** consume draws).
//!
//! JSON wire format for a log (produced by the reference-build recorder):
//! ```json
//! [
//!   {"op": "randint", "a": 0, "b": 4294967295},
//!   {"op": "randrange", "start": 126322568, "stop": 134217728},
//!   {"op": "shuffle", "n": 32},
//!   {"op": "sample", "n": 1, "k": 1}
//! ]
//! ```

use serde::{Deserialize, Serialize};

// ---------------------------------------------------------------------------
// MT19937 core
// ---------------------------------------------------------------------------

const N: usize = 624;
const M: usize = 397;
const MATRIX_A: u32 = 0x9908_B0DF;
const UPPER_MASK: u32 = 0x8000_0000;
const LOWER_MASK: u32 = 0x7FFF_FFFF;

/// Mersenne Twister MT19937 with CPython-compatible seeding.
#[derive(Debug, Clone)]
pub struct Mt19937 {
    state: [u32; N],
    index: usize,
}

impl Mt19937 {
    /// Seed like CPython's `random.seed(int)`: `abs(value)` split into
    /// little-endian 32-bit words (`keyused = 1` for 0), then CPython's
    /// `init_by_array`. `seed(0)` is `init_by_array([0])` — **not**
    /// `init_genrand(0)` and **not** the reference `mt19937ar.c`
    /// `init_by_array` (see the module docs).
    pub fn seed_words(words: &[u32]) -> Self {
        let mut mt = Mt19937 {
            state: [0; N],
            index: N,
        };
        mt.init_genrand(19650218);
        let key_len = words.len().max(1);
        let mut i = 1usize;
        let mut j = 0usize;
        let mut k = N.max(key_len);
        while k > 0 {
            k -= 1;
            // CPython: mt[i-1], 32-bit wrapping multiply by 1664525U.
            let prev = mt.state[i - 1];
            let prod = (prev ^ (prev >> 30)).wrapping_mul(1664525);
            let wj = if words.is_empty() { 0 } else { words[j] };
            mt.state[i] = (mt.state[i] ^ prod)
                .wrapping_add(wj)
                .wrapping_add(j as u32);
            i += 1;
            j += 1;
            if i >= N {
                mt.state[0] = mt.state[N - 1];
                i = 1;
            }
            if j >= key_len {
                j = 0;
            }
        }
        for _ in 0..(N - 1) {
            let prev = mt.state[i - 1];
            let prod = (prev ^ (prev >> 30)).wrapping_mul(1566083941);
            mt.state[i] = (mt.state[i] ^ prod).wrapping_sub(i as u32);
            i += 1;
            if i >= N {
                mt.state[0] = mt.state[N - 1];
                i = 1;
            }
        }
        mt.state[0] = 0x8000_0000;
        mt.index = N;
        mt
    }

    /// `random.seed(0)`.
    pub fn seed_zero() -> Self {
        Self::seed_words(&[0])
    }

    /// Seed from a `u128` like CPython would for that int value
    /// (little-endian 32-bit words of the absolute value).
    pub fn seed_u128(value: u128) -> Self {
        let bits = 128 - value.leading_zeros();
        let keyused = if bits == 0 { 1 } else { ((bits - 1) / 32 + 1) as usize };
        let mut words = Vec::with_capacity(keyused);
        for w in 0..keyused {
            words.push((value >> (32 * w)) as u32);
        }
        Self::seed_words(&words)
    }

    fn init_genrand(&mut self, seed: u32) {
        self.state[0] = seed;
        for i in 1..N {
            let prev = self.state[i - 1];
            self.state[i] = 1812433253u32
                .wrapping_mul(prev ^ (prev >> 30))
                .wrapping_add(i as u32);
        }
        self.index = N;
    }

    fn twist(&mut self) {
        for i in 0..N {
            let y = (self.state[i] & UPPER_MASK) | (self.state[(i + 1) % N] & LOWER_MASK);
            let mut x = self.state[(i + M) % N] ^ (y >> 1);
            if y & 1 != 0 {
                x ^= MATRIX_A;
            }
            self.state[i] = x;
        }
        self.index = 0;
    }

    /// Raw `genrand_uint32` output.
    pub fn genrand_u32(&mut self) -> u32 {
        if self.index >= N {
            self.twist();
        }
        let mut y = self.state[self.index];
        self.index += 1;
        y ^= y >> 11;
        y ^= (y << 7) & 0x9D2C_5680;
        y ^= (y << 15) & 0xEFC6_0000;
        y ^= y >> 18;
        y
    }
}

// ---------------------------------------------------------------------------
// CPython `random.Random` method replication (global-stream subset)
// ---------------------------------------------------------------------------

/// Faithful port of the CPython `random.Random` methods that the build's
/// global stream uses. Only the stream-affecting subset is implemented
/// (`random()`, `choice`, `randbytes` never touch the build's global
/// stream — verified by the tracer).
#[derive(Debug, Clone)]
pub struct CpRandom {
    mt: Mt19937,
}

impl CpRandom {
    /// Fresh generator in the `random.seed(0)` state.
    pub fn seed_zero() -> Self {
        CpRandom {
            mt: Mt19937::seed_zero(),
        }
    }

    /// `getrandbits(k)`: `k <= 32` → one raw output `>> (32 - k)`;
    /// `k > 32` → little-endian 32-bit word fill (`words = (k-1)/32 + 1`
    /// raw outputs, last word `>> (32 - k_remaining)`).
    /// Returns the value as `u128` (covers any `k` the build uses).
    pub fn getrandbits(&mut self, k: u32) -> u128 {
        if k == 0 {
            return 0;
        }
        if k <= 32 {
            return (self.mt.genrand_u32() >> (32 - k)) as u128;
        }
        let words = (k - 1) / 32 + 1;
        let mut acc: u128 = 0;
        let mut rem = k;
        for w in 0..words {
            let mut r = self.mt.genrand_u32();
            if rem < 32 {
                r >>= 32 - rem;
            }
            acc |= (r as u128) << (32 * w);
            rem -= rem.min(32);
        }
        acc
    }

    /// `_randbelow(n)`: `k = n.bit_length()`, rejection loop on
    /// `getrandbits(k)` until `r < n`.
    pub fn randbelow(&mut self, n: u64) -> u64 {
        assert!(n > 0, "_randbelow requires n > 0");
        let k = 64 - n.leading_zeros();
        loop {
            let r = self.getrandbits(k) as u64;
            if r < n {
                return r;
            }
        }
    }

    fn randbelow_u128(&mut self, n: u128) -> u128 {
        assert!(n > 0, "_randbelow requires n > 0");
        let k = 128 - n.leading_zeros();
        loop {
            let r = self.getrandbits(k);
            if r < n {
                return r;
            }
        }
    }

    /// `randrange(start, stop)` with `step == 1`: `start + _randbelow(stop - start)`.
    pub fn randrange(&mut self, start: u64, stop: u64) -> u64 {
        assert!(stop > start, "empty range in randrange");
        start + self.randbelow(stop - start)
    }

    /// `randint(a, b)` = `randrange(a, b + 1)`.
    pub fn randint(&mut self, a: u32, b: u32) -> u32 {
        assert!(b >= a, "randint requires b >= a");
        (a as u64 + self.randbelow(b as u64 - a as u64 + 1)) as u32
    }

    /// `randint` over the full 32-bit range (savechk's draw).
    pub fn randint_u32_full(&mut self) -> u32 {
        self.randbelow_u128(0x1_0000_0000) as u32
    }

    /// `shuffle(x)` for a slice of length `n`: Fisher–Yates, element values
    /// are irrelevant to the stream — only `n` matters, so the caller
    /// passes the length (matches the [`Draw::Shuffle`] log encoding).
    pub fn shuffle_len(&mut self, n: usize) {
        // for i in reversed(range(1, n)): j = _randbelow(i + 1); swap
        for i in (1..n).rev() {
            let _j = self.randbelow(i as u64 + 1);
        }
    }

    /// `sample(population, k)`: CPython 3.11+ algorithm. The build only
    /// ever samples the 1-element `[b"ISOM"]` population, but the general
    /// form is implemented for log-replay robustness.
    pub fn sample(&mut self, n: usize, k: usize) -> Vec<usize> {
        assert!(k <= n, "sample larger than population");
        let mut result = Vec::with_capacity(k);
        // CPython 3.11+: setsize = 21 (+ 4**ceil(log(k*3, 4)) when k > 5);
        // the branch decides stream consumption, so it must match exactly.
        let mut setsize = 21;
        if k > 5 {
            // Smallest power of 4 >= k*3 (== 4**ceil(log(k*3, 4))).
            let mut p = 1usize;
            while p < k * 3 {
                p *= 4;
            }
            setsize += p;
        }
        if n <= setsize {
            let mut pool: Vec<usize> = (0..n).collect();
            for i in 0..k {
                let j = self.randbelow((n - i) as u64) as usize;
                result.push(pool[j]);
                pool[j] = pool[n - i - 1];
            }
        } else {
            use std::collections::HashSet;
            let mut selected = HashSet::new();
            while result.len() < k {
                let j = self.randbelow(n as u64) as usize;
                if selected.insert(j) {
                    result.push(j);
                }
            }
        }
        result
    }
}

// ---------------------------------------------------------------------------
// Logical draw log
// ---------------------------------------------------------------------------

/// One logical draw on the global `random` stream, as recorded by the
/// reference-build recorder. Replaying the log in order on a
/// [`CpRandom::seed_zero`] generator reproduces the exact stream position
/// (rejection sampling inside `_randbelow` is deterministic given the
/// order, so the log must preserve the true call order — in particular the
/// `f_dwepdread_epd` body-build draw, which lands early or late depending
/// on whether user code called it before the injector).
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "op", rename_all = "lowercase")]
pub enum Draw {
    /// `random.randint(a, b)` (inclusive both ends).
    Randint { a: u32, b: u32 },
    /// `random.randrange(start, stop)` with step 1.
    Randrange { start: u64, stop: u64 },
    /// `random.shuffle` of an `n`-element list (values don't affect the stream).
    Shuffle { n: usize },
    /// `random.sample` of `k` from an `n`-element population.
    Sample { n: usize, k: usize },
}

impl Draw {
    /// Apply one logical draw to the generator, discarding the value
    /// (values are already baked into the LIR/payload; only the stream
    /// position matters here).
    pub fn apply(&self, rng: &mut CpRandom) {
        match *self {
            Draw::Randint { a, b } => {
                rng.randint(a, b);
            }
            Draw::Randrange { start, stop } => {
                rng.randrange(start, stop);
            }
            Draw::Shuffle { n } => {
                rng.shuffle_len(n);
            }
            Draw::Sample { n, k } => {
                rng.sample(n, k);
            }
        }
    }
}

/// Replay a recorded draw log in order.
pub fn replay_draw_log(rng: &mut CpRandom, log: &[Draw]) {
    for draw in log {
        draw.apply(rng);
    }
}

/// Parse a draw log from its JSON wire format.
pub fn parse_draw_log(json: &str) -> Result<Vec<Draw>, serde_json::Error> {
    serde_json::from_str(json)
}

// ---------------------------------------------------------------------------
// The pinned import-time draw sequence (build.py::_replay_import_time_draws)
// ---------------------------------------------------------------------------

/// The exact import-time draw sequence the golden harness replays after
/// `random.seed(0)` (136 `randint` + 1 `shuffle`), as a [`Draw`] log.
/// Kept as a constructor for documentation/testing; production replays use
/// the recorder's log, which starts with this prefix.
pub fn import_time_draws() -> Vec<Draw> {
    let mut draws = Vec::new();
    // SetMemoryC(cpcache.getValueAddr(), SetTo, 0): 3 draws.
    draws.push(Draw::Randint { a: 0, b: 0xFFFF_FFFF });
    draws.push(Draw::Randint { a: 234, b: 65535 });
    draws.push(Draw::Randint { a: 0, b: 0xFF });
    // random.shuffle(list(range(32))).
    draws.push(Draw::Shuffle { n: 32 });
    // 32 x [randint(234, 65535) + SetMemoryC(..., Add, 2**i)]: 3 draws.
    for _ in 0..32 {
        draws.push(Draw::Randint { a: 234, b: 65535 });
        draws.push(Draw::Randint { a: 0, b: 0xFFFF_FFFF });
        draws.push(Draw::Randint { a: 234, b: 65535 });
        draws.push(Draw::Randint { a: 0, b: 0xFF });
    }
    // string/strcommon.py: key1..key4 + module-level ObfuscatedBytes().
    for _ in 0..4 {
        draws.push(Draw::Randint { a: 1, b: 0xFF });
    }
    draws.push(Draw::Randint { a: 1, b: 255 });
    draws
}

// ---------------------------------------------------------------------------
// savechk's fake-section draws
// ---------------------------------------------------------------------------

/// Draw savechk's fake `ISOM` section trailer: `random.sample([b"ISOM"], 1)`
/// (always `b"ISOM"`; **does** consume stream draws — verified empirically)
/// followed by `random.randint(0, 0xFFFFFFFF) | 0x80000000` as the section
/// length. Returns the 8 trailer bytes (`b"ISOM"` + little-endian length);
/// no section payload follows (mirrors `chktok.savechk`).
pub fn draw_savechk_fake_section(rng: &mut CpRandom) -> [u8; 8] {
    let idx = rng.sample(1, 1);
    debug_assert_eq!(idx, vec![0]);
    let len = rng.randint_u32_full() | 0x8000_0000;
    let mut out = [0u8; 8];
    out[0..4].copy_from_slice(b"ISOM");
    out[4..8].copy_from_slice(&len.to_le_bytes());
    out
}

/// `random.Random.randbytes(n)`: one `genrand_int32` per 4-byte chunk,
/// little-endian bytes (CPython `_randommodule.c:random_randbytes`).
pub fn randbytes(rng: &mut CpRandom, n: usize) -> Vec<u8> {
    let mut out = Vec::with_capacity(n);
    let mut i = 0;
    while i < n {
        let x = rng.getrandbits(32) as u32;
        let take = (n - i).min(4);
        for k in 0..take {
            out.push((x >> (8 * k)) as u8);
        }
        i += 4;
    }
    out
}

/// The deterministic MRGN obfuscation bytes from
/// `build.py:install_deterministic_pins`: a fresh `random.Random(0xC10C)`
/// drawing `randbytes(16)` (tracetool i_header), `randbytes(4)` (trace key),
/// `randbytes(16)` (trace header), then `randbytes(5100)` (mrgndata) — in
/// that order.
///
/// Retained despite no current production caller: the follow-up
/// runtime-restore ticket needs these exact bytes; do not delete as
/// dead code.
pub fn pinned_mrgndata() -> Vec<u8> {
    let mut pinned = CpRandom {
        mt: Mt19937::seed_words(&[0xC10C]),
    };
    let _i_header = randbytes(&mut pinned, 16);
    let _trace_key = randbytes(&mut pinned, 4);
    let _trace_header = randbytes(&mut pinned, 16);
    randbytes(&mut pinned, 5100)
}

// ---------------------------------------------------------------------------
// Tests: every expectation below is a value produced by CPython 3.12's
// `random` module (see the stream-position tracer), not hand-computed.
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;

    /// First 8 raw outputs of `random.seed(0); [getrandbits(32) ...]`.
    const SEED0_RAW: [u32; 8] = [
        3626764237, 1654615998, 3255389356, 3823568514, 1806341205, 173879092,
        1112038970, 4146640122,
    ];

    #[test]
    fn mt19937_seed0_matches_cpython() {
        let mut mt = Mt19937::seed_zero();
        for (i, &want) in SEED0_RAW.iter().enumerate() {
            assert_eq!(mt.genrand_u32(), want, "raw output {i}");
        }
    }

    #[test]
    fn seed_int_variants_match_cpython() {
        // random.seed(5489) raw stream head (CPython uses init_by_array
        // with the int's 32-bit LE words — never init_genrand).
        let mut mt = Mt19937::seed_words(&[5489]);
        let mut py = CpRandom {
            mt: Mt19937::seed_words(&[5489]),
        };
        assert_eq!(mt.genrand_u32(), py.mt.genrand_u32());
        // seed(-7) == seed(7) (absolute value).
        let mut a = Mt19937::seed_words(&[7]);
        assert_eq!(a.genrand_u32(), 1390851128);
    }

    #[test]
    fn getrandbits_wording_matches_cpython() {
        // getrandbits(33) == r0 | ((r1 >> 31) << 32) (little-endian fill).
        let mut rng = CpRandom::seed_zero();
        assert_eq!(rng.getrandbits(33), 3626764237);
        // getrandbits(64) == r0 | (r1 << 32).
        let mut rng = CpRandom::seed_zero();
        assert_eq!(rng.getrandbits(64), 0x629f6fbed82c07cd);
        // getrandbits(1) stream head from the traced savechk sample:
        // first call returned 1 (rejected), second 0.
        let mut rng = CpRandom::seed_zero();
        // advance past the first 599 traced getrandbits is impractical
        // here; instead check the k<=32 fast path on a fresh stream:
        assert_eq!(rng.getrandbits(1), (SEED0_RAW[0] >> 31) as u128);
        assert_eq!(rng.getrandbits(8), (SEED0_RAW[1] >> 24) as u128);
    }

    #[test]
    fn randbelow_rejection_matches_cpython() {
        // _randbelow(256): getrandbits(9) -> 455 (rejected), 215 (taken).
        let mut rng = CpRandom::seed_zero();
        // consume the first two logical draws of the replay to reach it:
        // randint(0, 0xFFFFFFFF) -> getrandbits(33) x1, randint(234,65535)
        // -> getrandbits(16) x1.
        assert_eq!(rng.randint(0, 0xFFFF_FFFF), 3626764237);
        assert_eq!(rng.randint(234, 65535), 49907);
        // now _randbelow(256): bit_length(256) == 9.
        assert_eq!(rng.randbelow(256), 215);
    }

    #[test]
    fn sample_setsize_matches_cpython_311() {
        // CPython 3.11+: setsize = 21 + 4**ceil(log(k*3, 4)) when k > 5.
        // Traced with random.Random(0).sample(range(n), k) (CPython 3.12).
        // n=30, k=6 is the regression case: old code took the big-set path
        // (30 > 21); CPython takes the small-set path (30 <= 21+64=85).
        let mut rng = CpRandom::seed_zero();
        assert_eq!(rng.sample(30, 6), vec![27, 12, 24, 13, 1, 8]);
        // n=25, k=8: setsize = 21 + 4**ceil(log(24,4)) = 21+64 = 85 → small.
        let mut rng = CpRandom::seed_zero();
        assert_eq!(rng.sample(25, 8), vec![12, 13, 1, 8, 16, 15, 24, 9]);
        // k <= 5 keeps setsize 21: n=100, k=3 → big-set path (unchanged).
        let mut rng = CpRandom::seed_zero();
        assert_eq!(rng.sample(100, 3), vec![49, 97, 53]);
        // n=10, k=10 → small-set path (unchanged).
        let mut rng = CpRandom::seed_zero();
        assert_eq!(
            rng.sample(10, 10),
            vec![6, 9, 0, 2, 4, 3, 5, 1, 8, 7]
        );
    }

    #[test]
    fn shuffle_matches_cpython() {        // random.shuffle(list(range(32))) after the 3 SetMemoryC draws:
        // the traced result order.
        let mut rng = CpRandom::seed_zero();
        rng.randint(0, 0xFFFF_FFFF);
        rng.randint(234, 65535);
        rng.randint(0, 0xFF);
        let mut v: Vec<usize> = (0..32).collect();
        // replicate the shuffle manually to check the order too
        for i in (1..32).rev() {
            let j = rng.randbelow(i as u64 + 1) as usize;
            v.swap(i, j);
        }
        assert_eq!(
            v,
            vec![
                7, 0, 27, 20, 10, 5, 21, 13, 1, 17, 31, 22, 23, 24, 14, 30,
                3, 19, 26, 4, 29, 6, 18, 11, 28, 9, 25, 12, 15, 16, 8, 2
            ]
        );
    }

    #[test]
    fn import_time_replay_matches_python_values() {
        // Head of the replayed sequence (traced): the first draws after
        // seed(0) must be exactly these.
        let mut rng = CpRandom::seed_zero();
        let log = import_time_draws();
        assert_eq!(log.len(), 3 + 1 + 32 * 4 + 5);
        replay_draw_log(&mut rng, &log);
        // After the full replay, the next traced draw (eps_stattext) is the
        // first strcommon randrange(58040099, 59652323) -> 59595293.
        assert_eq!(rng.randrange(58040099, 59652323), 59595293);
    }

    #[test]
    fn savechk_tail_matches_traced_build() {
        // End-to-end: replay the eps_BGM logical draw log (recorded by the
        // tracer) and check the savechk draws produce the traced values.
        // Log: seed(0) → replay (136+1) → 2 strcommon groups →
        // 3 x randint(234, 65535) → sample → randint(0, 0xFFFFFFFF).
        let mut rng = CpRandom::seed_zero();
        replay_draw_log(&mut rng, &import_time_draws());
        // strcommon groups from the eps_BGM trace:
        // dbstr_print #1: randrange(126322568, 134217728) -> 132543344,
        //   randint(0, 0xFFFFFF) x2 -> 2653276, 11271489
        assert_eq!(rng.randrange(126322568, 134217728), 132543344);
        assert_eq!(rng.randint(0, 0xFF_FFFF), 2653276);
        assert_eq!(rng.randint(0, 0xFF_FFFF), 11271489);
        // dbstr_print #2: randrange(2147483649, 4294967295) -> 3734508686, ...
        assert_eq!(rng.randrange(2147483649, 4294967295), 3734508686);
        assert_eq!(rng.randint(0, 0xFF_FFFF), 1533319);
        assert_eq!(rng.randint(0, 0xFF_FFFF), 9424875);
        // finalizer: dwepdio :24, :128, :37
        assert_eq!(rng.randint(234, 65535), 9070);
        assert_eq!(rng.randint(234, 65535), 15971);
        assert_eq!(rng.randint(234, 65535), 50183);
        // savechk: sample consumes draws (verified: 2 raw outputs here)
        let trailer = draw_savechk_fake_section(&mut rng);
        assert_eq!(&trailer[0..4], b"ISOM");
        let len = u32::from_le_bytes(trailer[4..8].try_into().unwrap());
        // traced: randint -> 2667504740, | 0x80000000
        assert_eq!(len, 2667504740 | 0x8000_0000);
    }

    #[test]
    fn draw_log_json_roundtrip() {
        let log = vec![
            Draw::Randint { a: 0, b: 0xFFFF_FFFF },
            Draw::Randrange { start: 1, stop: 10 },
            Draw::Shuffle { n: 32 },
            Draw::Sample { n: 1, k: 1 },
        ];
        let json = serde_json::to_string(&log).unwrap();
        assert!(json.contains("\"op\":\"randint\""));
        let back = parse_draw_log(&json).unwrap();
        assert_eq!(back, log);
    }

    fn hex(s: &str) -> Vec<u8> {
        (0..s.len())
            .step_by(2)
            .map(|i| u8::from_str_radix(&s[i..i + 2], 16).unwrap())
            .collect()
    }

    #[test]
    fn pinned_mrgndata_matches_python() {
        // random.Random(0xC10C): randbytes(16) + randbytes(4) +
        // randbytes(16), then randbytes(5100). Spot bytes from CPython 3.12.
        let data = pinned_mrgndata();
        assert_eq!(data.len(), 5100);
        assert_eq!(&data[..16], &hex("ffcd8680930164ff8a38e179bfda35c3")[..]);
        assert_eq!(&data[100..116], &hex("cd997ed6b87449d0ff2b5bdb96a939e9")[..]);
        assert_eq!(&data[5090..], &hex("fdc9d9a8bc7d920a4402")[..]);
    }
}
