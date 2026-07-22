# Subtitle Artifact and Heartbeat Fencing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prevent reclaimed subtitle workers from replacing stable candidate/cache bytes and prevent dispatcher recovery from beating a heartbeat's first renewal.

**Architecture:** Subtitle work downloads or copies into a unique per-execution staging file, normalizes and validates only that file, then serializes stable candidate/cache replacement with a per-target `flock`; the exact live lease/cancellation callback is revalidated inside the lock immediately before `os.replace`. The dispatcher heartbeat renews once immediately when scheduled, then sleeps between later renewals, so the initial interval is measured from a fresh lease rather than a late task start.

**Tech Stack:** Python 3.14, asyncio, SQLite leases, `fcntl.flock`, atomic `os.replace`, pytest/AnyIO, Ruff.

## Global Constraints

- Preserve `scripts/get_youtube_token.py` exactly and keep it unstaged.
- No schema migration, external provider call, or Task 6 work.
- Observe deterministic RED before each production change.
- Keep staging unique, same-filesystem, cleaned on every exit, and never selected as a durable artifact path.
- Stable candidate/cache replacement must be serialized across processes and guarded by an exact live-lease/cancellation check inside the lock immediately before replacement.

---

### Task 1: Fence subtitle candidate and IMDb cache bytes

**Files:**
- Modify: `api/subtitles.py`
- Modify: `src/data/opensubtitles.py`
- Modify: `tests/integration/test_generation_scenarios.py`
- Modify: `tests/unit/test_opensubtitles.py`

**Interfaces:**
- `_ExecutionContext.publication_allowed()` revalidates the same lease/cancellation capability at the stable-file replacement boundary.
- `SubtitleCache.store(..., publish_allowed=None)` stages uniquely, locks per IMDb target, and revalidates immediately before replacement.

- [x] Add deterministic RED races for reclaim during provider download, after normalization/before candidate promotion, and during IMDb cache promotion; publish distinct replacement-owner bytes and assert stale resumption cannot change stable candidate/cache bytes.
- [x] Add RED cache atomicity coverage proving a rejected publication guard leaves the previous bytes and no partial files.
- [x] Render provider/upload/cache inputs into unique candidate staging files; inspect and normalize staging only.
- [x] Serialize candidate replacement with a per-candidate cross-process lock and run the execution-context check inside the lock immediately before atomic replacement.
- [x] Give `SubtitleCache.store` unique staging, per-IMDb locking, guarded replacement, fsync/cleanup crash safety, and propagate cancellation without converting it to candidate rejection.
- [x] Run the new subtitle/cache races and existing subtitle suites to GREEN.

### Task 2: Renew the dispatcher lease before the first heartbeat sleep

**Files:**
- Modify: `api/dispatcher.py`
- Modify: `tests/unit/test_dispatcher.py`
- Modify: `tests/integration/test_generation_scenarios.py`

**Interfaces:**
- `JobDispatcher._heartbeat(job_id, owner)` attempts one renewal immediately and returns `False` on lease loss before entering periodic sleep.

- [x] Add deterministic RED where the runner reaches its claimed/running boundary before the heartbeat's delayed first interval; assert a renewal already occurred.
- [x] Strengthen the blocking generation test to snapshot a live claimed/running lease boundary and retain the assertion that expiry advances while provider work is blocked.
- [x] Implement immediate-first-renew with the existing `False` lease-loss path, then sleep between successful renewals.
- [x] Re-run the deterministic heartbeat test, repeated isolated stress, and generation module order to GREEN.

### Task 3: Verification, report, review, and commit

**Files:**
- Modify: `.superpowers/sdd/task-5-report.md`

- [x] Append event timestamps, causal transitions, RED/GREEN evidence, filesystem/heartbeat invariants, and reviewer outcome.
- [x] Run affected subtitle/cache/dispatcher/generation suites, the Task 3-5 focused suite, changed-file Ruff, and `git diff --check`.
- [x] Run the full suite and separate the unchanged analysis baseline.
- [x] Request independent review of all new code and resolve Critical/Important findings test-first.
- [x] Stage the exact allowlist and commit `fix: fence subtitle artifacts and lease heartbeat` while leaving the helper unstaged.
