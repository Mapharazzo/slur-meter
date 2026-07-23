# Heartbeat follow-up report

## Outcome

The intermittent failure in
`test_blocking_generation_stage_keeps_event_loop_and_lease_heartbeat_alive`
was caused by an unrealistic compressed-timing assertion, not by a production
heartbeat, supervisor, worker-adapter, or lease-duration propagation defect.

The regression used a 120 ms lease and then slept for twelve nominal 10 ms
event-loop ticks before inspecting the lease. That observation interval equals
the entire lease and has no scheduler margin. Counting twelve completed sleeps
proves that the event loop ran, but does not bound the wall-clock gap between
two ticks. A process pause longer than 120 ms therefore allows the lease to
expire correctly before the next heartbeat.

The test now uses the same 600 ms lease in both `JobDispatcher` and
`PipelineRunner`, counts event-loop ticks while the synchronous metadata client
remains blocked in its worker thread, and polls until the persisted expiry
actually advances. The bounded 50 x 10 ms observation window is shorter than
the lease and the expected heartbeat arrives after 200 ms. Every observation
also asserts that the expiry remains present. This preserves both required
invariants: the event loop progresses while blocking generation runs, and the
durable lease expiry advances before the blocked stage is released.

No production code changed. `scripts/get_youtube_token.py` was preserved and
excluded from all staging. No external/provider calls were made.

## Root-cause evidence

At HEAD `6147fa8`, the original test passed 100/100 isolated runs and 100/100
runs with pytest, its generation worker, and three CPU-bound competitors pinned
to one CPU. This established that ordinary blocking metadata work is correctly
offloaded and does not itself block asyncio.

A diagnostic-only tracing store then recorded `time.monotonic()` immediately
before and after each renewal/recovery. A deliberate 140 ms event-loop pause
after the first observed expiry reproduced the reported `None` deterministically:

```text
renew succeeded: 4968102.935387 -> 4968102.938958
next renew began: 4968103.080472
renew result: False
recovery began: 4968103.081965
recovered job: job_1fbe8eea3fd143219d85a77642b2945c
```

The gap from the last successful renewal completion to the next renewal was
about 141.5 ms, exceeding the 120 ms lease. `OperationStore.renew_lease()`
correctly rejected the already-expired lease before the supervisor recovered
it; the supervisor did not steal a still-renewable lease.

The retained diagnostic database is under
`/tmp/heartbeat-forced-evidence-2/test_blocking_generation_stage0/heartbeat.db`.
Its durable timestamps show the job claimed at
`2026-07-23T00:42:35.709729+00:00`, the metadata stage running at
`2026-07-23T00:42:35.723615+00:00`, and `restart_recovery` at
`2026-07-23T00:42:35.874239+00:00`. Recovery requeued the stage, closed its
attempt as `interrupted`, cleared `lease_expires_at`, and emitted the expected
`restart_recovery` event.

Code/data-flow inspection confirmed:

- `JobDispatcher._heartbeat()` renews immediately, then sleeps for one third
  of `lease_seconds` between renewals.
- The generation worker adapter polls asynchronously every 10 ms while the
  blocking provider executes in a daemon worker thread.
- The test supplied the same lease duration to the dispatcher and runner; no
  propagation mismatch existed.
- `OperationStore.renew_lease()` intentionally refuses renewal at or after the
  persisted expiry, and `recover_expired_leases()` then clears ownership and
  requeues interrupted work.
- The supervisor polls recovery while work is active, but the trace shows the
  renewal had already failed due to expiry before recovery ran.

## Strict regression proof

After redesigning the test, the unchanged production heartbeat passed once.
For a deterministic RED mutation, `_heartbeat()` was temporarily replaced by
one lease-length sleep followed by `False`. The redesigned test failed with:

```text
assert second_expiry > first_expiry
AssertionError: assert '2026-07-23T00:43:22.938980+00:00'
                   > '2026-07-23T00:43:22.938980+00:00'
```

The mutation was reverted exactly. With the production immediate/periodic
heartbeat restored, the same test passed. `git diff -- api/dispatcher.py` is
empty.

## Stress and verification

- Redesigned heartbeat regression, isolated: 100/100 passed.
- Redesigned heartbeat regression under forced single-CPU contention: 100/100
  passed.
- Entire `tests/integration/test_generation_scenarios.py`: 10/10 repeated file
  runs passed.
- Task 4 focused gate (retry, dispatcher, runner, operation store): 71 passed.
- Task 5 focused gate (media/artifacts/generation): 60 passed, 2 known warnings.
- Task 6 focused gate (publishing/platform/metadata): 125 passed.
- Task 7 API trio: 72 passed.
- Unit plus API gate: 413 passed, 18 known warnings.
- Full suite: 451 passed, 1 failed, 22 known warnings. The sole failure is the
  unchanged Task 8 fixture baseline
  `tests/integration/test_pipeline.py::TestAnalysisEngineIntegration::test_django_srt_pipeline`
  (`total_f_bombs`: expected 100, actual 200). The heartbeat regression passed
  in this full run.
- `.venv/bin/ruff check tests/integration/test_generation_scenarios.py`:
  `All checks passed!`
- `git diff --check`: exit 0.

## Independent review

An independent medium-effort read-only reviewer inspected the test, report,
dispatcher heartbeat, database renewal/recovery semantics, and generation
worker boundary. The reviewer independently repeated the focused regression
20/20 and approved the change with no Critical, Important, or Minor findings.
The review confirmed that no metadata progress callback can account for the
observed expiry advance while `fetch()` remains blocked, so the redesigned
assertion specifically observes the dispatcher heartbeat.

## Remaining considerations

No in-process heartbeat can retain a lease if its event loop/process is paused
longer than the entire lease. That is the intended crash-recovery boundary; a
different worker must be allowed to recover expired ownership. Production uses
a 30 second default lease rather than this test's compressed duration. The new
test keeps compressed execution while testing heartbeat behavior against an
observable state transition with explicit scheduling margin.
