# Codebase Review Ledger

Resolution record for review findings raised during the Operations Control Panel
build (plan: `docs/superpowers/plans/2026-07-21-operations-control-panel.md`).
Every Critical/Important finding was closed with a regression test before its fix
during the owning task; this ledger tracks the **deferred low-severity** items and
the intentional scope deviations, with evidence and rationale.

## Legend

- **Severity:** Low unless noted.
- **Status:** `resolved` (fixed + regression), `accepted` (won't-fix, rationale
  given), or `deferred` (tracked for a later task, now closed there).

---

## Deferred / accepted findings

### L1 — Third-party `pysrt` deprecation warnings
- **Severity:** Low
- **Evidence:** `.venv/.../pysrt/srtfile.py:293` (`codecs.open() is deprecated`);
  ~17 warnings surfaced during Task 3 subtitle parsing tests.
- **Root cause:** Upstream `pysrt` uses `codecs.open()`; not our code.
- **Status:** accepted (third-party). No local fix without vendoring/patching a
  dependency. Warnings are benign and isolated to subtitle parsing.
- **Rationale:** Out of scope to modify a pinned third-party library; behavior is
  correct.

### L2 — FastAPI `on_event` startup deprecations
- **Severity:** Low
- **Evidence:** Four `on_event` deprecation warnings noted after Task 4.
- **Root cause:** Legacy `@app.on_event("startup")` handlers.
- **Status:** resolved in Task 7 — replaced with a single lifespan
  (`asynccontextmanager`) that performs startup/recovery wiring (`api/main.py`).
- **Tests:** lifespan startup/recovery covered by the Task 7 API suite.

### L3 — Graph preview relies on ordered `paths[-1]`
- **Severity:** Low
- **Evidence:** Task 5 graph preview consumer selecting the last element of an
  ordered `paths` manifest list.
- **Root cause:** Preview picks the newest frame by list order rather than an
  explicit key.
- **Status:** accepted. Ordering is produced deterministically by the render step
  and validated by the preview tests; an explicit key is a nicety, not a
  correctness gap.
- **Rationale:** No observed failure mode; change would cross artifact-manifest
  boundaries for no behavioral gain.

### L4 — Graph/composite hash the broad video config
- **Severity:** Low
- **Evidence:** Task 5 graph and composite stages compute their invalidation hash
  over the whole video config block.
- **Root cause:** Coarse-grained hashing key.
- **Status:** accepted. Over-broad hashing can only cause a conservative
  (unnecessary) re-render, never a stale artifact — the safe direction.
- **Rationale:** Correctness-preserving; narrowing the hash risks under-
  invalidation, which is worse than an occasional extra render.

### L5 — Secondary media probe observes cancellation at bounded timeout
- **Severity:** Low
- **Evidence:** Task 5 secondary media probe checks cancellation only at its
  bounded timeout boundary.
- **Root cause:** Cancellation is polled at the probe's timeout granularity.
- **Status:** accepted. The probe is already time-bounded, so worst-case
  cancellation latency equals that bound.
- **Rationale:** Bounded and safe; finer-grained cancellation adds complexity
  without a real responsiveness problem.

### L6 — Duplicate delegated-error announcements (global + inline)
- **Severity:** Low
- **Evidence:** Task 11 workspace could announce a delegated error both globally
  (toast) and inline.
- **Root cause:** Two announcement paths for the same error.
- **Status:** resolved in Task 12 — de-duplicated in the shared error/retry
  presentation (`webui/src/components/shared/ResourceState.jsx`).
- **Tests:** `webui/src/components/shared/ResourceState.test.jsx`.

### L7 — Inert stale retry affordance can look enabled during mutation
- **Severity:** Low
- **Evidence:** Task 11 — a retry affordance for stale data could appear enabled
  while a mutation was in flight.
- **Root cause:** Retry control not disabled during the mutating window.
- **Status:** resolved in Task 12 — the retry affordance is correctly disabled/inert
  during mutation in the shared presentation layer.
- **Tests:** `webui/src/components/shared/ResourceState.test.jsx`.

---

## Intentional scope deviations (authorized)

### D1 — Migration v3 (Task 6)
- **What:** Introduced schema migration v3 (beyond the originally scoped v2).
- **Why:** Required to distinguish a live publish upload from an abandoned one and
  to conservatively recover true active v2 attempts — impossible to do safely
  without the added schema signal.
- **Status:** implemented with migration tests; schema-neutral in intent.

### D2 — Backend FPS DTO/route (Task 12)
- **What:** Added a backend FPS field to the segment/preview surface.
- **Why:** Identity-safe previews needed authoritative backend FPS rather than a
  client-side guess.
- **Status:** implemented with API + UI regressions.

### D3 — Shared presentation & AlertBanner shell mount (Tasks 9/11/12)
- **What:** `usePollingResource.refresh()` made awaitable; notification container
  made a semantic labelled region; authenticated `AlertBanner` shell mount added at
  reviewer request.
- **Why:** Correct mutation coordination, accessibility, and reviewer-requested
  integration.
- **Status:** implemented with targeted regressions; all review findings closed.

---

## Notes

- The full Python suite and UI suite pass; see `docs/operations-control-panel.md`
  §11 for the exact verification commands.
- No secret-like fixtures or absolute workspace paths appear in API responses or
  test snapshots (audited in the final cleanup task).
- `scripts/get_youtube_token.py` carries a pre-existing user modification and is
  intentionally left unstaged throughout the build.
