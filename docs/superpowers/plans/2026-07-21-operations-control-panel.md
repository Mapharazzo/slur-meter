# Operations Control Panel Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a durable, observable, recoverable video-generation and publishing workflow with an operations-first admin interface.

**Architecture:** SQLite stores normalized current state plus append-only events and a leased work queue. A shared pipeline service records real stage progress, bounded attempts, subtitle decisions, artifacts, and publishing outcomes. React uses abortable structured polling and renders a work queue plus timeline-centric job workspace.

**Tech Stack:** Python 3.11+, FastAPI, Pydantic 2, SQLite/WAL, pytest, React 19, React Router 7, Vite 6, Tailwind 3, Vitest, React Testing Library.

## Global Constraints

- Preserve the pre-existing modification to `scripts/get_youtube_token.py`; never stage or alter it.
- Do not call real social, paid audio, OpenSubtitles, TMDB, or OMDB services in tests or verification.
- Keep the subtitle automatic acceptance threshold at 70 percent and automatically evaluate no more than three candidates per retry cycle.
- Persist only sanitized errors and diagnostics; never expose credentials, cookies, bearer tokens, upstream bodies, or absolute workspace paths.
- Use test-driven development: observe each new regression/behavior test fail for the intended reason before production edits.
- Keep the existing React/Tailwind stack; do not add a large component or state-management framework.
- Use structured polling rather than SSE/WebSockets.
- Default dispatcher concurrency to one and require a single dispatcher owner for SQLite deployments.
- Every mutation is authenticated and idempotent; publishing remains explicit and is never exercised during this work.
- All expensive artifacts are validated before reuse or promotion.

---

### Task 1: Domain states, safe errors, settings, and identifiers

**Files:**
- Create: `api/domain.py`
- Create: `api/errors.py`
- Create: `api/settings.py`
- Test: `tests/unit/test_domain.py`
- Test: `tests/unit/test_errors.py`

**Interfaces:**
- Produces: `JobState`, `StageState`, `AttemptTrigger`, `FailureCategory`, `assert_job_transition(old, new)`, `assert_stage_transition(old, new)`.
- Produces: `OperationalError`, `AttentionRequired`, `TransientFailure`, `AmbiguousPublishOutcome`, `classify_exception(exc, operation)`, `sanitize_text(value, settings)`, `error_payload(error, request_id)`.
- Produces: `Settings.from_env(base_dir)`, `validate_job_id(value)`, `canonical_imdb_id(value)`, and `confined_path(root, *parts)`.

- [ ] **Step 1: Write transition, identifier, confinement, classification, and redaction tests.**

```python
def test_completed_run_can_queue_an_explicit_publish_operation():
    assert_job_transition(JobState.COMPLETED, JobState.QUEUED)

def test_running_run_cannot_skip_to_queued_without_recovery():
    with pytest.raises(InvalidTransition):
        assert_job_transition(JobState.RUNNING, JobState.QUEUED)

@pytest.mark.parametrize("value", ["../outside", "/tmp/x", "tt12/x", "q key"])
def test_job_ids_reject_path_material(value):
    with pytest.raises(ValueError):
        validate_job_id(value)

def test_diagnostics_redact_secrets_and_workspace(settings, monkeypatch):
    monkeypatch.setenv("YOUTUBE_REFRESH_TOKEN", "super-secret-value")
    text = sanitize_text("Bearer abc /home/mapha/slur-meter/x super-secret-value", settings)
    assert "abc" not in text
    assert "super-secret-value" not in text
    assert "/home/mapha" not in text
```

- [ ] **Step 2: Run the tests and confirm imports/functions are missing.**

Run: `.venv/bin/python -m pytest tests/unit/test_domain.py tests/unit/test_errors.py -v`

Expected: collection fails because `api.domain`, `api.errors`, and `api.settings` do not exist.

- [ ] **Step 3: Implement the enums, explicit transition maps, strict validation, settings parsing, exception taxonomy, retryability classification, and sanitizer.**

`OperationalError` must carry `code`, operator `message`, `category`, `retryable`, sanitized `technical_detail`, `actions`, and HTTP status without serializing the source exception. `Settings` must default allowed origins to `http://localhost:5173` and `http://localhost:8001`, load `.env` with `override=False`, and expose zero-delay retry policies for tests through constructor arguments.

- [ ] **Step 4: Run the focused tests and the legacy helper tests.**

Run: `.venv/bin/python -m pytest tests/unit/test_domain.py tests/unit/test_errors.py tests/unit/test_opensubtitles.py -v`

Expected: all pass; legacy `safe_imdb_id` behavior remains temporarily isolated until Task 3 replaces path-facing use.

- [ ] **Step 5: Commit the domain checkpoint.**

```bash
git add api/domain.py api/errors.py api/settings.py tests/unit/test_domain.py tests/unit/test_errors.py
git commit -m "feat: define operational states and safe errors"
```

### Task 2: Versioned operational schema and transactional store

**Files:**
- Create: `api/migrations.py`
- Rewrite: `api/database.py`
- Test: `tests/unit/test_database_migrations.py`
- Test: `tests/unit/test_operation_store.py`

**Interfaces:**
- Produces: `OperationStore(path, clock=utc_now)` with `initialize()`, `create_or_get_active_job()`, `get_job()`, `list_jobs()`, `get_job_detail()`, `transition_job()`, `claim_next_job()`, `renew_lease()`, `recover_expired_leases()`, `request_cancel()`, `ensure_stage()`, `transition_stage()`, `start_attempt()`, `finish_attempt()`, `record_event()`, `list_events()`, candidate/decision/publishing/cost/revenue methods.
- Consumes: Task 1 enums and errors.

- [ ] **Step 1: Write a legacy-schema fixture and migration preservation tests.**

```python
def test_legacy_jobs_children_and_orphans_are_preserved(tmp_path):
    db_path = create_legacy_database_with_orphans(tmp_path)
    store = OperationStore(db_path)
    store.initialize()
    assert store.get_job("tt0110912")["state"] == "completed"
    assert store.get_job("orphan_legacy")["label"].startswith("Recovered legacy run")
    assert store.foreign_key_violations() == []
    assert store.schema_versions() == [1, 2]
```

- [ ] **Step 2: Write transaction, lease, event, and idempotency tests.**

```python
def test_concurrent_duplicate_submission_returns_one_run(store):
    with ThreadPoolExecutor(max_workers=2) as pool:
        rows = list(pool.map(lambda _: store.create_or_get_active_job("tt0110912", "", "Pulp Fiction"), range(2)))
    assert len({row[0]["id"] for row in rows}) == 1
    assert sum(created for _, created in rows) == 1

def test_claim_is_atomic_and_restart_requeues_expired_work(store, clock):
    job, _ = store.create_or_get_active_job("tt0110912", "", "Pulp Fiction")
    claimed = store.claim_next_job("worker-a", lease_seconds=10)
    assert claimed["id"] == job["id"]
    assert store.claim_next_job("worker-b", lease_seconds=10) is None
    clock.advance(seconds=11)
    assert store.recover_expired_leases() == [job["id"]]
    assert store.get_job(job["id"])["state"] == "queued"
```

- [ ] **Step 3: Run focused tests and confirm the current unversioned database layer fails them.**

Run: `.venv/bin/python -m pytest tests/unit/test_database_migrations.py tests/unit/test_operation_store.py -v`

Expected: failures show missing `OperationStore`, schema versions, atomic claims, and operational tables.

- [ ] **Step 4: Implement migration 1 detection and migration 2 conversion.**

Create `schema_migrations`, `job_runs`, `pipeline_stages`, `subtitle_candidates`, `pipeline_attempts`, `pipeline_events`, `admin_decisions`, `publishing_attempts`, and new foreign-key-safe `costs`, `releases`, and `revenue`. Before copying legacy children, materialize recovered parents for every referenced missing ID. Map `done` to `completed`, interrupted legacy states to `queued`, and legacy steps to stages/attempts/events. Run `foreign_key_check` before commit.

- [ ] **Step 5: Implement the store with `BEGIN IMMEDIATE` mutations, compare-and-set transitions, WAL/busy timeout, JSON serialization, monotonic event IDs, retry cycles, and paginated summaries.**

List methods must return DTO-shaped dictionaries rather than raw `SELECT *` rows. Internal paths and full analysis events remain absent from queue summaries.

- [ ] **Step 6: Run focused tests, then the existing database-consuming unit suite.**

Run: `.venv/bin/python -m pytest tests/unit/test_database_migrations.py tests/unit/test_operation_store.py tests/unit -v`

Expected: focused tests and all legacy unit tests pass.

- [ ] **Step 7: Commit the persistence checkpoint.**

```bash
git add api/migrations.py api/database.py tests/unit/test_database_migrations.py tests/unit/test_operation_store.py
git commit -m "feat: persist runs attempts and events"
```

### Task 3: Subtitle safety, inspection, ranking, and candidate selection

**Files:**
- Rewrite: `src/data/opensubtitles.py`
- Create: `src/data/subtitle_quality.py`
- Create: `api/subtitles.py`
- Modify: `src/analysis/engine.py`
- Test: `tests/unit/test_opensubtitles.py`
- Create: `tests/unit/test_subtitle_quality.py`
- Create: `tests/unit/test_subtitle_service.py`

**Interfaces:**
- Produces: expanded `SubtitleResult`, `OpenSubtitlesClient.search()` with timeouts, and `download(file_id, destination)` using a caller-generated destination.
- Produces: `inspect_subtitle(path) -> SubtitleInspection`, `rank_candidates(candidates, request)`, and `evaluate_quality(inspection, runtime_seconds, threshold=0.70)`.
- Produces: `SubtitleService.discover(job_id)` and `SubtitleService.select(job_id, manual_candidate_id=None)`.

- [ ] **Step 1: Add regression tests for actual cue duration, deterministic ranking, encoding fallback, archive traversal, absolute provider filenames, nested archives, absent SRTs, and size caps.**

```python
def test_coverage_uses_final_cue_not_last_profanity(tmp_path):
    path = write_srt(tmp_path, final_end="01:35:00,000", text="clean dialogue")
    inspection = inspect_subtitle(path)
    result = evaluate_quality(inspection, runtime_seconds=100 * 60)
    assert result.coverage_percent == pytest.approx(95.0)
    assert result.accepted is True

def test_zip_traversal_member_is_rejected(client, tmp_path):
    response = zip_response({"../../escaped.srt": VALID_SRT})
    with stub_download(client, response), pytest.raises(UnsafeArchiveError):
        client.download("42", tmp_path / "candidate.srt")
    assert not (tmp_path.parent / "escaped.srt").exists()
```

- [ ] **Step 2: Add service tests proving exactly three automatic candidates, durable rejection reasons, exhaustion attention, manual override, cache replacement, upload confinement, and idempotent resume.**

- [ ] **Step 3: Run the new tests and observe failures for the current profanity-bin coverage and unsafe downloader.**

Run: `.venv/bin/python -m pytest tests/unit/test_opensubtitles.py tests/unit/test_subtitle_quality.py tests/unit/test_subtitle_service.py -v`

- [ ] **Step 4: Implement streamed bounded downloads, generated paths, safe ZIP/RAR member reads, encoding normalization, SRT structural inspection, explicit ranking/quality reasons, and content-hash cache provenance.**

The provider filename is metadata only. Unsupported or malformed candidates are rejected without failing the entire run. The selected cache is promoted only after validation.

- [ ] **Step 5: Implement discovery/selection using store attempts and events.**

Selection reads at most three ranked, unattempted candidates in a cycle. Manual selection requires a parsable file but can record a threshold override. Exhaustion raises `AttentionRequired` with `select_subtitle`, `rediscover_subtitles`, `upload_subtitle`, and `cancel` actions.

- [ ] **Step 6: Run subtitle and analysis tests.**

Run: `.venv/bin/python -m pytest tests/unit/test_opensubtitles.py tests/unit/test_subtitle_quality.py tests/unit/test_subtitle_service.py tests/unit/test_analysis.py -v`

- [ ] **Step 7: Commit the subtitle checkpoint.**

```bash
git add src/data/opensubtitles.py src/data/subtitle_quality.py src/analysis/engine.py api/subtitles.py tests/unit/test_opensubtitles.py tests/unit/test_subtitle_quality.py tests/unit/test_subtitle_service.py
git commit -m "feat: model and select subtitle candidates"
```

### Task 4: Retry executor, leased dispatcher, and resumable stage runner

**Files:**
- Create: `api/retry.py`
- Create: `api/dispatcher.py`
- Rewrite: `api/pipeline.py`
- Test: `tests/unit/test_retry.py`
- Test: `tests/unit/test_dispatcher.py`
- Test: `tests/integration/test_pipeline_runner.py`

**Interfaces:**
- Produces: `RetryPolicy(max_attempts, delays)`, `run_with_attempts(operation, context, policy, store, sleep)`.
- Produces: `JobDispatcher(store, runner_factory, concurrency=1)` with `start()`, `wake()`, `stop()`.
- Produces: `PipelineRunner.run(job_id, lease_owner)` and injected `PipelineServices` protocol.

- [ ] **Step 1: Write retry tests for transient success on attempt three, transient exhaustion, deterministic single attempt, and retry scheduling events.**

- [ ] **Step 2: Write dispatcher tests for durable enqueue, atomic single ownership, wake coalescing, graceful stop, expired lease recovery, and no duplicate execution.**

- [ ] **Step 3: Write runner tests for ordered stages, completed-stage reuse, cancellation between stages, restart from the interrupted stage, stage timing, and truthful progress fields.**

- [ ] **Step 4: Run the new tests and confirm missing retry/dispatcher/runner behavior.**

Run: `.venv/bin/python -m pytest tests/unit/test_retry.py tests/unit/test_dispatcher.py tests/integration/test_pipeline_runner.py -v`

- [ ] **Step 5: Implement retry execution and the dispatcher loop.**

The dispatcher owns retained tasks, polls only when not woken, recovers expired leases, limits concurrency to one by default, and closes attempts during shutdown/recovery. No route creates a pipeline task directly.

- [ ] **Step 6: Implement the injected runner and stage wrapper.**

The wrapper creates attempts, transitions stage/job atomically, renews leases, stores sanitized output/errors, applies automatic retry policy, and maps final deterministic/transient/attention/cancel outcomes consistently. Completed stages are reused only when their handler validates output.

- [ ] **Step 7: Run focused and persistence tests.**

Run: `.venv/bin/python -m pytest tests/unit/test_retry.py tests/unit/test_dispatcher.py tests/integration/test_pipeline_runner.py tests/unit/test_operation_store.py -v`

- [ ] **Step 8: Commit the execution checkpoint.**

```bash
git add api/retry.py api/dispatcher.py api/pipeline.py tests/unit/test_retry.py tests/unit/test_dispatcher.py tests/integration/test_pipeline_runner.py
git commit -m "feat: add durable resumable pipeline execution"
```

### Task 5: Real generation stages, atomic artifacts, streaming compositor, and encoding progress

**Files:**
- Create: `api/artifacts.py`
- Create: `src/data/movie_metadata.py`
- Create: `src/video/encoder.py`
- Modify: `src/video/compositor.py`
- Modify: `src/video/plotter.py`
- Modify: `src/audio/pipeline.py`
- Modify: `src/audio/providers.py`
- Modify: `src/audio/mixer.py`
- Modify: `api/pipeline.py`
- Test: `tests/unit/test_artifacts.py`
- Test: `tests/unit/test_encoder.py`
- Modify: `tests/unit/test_compositor.py`
- Create: `tests/integration/test_generation_scenarios.py`

**Interfaces:**
- Produces: `ArtifactManager` with generated per-run paths, manifests, hash validation, partial promotion, and prior-final preservation.
- Produces: `MovieMetadataClient.fetch()` that distinguishes absent optional configuration, transient requests, and verified success.
- Produces: `FFmpegEncoder.encode(frames, audio, output, on_progress, cancel_requested)`.
- Produces: `VideoCompositor.iter_*()` and streaming `render_all()` output without returning frame arrays.

- [ ] **Step 1: Write tests for stale/missing/extra frames, config/input hash invalidation, atomic directory promotion, and failed encode preserving the last validated MP4.**

- [ ] **Step 2: Write a compositor regression test that proves `render_all()` consumes iterators and does not call list-building segment helpers.**

- [ ] **Step 3: Write encoder tests that parse real `frame=` progress from a fake process, retain bounded stderr, reject missing ffmpeg, and atomically replace only on success.**

- [ ] **Step 4: Run new tests and observe failures against existence-only caches and direct `final.mp4` overwrite.**

Run: `.venv/bin/python -m pytest tests/unit/test_artifacts.py tests/unit/test_encoder.py tests/unit/test_compositor.py tests/integration/test_generation_scenarios.py -v`

- [ ] **Step 5: Implement artifact manifests and stage handlers for metadata, analysis, graph, composite children, audio, and encode.**

Write JSON atomically. Validate sequential frame names/counts/dimensions, audio/video nonzero size and duration where ffprobe is available, relevant config/input hashes, and manifest version. Progress callbacks persist actual numerator/denominator and refresh the lease.

- [ ] **Step 6: Refactor compositor frame creation into generators while retaining existing short helper APIs as `list(iter_...)` wrappers.**

Render each segment directly to the caller-provided staging directory and release its arrays before the next segment. Remove stale tail-frame acceptance by always promoting a newly validated directory.

- [ ] **Step 7: Make audio cache writes atomic and include all output-affecting provider settings. Replace swallowed ffprobe errors with warning callbacks.**

- [ ] **Step 8: Run all media and generation tests.**

Run: `.venv/bin/python -m pytest tests/unit/test_compositor.py tests/unit/test_plotter.py tests/unit/test_artifacts.py tests/unit/test_encoder.py tests/integration/test_generation_scenarios.py -v`

- [ ] **Step 9: Commit the generation checkpoint.**

```bash
git add api/artifacts.py api/pipeline.py src/data/movie_metadata.py src/video/encoder.py src/video/compositor.py src/video/plotter.py src/audio tests/unit/test_artifacts.py tests/unit/test_encoder.py tests/unit/test_compositor.py tests/integration/test_generation_scenarios.py
git commit -m "feat: validate and resume generation artifacts"
```

### Task 6: Publishing attempts, reconciliation, and metrics preservation

**Files:**
- Create: `api/publishing.py`
- Modify: `src/publishing/youtube.py`
- Modify: `src/publishing/tiktok.py`
- Modify: `src/publishing/instagram.py`
- Modify: `src/publishing/metadata.py`
- Test: `tests/unit/test_publishing_service.py`
- Test: `tests/unit/test_platform_clients.py`

**Interfaces:**
- Produces: `PublishingService.request()`, `publish()`, `retry()`, and `refresh_stats()` with injected platform clients.
- Produces: platform clients that raise explicit confirmation/stats errors instead of returning success-like empty values.

- [ ] **Step 1: Write tests for three transient attempts, deterministic missing credentials, concurrent duplicate publish requests, already-uploaded idempotency, empty remote IDs, ambiguous post-submit timeout, metadata reuse, and last-good metrics preservation.**

- [ ] **Step 2: Run the tests and confirm current single-row release overwrites and empty-ID success.**

Run: `.venv/bin/python -m pytest tests/unit/test_publishing_service.py tests/unit/test_platform_clients.py tests/unit/test_metadata.py -v`

- [ ] **Step 3: Implement durable publication request/attempt behavior.**

Persist metadata once, default YouTube privacy to `private`, refuse duplicate uploaded/running work, retry only unambiguous transient failures, and map ambiguous commit outcomes to `needs_attention` with a reconcile action.

- [ ] **Step 4: Remove broad catches in platform clients. Stats failures must raise and leave prior snapshots untouched.**

- [ ] **Step 5: Run publishing tests.**

Run: `.venv/bin/python -m pytest tests/unit/test_publishing_service.py tests/unit/test_platform_clients.py tests/unit/test_metadata.py -v`

- [ ] **Step 6: Commit the publishing checkpoint.**

```bash
git add api/publishing.py src/publishing tests/unit/test_publishing_service.py tests/unit/test_platform_clients.py tests/unit/test_metadata.py
git commit -m "feat: persist safe publishing attempts"
```

### Task 7: Authenticated, structured operational API

**Files:**
- Rewrite: `api/main.py`
- Create: `api/schemas.py`
- Create: `api/auth.py`
- Test: `tests/integration/test_api.py`
- Test: `tests/integration/test_api_actions.py`
- Test: `tests/integration/test_api_security.py`

**Interfaces:**
- Produces: `create_app(settings, store, dispatcher)` and module-level `app`.
- Produces: documented read and mutation routes from the design, one error envelope, request IDs, auth dependency, and strict response DTOs.

- [ ] **Step 1: Write API tests for auth fail-closed behavior, exact-one-of submission, strict IDs, pagination, aggregate detail, unknown API JSON 404, validation envelopes, redaction, CORS allowlist, and protected technical details.**

- [ ] **Step 2: Write action tests for duplicate submit, cancel, resume, retry-from-stage, rediscovery, manual candidate selection, bounded SRT upload, publish request, and stable idempotency responses.**

- [ ] **Step 3: Run API tests and observe failures against wildcard CORS, raw rows, HTTPException detail, and direct tasks.**

Run: `.venv/bin/python -m pytest tests/integration/test_api.py tests/integration/test_api_actions.py tests/integration/test_api_security.py -v`

- [ ] **Step 4: Implement application lifespan, request/error middleware, bearer auth, strict Pydantic models, operational routes, artifact routes with confinement, and SPA/API fallback separation.**

Only `/api/health` is public. Mutations commit store actions and call `dispatcher.wake()`; they never create work tasks. Uploads use generated names, a byte limit, SRT inspection, and transactional candidate/decision creation.

- [ ] **Step 5: Implement compatibility analytics routes against the new store without leaking persistence rows. Fix failed-release timestamps and true aggregate summary counts.**

- [ ] **Step 6: Run all API and backend tests.**

Run: `.venv/bin/python -m pytest tests/unit tests/integration/test_api.py tests/integration/test_api_actions.py tests/integration/test_api_security.py -v`

- [ ] **Step 7: Commit the API checkpoint.**

```bash
git add api/main.py api/schemas.py api/auth.py tests/integration/test_api.py tests/integration/test_api_actions.py tests/integration/test_api_security.py
git commit -m "feat: expose authenticated operations API"
```

### Task 8: Shared CLI behavior and backend remediation

**Files:**
- Rewrite: `main.py`
- Modify: `scripts/dev_frames.py`
- Modify: `config.yaml`
- Modify: `.env.example`
- Test: `tests/integration/test_cli.py`
- Modify: `tests/integration/test_pipeline.py`

**Interfaces:**
- Consumes: the shared pipeline/store/services.
- Produces: CLI exit codes and output paths matching API-generated runs.

- [ ] **Step 1: Write CLI tests for shared stage invocation, query uniqueness, strict IDs, missing ffmpeg/non-produced output nonzero exit, and render-only confinement. Correct the integration fixture conflict so full integration tests express one F-bomb per line.**

- [ ] **Step 2: Run the CLI/full integration tests and observe current divergence and false-zero exits.**

Run: `.venv/bin/python -m pytest tests/integration/test_cli.py tests/integration/test_pipeline.py -v`

- [ ] **Step 3: Replace duplicated CLI orchestration with a thin shared-service adapter and deliberate console error reporting.**

- [ ] **Step 4: Make paid audio providers opt-in, document every credential variable, and keep environment injection authoritative.**

- [ ] **Step 5: Run the complete Python suite.**

Run: `.venv/bin/python -m pytest tests -v --tb=short`

- [ ] **Step 6: Commit the parity checkpoint.**

```bash
git add main.py scripts/dev_frames.py config.yaml .env.example tests/integration/test_cli.py tests/integration/test_pipeline.py
git commit -m "refactor: share cli and operations pipeline"
```

### Task 9: Frontend test foundation, API client, polling, auth, and feedback

**Files:**
- Modify: `webui/package.json`
- Modify: `webui/package-lock.json`
- Modify: `webui/vite.config.js`
- Create: `webui/src/test/setup.js`
- Create: `webui/src/api.test.js`
- Rewrite: `webui/src/api.js`
- Create: `webui/src/hooks/usePollingResource.js`
- Create: `webui/src/hooks/usePollingResource.test.jsx`
- Create: `webui/src/context/AppContext.jsx`
- Create: `webui/src/components/shared/ToastRegion.jsx`
- Create: `webui/src/components/shared/ResourceState.jsx`

**Interfaces:**
- Produces: `ApiError`, authenticated `request()`, operational API methods, abort/timeout support, and idempotency headers.
- Produces: `usePollingResource(load, options)` with explicit loading/success/error/disconnected/stale state and non-overlapping recursive scheduling.
- Produces: app context for session token, health/connectivity, and toasts.

- [ ] **Step 1: Add Vitest, jsdom, React Testing Library, jest-dom, user-event, and axe-core development dependencies and test scripts.**

- [ ] **Step 2: Write failing API/polling tests for structured error parsing, HTML/non-JSON rejection, auth headers, abort, no overlap, stale response suppression, backoff, unmount cancellation, visibility behavior, and terminal stop.**

- [ ] **Step 3: Run frontend tests and observe missing scripts/modules.**

Run: `npm test -- --run`

- [ ] **Step 4: Implement the API client, hook, context, toast live region, and honest resource-state component.**

- [ ] **Step 5: Run frontend tests and production build.**

Run: `npm test -- --run && npm run build`

- [ ] **Step 6: Commit the frontend foundation.**

```bash
git add webui/package.json webui/package-lock.json webui/vite.config.js webui/src/api.js webui/src/api.test.js webui/src/hooks webui/src/context webui/src/components/shared
git commit -m "test: establish frontend operations data layer"
```

### Task 10: Responsive operations queue and application shell

**Files:**
- Rewrite: `webui/src/App.jsx`
- Rewrite: `webui/src/index.css`
- Rewrite: `webui/src/components/layout/Sidebar.jsx`
- Rewrite: `webui/src/components/layout/Header.jsx`
- Create: `webui/src/components/layout/SystemStatusBar.jsx`
- Create: `webui/src/components/dashboard/OperationsOverview.jsx`
- Rewrite: `webui/src/components/dashboard/StatsGrid.jsx`
- Rewrite: `webui/src/components/jobs/JobSubmit.jsx`
- Rewrite: `webui/src/components/jobs/JobList.jsx`
- Create: `webui/src/components/dashboard/OperationsOverview.test.jsx`
- Create: `webui/src/components/layout/AppShell.test.jsx`

**Interfaces:**
- Consumes: operations summary/list endpoints and app context.
- Produces: grouped queue, true counts, search/filter/page state, responsive navigation, auth unlock surface, system status, route boundary, and accessible creation form.

- [ ] **Step 1: Write failing tests for grouped active/attention/failed/queued/completed runs, truthful counts, loading/empty/error/stale/disconnected views, token lock/unlock, inline submit errors, duplicate-submit disabling, mobile navigation, skip link, and wildcard route.**

- [ ] **Step 2: Run focused frontend tests and confirm current dashboard does not meet the operational hierarchy.**

Run: `npm test -- --run src/components/dashboard/OperationsOverview.test.jsx src/components/layout/AppShell.test.jsx`

- [ ] **Step 3: Implement the graphite/mint operations shell and queue using semantic headings, lists/tables, `aria-live`, focus-visible styles, responsive breakpoints, and URL-owned filters.**

- [ ] **Step 4: Run focused tests, accessibility assertions, and build.**

Run: `npm test -- --run src/components/dashboard/OperationsOverview.test.jsx src/components/layout/AppShell.test.jsx && npm run build`

- [ ] **Step 5: Commit the queue checkpoint.**

```bash
git add webui/src/App.jsx webui/src/index.css webui/src/components/layout webui/src/components/dashboard webui/src/components/jobs/JobSubmit.jsx webui/src/components/jobs/JobList.jsx
git commit -m "feat: rebuild admin as operations queue"
```

### Task 11: Timeline-centric job workspace, subtitle controls, and publishing panel

**Files:**
- Rewrite: `webui/src/components/jobs/JobDetail.jsx`
- Rewrite: `webui/src/components/jobs/PipelineSteps.jsx`
- Create: `webui/src/components/jobs/StageTimeline.jsx`
- Create: `webui/src/components/jobs/StageAttemptList.jsx`
- Create: `webui/src/components/jobs/AttentionBanner.jsx`
- Create: `webui/src/components/jobs/DiagnosticsPanel.jsx`
- Create: `webui/src/components/subtitles/SubtitleCandidates.jsx`
- Create: `webui/src/components/publishing/PublishingPanel.jsx`
- Create: `webui/src/components/jobs/JobDetail.test.jsx`
- Create: `webui/src/components/subtitles/SubtitleCandidates.test.jsx`
- Create: `webui/src/components/publishing/PublishingPanel.test.jsx`

**Interfaces:**
- Consumes: aggregate job detail, incremental events, recovery/publish/subtitle action methods.
- Produces: persisted banners, live timeline, actual progress, attempt histories, safe diagnostics copy, candidate comparison/select/upload/rediscover/resume, and per-platform attempt controls.

- [ ] **Step 1: Write failing tests for stage expansion, child stages, timings, attempt/max/cycle, warnings, actual progress roles, next action, technical-detail opt-in, sanitized copy payload, and terminal polling stop.**

- [ ] **Step 2: Write failing subtitle tests for three rejection rows, quality reasons, 70 percent acceptance display, manual selection, upload, rediscovery, resume, idempotency, and mutation failure feedback.**

- [ ] **Step 3: Write failing publishing tests for per-platform attempts, retryability, ambiguous outcome warning, already-uploaded disabling, explicit publish action, and no automatic external request.**

- [ ] **Step 4: Run focused tests and observe missing operations components.**

Run: `npm test -- --run src/components/jobs/JobDetail.test.jsx src/components/subtitles/SubtitleCandidates.test.jsx src/components/publishing/PublishingPanel.test.jsx`

- [ ] **Step 5: Implement the workspace, timeline, controls, diagnostics, and panels with keyboard/semantic behavior and deliberate mutation errors.**

- [ ] **Step 6: Run all frontend tests and build.**

Run: `npm test -- --run && npm run build`

- [ ] **Step 7: Commit the job workspace checkpoint.**

```bash
git add webui/src/components/jobs webui/src/components/subtitles webui/src/components/publishing
git commit -m "feat: add live operator job workspace"
```

### Task 12: Preserve and repair previews, analytics, alerts, costs, and revenue

**Files:**
- Modify: `webui/src/components/video/SegmentPlayer.jsx`
- Modify: `webui/src/components/video/FrameBrowser.jsx`
- Modify: `webui/src/components/video/VideoPreview.jsx`
- Modify: `webui/src/components/leaderboard/Leaderboard.jsx`
- Modify: `webui/src/components/costs/CostDashboard.jsx`
- Modify: `webui/src/components/costs/CostBreakdown.jsx`
- Rewrite: `webui/src/components/revenue/RevenueDashboard.jsx`
- Rewrite: `webui/src/components/alerts/AlertList.jsx`
- Rewrite: `webui/src/components/alerts/AlertBanner.jsx`
- Create: `webui/src/components/video/SegmentPlayer.test.jsx`
- Create: `webui/src/components/secondary/OperationalStates.test.jsx`

**Interfaces:**
- Consumes: shared resource/polling states and compatibility endpoints.
- Produces: identity-safe previews and honest secondary screens.

- [ ] **Step 1: Write preview tests proving cache/state reset by run+segment, late-image suppression, backend FPS use, labelled controls, and explicit loading/error states.**

- [ ] **Step 2: Write secondary-screen tests for alert identity/timestamps, no swallowed fetches, real revenue data, valid interactive structure, responsive tables, and accessible headings/captions.**

- [ ] **Step 3: Run focused tests and observe current cache leakage and empty-state masking.**

Run: `npm test -- --run src/components/video/SegmentPlayer.test.jsx src/components/secondary/OperationalStates.test.jsx`

- [ ] **Step 4: Implement the focused repairs and integrate existing capabilities into the new shell.**

- [ ] **Step 5: Run all frontend tests and build.**

Run: `npm test -- --run && npm run build`

- [ ] **Step 6: Commit the secondary UI checkpoint.**

```bash
git add webui/src/components/video webui/src/components/leaderboard webui/src/components/costs webui/src/components/revenue webui/src/components/alerts webui/src/components/secondary
git commit -m "fix: make operational secondary views reliable"
```

### Task 13: Tooling, containers, documentation, and review ledger

**Files:**
- Modify: `.gitignore`
- Add to Git: `.dockerignore`
- Rewrite: `Dockerfile`
- Rewrite: `docker-compose.yml`
- Rewrite: `Makefile`
- Rewrite: `setup.sh`
- Modify: `pyproject.toml`
- Modify: `.github/workflows/ci.yml`
- Modify: `webui/package.json`
- Create: `.node-version`
- Rewrite: `README.md`
- Create: `docs/operations-control-panel.md`
- Create: `docs/codebase-review.md`

**Interfaces:**
- Produces: `make verify`, reproducible Python/UI installation, a non-root admin container, persisted runtime volumes, health check, operations/recovery guide, and finding-resolution ledger.

- [ ] **Step 1: Add a regression test/scan that proves `.dockerignore` is tracked and excludes `.env`, Git, venvs, outputs, caches, and node modules while including `uv.lock`.**

- [ ] **Step 2: Make `pyproject.toml` and `uv.lock` canonical, correct Make/setup commands and ports, declare Node 20+, and add frontend tests/build plus full Python tests to CI.**

- [ ] **Step 3: Build a multi-stage Node/Python image that copies the built UI, uses the lock, runs as non-root, serves `api.main:app` on 8001, and never copies credentials. Configure Compose volumes for `data`, `results`, and `output`.**

- [ ] **Step 4: Document setup, auth, states, retry rules, API/actions, subtitle decisions, structured polling, worker/lease limits, recovery, artifacts, publishing precautions, credential-dependent manual checks, and exact verification commands.**

- [ ] **Step 5: Populate the review ledger with severity, original file/line evidence, root cause, resolution status, changed file/tests, and rationale for every deferred low issue.**

- [ ] **Step 6: Run config/document checks.**

Run: `git diff --check && .venv/bin/ruff check src api tests && npm --prefix webui test -- --run && npm --prefix webui run build`

- [ ] **Step 7: Commit the delivery checkpoint.**

```bash
git add .gitignore .dockerignore Dockerfile docker-compose.yml Makefile setup.sh pyproject.toml .github/workflows/ci.yml webui/package.json webui/package-lock.json .node-version README.md docs
git commit -m "docs: ship and operate the control panel"
```

### Task 14: Scenario verification, independent review, and cleanup

**Files:**
- Create: `tests/integration/test_operational_scenarios.py`
- Modify only files implicated by scenario failures or review findings.

**Interfaces:**
- Verifies the complete design and acceptance criteria with fakes and temporary artifacts.

- [ ] **Step 1: Add scenario tests for generation success, transient discovery retry, metadata retry exhaustion, three subtitle rejections, manual selection/resume, duplicate resume, restart recovery, deterministic render failure, cancellation, publish transient exhaustion, ambiguous publish, and stats failure retention.**

- [ ] **Step 2: Run each scenario test before its final integration fix and apply root-cause/TDD corrections only.**

Run: `.venv/bin/python -m pytest tests/integration/test_operational_scenarios.py -v`

- [ ] **Step 3: Request independent code review against the design and this plan. Address every critical/important finding with a regression test before the fix.**

- [ ] **Step 4: Run fresh full verification.**

```bash
.venv/bin/python -m pytest tests -v --tb=short
.venv/bin/ruff check src api tests
npm --prefix webui test -- --run
npm --prefix webui run build
git diff --check
git status --short
```

Expected: all commands exit zero; status contains only intentional implementation files plus the preserved pre-existing `scripts/get_youtube_token.py` modification.

- [ ] **Step 5: Audit requirements line by line, remove dead code/imports/components, ensure no secret-like fixtures or absolute workspace paths appear in responses/snapshots, and rerun the commands from Step 4 after cleanup.**

- [ ] **Step 6: Commit final review fixes without staging `scripts/get_youtube_token.py`.**

```bash
git add --all -- . ':!scripts/get_youtube_token.py'
git commit -m "fix: close operations control panel review"
```
