# Task 5 Atomic Recovery Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close every Critical and Important Task 5 review finding with schema-neutral recovery, a single-pointer versioned artifact protocol, bounded cancellable subprocesses, exact provenance validation, and atomic composite state convergence.

**Architecture:** `OperationStore` remains the sole durable state machine and gains fenced `BEGIN IMMEDIATE` helpers for artifact invalidation and composite-tree outcomes. `ArtifactManager` publishes immutable version bundles and atomically swaps one per-stage current pointer; manifests and artifact readers resolve only that pointer, so readers see either the prior complete version or the next complete version. Media processes expose injected cancellation/timeouts and stage only validated results; the store publishes success after the filesystem pointer is committed.

**Tech Stack:** Python 3.14, SQLite, pathlib/os atomic rename, Pillow, ffmpeg/ffprobe, asyncio/threads, pytest/AnyIO, Ruff.

## Global Constraints

- Preserve `scripts/get_youtube_token.py`; never edit, stage, or commit it.
- No schema changes and no real provider, social, paid-audio, TMDB, or OMDb calls.
- Every production behavior change follows strict test-first RED/GREEN.
- Artifact invalidation is one lease-fenced `BEGIN IMMEDIATE` mutation over existing job/stage/event rows.
- Artifact publication has one committed current pointer; no stable artifact/manifest dual-write and no live-target removal window.
- Reject every symlink in owned staging/run trees using `lstat`; resolving a symlink does not make it safe.
- Fail closed on probe failure when the tool is available; cancellation is checked immediately before every promotion/pointer swap.

---

### Task 1: Fenced general artifact invalidation and job-bound validation

**Files:**
- Modify: `api/domain.py`
- Modify: `api/database.py`
- Modify: `api/pipeline.py`
- Modify: `tests/unit/test_domain.py`
- Modify: `tests/unit/test_operation_store.py`
- Modify: `tests/integration/test_pipeline_runner.py`

**Interfaces:**
- Produce: `OperationStore.invalidate_stage_and_downstream(job_id, stage_name, *, lease_owner, safe_error_code, safe_error_message) -> dict | None`
- Change: `PipelineServices.validate_stage(stage_name, expected_job_id, output_manifest)`

- [ ] Add a domain RED proving any `completed -> queued` stage move is accepted only with `AttemptTrigger.ARTIFACT_INVALIDATION`, while the ordinary transition remains rejected.
- [ ] Add store RED tests that prepare completed upstream/target/downstream stages plus composite children, invoke invalidation under a live lease, and assert one atomic outcome: upstream remains completed, target becomes queued with cleared artifact/progress/error, downstream and children become pending with cleared artifacts/progress/errors, job becomes needs-attention and releases its lease, and one invalidation event is committed.
- [ ] Add store RED tests for stale-owner rejection and injected-event rollback leaving every row unchanged.
- [ ] Implement the schema-neutral helper inside one `_mutation()` transaction, validate the explicit trigger, update existing rows only, and record the diagnostic event before commit.
- [ ] Change runner/service validation to pass the claimed `job_id`; add RED tests for cross-run manifests, wrong job IDs, and changed selected candidate ID/hash, then enforce exact manifest version/stage/job/config/input/provenance.
- [ ] Replace `_invalid_completed_stage` with the atomic helper and run the domain/store/runner groups to GREEN.

### Task 2: Immutable version bundles and one atomic current pointer

**Files:**
- Modify: `api/artifacts.py`
- Modify: `tests/unit/test_artifacts.py`

**Interfaces:**
- Produce layout: `<root>/<job>/versions/<stage>/<version>/artifact` plus `manifest.json`; one `<root>/<job>/current/<stage>.json` pointer selects the readable version.
- Produce: `ArtifactManager.recover(job_id: str | None = None) -> None`
- Preserve callers: `write_json`, `promote_file`, `promote_directory`, `promote_frame_directory`, `manifest_path`, `artifact_path`, `validate`, `load_json`.

- [ ] Add RED tests with an injected checkpoint callback for failure before bundle install, after bundle install/before pointer, and immediately after pointer replacement. Assert readers always resolve the old complete version or the new complete version, never a missing/mismatched pair; prior version bytes remain present at every checkpoint.
- [ ] Add recovery RED tests for partial bundle/pointer files and installed-but-unreferenced bundles; re-instantiation/recovery removes partial state without changing the last valid pointer.
- [ ] Implement immutable bundle assembly entirely off the readable path, write/fsync manifest inside the bundle, atomically install the bundle, then write/fsync/replace the single pointer. Never rename/remove a current artifact during publication.
- [ ] Make `validate` load and hash the selected bundle manifest and require it to equal the supplied durable manifest/version; make `manifest_path`, `artifact_path`, and `load_json` resolve the selected pointer.
- [ ] Add RED tests for symlink files, symlink directories, and nested external links; implement lexical confinement plus `lstat` checks for every existing path component and every descendant.
- [ ] Change media inspection RED expectations so an injected/available ffprobe failure raises `ArtifactValidationError`; absence of ffprobe may retain nonzero fallback with a warning.
- [ ] Run artifact tests to GREEN and execute recovery twice to prove idempotency.

### Task 3: Staged encoder output, exact media duration/frame validation, and safe diagnostics

**Files:**
- Modify: `src/video/encoder.py`
- Modify: `api/pipeline.py`
- Modify: `tests/unit/test_encoder.py`
- Modify: `tests/integration/test_generation_scenarios.py`

**Interfaces:**
- Preserve: `FFmpegEncoder.encode(frames, audio, staged_output, on_progress, cancel_requested) -> Path`
- Encoder output is staging only; `GenerationPipelineServices._encode` passes it to `ArtifactManager.promote_file`.

- [ ] Add RED integration proving encoder success followed by probe/manifest/pointer failure leaves the prior current video selected and no public `final.mp4` is directly replaced.
- [ ] Add RED encoder tests for expected duration/frame count (`len(frames) / fps`), truncated `-shortest` output, probe timeout/failure, cancellation during encode/probe, and a final cancellation signal immediately before promotion.
- [ ] Replace raw diagnostic retention with a bounded allowlisted diagnostic sink (raw ffmpeg text is drained and discarded); add RED long-token, absolute-path, and 4096-byte chunk-boundary tests proving no suffix can appear.
- [ ] Implement cancellable/timeout-bounded ffprobe polling with terminate then kill, exact expected duration/frame validation, and final pre-`os.replace` cancellation.
- [ ] Change `_encode` to allocate an ArtifactManager staging file, encode there, and publish only through the versioned manager with expected frame count/duration details.
- [ ] Run encoder and generation scenario tests to GREEN.

### Task 4: Atomic audio cache validation and cancellable audio subprocesses

**Files:**
- Modify: `src/audio/providers.py`
- Modify: `src/audio/pipeline.py`
- Modify: `src/audio/mixer.py`
- Modify: `api/pipeline.py`
- Modify: `tests/integration/test_generation_scenarios.py`

**Interfaces:**
- Add injected `cancel_requested` and bounded process timeout to `AudioPipeline`, `AudioMixer`, and subprocess-backed providers.
- Cache hit validity requires nonzero bytes, matching stored SHA-256, and positive probe duration when ffprobe exists.

- [ ] Add RED tests for corrupt cache bytes, missing/mismatched cache checksums, ffprobe-rejected cache, and eviction/regeneration without promoting invalid cached content.
- [ ] Implement atomic cache payload/checksum staging; validate on every hit and evict both files when invalid.
- [ ] Add RED mixer/silence/ffprobe tests with a hung fake `Popen`; assert cancellation/timeout calls terminate, escalates to kill when needed, joins/drains, preserves the prior output, and raises without late promotion.
- [ ] Replace blocking `subprocess.run` audio/probe paths with injected `Popen` polling, bounded timeout, terminate/kill, safe drain/discard, and a final cancellation check before `os.replace`.
- [ ] Thread the durable cancellation callback from generation services through pipeline, providers, mixer, and probes; run audio/generation tests to GREEN.

### Task 5: Lazy composite children and atomic tree outcomes

**Files:**
- Modify: `api/database.py`
- Modify: `api/pipeline.py`
- Modify: `tests/unit/test_operation_store.py`
- Modify: `tests/integration/test_generation_scenarios.py`

**Interfaces:**
- Produce: `OperationStore.transition_stage_tree(...)` for one fenced parent/job/children outcome transaction.
- Child frame manifests contain stage/version/job, parent pointer stage/version, exact dimensions/prefix/digits/count/hash, inputs, and config hash.

- [ ] Add RED scenario proving future children remain pending until their first progress callback and a late verdict failure leaves no running/completed child under a failed/attention parent.
- [ ] Add RED retry scenario proving a prior attention/running child is reset and a successful retry atomically completes parent plus every child with current, independently validatable manifests.
- [ ] Add store rollback RED for an injected child/event failure; assert parent/job/all children remain unchanged.
- [ ] Start each child lazily on first progress; leave rendered child state nonterminal until publication succeeds.
- [ ] Derive complete child frame manifests from the committed composite bundle and return them with the parent manifest.
- [ ] Use the tree helper for composite success and failure so parent/child/job state changes converge in one transaction; remove eager child completion and handler-local failure transitions.
- [ ] Run composite/store/generation scenario tests to GREEN.

### Task 6: Construct services before claim and harden optional metadata

**Files:**
- Modify: `api/dispatcher.py`
- Modify: `src/data/movie_metadata.py`
- Modify: `tests/unit/test_dispatcher.py`
- Modify: `tests/integration/test_generation_scenarios.py`

**Interfaces:**
- Dispatcher constructs a runner before `claim_next_job`; factory failure stops that claim cycle with every queued row unleased.

- [ ] Add dispatcher RED where `runner_factory` raises; assert `claim_next_job` is never called, the queued job remains unleased, and shutdown observes the supervisor without a reclaim loop.
- [ ] Move factory invocation before claim and pass the preconstructed runner into the retained runner task; keep concurrency/heartbeat behavior unchanged.
- [ ] Add metadata RED cases for OMDb `Response=False` configuration errors, invalid poster bytes, and generic request transport failures; implement safe attention/transient classification and Pillow verification without external calls.
- [ ] Run dispatcher/metadata scenarios to GREEN.

### Task 7: Final verification, report, review, and commit

**Files:**
- Modify: `.superpowers/sdd/task-5-report.md`

- [ ] Run the exact Task 5 media/generation suites and affected Task 4 runner/dispatcher/persistence/route suites.
- [ ] Run focused Ruff and `git diff --check`.
- [ ] Run the full repository suite and record the known analysis baseline separately from Task 5 results.
- [ ] Append per-finding RED/GREEN commands and output, pointer protocol invariants/recovery checkpoints, root causes, self-review, deferred broad stage-hash Minor, and remaining concerns to the report.
- [ ] Request independent code review; resolve all Critical/Important findings with fresh RED/GREEN cycles.
- [ ] Stage only the fix allowlist (force-add the ignored metadata file if modified), confirm the protected script is unstaged, and commit `fix: make generation recovery atomic and safe`.
