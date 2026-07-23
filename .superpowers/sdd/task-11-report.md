# Task 11 Report — Live operator job workspace

## Scope and files

Task 11 started from `fbe0fb3848e47b20d7b79949e6e20a36cdeb148c`. It rewrote the aggregate job detail and pipeline presentation, added semantic stage/attempt, attention, and diagnostic components, added the subtitle candidate comparison/control surface and publishing release/attempt panel, and added the three planned focused test files. No Task 12 media or secondary-view component and no backend file was changed. After an observed workspace-wide duplicate-action regression, the controller authorized one narrow Task 9 shared-infrastructure deviation: make `usePollingResource.refresh()` return its request promise while preserving its state/error behavior, with a hook regression test.

The protected pre-existing `scripts/get_youtube_token.py` working-copy modification was not edited, restored, staged, or committed. Its SHA-256 remains `ac7e4a6a42a23ed678ddb051e7a14167fe2681e8791ec07cca454d1df371a32a`, matching the recorded Task 9 baseline.

## TDD evidence

### RED

Before production changes, the exact focused command

`npm test -- --run src/components/jobs/JobDetail.test.jsx src/components/subtitles/SubtitleCandidates.test.jsx src/components/publishing/PublishingPanel.test.jsx`

exited 1. The subtitle and publishing suites failed import resolution because their production components did not exist. All four initial job-detail tests failed against the legacy unauthenticated `setInterval`/legacy-status workspace because the semantic timeline, strict aggregate DTO, persisted banners, diagnostics, explicit token/action seams, incremental events, and terminal polling behavior were absent.

A later line-by-line audit added a publishing-attempt timestamp regression. `npm test -- --run src/components/publishing/PublishingPanel.test.jsx` exited 1 because persisted `started_at` and `finished_at` values were not rendered; the minimal rendering fix then made the focused suite green.

Independent review identified five Important gaps. RED regressions then demonstrated that completed stage duration/timestamp semantics were incomplete, mutation locks were panel-local and released before durable refresh, the operator-token redaction assertion was vacuous, ambiguous uploaded-without-ID releases could be mislabeled or hidden when reconciliation was not advertised, and the event lifecycle lacked explicit overlap/identity/unmount/strict-terminal coverage. The event lifecycle regression passed against the existing abortable recursive implementation, proving a coverage gap rather than a production defect; the other regressions failed before their minimal fixes. The polling-hook RED specifically proved `refresh()` returned `undefined`; its GREEN regression now verifies successful refresh data is awaitable while rejected loads still resolve safely after committing structured error state.

The first re-review found three residual Important composition gaps. New RED integration tests proved that an enabled manual refresh could supersede the mutation-owned refresh, an opaque operator token used as an arbitrary DTO object key escaped diagnostics redaction, and delegated subtitle/publishing failures appeared only globally rather than inline. The fixes guard both manual-refresh entry points with the synchronous workspace lock, sanitize exact token occurrences in diagnostic keys and values, and return a structured mutation result so each initiating panel renders the safe failure inline without introducing rejected click-handler promises.

### GREEN and refactor

The final focused suite has 18 tests across the three planned files. It covers ordinal stage order, parent/child nesting, semantic keyboard-operable expansion, bounded actual progress, completed timestamps and duration, attempts/cycles/triggers, warnings, next actions, gated retry and workspace-wide duplicate suppression through durable refresh (including guarded manual refresh), stopping banners, panel-local delegated mutation failures, opt-in sanitized diagnostic copy and non-vacuous opaque operator-token redaction in values and keys, incremental monotonic events without overlap or stale identity updates, unmount cancellation, strict terminal polling/manual refresh, all three rejected subtitle rows and threshold boundary, candidate/upload/rediscover/resume controls, explicit token/idempotency options, release and publishing-attempt truth, retryability/timings, uploaded and ambiguous states, reconciliation bodies and remote-ID validation, mutation failures, semantic table headers, and proof that rendering does not publish. The shared hook suite has 17 passing regressions.

## Operational and safety reasoning

- The route parameter is treated only as the canonical opaque run ID. Every aggregate read, incremental event read, and mutation receives the session token explicitly; tokens are not placed in URLs or copied diagnostics.
- Aggregate detail uses `usePollingResource` at two-second intervals. `completed`, `failed`, `cancelled`, and `needs_attention` stop automatic detail polling after their successful snapshot while manual refresh remains available. Cached detail remains visible through shared stale/disconnected/error resource states.
- Incremental events use one abortable recursive request at a time, maintain a monotonic cursor, deduplicate by durable integer ID, sort monotonically, abort on identity/terminal/unmount changes, and retain persisted events beneath safe warnings.
- Stage order and hierarchy come only from DTO ordinals and `parent_stage_id`. Progress comes only from persisted numerator/denominator/unit fields. Attempt histories come only from matching `stage_id`; no state-derived attempt or percentage is manufactured.
- Operator actions are rendered only for matching `available_actions` (except the separately specified explicit SRT upload surface, because the strict backend action list does not advertise an upload string). Each deliberate mutation creates one idempotency key outside the transport call, uses one shared synchronous workspace lock, disables every mutation control while in flight, surfaces safe inline failures, and retains the lock until the successful aggregate refresh settles.
- Diagnostic copy is constructed from a bounded allowlist of public aggregate DTO sections. Key names associated with tokens, authorization, credentials, cookies, headers, bodies, secrets, and paths are removed; exact operator-token occurrences and absolute-path-looking string values are redacted; recursion, arrays, objects, and strings are bounded. Technical JSON remains collapsed until requested and copy success/failure is announced.
- Publishing methods are reachable only from explicit buttons. Uploaded releases with valid remote IDs do not expose publish controls. `needs_attention` and uploaded-without-ID releases retain a persistent warning whether or not reconciliation is advertised; advertised uploaded reconciliation requires a nonblank, control-character-free remote ID using the API's supported character set, and the not-uploaded body has no remote ID. Rendering, mounting, detail polling, and event polling never invoke any publish method.

## Verification results

- Focused Task 11 command: exit 0; 3 files passed, 18 tests passed.
- Shared polling-hook command `npm test -- --run src/hooks/usePollingResource.test.jsx`: exit 0; 1 file passed, 17 tests passed.
- Full frontend command `npm test -- --run`: exit 0; 10 files passed, 109 tests passed.
- Production command `npm run build`: exit 0; Vite 6.4.1 transformed 66 modules and emitted the production bundle.
- `git diff --check`: exit 0.
- Scope/secret audit: production Task 11 files contain no fixture token or absolute workspace path. Test-only `session-token` and `/srv/private/output` sentinel values prove explicit transport and diagnostic exclusion. The protected script hash remains the recorded baseline and the script is excluded from Task 11 staging.

## Deviations and remaining concerns

- `upload_subtitle` is not produced by the backend `_available_actions()` implementation. The upload picker/button is therefore always an explicit deliberate operator control, as required by the Task 11 brief, while server validation remains authoritative. Selection, rediscovery, resume, retry, publish, reconciliation, and statistics controls remain strictly action-gated.
- `usePollingResource.refresh()` now returns the existing request promise so the workspace can keep its shared mutation lock through the authoritative refresh. This was an observed and controller-authorized Task 9 deviation; automatic polling, terminal stopping, supersession, abort, stale aging, and safe error semantics remain unchanged and regression-covered.
- Aggregate job costs are displayed read-only from the strict detail DTO. Task 12-owned cost, media, leaderboard, revenue, and alert components were not changed.
- Two non-blocking review Minors are owned by Task 12/shared frontend polish: delegated subtitle/publishing failures are announced both globally and inside the initiating panel, and the shared stale/error retry affordance can look enabled during a mutation even though its synchronous guard prevents a request. They are deferred because safe inline visibility and duplicate-request prevention are already correct; resolving them cleanly requires coordinated shared error-presentation and `ResourceState` affordance changes outside Task 11's narrow component scope.

## Independent review

The independent medium-effort reviewer initially reported five Important findings; after RED/GREEN remediation, the first re-review found three residual Important composition findings. After the second RED/GREEN remediation, final re-review found no Critical or Important findings and approved Task 11 as ready to commit. The reviewer independently confirmed 35/35 focused workspace plus polling-hook tests, 109/109 full frontend tests, the 66-module production build, and clean diff hygiene. The two non-blocking Minors above remain explicitly tracked.
