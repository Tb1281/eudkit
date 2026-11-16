# MPQ embedded-file surface in HIR modules

Status: accepted (2026-11-09; revised 2026-11-10)

## Context

euddraft plugins can add files to the output MPQ from a trigger-exec hook:
`bgmplayer.py` calls `MPQAddFile("bgm", data)` in `beforeTriggerExec`, and the
future dataDumper port needs the same ability to ship data files. eudkit has
no trigger-exec hook and no plugin system by design, so the file must be
recorded at build time instead.

The map assembler already accepts `AssemblyOptions.queued_files`
(`eudkit_map::mpq::QueuedFile` / `QueuedContent`), and the map tail applies
them with `replace_existing = true` — exactly `MPQAddFile` semantics. What was
missing was a frontend-visible, general way to populate that queue. Adding it
inside the BGM helper would have made the helper the only caller and forced
the dataDumper port to copy the plumbing.

## Decision

1. **HIR carries the list, not the bytes.** `HirModule.files:
   Vec<HirEmbeddedFile>` where `HirEmbeddedFile { name, path }`. `name` is the
   archived MPQ name, `path` is the source path as authored. The list is
   omitted from HIR JSON when empty, so existing dumps are byte-identical
   apart from the version field.

   **HIR 3.1 → 3.2.** The version is exact-checked (`HirDocument::validate`
   rejects anything else), so this bump is what makes the field safe. The
   field alone would not be: `files` is `#[serde(default)]` and the JSON
   reader has no `deny_unknown_fields`, so a 3.1 reader handed a 3.1-tagged
   document carrying `files` would parse it, drop the list, and happily emit
   a map with a dangling `PlayWAV("bgm")` — silence instead of a build error.
   Tagging the document 3.2 makes every older reader fail clean with
   `Unsupported HIR version: 3.2 (expected 3.1)`, the same contract ADR 0006
   established for 3.0 → 3.1. The cost is that a stale 3.1 `.hir.json` must
   be re-synthesized before it can be compiled; nothing else in the document
   changed shape.
2. **One frontend API, module-builder scoped.** `embedFile(name, path)` in
   TypeScript and `embedFile` / `embed_file` in Python. Names must be
   non-empty and unique; an identical `(name, path)` request is idempotent,
   while a second path for an existing name fails with `EUDSL-BOUNDS`. Paths
   must be non-empty. Calls outside `buildModule` fail with `EUDSL-SCOPE`.
3. **Paths stay machine-independent in HIR.** The CLI resolves relative paths
   against the directory of the compiling source
   (`PreparedProgram::source_dir`) and uses absolute paths verbatim. HIR
   artifacts therefore do not embed build-machine paths. A `.hir.json` file
   input resolves relative paths against the directory containing that JSON
   file (`prepare_source` sets `source_dir` from its parent), not the process
   working directory; only a programmatic `prepare_hir_json(json_string, …)`
   has no source directory, and only that case falls back to the working
   directory.
4. **The existing assembler queue is the only consumer.**
   `PreparedProgram::queued_files()` maps each entry to
   `QueuedFile { archived_name, content: QueuedContent::Path(resolved) }`,
   which `compile_prepared_with_options` passes as
   `AssemblyOptions.queued_files`. No new MPQ code path is introduced.
5. **The optimizer preserves the list verbatim.** `optimize_module` copies
   `files` through; it is build metadata, not a statement.

## Consequences

- `play_bgm` / `playBgm` and the future dataDumper port share one surface.
  Adding a second embedded file is one call.
- Files are read once at assembly time and added with replace-existing
  semantics. A missing or unreadable file fails the build rather than
  producing a map with a dangling `PlayWAV`/data reference.
- A reader older than HIR 3.2 rejects the document outright instead of
  silently dropping `files` (see Decision 1); a stale 3.1 dump must be
  re-synthesized.
- Existing HIR, cost-gate goldens, and map output are unchanged: the list is
  empty unless a frontend calls `embedFile`. Only the `version` field of a
  synthesized document moves.
- A wav-only module seeds the basemap string table from its `PlayWAV` name:
  the lowerer's seeding predicate counts a `wav` action string as well as a
  `text` one, because both are interned into the same table. Before that,
  a module whose only string use was `playBgm`/`play_bgm` failed to compile
  against a basemap (`LIR strings (1 ids) shorter than template table`).
- HIR size stays small; file contents never enter the JSON.

## Rejected alternatives

- **Keep HIR at 3.1 and rely on `serde(default)`.** Rejected: a 3.1 reader
  would parse the document, drop `files` without a word, and emit a map whose
  `PlayWAV("bgm")` names a missing entry — a silent gameplay bug. Failing
  clean costs a re-synth; silence costs a debugged match.
- **Embed the bytes in HIR.** Rejected: HIR is a reviewable text artifact and
  a diff unit; base64 audio or data blobs would swamp it and make goldens
  machine-specific.
- **Add files from a trigger-exec hook.** Rejected: eudkit has no such hook,
  and keeping plugin registration out of the runtime surface is a deliberate
  design constraint.
- **Keep it private to the BGM helper.** Rejected: the dataDumper port would
  duplicate the plumbing, and the acceptance criteria require a general
  surface.
- **Resolve relative paths at frontend time.** Rejected: it would bake
  absolute build-machine paths into HIR and break reproducible dumps.
- **A plugin/settings registry.** Rejected for the same reason;
  a helper is a plain function, not a registered extension.
