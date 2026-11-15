//! Opt-in compile-stage timings used by the manual benchmark harness.
//!
//! Set `EUDKIT_STAGE_TIMINGS=1` to emit one JSON line per completed stage on
//! stderr:
//!
//! ```text
//! __EUDKIT_STAGE__ {"name":"hir.parse","ms":12.345}
//! ```
//!
//! Normal builds only pay for the environment lookup in [`Stage::start`]; no
//! clock is read and nothing is printed when the variable is absent.

use std::env;
use std::time::Instant;

/// Prefix of one stage-timing line on stderr.
pub const STAGE_MARKER: &str = "__EUDKIT_STAGE__ ";
/// Environment variable that enables stage timings.
pub const STAGE_ENV: &str = "EUDKIT_STAGE_TIMINGS";

/// A scope timer for one pipeline stage.
#[must_use = "call finish() to record the stage"]
pub struct Stage {
    name: &'static str,
    start: Option<Instant>,
}

impl Stage {
    /// Start a stage timer. Does nothing unless [`STAGE_ENV`] is set.
    pub fn start(name: &'static str) -> Self {
        Self {
            name,
            start: enabled().then(Instant::now),
        }
    }

    /// Record the elapsed time for this stage.
    pub fn finish(self) {
        if let Some(start) = self.start {
            let ms = start.elapsed().as_secs_f64() * 1000.0;
            eprintln!(
                "{STAGE_MARKER}{{\"name\":\"{}\",\"ms\":{ms:.3}}}",
                self.name
            );
        }
    }
}

fn enabled() -> bool {
    let Some(value) = env::var_os(STAGE_ENV) else {
        return false;
    };
    let value = value.to_string_lossy();
    !value.is_empty() && value != "0" && value != "false"
}
