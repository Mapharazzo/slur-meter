# Task 5 Final Review Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** Close the final Task 5 lease, preview, local-audio provenance, and bounded-diagnostics review gaps without schema changes or external provider calls.

**Architecture:** The runner's existing progress capability supplies the exact lease owner and cancellation callback to `SubtitleService`; every service-owned durable mutation uses store-level lease fencing and treats `None` as cancellation. Graph frames and preview publish in one immutable graph directory selected by the existing current pointer. Audio input hashes include each enabled `FileProvider` source SHA-256, while encoder failures retain only strictly parsed ffmpeg progress facts.

**Tech Stack:** Python 3.14, SQLite, asyncio/thread workers, pathlib, ffmpeg progress protocol, pytest/AnyIO, Ruff.

## Global Constraints

- Preserve `scripts/get_youtube_token.py` exactly and keep it unstaged.
- No schema migration and no real provider, paid-audio, publishing, TMDB, or OMDb calls.
- Write and observe each focused regression fail before changing production behavior.
- Do not begin Task 6.
- Leave broad graph/composite config over-invalidation and bounded secondary-probe latency documented unless directly touched.

---

### Task 1: Lease-fence real subtitle discovery and selection

**Files:**
- Modify: `api/database.py`
- Modify: `api/pipeline.py`
- Modify: `api/subtitles.py`
- Modify: `tests/integration/test_generation_scenarios.py`
- Modify: `tests/unit/test_operation_store.py`

**Interfaces:**
- `SubtitleService.discover(job_id, *, lease_owner, cancel_requested)`
- `SubtitleService.select(job_id, manual_candidate_id=None, *, lease_owner, cancel_requested)`
- Candidate/decision/ensure-stage store mutations accept optional `lease_owner` and return `None` on lease rejection.

- [x] Add a RED real-service runner scenario where candidate one is rejected and candidate two is selected; assert separate fenced attempts, candidate transitions, event, and completed manifest.
- [x] Add a RED real-service scenario whose download expires/reclaims the lease; snapshot replacement-owned durable state and assert the stale service raises cancellation without any later candidate/event/attempt/stage mutation.
- [x] Add store RED coverage that stale owners cannot ensure a stage, record/update a candidate, or record a decision.
- [x] Pass the runner capability into real discovery/selection and thread one immutable execution context through every evaluation/resume/completion path.
- [x] Fence and require success for every attempt, candidate, event, decision, job, and stage mutation; check cancellation around provider/file/cache boundaries.
- [x] Run the new scenarios and subtitle/store suites to GREEN.

### Task 2: Publish and serve preview from the current graph bundle

**Files:**
- Modify: `src/video/plotter.py`
- Modify: `api/pipeline.py`
- Modify: `api/main.py`
- Modify: `tests/integration/test_generation_scenarios.py`
- Modify: `tests/integration/test_job_submission.py`
- Modify: `tests/unit/test_plotter.py`

**Interfaces:**
- New graph bundles contain `frames/` plus `preview.png`; manifest details name both paths.
- `/api/jobs/{identifier}/preview` resolves the store job and current graph manifest for opaque job IDs or IMDb aliases.

- [x] Add RED generation coverage proving preview is inside the pointer-selected graph bundle and no staging/legacy preview exists.
- [x] Add RED route coverage for both opaque job ID and IMDb alias, including no staging exposure.
- [x] Remove plotter's side-channel preview write; render frames only into its requested directory.
- [x] Verify the exact frame sequence, copy the final frame to graph staging as `preview.png`, and promote the entire validated graph directory under the existing lease guard.
- [x] Resolve preview through `_current_artifact` and manifest details, returning no-store bytes only from the committed bundle.
- [x] Run plotter, generation, and route tests to GREEN.

### Task 3: Bind audio reuse to every effective local file input

**Files:**
- Modify: `api/pipeline.py`
- Modify: `tests/integration/test_generation_scenarios.py`

**Interfaces:**
- `_input_hashes("audio", job_id)` includes a SHA-256 entry for each enabled effective `FileProvider` source location (intro, outro, background, verdict default, verdict rating).

- [x] Add a RED reuse regression that publishes an audio manifest, mutates a configured source at the same path, and expects validation false; deleting the source must also fail closed.
- [x] Implement a deterministic effective-source projection matching `AudioPipeline` provider defaults/overrides and hash each regular source file.
- [x] Make validation convert missing/invalid local source errors to `False`, while generation raises before provider work.
- [x] Run the audio provenance regression and generation suite to GREEN.

### Task 4: Retain bounded allowlisted structured ffmpeg diagnostics

**Files:**
- Modify: `src/video/encoder.py`
- Modify: `tests/unit/test_encoder.py`

**Interfaces:**
- `EncodingError.stderr_tail` remains compatibility-facing but contains only bounded JSON assembled from strict ffmpeg progress keys and value grammars; raw stderr is still drained and discarded.

- [x] Add a RED failure test with useful progress facts plus raw stderr/stdout secrets and oversized unknown fields; expect nonempty bounded JSON containing only allowlisted facts.
- [x] Parse only fixed numeric/time/progress fields from `pipe:1`, store at most one bounded value per fixed key, and serialize within `stderr_limit`.
- [x] Keep stderr drain-only and prove long-token/chunk-boundary tests remain GREEN.
- [x] Run encoder tests to GREEN.

### Task 5: Report, verification, independent review, and commit

**Files:**
- Modify: `.superpowers/sdd/task-5-report.md`

- [x] Append final reviewer findings, root causes, exact RED/GREEN evidence, invariants, final verification, and acknowledged Minors.
- [x] Run focused Task 3-5 route/store/media suites, Ruff on changed Python files, and `git diff --check`.
- [x] Run the full repository suite and separate the unchanged analysis baseline.
- [x] Request independent re-review and resolve every Critical/Important issue test-first.
- [x] Stage the exact allowlist, verify `scripts/get_youtube_token.py` is unstaged, and commit `fix: close generation lease and provenance gaps`.
