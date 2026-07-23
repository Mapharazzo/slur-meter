# Operations Control Panel — Operator Guide

This is the operational reference for running, monitoring, and recovering the
Daily Slur Meter generation and publishing pipeline through its FastAPI backend
and React UI.

- **Backend:** `api.main:app` (FastAPI), served on port **8001**.
- **UI:** built into `webui/dist` and served by the same app at `/`.
- **Store:** versioned SQLite operational database at `data/slur_meter.db`.
- **CLI:** `main.py` is a thin synchronous adapter over the same durable pipeline.

---

## 1. Setup

```bash
./setup.sh            # venv + locked Python deps + build UI (or: make install)
cp .env.example .env  # then fill in credentials + ADMIN_API_TOKEN
make server           # serve API + UI on :8001
```

Docker:

```bash
docker compose up --build     # non-root container on :8001, health at /api/health
```

Runtime state persists in the `data`, `results`, and `output` volumes. Credentials
are injected at runtime via `.env` and are **never** baked into the image.

---

## 2. Authentication & CORS

Every `/api` route (except `GET /api/health`) requires a bearer token:

```
Authorization: Bearer <ADMIN_API_TOKEN>
```

- `ADMIN_API_TOKEN` — the exact token, compared in constant time. If unset, the
  API is **closed** unless `ALLOW_LOCAL_DEVELOPMENT_AUTH=true` (local dev only).
- `ALLOWED_ORIGINS` — comma-separated CORS allow-list for browser origins
  (default `http://localhost:5173,http://localhost:8001`). Preflight and
  credentialed requests are honored only for allow-listed origins.
- Auth fails **closed**: a missing/wrong token yields `401` with a structured
  error envelope; it never silently downgrades.

The UI stores the token in session memory only (never persisted to disk).

---

## 3. Job & stage states

**Job states** (`JobState`): `queued → running → completed`, with
`needs_attention`, `failed`, and `cancelled` as off-ramps.

**Stage states** (`StageState`): `pending`, `queued`, `running`,
`needs_attention`, `failed`, `cancelled`, `completed`, `skipped`.

**Generation stages**, in order:

```
input_resolution → subtitle_discovery → metadata → subtitle_selection
  → analysis → graph → composite → audio → encode
```

**Attempt triggers** (`AttemptTrigger`): `automatic`, `manual_retry`, `resume`,
`restart_recovery`, `artifact_invalidation`.

**Failure categories** (`FailureCategory`) drive retry policy: `transient`,
`validation`, `configuration`, `deterministic`, `ambiguous_publish`, `unexpected`.

---

## 4. Retry rules

- **Transient** failures are retried automatically with backoff from
  `RETRY_DELAYS` (default `1,3,8` seconds → three attempts). On exhaustion the
  stage moves to `failed`/`needs_attention` for operator action.
- **Deterministic / validation / configuration** failures are **not** retried
  blindly — they surface as `needs_attention` because a retry would just repeat.
- **Ambiguous publish** outcomes (see §8) are never blind-retried; they require
  operator reconciliation.
- Manual `retry`/`resume` actions re-run from the failed stage, reusing any
  still-valid durable artifacts rather than restarting from scratch.

---

## 5. API & operator actions

Reads:

| Endpoint | Purpose |
| --- | --- |
| `GET /api/health` | Liveness (public). |
| `GET /api/operations/summary` | Queue totals / dashboard summary. |
| `GET /api/jobs` | Paginated job list. |
| `GET /api/jobs/{id}` | Full job detail (run, stages, candidates, releases). |
| `GET /api/jobs/{id}/events` | Incremental event timeline (polling). |
| `GET /api/jobs/{id}/costs`, `GET /api/costs` | Per-job / aggregate costs. |
| `GET /api/releases`, `GET /api/releases/{id}` | Publishing releases. |
| `GET /api/jobs/{id}/platform-stats` | Native platform metrics. |
| `GET /api/revenue`, `GET /api/alerts`, `GET /api/leaderboard` | Secondary views. |
| `GET /api/analysis/{id}` | Analysis result. |
| `GET /api/videos/{id}`, `/preview`, `/segments/{seg}`, `/frames/{seg}/{n}` | Media/preview. |

Actions (all `POST`, all idempotency-aware):

| Endpoint | Action |
| --- | --- |
| `POST /api/jobs` | Submit a new generation job. |
| `POST /api/jobs/{id}/actions/cancel` | Cancel a running/queued job. |
| `POST /api/jobs/{id}/actions/resume` | Resume a failed/cancelled/attention job. |
| `POST /api/jobs/{id}/stages/{stage}/retry` | Retry a specific failed stage. |
| `POST /api/jobs/{id}/subtitles/rediscover` | Re-run subtitle discovery. |
| `POST /api/jobs/{id}/subtitle-candidates/{cid}/select` | Manually select a candidate. |
| `POST /api/jobs/{id}/subtitles/upload` | Upload an operator-provided subtitle. |
| `POST /api/jobs/{id}/publish/{platform}` | Publish to a platform. |
| `POST /api/jobs/{id}/publish/{platform}/retry` | Retry a failed publish. |
| `POST /api/jobs/{id}/stats/refresh` | Refresh native platform stats. |

Available actions for a job are also computed server-side and returned in its
detail response, so the UI only ever offers valid transitions.

---

## 6. Subtitle decisions

- Discovery fetches up to `SUBTITLE_CANDIDATES_PER_CYCLE` (default 3) candidates
  per cycle and ranks them.
- A candidate is auto-accepted only if its coverage meets
  `SUBTITLE_COVERAGE_THRESHOLD` (default `0.70`).
- If no candidate qualifies, the job moves to `needs_attention` and an operator
  can **rediscover**, **select** a specific candidate, or **upload** a subtitle
  file directly. Uploads are confined and crash-recoverable.
- Manual selection invalidates any downstream artifacts derived from the previous
  (rejected) subtitle and resumes from `subtitle_selection`.

---

## 7. Structured polling

- The UI polls `GET /api/jobs/{id}/events` for an **incremental** event stream
  (cursor/positional), plus authoritative detail refreshes after any mutation.
- Polling is **abortable and race-safe**: superseded responses are dropped, and a
  mutation triggers an immediate authoritative refresh rather than waiting for the
  next tick.

---

## 8. Worker, lease & recovery model

- The dispatcher runs with **concurrency 1** by default, polling every **1s**.
- Each claimed job holds a **lease** (default **30s**) renewed by a heartbeat at
  roughly one-third of the lease interval (~10s); the first renewal is immediate.
- On startup the lifespan runs **recovery**: expired leases are reclaimed and
  interrupted uploads are recovered. Jobs interrupted mid-stage resume via
  `restart_recovery` from their last durable checkpoint — no work is silently lost
  and no partial artifact is trusted.
- Graceful shutdown drains in-flight work within `shutdown_timeout` (default 30s).

**Artifacts** are written atomically and lease-fenced: a stale owner cannot
overwrite a newer attempt's output, and consumers are version-aware so they only
read artifacts that match the current run.

---

## 9. Publishing precautions

- Publishing attempts are **owner/expiry-fenced** and persisted before the remote
  call, so a crash mid-upload is distinguishable from a clean failure.
- **Ambiguous** outcomes (crash after the remote may have accepted the upload) are
  marked `needs_attention` for **reconciliation** — never blind-retried, to avoid
  duplicate uploads.
- Native metrics are validated and stored; the **last good** statistics are
  preserved if a later stats refresh fails.
- Idempotency: publish/retry actions reconcile by platform, outcome, and remote
  identity so a repeated request cannot create a second live upload.

---

## 10. Credential-dependent manual checks

Some paths require real third-party credentials and cannot run in CI. Verify
these manually in a configured environment:

- **OpenSubtitles** discovery/download (`OPENSUBTITLES_*`).
- **Movie metadata** enrichment (`TMDB_READ_TOKEN`, `OMDB_API_KEY`).
- **YouTube** publishing (`YOUTUBE_*`; generate the refresh token with
  `scripts/get_youtube_token.py`).
- **TikTok / Instagram** publishing (`TIKTOK_SESSION_ID`, `INSTAGRAM_SESSION_ID`).
- **Paid audio providers** (`ELEVENLABS_API_KEY`, `OPENROUTER_API_KEY`), only when
  explicitly enabled in `config.yaml`.

---

## 11. Verification commands

```bash
# Full local gate
make verify

# Or individually:
.venv/bin/ruff check src api tests
.venv/bin/python -m pytest tests -v --tb=short
npm --prefix webui test -- --run
npm --prefix webui run build
git diff --check
```

Scenario coverage lives in `tests/integration/test_operational_scenarios.py`
(generation success, retries, subtitle rejection/selection, restart recovery,
render failure, cancellation, publish exhaustion/ambiguity, stats retention).
