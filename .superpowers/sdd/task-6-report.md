# Task 6 Report — Publishing attempts, reconciliation, and metrics preservation

## Scope and baseline

- Binding base: `7331fa895c2abd4129b2f40b553b3b4957fe6db5`.
- Scope remained within Task 6. No Task 7 route/auth work.
- A narrowly scoped migration became genuinely unavoidable during independent safety
  review: migration v3 adds nullable owner/expiry fields to publishing attempts so a
  crashed uploader can be distinguished from live work. Upgraded unfinished v2
  attempts are conservatively converted to ambiguity. No other schema changed.
- No real platform, browser, analytics, or metadata provider calls were made.
- The protected user edit `scripts/get_youtube_token.py` was neither altered nor staged.

## Preserved checkpoint evidence

The resumed checkpoint already contained test-first service/store work. Its recorded
sequence was:

- Initial missing-module RED for `api.publishing` / publishing error interfaces.
- Fifteen service cases reached intended `NotImplemented` REDs.
- Two atomic store-helper regressions reached intended missing-helper REDs.
- The prior implementer then reached 19 GREEN cases covering bounded three-attempt
  cycles, credential determinism/redaction, duplicate coalescing, uploaded
  idempotency, confirmation and ambiguous reconciliation, immutable metadata,
  private-by-default YouTube publication, last-good stats preservation, aliases,
  artifact validation, and transaction rollback.

On takeover, the combined preserved group reproduced as 55 passing cases with eight
expected platform-client REDs. All eight failed because the legacy TikTok and
Instagram clients lacked the injected Playwright boundary (`TypeError` at client
construction), before any network or browser launch.

## Additional RED / GREEN cycles

### Browser platform boundary

- RED: 8 platform cases failed at the missing injected client constructor.
- GREEN: injected fake Playwright managers, typed credentials/confirmation/
  ambiguity/stats failures, and `finally` cleanup produced 13/13 passing platform
  cases.
- Added success-path coverage for confirmed remote IDs and complete fake metrics;
  the expanded platform group reached 19/19.

### Generic upstream failures

- RED: 7 new cases failed because generic non-timeout YouTube SDK and browser
  exceptions escaped as raw `RuntimeError` before submit, after submit, and during
  stats fetching (7 failed, 19 passed).
- GREEN: explicit boundary mapping made pre-submit failures transient, post-submit
  failures ambiguous, and stats failures typed and safe (26/26 passed).

### Idempotency and complete-metrics edge cases

- RED: replaying an already-uploaded publication with a removed local artifact
  raised validation; `NaN` reached SQLite; and missing YouTube like/comment counts
  silently became zero (4 intended failures among 9 selected cases).
- GREEN: uploaded state is checked before local artifact validation, finite-number
  validation rejects `NaN`/infinity, and YouTube requires the returned count fields
  (9/9 selected cases passed).

### Independent-review repair cycles

- First review: no Critical and six Important findings. Intended RED coverage then
  proved abandoned-claim recovery, fabricated metrics, generic client boundaries,
  cleanup failure masking, the Instagram New-post fallback, and concurrent metadata
  generation gaps (18 failed / 21 passed in the selected group). GREEN added
  store-serialized lazy metadata, supplemental verified dimensions, explicit client
  mappings, independent cleanup, and fallback restoration (39/39 selected).
- Second review found two duplicate-publication paths plus transport/confirmation
  gaps. Seven targeted REDs proved live-upload reconciliation could supersede work,
  confirmed cleanup could look retryable, Google transport errors were
  deterministic, and empty IDs were stuck. GREEN introduced durable attempt leases,
  heartbeat renewal, expired-only recovery, cleanup-safe confirmed returns, transport
  classification, and empty-ID ambiguity (14/14 selected).
- Third review found silent renewal exceptions, unfenced completion, real-client
  missing-ID behavior, and v2 lease-less attempts. Seven targeted REDs reproduced all
  paths. GREEN added exception-as-loss, a process-local live-call guard, matching
  owner/unexpired completion fencing, actual-client service tests, and conservative
  v2 migration recovery (7/7 selected).
- Fourth review found one owner-omission fence bypass. Its exact regression failed,
  then passed after leased attempts required a supplied matching owner.
- Fifth independent confirmation reran 114 focused cases plus Ruff/diff checks and
  reported no remaining Critical or Important findings: ready.

## Contract/self-review

- Publication request metadata is persisted once and reused from the release on all
  attempts/retries; YouTube defaults to private and accepts only private/unlisted/
  public overrides.
- Claims, attempt completion, release transitions, and events use immediate SQLite
  transactions; injected event failures prove rollback rather than partial state.
- Active and uploaded publications coalesce/idempotently return; live publishing
  calls are lease-protected and only expired claims can enter explicit recovery.
  Completion requires the matching unexpired owner.
- Empty IDs are ambiguity failures. Post-submit uncertainty becomes
  `needs_attention` and cannot upload again until explicit reconciliation.
- Persisted errors contain only stable codes/messages; adversarial bearer, cookie,
  query-secret, upstream body, and absolute-path text is excluded.
- Stats require a confirmed remote ID and a complete finite non-negative snapshot.
  Fetch, parse, confirmation, and atomic-store failures preserve prior last-good
  revenue rows.
- TikTok, Instagram, and YouTube now raise typed errors rather than returning empty
  IDs or empty/partial zero snapshots. Injected browser/context resources close in
  `finally`.

## Verification evidence

- `.venv/bin/python -m pytest tests/unit/test_publishing_service.py tests/unit/test_platform_clients.py tests/unit/test_metadata.py tests/unit/test_operation_store.py -q`
  - `81 passed in 1.74s`.
- `.venv/bin/python -m pytest tests/unit tests/integration/test_job_submission.py -q`
  - `257 passed, 22 warnings in 10.07s`.
- `.venv/bin/python -m pytest -q`
  - `290 passed, 1 failed, 26 warnings in 14.69s`.
  - Sole failure is the documented Task 8 baseline:
    `tests/integration/test_pipeline.py::TestAnalysisEngineIntegration::test_django_srt_pipeline`
    (`total_f_bombs`, expected 100 vs actual 200).

The pre-review verification above established the repository baseline. Final fresh
post-review verification, exact staging audit, and commit evidence are appended at
the commit gate.

## Final post-review gate

- Required focused command: `107 passed in 3.03s`.
- Required unit plus job-submission command: `284 passed, 22 warnings in 11.94s`.
- Required repository-wide command: `317 passed, 1 failed, 26 warnings in 17.92s`.
  The sole failure remained the documented Task 8 Django SRT fixture conflict
  (`total_f_bombs`, expected 100 vs actual 200); Task 6 did not modify it.
- Independent final review: no Critical or Important findings; ready.
