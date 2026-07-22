# Task 5 Report: validated, resumable generation artifacts

## Outcome

Implemented real durable generation services for metadata, analysis, graph rendering,
streaming compositing, audio generation/mixing, and final ffmpeg encoding. Artifacts are
created below opaque run roots, validated by version/input/config/content contracts, and
promoted atomically without overwriting the previous validated result on failure. The
application's retained dispatcher now constructs `GenerationPipelineServices` by default.

No real subtitle, TMDB, OMDb, paid-audio, or social API was called by tests. The existing
user modification in `scripts/get_youtube_token.py` was not edited, staged, or committed.

## TDD evidence

Initial required RED command:

```text
.venv/bin/python -m pytest tests/unit/test_artifacts.py tests/unit/test_encoder.py tests/unit/test_compositor.py tests/integration/test_generation_scenarios.py -v
```

Collection reached the eight existing compositor tests and failed on the three intended
missing interfaces: `api.artifacts`, `src.video.encoder`, and the generation scenario's
`api.artifacts` import.

Focused RED/GREEN cycles also exposed and fixed:

- invalid frame staging survived failed validation;
- composite-child progress was rejected without the parent's lease owner;
- child completion erased the `frames` progress unit;
- an automatic subtitle service could complete the stage before the runner persisted its
  resumable manifest;
- the first worker adapter blocked interpreter shutdown, and a callback-based replacement
  did not reliably wake AnyIO; a daemon worker with asyncio-side polling keeps the event
  loop and lease heartbeat live;
- a manifest labelled for another stage was accepted when its bytes/hashes otherwise
  matched. The regression failed `assert not True`, then passed after stage-name binding.

## Implemented behavior

- `ArtifactManager` confines generated paths, streams SHA-256 calculation, writes manifests
  and JSON atomically, validates exact sequential PNG names/counts/dimensions/content,
  rejects stale tails, validates generic directory file maps, probes non-empty media when
  ffprobe is available, and rolls back failed replacement.
- `MovieMetadataClient` canonicalizes IMDb IDs, applies bounded HTTP timeouts, explicitly
  distinguishes optional missing configuration, transient provider failures, deterministic
  rejection/invalid responses, and verified metadata/poster results.
- `FFmpegEncoder` uses argument arrays with `shell=False`, concurrently drains progress and
  bounded stderr, reports actual `frame=` values, sanitizes diagnostics, supports
  cancellation/termination, validates the partial, and only then replaces the final MP4.
- `VideoCompositor.render_all()` consumes `iter_*` generators directly, writes one frame at
  a time into fresh segment/concat staging, releases frame references, reports truthful
  per-segment progress, and returns metadata rather than raw frame arrays. Short public
  helpers remain `list(iter_*)` wrappers.
- Plot rendering uses an in-memory base image and atomic preview replacement; it emits real
  per-frame progress and no `_base.png` contaminates a validated sequence.
- Audio provider cache keys include effective output settings; output/cache writes and all
  mixer paths use unique partials and atomic replacement. ffprobe failures become warnings.
- `GenerationPipelineServices` runs blocking media/provider work off the event loop, binds
  every stage to relevant input/config hashes, persists composite child stages/progress,
  validates completed work before reuse, and records safe warnings. Automatic subtitle
  completion is lease-fenced and receives the runner-validated manifest.

## Verification

Fresh required focused suite:

```text
28 passed, 1 known pysrt deprecation warning
```

Fresh all-media/generation suite, including the dedicated plotter tests:

```text
31 passed, 2 known warnings
```

Pipeline/persistence regression suite:

```text
48 passed, 4 existing FastAPI on_event deprecation warnings
```

Full repository suite:

```text
203 passed, 1 failed, 26 warnings
```

The sole failure is the disclosed unrelated baseline
`tests/integration/test_pipeline.py::TestAnalysisEngineIntegration::test_django_srt_pipeline`:
the test expects 100 f-bombs while the existing engine reports 200. Task 5 does not modify
that analysis behavior or expectation.

Focused Ruff reported `All checks passed!`; `git diff --check` exited 0.

## Self-review

- Confirmed generated/manifest paths cannot escape a validated opaque run root and remote
  filenames never become generated paths.
- Confirmed stage/version/input/config/content hashes cover reuse decisions, including
  stage-label integrity and exact directory contents.
- Confirmed failed frame, audio, cache, mixer, and encode work cannot replace the last valid
  result; new validated directories remove stale tail frames.
- Confirmed ffmpeg/ffprobe calls use argument arrays, bounded diagnostics, and explicit
  `shell=False`; external HTTP calls have bounded timeouts and tests use injected fakes.
- Confirmed production compositing does not retain full segment frame arrays and blocking
  generation leaves dispatcher heartbeat scheduling responsive.
- Confirmed durable output paths are relative and errors/warnings pass through existing
  recursive sanitization before persistence.

## Independent review

The independent reviewer found three Important pre-commit gaps and no Critical issues:

- `src/data/movie_metadata.py` is hidden by the repository's broad `data/` ignore rule;
  the final staging command therefore force-adds that exact file.
- bounded stderr was truncated before sanitization, which could leak a long token suffix;
  a focused regression observed the leak before bounded raw retention plus
  sanitize-before-public-truncate fixed it.
- ffmpeg text pipes lacked an explicit decode-error policy, so malformed bytes could stop a
  drain thread; the process contract now pins UTF-8 with `errors="replace"`.

After those fixes the reviewer reported no remaining Critical or Important code issues.

## Files

Created `api/artifacts.py`, `src/data/movie_metadata.py`, `src/video/encoder.py`,
`tests/unit/test_artifacts.py`, `tests/unit/test_encoder.py`, and
`tests/integration/test_generation_scenarios.py`. Modified `api/main.py`,
`api/pipeline.py`, `src/video/compositor.py`, `src/video/plotter.py`,
`src/audio/pipeline.py`, `src/audio/providers.py`, `src/audio/mixer.py`, and
`tests/unit/test_compositor.py`.

## Commit

`5d1c248 feat: validate and resume generation artifacts`

---

## Atomic recovery review follow-up (2026-07-22)

This section records the post-commit recovery review and supersedes the earlier review
status for the recovery patch. The implementation remains schema-neutral and tests make no
real provider, publishing, paid-audio, TMDB, or OMDb calls. The protected local change in
`scripts/get_youtube_token.py` remains outside the patch.

### Root causes and RED/GREEN evidence

- **General invalidation was not a single durable state change.** A completed artifact that
  failed validation needed to reset its downstream stage/child tree, clear stale outputs and
  progress, move the job to operator attention, release its lease, and record the reason in
  one fenced mutation. RED covered general `completed -> queued` rejection, target/downstream
  state, stale-owner rejection, and injected-event rollback. GREEN added the explicit
  `ARTIFACT_INVALIDATION` domain trigger and `OperationStore.invalidate_stage_and_downstream`.
- **Artifact replacement previously coupled two independently readable paths.** Replacing a
  stable artifact and manifest could expose a mixed or missing pair after a crash. RED injected
  crashes at `journal_written`, `bundle_installed`, and `pointer_replaced`, then instantiated
  recovery twice. GREEN publishes an immutable version directory and changes only one atomic
  current-pointer file; abandoned partial/unreferenced work is recoverable and prior versions
  remain intact.
- **Reuse provenance trusted manifest-owned identity too broadly.** RED supplied cross-job,
  wrong-stage, changed candidate-ID/hash, and changed input/config manifests. GREEN passes the
  runner's claimed job ID into validation and binds subtitle selection and every generated
  stage to exact job/stage/version/input/config/content provenance.
- **Media success could be published without complete facts.** RED covered truncated video,
  frame-count mismatch, probe timeout/failure/cancellation, an injected probe returning `None`,
  corrupt/missing-checksum/rejected audio cache hits, and hung audio/ffmpeg subprocesses.
  GREEN makes configured/available probes fail closed, validates expected video duration and
  frame count, uses cancellable bounded `Popen` polling, drains/discards raw diagnostics, and
  performs a final cancellation check before promotion.
- **Composite child state was eager and separately committed.** RED covered lazy start, late
  child failure, retry reset, full independently validated child manifests, empty child output
  rejection, and injected transaction rollback. GREEN starts a child on its first callback,
  derives its exact frame contract from the committed parent version, and commits composite
  success or failure convergence through fenced store transactions.
- **Service construction could fail after a job claim.** RED proved a factory exception must
  leave the queue untouched. GREEN constructs/caches generation services before dispatcher
  startup and constructs a runner before `claim_next_job` in each claim cycle.
- **Versioned generation paths were not consumed by legacy routes.** A RED route integration
  returned 404 for a valid versioned encode/composite bundle. GREEN resolves video, segment,
  frame, and publish inputs from the durable stage manifest through `ArtifactManager`, and the
  publish record uses the opaque job ID.
- **A stale blocking worker could replace the current pointer after losing ownership.** RED
  proved both an async-cancelled worker released after cancellation and an expired/reclaimed
  lease owner could still publish. GREEN propagates a thread-local cancellation event, carries
  the exact lease owner/duration on the runner progress capability, and supplies every artifact
  publication with a fail-closed guard. Under a per-job/stage filesystem lock the guard renews
  that exact live lease immediately before the pointer swap; failure removes the candidate
  bundle and journal without changing current.

Representative fresh focused command after the final stale-owner fixes:

```text
.venv/bin/python -m pytest tests/unit/test_artifacts.py tests/unit/test_encoder.py \
  tests/integration/test_generation_scenarios.py tests/integration/test_pipeline_runner.py \
  tests/unit/test_dispatcher.py tests/unit/test_operation_store.py \
  tests/unit/test_domain.py tests/integration/test_job_submission.py -q

123 passed, 5 warnings
```

Fresh full repository verification after the recovery fixes:

```text
.venv/bin/python -m pytest -q

232 passed, 1 failed, 26 warnings
```

The sole failure is the same disclosed baseline
`tests/integration/test_pipeline.py::TestAnalysisEngineIntegration::test_django_srt_pipeline`:
the unchanged analysis engine reports 200 f-bombs while that test expects 100. Focused Ruff
reported `All checks passed!`, and `git diff --check` exited 0.

### Pointer and recovery invariants

- Readers select exactly one immutable version through `current/<stage>.json`; no stable
  artifact/manifest dual-write exists.
- A bundle contains its artifact and manifest before becoming selectable. The pointer embeds
  the selected manifest path and SHA-256, and validation requires equality with the durable
  stage manifest.
- Publication never removes the previous version. A crash before pointer replacement leaves
  old current selected; a crash after replacement leaves new current selected.
- Recovery is idempotent: partial paths and installed-but-unselected journal versions are
  removed, while the pointer-selected bundle is retained.
- Lexical confinement and `lstat` reject symlink components/descendants in generated trees.
- The pointer swap is cancellation- and lease-fenced. A stale owner cannot publish after a
  replacement owner has claimed the run, including across worker processes using the same
  artifact root.

### Review feedback and resolution

The independent recovery reviewer initially marked the patch **Not Ready** with two Critical
and two Important findings:

- Critical: versioned bundles broke video/frame/segment/publish consumers — resolved with
  durable-manifest route resolution and a route-level regression.
- Critical: lease loss did not fence the filesystem pointer swap — resolved with the locked
  cancellation/live-lease publication guard and two integration regressions.
- Important: subprocess-backed public audio providers did not uniformly receive cancellation
  — resolved by threading the callback through provider generation and duration probes,
  including a hung silence-provider regression.
- Important: an explicitly configured encoder duration probe could return no facts and still
  pass — resolved by failing closed, with a focused regression.

The reviewer also requested the missing report append and broader crash checkpoint coverage;
this section supplies the report, and the crash matrix now includes the post-pointer checkpoint
with idempotent recovery.

After these changes, the follow-up independent review returned **Ready** with no remaining
Critical or Important code issues.

### Self-review and remaining concerns

- Confirmed every `GenerationPipelineServices` call to `write_json`, `promote_file`,
  `promote_directory`, or `promote_frame_directory` carries the same publication guard.
- Confirmed publication guard failure leaves the prior pointer readable and cleans its bundle
  and journal; focused crash, stale-owner, and cancellation tests pass together.
- Confirmed route readers validate the store-owned manifest rather than constructing a stable
  output path, and publishing refuses incomplete jobs.
- Confirmed the dispatcher cannot claim before production service construction succeeds and
  optional metadata errors remain safely classified.
- Confirmed no schema migration was added and the protected token helper was not touched.
- Deferred Minor: graph and composite currently hash the broad `video` config mapping. This is
  safe but may over-invalidate one stage for an unrelated video setting; stage-minimal config
  projections can be refined separately without weakening correctness.
- Filesystem atomicity assumes the existing deployment contract that a job's artifact root is
  on one local filesystem and supports atomic `os.replace`, directory `fsync`, and advisory
  `flock` (the current Linux target does).
- Minor: `ArtifactManager`'s secondary media probe has a bounded 15-second subprocess timeout
  rather than immediate cancellation. The locked cancellation/live-lease guard still prevents
  late publication, so this affects shutdown latency rather than artifact correctness.

### Recovery-fix files

Modified `api/artifacts.py`, `api/database.py`, `api/dispatcher.py`, `api/domain.py`,
`api/main.py`, `api/pipeline.py`, `src/audio/mixer.py`, `src/audio/pipeline.py`,
`src/audio/providers.py`, `src/data/movie_metadata.py`, `src/video/encoder.py`, and their
focused unit/integration tests. Added the implementation plan at
`docs/superpowers/plans/2026-07-22-task-5-recovery-fixes.md`.
