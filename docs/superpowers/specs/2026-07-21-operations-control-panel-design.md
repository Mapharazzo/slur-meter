# Operations Control Panel Design

Date: 2026-07-21

Status: approved for autonomous implementation by the product brief supplied with this work

## Purpose

Turn the existing admin dashboard into the source of operational truth for a video run from input resolution through publishing. An operator must be able to tell what happened, what is happening, why work stopped, what will happen next, and which safe recovery actions are available without reading server logs.

The implementation also corrects confirmed repository-wide correctness and security problems that directly affect this workflow. It preserves the existing analysis, previews, costs, leaderboard, revenue, alert, and platform-statistics capabilities while moving them behind the job workflow.

## Chosen approach

The system will use normalized SQLite snapshots plus append-only operational events, a database-backed worker with leases, and structured polling from React.

Two alternatives were rejected:

1. Full event sourcing would make every snapshot a projection and add replay/versioning complexity that this single-node application does not need.
2. Keeping the current mutable job row and adding JSON history blobs would avoid migrations, but it would not provide transactional state validation, attempt-level uniqueness, candidate comparison queries, or safe concurrent actions.

The selected hybrid keeps current state cheap to query and history durable and chronological. SQLite remains proportionate for the repository and deployment model.

## Domain model

### Job runs

Each submission creates an immutable run identity such as `job_3f21d8c1a62e4a90`. The source IMDb ID and normalized query are attributes, not primary keys. This prevents a rerun from deleting prior steps, costs, releases, or artifacts.

The legacy `jobs.imdb_id` rows will be migrated into `job_runs`: the old primary key becomes the run ID and also seeds `source_imdb_id`. Existing costs, releases, revenue, and step history will be copied into the new schema in one versioned transaction.

Canonical job states are:

- `queued`: durable work exists but no worker owns it.
- `running`: a worker owns a live lease and a stage is executing.
- `needs_attention`: automatic work stopped on a deterministic/configuration/quality decision that needs an operator recovery action.
- `failed`: a retryable external operation exhausted its automatic attempts or an unexpected infrastructure failure could not be recovered automatically.
- `cancelled`: an operator cancellation was applied. Running work becomes cancellation-pending until the current non-interruptible library call yields.
- `completed`: all requested stages, including any currently requested publishing operation, completed.

Allowed transitions are validated centrally. A completed generation run may move back to `queued` when an operator requests a publishing operation. Failed, attention, or cancelled runs may move to `queued` only through an explicit retry/resume decision. Illegal transitions return a conflict and are recorded as rejected admin decisions.

Every run stores start, finish, update, lease, cancellation, current-stage, next-action, safe error, and artifact-summary fields. The legacy integer percentage remains migration-only and is not displayed as authoritative progress.

### Pipeline stages and substages

Initial generation stages are ordered as follows:

1. `input_resolution`
2. `subtitle_discovery`
3. `metadata`
4. `subtitle_selection`
5. `analysis`
6. `graph`
7. `composite`
8. `audio`
9. `encode`

`composite` has persisted children for `intro_hold`, `intro_transition`, `graph`, and `verdict`. Publishing creates a `publishing` parent and a child named `publish.youtube`, `publish.tiktok`, or `publish.instagram` only when requested. This prevents platforms that were never requested from appearing incomplete.

Stage states use `pending`, `queued`, `running`, `needs_attention`, `failed`, `cancelled`, `completed`, and `skipped`. Stages store an ordinal, parent, current retry cycle, attempt policy, actual progress numerator/denominator/unit, timings, warnings, output manifest, safe error, retryability, and next action.

Progress exists only when a producer emits measurable work:

- graph frames: completed frames / planned frames;
- composite child: written frames / planned frames;
- encode: ffmpeg-reported frames / validated input frames;
- candidate selection: attempted candidates / automatic candidate limit;
- publishing: attempt number / maximum attempts.

Stages that do not expose measurable work show an indeterminate running state, not an estimated percentage.

### Attempts

Every execution of a stage creates a `pipeline_attempts` row with retry cycle, attempt number, maximum automatic attempts, trigger (`automatic`, `manual_retry`, `resume`, or `restart_recovery`), timings, outcome, retryability, candidate reference where relevant, sanitized diagnostics, and output.

A manual retry creates a new retry cycle so the UI can truthfully show that automatic attempt 3/3 exhausted before a later operator-triggered attempt 1/3.

### Subtitle candidates

Every discovery result becomes a durable candidate. A candidate stores:

- provider and provider/file identifier;
- provider filename as metadata only;
- source type (`provider`, `cache`, or `upload`);
- language, FPS, title, year, IMDb match, provider rating/download count when supplied;
- discovery cycle, deterministic rank and rank reasons;
- detected encoding, cue count, first cue, final cue, parsed duration, expected runtime, and coverage percentage;
- download or parse error, quality/rejection reasons, status, content hash, and internal generated artifact path;
- selected timestamp and `automatic` or `manual` selection method.

Remote filenames and uploaded filenames are never used as filesystem paths. Files are stored under a generated per-run/per-candidate path after size and content validation.

Candidate ranking is stable and testable. It prioritizes exact IMDb match, requested language, normalized title match, year match, provider rating/download count, then filename and provider ID as deterministic tie breakers. The score and each contributing reason are exposed.

Candidate quality uses the final parsed subtitle cue end, not the timestamp of the final profanity match. A valid candidate is automatically accepted at coverage greater than or equal to the configured threshold, which remains 70 percent. Coverage above 120 percent is retained with a warning rather than silently rejected because extended editions can legitimately exceed catalog runtime.

One selection retry cycle automatically evaluates no more than three ranked candidates. Every candidate download, parse, rejection, and acceptance is an attempt. After three unsuccessful candidates, or after all available candidates when fewer than three exist, the run enters `needs_attention` with actions to select a candidate, rediscover, upload an SRT, or cancel.

A manual selection may override the coverage threshold after the file parses successfully. The override and warning are recorded as an admin decision and event. Selecting the same candidate or resuming twice is idempotent.

### Events and admin decisions

`pipeline_events` is append-only and ordered by integer ID. Each event has run, stage, attempt, severity, type, operator-safe message, sanitized structured data, and UTC timestamp. Events include state changes, progress, retry scheduling, candidate outcomes, artifact reuse, restart recovery, cancellation, and operator decisions.

`admin_decisions` records requested action, target stage/candidate/platform, idempotency key, accepted/rejected result, reason, and timestamp. Mutation endpoints accept an `Idempotency-Key`; the store also enforces state-based uniqueness so missing/replayed keys cannot launch duplicate concurrent work.

### Publishing

The existing release row becomes a per-platform summary. `publishing_attempts` retains every attempt, timing, retry cycle, safe error, retryability, remote ID, metadata snapshot, and outcome.

Publishing metadata is generated once per run and reused across retries. Already-uploaded platforms are idempotent and are never posted again automatically. An empty remote ID is an unknown/attention state, not success. Ambiguous timeouts after a browser submission enter `needs_attention` with a reconciliation explanation; they are not blindly retried because doing so can duplicate a public post.

Stats refresh failures retain the last verified snapshot and create a visible attempt/event. They never replace valid metrics with synthetic zeroes.

## Retry policy

Automatic attempt limits are policy, not a universal loop:

- OpenSubtitles discovery, configured metadata network calls, and unambiguously transient publishing failures: three attempts.
- Subtitle candidate evaluation: at most three distinct candidates per retry cycle.
- Rendering, validation, analysis, audio configuration, encoding configuration, and code failures: one automatic attempt.

Transient classification includes connect/read timeouts, connection errors, HTTP 408/425/429, and HTTP 5xx. Bounded delays default to 1, 3, and 8 seconds and respect a bounded `Retry-After` when available. Tests inject zero delay.

Missing credentials, invalid configuration, invalid paths/content, parse failures, missing executables, failed artifact validation, and deterministic renderer/encoder failures enter `needs_attention` immediately with a recovery explanation. An operator may retry the failed stage after fixing the cause; completed upstream stages and validated artifacts are reused.

Unexpected exceptions are sanitized, recorded with their exception type and correlation ID, and enter `needs_attention` rather than being silently swallowed. Exhausted transient operations enter `failed` with the stopping reason and a manual retry action.

## Persistence and migrations

Migrations are ordered and recorded in `schema_migrations`. Migration application uses an immediate transaction, explicit schema inspection, and named versions; broad exception suppression is removed.

The operational schema contains:

- `job_runs`
- `pipeline_stages`
- `subtitle_candidates`
- `pipeline_attempts`
- `pipeline_events`
- `admin_decisions`
- `publishing_attempts`
- migrated `costs`, `releases`, and `revenue`

Foreign keys, uniqueness constraints, and indexes enforce run ownership and action/attempt identity. SQLite connections use WAL, foreign keys, a busy timeout, and explicit rollback. State-changing actions use `BEGIN IMMEDIATE` so concurrent clicks or workers cannot both claim the same operation.

The migration preserves existing data, maps `done` to `completed`, maps interrupted active legacy work to `queued`, copies legacy steps into stage/attempt/event history, and records a recovery event. If legacy child rows reference a missing parent, the migration creates an explicitly labelled recovered legacy run before copying those rows; it does not silently delete history. It finishes with `PRAGMA foreign_key_check` and aborts atomically on any remaining violation. It does not blanket-mark work failed at startup.

## Durable execution and restart recovery

Raw per-request `asyncio.create_task` calls are replaced with a controlled `JobDispatcher`. API mutations only commit queued work and wake the dispatcher.

The dispatcher atomically claims one run, assigns an owner token and expiring lease, and renews the lease at stage/progress boundaries. This deliberately bounds render concurrency to one by default because the current image pipeline is memory intensive. The limit is configurable only upward.

On startup and periodically, expired leases are recovered. The interrupted attempt is closed with a restart-recovery diagnostic, the current stage is requeued in a new cycle, and the run resumes from the first incomplete stage. Graceful shutdown requests cancellation of the dispatcher and releases owned queued work. This design remains an in-process worker, but durable queue state and leases make restarts recoverable without introducing Redis/Celery.

The deployment contract is a single SQLite writer service. Multiple API processes may serve reads and enqueue actions, but only one dispatcher should be enabled. The health endpoint reports dispatcher ownership/readiness.

## Artifact safety and reuse

Every job path uses a generated strict run ID and a path-confinement check. IMDb IDs are canonical `tt` identifiers; query identities are data, not paths. OpenSubtitles downloads use timeouts, response-size bounds, basename-independent generated names, and safe ZIP/RAR member reads that reject traversal and archives without an SRT.

Expensive artifacts are produced under unique partial names/directories. A stage validates its output before atomically promoting it and writing an output manifest. Manifests include relevant input/config hashes, expected frame counts, dimensions/timing, and file size. Reuse is allowed only when the manifest and files validate. A failed encode never overwrites the last validated MP4.

The compositor is changed to stream segment frames to disk instead of retaining the complete raw video in memory. Public short-duration segment helpers remain for focused tests and previews.

ffmpeg encoding uses `-progress pipe:1`; reported frames drive persisted progress. A bounded sanitized stderr tail is attached to failures. Commands remain argument arrays with `shell=False`.

The CLI becomes a thin caller of the same pipeline service, eliminating behavioral drift. It returns a nonzero exit code when a requested stage does not produce a validated result.

## Error and security boundary

All API failures use:

```json
{
  "error": {
    "code": "subtitle_candidates_exhausted",
    "message": "Three subtitle candidates were rejected.",
    "retryable": false,
    "details": {"actions": ["select_subtitle", "rediscover_subtitles", "upload_subtitle"]},
    "request_id": "req_..."
  }
}
```

Validation, not-found, conflict, authorization, and internal exception handlers use the same envelope. Unknown `/api/*` routes return JSON 404 rather than SPA HTML.

Sanitization removes bearer values, cookies, configured secret values, credential-like query parameters, and absolute workspace paths before diagnostics are stored or returned. Raw upstream bodies and request headers are never persisted. `.env` loading no longer overrides deployment-provided variables.

All admin API routes except the minimal health probe require an operator bearer token. If `ADMIN_API_TOKEN` is not configured, the API fails closed unless an explicit local-development override is enabled. Comparisons are constant-time. The UI stores the entered token in session storage, never renders or copies it, and omits it from diagnostics. Default CORS origins are the local UI/API origins; wildcard credentialed CORS is removed. Publishing remains an explicit privileged operator action, and YouTube defaults to private visibility unless configuration deliberately chooses another value.

The versioned `.dockerignore` excludes `.env`, Git metadata, caches, runtime artifacts, and dependency directories so secrets cannot enter the image context.

## API contract

Core read routes:

- `GET /api/health`
- `GET /api/operations/summary`
- `GET /api/jobs?state=&limit=&offset=&query=` returns `{items,total,limit,offset}`
- `GET /api/jobs/{job_id}` returns run, stages, attempts, candidates, events, decisions, costs, releases, publishing attempts, available actions, and server timestamp
- `GET /api/jobs/{job_id}/events?after=` supports incremental polling
- existing video, frame, cost, leaderboard, revenue, release, and platform-stat routes remain with strict run-ID validation

Core mutation routes:

- `POST /api/jobs`
- `POST /api/jobs/{job_id}/actions/cancel`
- `POST /api/jobs/{job_id}/actions/resume`
- `POST /api/jobs/{job_id}/stages/{stage}/retry`
- `POST /api/jobs/{job_id}/subtitles/rediscover`
- `POST /api/jobs/{job_id}/subtitle-candidates/{candidate_id}/select`
- `POST /api/jobs/{job_id}/subtitles/upload`
- `POST /api/jobs/{job_id}/publish/{platform}`
- `POST /api/jobs/{job_id}/publish/{platform}/retry`
- `POST /api/jobs/{job_id}/stats/refresh`

Responses return the updated aggregate or a durable operation/decision identifier. They never require a client to guess completion after a fixed delay.

## Live update choice

Structured polling is selected over SSE and WebSockets. Each job snapshot includes `server_time`, `updated_at`, `last_event_id`, and terminal/active state. The client polls active runs every two seconds and incremental events after the known ID. Queue summaries poll every five seconds. Terminal detail pages stop automatic polling after one final refresh.

Polling uses recursive `setTimeout` only after the prior request settles, `AbortController`, request generations, visibility-aware intervals, bounded backoff, and stale thresholds. This is reliable across the existing Vite proxy and simple deployments, survives page refresh naturally, and avoids maintaining long-lived connection infrastructure for a small operator population.

## Admin UI

The visual system remains lightweight React, Tailwind, and CSS. The design is a high-contrast graphite control surface with mint for healthy/active state, amber for attention, red for failures, blue for queued/informational state, and neutral gray for completed historical data. Color is always paired with text/iconography.

The primary route is an operations queue grouped by active, needs-attention, failed, queued, and recently completed runs. It includes truthful global counts, search/filter/pagination, creation, last-update state, and direct action links.

The job workspace is dominated by a semantic pipeline timeline. Each stage is keyboard-expandable and shows state, actual progress, attempt/cycle, timestamps/duration, warnings, safe diagnostics, manifest outputs, next action, and relevant recovery controls. Composite children and per-platform publishing children appear nested.

Persistent attention/failure banners explain the stopping condition and contain direct actions. Technical detail is opt-in and copy diagnostics produces sanitized JSON only.

Subtitle candidates appear in a responsive comparison table with ranking, match data, parsed duration, expected runtime, coverage, status, quality reasons, and selection method. Operators can select, rediscover, or upload an SRT. Mutations disable while in flight and use idempotency keys.

The workspace also contains live preview frames when present, final video/segment previews, publishing status and attempts, analysis summary, costs, and platform metrics. Leaderboard, aggregate cost, revenue, and alert views remain secondary routes.

The shell includes a responsive navigation drawer, skip link, system connectivity/staleness bar, toast/live region, route error boundary, and not-found state. Loading, genuinely empty, API error, disconnected, stale, and mutation-error states are distinct. Focus-visible styles, accessible labels, semantic tables, progress roles, and no nested interactive controls are required.

## Testing strategy

Backend tests follow red-green-refactor cycles for:

- state transition validation;
- legacy migration preservation and restart recovery;
- atomic claims, duplicate submission/action idempotency, and concurrent requests;
- durable events, attempts, timings, warnings, and sanitized errors;
- transient retry success/exhaustion and deterministic no-retry behavior;
- cue-based coverage, ranking, no-more-than-three candidate attempts, exhaustion, manual override, upload, and resume;
- safe filenames/archive extraction, response bounds, encodings, and path confinement;
- artifact validation/reuse and interrupted encode preservation;
- publishing attempts, ambiguous outcomes, metadata reuse, and stats preservation;
- API error envelopes, auth, unknown API routes, and action availability;
- CLI/API service parity and nonzero failure exits.

Frontend tests use Vitest, React Testing Library, jest-dom, and targeted accessibility assertions. They cover the queue groups, honest loading/empty/error/stale states, non-overlapping polling, terminal stop, timeline attempts, subtitle selection/resume, duplicate-action disabling, diagnostic copying, persistent banners, publishing status, responsive navigation semantics, and media identity reset.

Scenario tests exercise success, transient retry, retry exhaustion, subtitle exhaustion, manual selection, restart recovery, deterministic render failure, cancellation, and publishing failure with injected fakes. No test calls a real social or paid provider.

Full verification runs Python tests, frontend tests, Ruff, frontend lint, production build, and a secret/path scan. Docker configuration is validated without exercising credentials or publishing.

## Documentation and deployment

`README.md` will distinguish CLI and admin workflows. An operations guide will document states, retry policies, API/actions, authentication, setup, recovery, database/artifact locations, worker limitations, and safe credential-dependent manual checks.

Docker becomes a multi-stage UI/Python image that serves the API/UI on port 8001 as a non-root user. Compose persists `data`, `results`, and `output`, includes a health check, and keeps credentials runtime-only. Paid audio providers become opt-in defaults; the environment template lists every supported credential without values.

## Confirmed review scope

The implementation will resolve all confirmed critical/high findings and reasonable medium findings tied to correctness, security, observability, developer tooling, and accessibility. Low findings are fixed when touched safely. The final review document will cite original evidence and mark each item fixed, mitigated, or deferred with rationale.
