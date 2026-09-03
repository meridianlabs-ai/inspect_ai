# CI performance report — 2026-09-03

Data: 200 PR runs, 2026-09-02 20:40 .. 2026-09-03 09:13 UTC (**12.5h**, 15.9
runs/hour, 52 pushes). Snapshot: `history/2026-09-03.json`. Previous:
2026-09-01 (200 runs over 10.2h, ending 05:30 UTC). **39.2h between the two
windows is uncovered by either snapshot** — a 200-run snapshot spans half a day
while the schedule runs every two days (proposal 10). Largest gap inside this
window 4.3h (a quiet stretch, not a collector misfire). Produced by the
unattended scheduled run
([workflow run](https://github.com/meridianlabs-ai/actions/actions/runs/33737938759)).

## Summary

**Two blockers moved this run, and one of them was the standing excuse for
shipping nothing.** `.github/workflows/**` is now pushable to the fork — a probe
branch carrying a `build.yml` edit was accepted, where every run since
2026-08-21 was rejected for want of `workflow` scope. So this run ships its
first workflow fix, and proposals 2, 5 and 6 stop being un-actionable.

**Queue time stopped being a non-issue.** After eight windows of a flat 3s
median and a sub-70s worst case, this window's p90 is **57s** (was 23s), p99
**125s**, max **157s**, with **65 waits over 60 seconds** (was 8). All of it is
one 16-minute burst: 47 runs across 6 branches started between 20:40:47 and
20:56:04, fanning out 216 jobs. Inside that burst, code-only Build wall is
**418s against 350s outside it (+68s, +19%)** while the `test` legs' *execution*
is unchanged (333/331s in-burst vs 335/337s out) — the entire difference is
waiting for a runner. This is the first direct measurement of burst contention
costing wall clock in this series.

**It is not a hard concurrency cap.** Merging the push and `pull_request_target`
runs the collector never fetches, concurrency peaked at exactly 20 during the
burst — but reached **35** two hours earlier the same evening, so the pool can
go higher and this is ramp latency, not a ceiling. Adding runners is still not
the demonstrated fix; the demonstrated fact is that a batch of ~6 PRs landing at
once costs each of them a minute of wall clock.

**`uv run` re-syncs the environment the install step just built, in five jobs.**
`uv pip install .[dev]` installs an unlocked resolution; the next step's
`uv run` syncs to `uv.lock`, rebuilding `inspect_ai` and swapping packages:
5.3s on each `test` leg, 3.5s on each `mypy` leg, 4.5s in `docs`, 7.1s in
`sandbox-tools-unit`. The `test`/`mypy` half is shipped in this PR (the
resulting environment is provably identical — `uv pip freeze` after the old and
new sequences match byte for byte). The other two are worse than slow: they
**change which environment the job runs in**, and in `sandbox-tools-unit` that
silently undoes the fix from issue #308 (proposal 3).

**Build wall is 370s median, up from 346s**, and that is honest growth plus the
burst: code-only outside the burst is **350s** against 341s last window, with
the suite up **+675 collected items and +391 test functions in two days**.

**The model-info fix landed and verified on main.** Upstream #5196 merged
2026-09-02; in today's report log the four affected files cost **30.8s against
73.5s before** — `tests/model/test_model_info.py` alone is **0.71s over 51
tests**, from 17.4s over 47.

## Queue vs execution

Median execution / queue over successful jobs, this window against the last.
Queue is measured from run start for independent jobs and from the predecessor's
completion for dependent ones (`needs` map read from `.github/workflows/build.yml`:
`docs`/`sandbox-tools-unit` ← `changes`; `check-version-bump`/`slow-tests` ←
`detect-slow`; `slow-tool-tests-{dev,release}` ← `detect-slow` +
`check-version-bump`). p90 is linear interpolation, as in every prior report.

| workflow | job | n | exec med | prev | exec p90 | queue med | queue p90 |
|---|---|---:|---:|---:|---:|---:|---:|
| Build | slow-tool-tests-dev | 4 | 912 | 794 | 1085 | 3 | 3 |
| Build | docs | 8 | 386 | 399 | 412 | 3 | 10 |
| Build | test (3.11) | 41 | 336 | 331 | 353 | 3 | **73** |
| Build | test (3.10) | 41 | 333 | 328 | 348 | 4 | **70** |
| Build | sandbox-tools-unit | 7 | 131 | — | 138 | 2 | 3 |
| Build | mypy (3.10) | 44 | 92 | 89 | 97 | 4 | 71 |
| Build | mypy (3.11) | 44 | 91 | 87 | 96 | 4 | 68 |
| Viewer | viewer-tests | 47 | 69 | 65 | 74 | 3 | 26 |
| Viewer | check-schema-and-types | 48 | 55 | 54 | 66 | 3 | 18 |
| Viewer | dist-validation | 47 | 34 | 34 | 39 | 3 | 26 |
| Build | pre-commit | 45 | 33 | 31 | 37 | 3 | 69 |
| Build | package | 46 | 32 | 28 | 35 | 3 | 66 |
| Suppressions | suppressions | 46 | 16 | 16 | 22 | 3 | 16 |
| Build | ruff | 46 | 11 | 10 | 12 | 3 | 62 |
| Build | check-version-bump | 5 | 9 | 9 | 11 | 3 | 4 |
| Viewer | submodule-on-main | 47 | 8 | 8 | 9 | 3 | 32 |
| Build | detect-slow | 46 | 8 | 9 | 10 | 4 | 64 |
| Build | changes | 46 | 7 | 7 | 9 | 4 | 63 |
| Changelog Lint | entries-under-unreleased | 32 | 6 | 7 | 9 | 3 | 34 |

Execution is within 1–5s of last window everywhere except `slow-tool-tests-dev`
(+118s on n=4, noise at that sample size). **The queue p90 column is the story**:
every independent job's p90 went from 23–29s to 62–73s. `package` is recorded as
`Build & inspect the package.` in the snapshot; `sandbox-tools-unit` ran often
enough to table this window.

Workflow wall clock, successful runs only:

| workflow | n | wall med | prev | wall p90 |
|---|---:|---:|---:|---:|
| Build | 37 | **370** | 346 | 481 |
| Validate Embedded Viewer | 46 | 76 | 74 | 100 |
| Suppressions | 46 | 22 | 20 | 36 |
| Changelog Lint | 32 | 11 | 11 | 41 |

Split by what the push touched:

| class | n | wall med | p90 | Build runner-min/run |
|---|---:|---:|---:|---:|
| sandbox-tools | 3 | 1022 | 1175 | 34.2 |
| code + docs | 8 | 414 | 477 | 21.2 |
| code only | 26 | **360** | 452 | 15.7 |

A push costs **18.9 runner-minutes** end to end (median over 52 pushes, all four
PR workflows), against 18.5 last window.

### The burst

47 runs across 6 branches started in 16 minutes (2026-09-02 20:40:47 ..
20:56:04), fanning out 216 jobs: `issue-5166-chain-of-thought-format-template`
(12 runs), `issue-5091-mockllm-callable-token-usage` (11),
`issue-4758-thread-extension-system-prompt` (8),
`issue-5100-google-batch-system-instruction` (8), `fix/hf-hidden-states-jsonable`
(4), `dragonstyle/add-gemini-3-8` (4).

Splitting Build runs on burst membership, holding the change class fixed:

| class | in burst | n | wall med | walls |
|---|---|---:|---:|---|
| code only | no | 18 | **350** | 306 … 403 |
| code only | **yes** | 8 | **418** | 366, 389, 397, 414, 422, 452, 465, 487 |

`test`-leg *execution* over the same split is 333s (3.10) / 331s (3.11) in the
burst against 335s / 337s outside — i.e. **flat**. The +68s is entirely queue.
`Validate Embedded Viewer` wall 84s vs 73s and `Suppressions` 30s vs 21s tell the
same story on smaller workflows.

### Concurrency — the collector's blind spot matters here

The collector fetches `event=pull_request` only, but every run in the repo
competes for the same hosted-runner pool. Merging the rest for this window adds
**108 runs and 376 job records** the snapshot never saw:

| runner-min | n | event | workflow |
|---:|---:|---|---|
| 186.2 | 12 | push | Build |
| 55.3 | 4 | pull_request | Build (missed by the 200-run cut) |
| 35.0 | 12 | push | Validate Embedded Viewer |
| 30.1 | **55** | pull_request_target | Suppressions comment |
| 3.2 | 12 | push | Suppressions |
| 8.6 | 13 | (various) | Publish React Viewer Lib, PR Gate, Stale PRs, … |

**319 runner-minutes**, on top of the snapshot's 951 — so real load in this
window is **1,270 runner-minutes over 12.5h** (~102/hour), and every prior
report's compute figure understated the total by about a quarter. Note there are
more `Suppressions comment` runs (55) than Build runs (49).

With those merged in, peak concurrency across the window is exactly **20**, and
during the burst the pool sat at ≥18 for 16.4% of the time and at 20 for 4.1%.
But a wider fetch of the same evening shows concurrency reaching **35** at
18:57:45, so 20 is not a cap — the burst was absorbed at a rate the pool ramps
to, not a rate it is limited to. Wait rises monotonically with jobs already in
flight when a run starts:

| jobs in flight at run start | n | wait med | p90 | max |
|---|---:|---:|---:|---:|
| 0–3 | 396 | 3 | 10 | 125 |
| 4–7 | 90 | 3 | 19 | 21 |
| 8–11 | 79 | 11 | 54 | 70 |
| 12–15 | 45 | 41 | 77 | 110 |
| 16–19 | 66 | 41 | 126 | 157 |
| 20–23 | 24 | 38 | 85 | 86 |

### Queue distribution

700 independent-job samples: median **3s**, p90 **57s**, p95 82s, p99 125s,
**max 157s**; 120 samples above 30s, **65 above 60s** (last window: p90 23s, max
69s, 8 above 60s). Per 10-minute bucket, everything outside the burst is
unremarkable:

| bucket (UTC) | runs | job samples | queue med | p90 | max |
|---|---:|---:|---:|---:|---:|
| **09-02 20:50** | 20 | 75 | **65s** | 121s | **157s** |
| **09-02 20:40** | 27 | 81 | **23s** | 102s | 125s |
| 09-02 22:10 | 4 | 15 | 55s | 56s | 56s |
| 09-03 02:00 | 12 | 45 | 4s | 47s | 62s |
| 09-03 02:50 | 24 | 90 | 3s | 25s | 39s |
| everything else | ≤15 | ≤60 | 2–4s | 3–20s | ≤46s |

The 02:50 bucket is the control: 24 runs in ten minutes, peak concurrency 18, and
a 3s median. The pool absorbs a steady 24 runs/10min fine; it does not absorb 47
runs in 16 minutes.

### Critical path

Binding (last-finishing) job across the 37 successful Build runs:

| binding job | runs | wall median | median margin over runner-up |
|---|---:|---:|---:|
| `test (3.10)` | 15 | 359s | 13s |
| `test (3.11)` | 11 | 366s | 20s |
| `docs` | 6 | 414s | 61s |
| `slow-tool-tests-dev` | 3 | 1022s | 649s |
| `mypy (3.10)` / `mypy (3.11)` | 1 / 1 | 104s / 88s | 18s / 16s |

The two `test` legs bind 26 of 37 runs, by 13–20s. `docs` binds 6 (was 3) by a
61s margin — proposal 2's lever.

## Where the pytest step actually goes

Timestamps from the raw job log of `test (3.10)` in upstream run 33736817639:

| phase | seconds | prev | note |
|---|---:|---:|---|
| `uv run` project re-sync | 5.3 | 5.2 | removed by this PR's workflow fix |
| startup + collection (5 interpreters) | 58.6 | 50.8 | assertion rewriting; grows with the suite |
| test execution | 247.6 | 235.8 | 906.7 worker-seconds over 4 workers |
| reporting (durations, summary) | 0.2 | 1.1 | `-ra`, holding |
| **step total** | **311.8** | 292.9 | window median 307 (3.10) / 309 (3.11) |

The raw log is **5,709 lines** against ~31,000 before #5075. Startup grew 7.8s
in two days on +675 collected items — the assertion-rewrite cost is now the
second-largest line in the step and scales directly with suite size (proposal 5).

## Worker balance (`--dist worksteal`, #4948)

Ninth window holding, from the `test (3.10)` report-log artifact of run
33736817639: per-worker test seconds 225.3 / 222.8 / 233.0 / 225.6 —
**imbalance +6.3s, efficiency 97.3%**, no stragglers.

## Slowest tests

Median seconds per test across the 18 legs mined this window
(`--durations=50 --durations-min=1`, `call` + `setup` + `teardown` summed). 157
tests captured; per-leg tail total **216.4s** (median; 181.4–226.9), against
208.5s last window.

| s | test | classification |
|---:|---|---|
| 11.7 | `test_eval_set.py::test_retry_attempt_killed_mid_sweep_leaves_completed_samples_reusable` | genuinely heavy — kills a live attempt mid-sweep |
| 10.8 | `test_eval_set_selection.py::test_eval_set_selection_concurrent_workers` | genuinely heavy — three real subprocesses |
| 10.1 | `test_eval_set_scanner.py::test_scout_scan_resume_reruns_failed_scans` | genuinely heavy |
| 9.9 | `_control/test_launch_handoff.py::test_eval_detach_hands_off_and_leaves_eval_running` | subprocess launch; pays `import inspect_ai` (#311) |
| 9.6 | `_control/test_launch_handoff.py::test_eval_detach_via_dotenv_detaches_exactly_once` | subprocess launch (#311) |
| 7.6 | `agent/test_agent_bridge.py::test_google_bridge_computer_use_incompatible_model` | ~3.9s is `traceback_ansi` rendering (#374) |
| 6.7 | `_control/test_launch_handoff.py::test_eval_json_redirects_subprocess_stdout_to_stderr` | subprocess launch (#311) |
| 6.6 | `test_eval_set_scanner.py::test_scanner_resume_accumulates_summary_…[s3]` | moto S3 + full eval-set resume |
| 6.4 | `agent/test_agent_bridge.py::test_google_bridge_streaming_not_supported` | ~3.9s is `traceback_ansi` rendering (#374) |
| **6.3** | `model/test_parse_tool_call.py::test_parse_error_on_deeply_nested_yaml_arguments` | **new** — parses a 5000-deep nested list twice to reach `yaml.safe_load`'s `RecursionError`; guards a real uninterruptible hang (#5095), so the cost is the point |
| 6.0 | `_control/test_launch_handoff.py::test_eval_detach_fails_when_control_bind_fails` | subprocess launch (#311) |
| 6.0 | `test_eval_set.py::test_eval_set_previous_task_args` | ~5s real sleep around `keyboard_interrupt(2)` |
| 5.9 | `log/test_eval_log_config.py::test_eval_log_run_config_round_trip` | genuinely heavy |
| 4.4 | `scorer/test_score_editing.py::test_edit_history_captures_original_reason[asyncio]` | new to the tail, near the 1s cutoff band |
| 4.3 | `_control/test_pause.py::test_eval_hard_pause_time_limit_reap_reparks_grader` | timer-bound |

Heaviest files by total worker time in the report log (not just the tail):
`test_eval_set.py` 62.7s, `test_eval_set_scanner.py` 61.7s,
`_control/test_launch_handoff.py` 48.6s, `_control/test_eval_set_integration.py`
40.5s, `test_sample_limits.py` 29.5s, `test_eval_set_selection.py` 22.6s,
`_view/test_view_server.py` 22.1s, `agent/deepagent/test_deepagent_background.py`
20.2s.

### No per-test regression

Diffing per-test medians against the 2026-09-01 snapshot over the 92 tests in
both tails: sum **310.9 → 318.6s**, largest increase **+2.0s**
(`test_write_s3_eval_header_only_compacts_zip`), largest decrease **−1.1s**
(`test_eval_set[True]`). 65 tests entered the tail and 57 left it, all near the
1s cutoff — ranking-boundary churn, not new cost.

### Docker-trap sweep

Unchanged for seven runs: **6** test functions pair `skip_if_no_docker` with no
`@pytest.mark.slow` — `util/sandbox/test_docker_compose_config.py` ×3 (ungated,
never start a container), `tools/test_think_tool.py` ×2 and
`agent/test_agent_docs.py::test_agent_collect` (gated by `skip_if_no_anthropic` /
`skip_if_no_openai`). No new offenders.

## Suite size

| snapshot | collected items | pytest wall (median leg) | Build wall (success) |
|---|---:|---:|---:|
| 2026-08-23 | 13,410 | 299.3 | 354.0 |
| 2026-08-25 | 13,449 | 304.8 | 353.0 |
| 2026-08-27 | 14,123 | 328.7 | 390.0 |
| 2026-08-29 | 14,674 | 287.1 | 342.0 |
| 2026-08-31 | 14,950 | 290.3 | 338.0 |
| 2026-09-01 | 15,220 | 289.6 | 346.0 |
| 2026-09-03 | **15,895** | **300.4** | **370.0** |

+675 collected items in two days. Top-level test functions on `main`
(`^(async )?def test_` under `tests/`, re-derived per date):

| date | test functions | Δ |
|---|---:|---|
| 2026-08-27 | 8,192 | — |
| 2026-08-29 | 8,358 | +166 (2d) |
| 2026-08-31 | 8,468 | +110 (2d) |
| 2026-09-01 | 8,663 | +195 (1d) |
| 2026-09-02 | 8,934 | +271 (1d) |
| 2026-09-03 | **9,054** | **+120 (1d)** |

**+391 test functions in the two days this report covers**, the fastest
two-day growth the series has recorded.

### Where the time sits (report log, 15,895 tests, 906.7 worker-seconds)

| band | tests | worker-s | share | prev share |
|---|---:|---:|---:|---:|
| ≥5s | 13 | 106.1 | 11.7% | 11.2% |
| 1–5s | 157 | 276.8 | 30.5% | 30.9% |
| **0.1–1s** | **1,241** | **437.2** | **48.2%** | 49.2% |
| <0.1s | 14,484 | 86.6 | 9.5% | 8.8% |

Phases: call 840.5s, setup 43.3s, teardown 22.9s. 11,192 passed, 4,703 skipped —
30% of collected items never run in the PR gate. Worker time 852.9 → 906.7s
across the two days; the model-info fix took ~43s *out* over the same period, so
gross growth is on the order of 95s / two days.

The shape is stable across five windows: the ~91% of tests under 0.1s are under
10% of the time, and the 0.1–1s band is half of it. Proposal 1 remains the direct
attack on that band.

### Duplicate-coverage and low-value sampling

The AST sweep (identical decorators + signature + body) finds **5 groups / 11
tests**. Three are coincidental — the same one-line assertion in three provider
`test_known_models_not_latest` files, two CLI `test_omitted_returns_none` flag
tests, and a pair of nested `async def test_func` helpers pytest never collects.

**Two were a real defect and are fixed in this PR**: in
`tests/agent/test_agent_execute.py`, `test_agent_as_tool_no_param_docs_error` and
`test_agent_handoff_no_param_docs_error` both called
`check_agent_as_tool_no_docs_error`, making them byte-identical duplicates of the
two tests above them and leaving `check_agent_as_tool_no_param_docs_error` — the
"Description not provided for parameter" path — unreferenced and never
exercised. Wall-clock impact: none. Coverage impact: one error path that was
silently untested now is.

## Regressions since last report

**No per-job execution regression.** Every job is within 1–5s of last window
(`slow-tool-tests-dev` +118s on n=4 is small-sample noise). Test worker time
852.9 → 906.7s is suite growth, and the matched per-test tail moved +7.7s over 92
tests.

**Queue is a regression**, and it is the one to watch: p90 23 → 57s, max 69 →
157s, waits over 60s 8 → 65. Confined to one burst; the next snapshot says
whether that recurs.

Red checks a contributor actually sees — 22 failed job records:
`entries-under-unreleased` **14**, `check-version-bump` 2, `suppressions` 2,
`dist-validation` 1, `submodule-on-main` 1, `slow-tool-tests-release` 1,
`test (3.11)` **1**. Two things stand out. Only **one `test`-leg failure in 200
runs**. And the changelog check failed on **13 distinct branches** — this window
contains the 0.3.261 release and the follow-up
[#5205](https://github.com/UKGovernmentBEIS/inspect_ai/pull/5205) restoring the
release section, i.e. exactly the "a merge from the base relocates your entry
under a released heading" failure AGENTS.md warns about, hitting a third of the
window's branches at once. Not a wall-clock cost (the job is 6s) but the largest
single source of red on contributor PRs right now.

## Waste

- **Cancelled superseded jobs: 44.0 runner-minutes** across 16 jobs (was 71.2
  over 31), led by `test (3.10)` 15.4 and `slow-tool-tests-dev` 11.6.
- **Failed jobs: 8.2 runner-minutes** over 22 jobs (was 30.3) — the failures are
  cheap ones this window.
- **Duplicated Quarto renders: ~194 runner-minutes.** The repository cache index
  holds **165 `docs-render-*` entries over 129 distinct keys with 3 ever re-read
  (1.8%)**, and **36 keys exist twice — once under a PR merge ref and once under
  `refs/heads/main`** (was 30 of 77), at ~324s a render over the seven days the
  index covers.
- **`uv run` re-sync: 5.3s per `test` leg, 3.5s per `mypy` leg, 4.5s in `docs`,
  7.1s in `sandbox-tools-unit`** — ~26s of runner time per push, of which ~11s is
  on the critical path. Fixed for `test`/`mypy` in this PR; see proposal 3 for
  the rest.
- **Compute: 951 runner-minutes** per 200 PR runs (Build 800, Viewer 133,
  Suppressions 14, Changelog Lint 5), plus **319 runner-minutes** of push /
  `pull_request_target` / scheduled runs the collector does not fetch —
  **1,270 total over 12.5h**.
- **Runs that never ran: 0 `action_required`** (was 6).
- **Overhead-dominated jobs:** `changes` 7s, `detect-slow` 8s,
  `submodule-on-main` 8s, `ruff` 11s, `suppressions` 16s — ~50s of runner time
  per push, none of it on the critical path.

## Impact verification (previous runs' changes)

- **#5196 (model-info cache) — merged 2026-09-02, verified on main, prediction
  beaten.** In upstream run 33736817639's report log the four affected files cost
  **30.8s** against 73.5s pre-fix: `tests/model/test_model_info.py` **0.71s over
  51 tests** (was 17.4s over 47), `test_model_family.py` **0.13s** (7.4s),
  `test_canonical_names.py` **0.49s** (5.1s), `test_sample_limits.py` **29.5s**
  (37.5s). This closes out the fix the previous three runs carried on an unlanded
  fork branch; the maintainer promoted it as
  [#5196](https://github.com/UKGovernmentBEIS/inspect_ai/pull/5196).
- **#5075 (`-rA` → `-ra`) — holding, fifth window.** Reporting phase 0.2s, raw
  test-leg log 5,709 lines.
- **#4948 (`--dist worksteal`) — holding, ninth window** (+6.3s imbalance, 97.3%
  efficiency, no stragglers).
- **#4760 (`test_package` pre-installed) — holding**, and now confirmed to
  survive `uv run`'s sync: `tests/test_extensions.py` is 0.43s over 11 tests and
  the raw log shows no mid-run `pip install`.
- **#4935 (`blob:none` checkouts) — holding.** Checkout is 4–7s median across
  every job and **no always-run step has a p90 above 2× its median** anywhere in
  the snapshot.
- **#299 (`design/**` excluded from the test filter) — holding**, 3 observations:
  `fix/changelog-4418-entry-placement` (test legs 6s/6s, Build wall 104s),
  `dragonstyle/restore-changelog-0.3.261` (3s/5s, 88s), and
  `add-corrlog-extension` (7s/4s but docs-touching, so `docs` bound it at 426s).
  `mypy` remains the long pole of a docs/md-only push.
- **#297 / #317 (docs render cache) — sixth window, hit rate still 0.** `Render
  docs` ran in all 8 `docs` jobs (324s median). Cache-index evidence above; the
  duplicate-key count grew 30 → 36. Posted to #317.
- **#374 (traceback rendering) — unchanged, still open.** The two Google bridge
  tests are 7.6s and 6.4s.
- **#393 (control-server startup) — filed 2026-09-01, no action yet**; both
  candidate fixes remain maintainer decisions.

## Proposals (ranked)

1. **Stop paying 214ms of control-server startup on every `eval()`.** Carried,
   unchanged, and still the largest measured lever in this series. A one-sample
   `mockllm` eval is 249ms with the control channel and 35ms without; the 214ms
   splits 30ms building an identical 28-route FastAPI app, 30ms binding, and
   100ms waiting out uvicorn's fixed 0.1s `should_exit` poll. 796 test functions
   across 153 files call `eval()`/`eval_set()` directly, and a full-suite A/B on
   4 workers runs **725.8 → 529.0s of wall (−27%)** with **381 tests leaving the
   0.1–1s band** — the band that is still 48% of CI test time. Extrapolated to a
   CI leg (248s of execution over 4 workers, 907 worker-seconds), on the order of
   **50–60s off both `test` legs**, the only lever large enough to clear the
   13–20s binding margin on both at once. Not shipped: the test-side fix moves
   coverage of the *default* configuration out of the bulk of the suite, and the
   product-side fix changes eval teardown semantics. Status: carried,
   [#393](https://github.com/meridianlabs-ai/inspect_ai/issues/393).

2. **Fix the docs render cache key so `main` churn stops invalidating it.**
   Carried; evidence grew again and **the push blocker is now gone**, so this is
   actionable for the first time. Hit rate by window: 14%, 11%, 8%, 5%, 0%,
   **0 of 8**. The index holds 165 entries over 129 distinct keys with **3 ever
   re-read**, and **36 keys exist twice — once under a PR merge ref, once under
   `refs/heads/main`** (~194 runner-minutes of provably identical renders in
   seven days). Cause unchanged: on `pull_request` the checkout is the merge ref,
   so `hashFiles('docs/**', 'requirements-doc.txt', 'src/inspect_ai/**')` hashes
   the PR merged into *current* `main`, and any push to `main` touching `src/`
   invalidates every open PR's entry. Est. ~324s of job exec per hit and ~61s of
   Build wall on the 6-of-37 runs where `docs` binds. **Deliberately not shipped
   this run**: every candidate key (drop `src/**`; key on the merge-base plus the
   PR's own delta; add `restore-keys`) trades render correctness for hit rate in
   a way a reviewer could reasonably object to, which puts it outside the
   safe-fix line. It wants a maintainer's call on how much staleness a
   render-validation marker may carry. Status: carried,
   [#317](https://github.com/meridianlabs-ai/inspect_ai/issues/317),
   re-evidenced today, **now unblocked**.

3. **`uv run` re-syncs the environment the install step just built — and in two
   jobs it changes what they test.** NEW. Every job that installs with
   `uv pip install` and then runs under `uv run` pays a full project sync to
   `uv.lock` first, rebuilding `inspect_ai` and swapping packages. Measured on
   upstream run 33736817639 and 33711063284:

   | job | sync cost | effect |
   |---|---:|---|
   | `test (3.10/3.11)` | 5.3s | uninstall 46 / install 48 — same locked env, just late |
   | `mypy (3.10/3.11)` | 3.5s | uninstall 46 / install 48 — same locked env, just late |
   | `docs` | 4.5s | uninstall 13 / install 157 — **renders against the root lockfile, not `requirements-doc.txt`** |
   | `sandbox-tools-unit` | 7.1s | uninstall 13 / install 204 — **tests the injectable against root-lockfile deps** |

   The `test`/`mypy` half is **shipped in this PR** (`uv sync` + `uv run
   --no-sync`; environments verified byte-identical). The other two are not, and
   the `sandbox-tools-unit` one is the serious finding: issue
   [#308](https://github.com/meridianlabs-ai/inspect_ai/issues/308) is closed on
   the strength of the job installing `./src/inspect_sandbox_tools[dev]` into its
   own venv, and `uv run`'s implicit sync silently undoes that a step later.
   Measured today, **14 packages differ** between the injectable's own resolution
   and the root lockfile the job actually gets — including `mcp 2.1.1 → 2.0.0`,
   the exact package #308 was filed about, plus `anthropic 1.3.0 → 1.0.0`,
   `openai 3.7.0 → 3.3.1`, `google-genai 2.22.0 → 2.19.0`. The injectable's tests
   **pass under its own pins** (212 passed / 1 skipped, run locally), so
   `--no-sync` there is green today — but changing what a job tests is a coverage
   decision, not hygiene. Evidence posted to #308.

   Three further jobs — `slow-tool-tests-dev`, `slow-tool-tests-release` and
   `slow-tests` — still run `uv venv` + `uv pip install .[dev]` + a plain
   `uv run` against the root project, so the same 3–5s applies; they are rare
   and long, so the payoff is negligible. **One of them must not be changed
   naively**: `slow-tool-tests-release` downloads the published binaries into
   `src/inspect_ai/binaries/` after a *wheel* install, and `_binaries_dir()` is
   `Path(inspect_ai.__file__).parent / "binaries"` — the job only finds them
   because `uv run`'s sync replaces that wheel with the lockfile's editable
   install and repoints `inspect_ai.__file__` at the source tree. Adding
   `--no-sync` there without also switching its install step to `uv sync` would
   break the release gate. Recorded here so a future run does not walk into it.

   Status: **new, half shipped**.

4. **Burst contention now costs measurable wall clock.** NEW, and it retires
   proposal 11's "recommend dropping". Eight windows of a flat 3s median ended
   here: p90 57s, p99 125s, max 157s, 65 waits over a minute — all from 47 runs
   across 6 branches landing in 16 minutes, costing those runs **+68s of Build
   wall (350 → 418s) with test execution unchanged**. What the data does *not*
   support is "buy more runners": concurrency reached 35 earlier the same
   evening, and a 24-runs-per-10-minutes stretch the next morning held a 3s
   median at peak 18. So the pool ramps, and the cost is ramp latency on a step
   change in demand. Two directions worth a maintainer's view, neither shippable
   from here: reduce jobs-per-push (proposal 12 territory — 13–21 job records per
   push across four workflows, plus a `Suppressions comment` run that outnumbers
   Build runs), or accept it as the price of batch-landing agent PRs. Report-only
   until the next snapshot says whether it recurs — one burst is an observation,
   not a trend. Status: **new**.

5. **Cache pytest's assertion-rewrite bytecode across runs.** Carried, and
   growing: startup+collection is now **58.6s of the 311.8s step (19%)**, up from
   50.8s of 292.9s, because the cost scales with suite size and the suite added
   675 items in two days. Fix shape: restore `**/__pycache__` from
   `actions/cache` keyed on a hash of `src/**/*.py` + `tests/**/*.py`, normalizing
   source mtimes deterministically after checkout in both the producing and
   consuming run (pytest validates a rewritten pyc against source mtime + size).
   Est. ~30s off both `test` legs. Now pushable, but still **needs one CI
   experiment before it is a safe fix** — the mtime normalization is the part
   that can silently no-op. What does not work, both measured on 2026-08-29:
   `compileall` and `uv`'s `compile-bytecode`. Status: carried, **unblocked, next
   in line to ship once measured**.

6. ~~**`uv run` re-syncs the environment.**~~ **Shipped this run** for `test` and
   `mypy` — merged into proposal 3, which carries the remainder.

7. **Defer the `acp.schema` import.** Unchanged: 483ms of the 1.70s self-time of
   `import inspect_ai`, ~7x the next-largest module, reached through two eager
   edges. Paid by 5 interpreters per leg plus the four
   `_control/test_launch_handoff.py` tests that hold slots 4, 5, 7 and 11 of the
   tail. Product change with a public-API surface. Status: carried,
   [#311](https://github.com/meridianlabs-ai/inspect_ai/issues/311).

8. **Test-volume policy — it is the 0.1–1s band that matters.** Fifth window
   confirming it: 14,484 tests under 0.1s are 9.5% of test time; 1,241 tests
   between 0.1s and 1s are 48.2%. Growth accelerated to **+391 test functions in
   two days** (+675 collected items), the fastest in the series, and pytest wall
   rose 289.6 → 300.4s despite the model-info fix removing ~43s. Proposal 1
   reframes the question usefully: much of that band is not what the tests
   *assert*, it is what `eval()` *costs*. Structural. Status: carried,
   **growth rate worsening**.

9. **`tests/util/test_display_counter.py` sleeps 6 × 1.1s for 2 throttle paths.**
   Carried at **8.93s of worker time** in this window's report log (n=6).
   Re-examined for a mock-clock fix on 2026-09-01 and rejected:
   `inspect_ai.util._throttle` reads `time.time()` directly *and* schedules a real
   `anyio.sleep(remaining)` trailing-edge fire in a background task, so faking the
   clock without also faking the sleep changes what the test exercises. The honest
   options remain a coverage judgement (drop the sleep for the params whose
   `@throttle(5)` a 1.1s sleep can never fire) or an injectable throttle window
   (product change). Status: carried.

10. **Collector: fetch more than 200 runs, and fetch more than `pull_request`.**
    Carried and **broadened twice**. (a) At 15.9 runs/hour a 200-run snapshot
    spans 12.5h against a ~2-day cadence, so **39.2h between this window and the
    last is uncovered** and the series samples a shrinking fraction of CI. (b)
    New: the `event=pull_request` filter hides **319 runner-minutes and 376 job
    records** per window — including the 12 push-event Build runs that are the
    single largest compute line — which understates load by ~25% and, worse,
    makes every concurrency number in every prior report a lower bound. Both
    fixes are one file, `.claude/skills/ci-perf/scripts/collect_ci_data.py`,
    still unwritable by the agent's edit tooling (proposal 11). Status: carried,
    **broadened**.

11. **Unblock the scheduled run — one of three blockers is now clear.**
    Re-probed today with real attempts:
    - *`workflow` scope* — **CLEARED**. A probe branch carrying a
      `.github/workflows/build.yml` edit pushed to the fork successfully and the
      blob was confirmed changed on the remote (branch deleted afterwards). Every
      run since 2026-08-21 was rejected here. This unblocks proposals 2, 5 and the
      shipped half of 3.
    - *No upstream write* — still blocked;
      `repos/UKGovernmentBEIS/inspect_ai` reports
      `{"admin":false,"maintain":false,"pull":true,"push":false,"triage":false}`
      for this token. PR creation attempted at the end of this run (result in
      `prs.md`).
    - *`.claude/**` unwritable by the agent's edit tooling* — still blocked; a
      plain write under `.claude/skills/ci-perf/` was refused as a sensitive
      file. Blocks proposal 10.

    Status: carried, **materially improved**, updated on
    [#298](https://github.com/meridianlabs-ai/inspect_ai/issues/298).

12. **Merge the 4 Viewer jobs into 1–2** — required-check rename. More
    interesting than last window for a new reason: proposal 4 makes
    jobs-per-push, not job duration, the thing that determines how badly a batch
    of PRs collides. A push currently produces 13–21 job records across four
    workflows plus a `Suppressions comment` run. The Viewer workflow is 76s of
    wall and 2.9 runner-min across 4 jobs. Structural. Status: carried, **reframed
    by proposal 4**.

13. **Duplicate and near-duplicate test cleanups.** The strict AST sweep finds 5
    groups / 11 tests; the two that were a genuine defect are fixed in this PR
    (see "Duplicate-coverage" above), and the remaining three are coincidental
    one-liners with no cleanup value. The real-sleep candidates are unchanged.
    Status: carried, **partly actioned**, low.

14. **Policy consistency: docker tests without `@pytest.mark.slow`.** Still six,
    still ~0.05s combined; the right fix is probably to drop `skip_if_no_docker`
    from the three ungated ones rather than to mark them slow. Zero wall-clock
    impact. Status: carried.

15. **Changelog-entry relocation is the top source of red PR checks.**
    NEW, and not a wall-clock item. `entries-under-unreleased` failed **14 times
    across 13 distinct branches** in 12.5h — more than every other check
    combined — because the 0.3.261 release moved the `## Unreleased` heading and
    open branches' entries rode along under it. AGENTS.md documents the failure
    and the manual check (`git diff "$(git merge-base origin/main HEAD)" HEAD --
    CHANGELOG.md`); the check itself already tells contributors what is wrong.
    Worth noting only because a release turns a third of open branches red at
    once, and nobody is measuring that. Status: **new**, report-only.

Proposal 6 folded into 3; nothing else dropped.

## PRs opened by this skill

See `prs.md`. The previous run's PR
([meridianlabs-ai/inspect_ai#375](https://github.com/meridianlabs-ai/inspect_ai/pull/375))
was closed on 2026-09-02, superseded by the maintainer-promoted upstream
[#5196](https://github.com/UKGovernmentBEIS/inspect_ai/pull/5196) (merged), so
this run opens a fresh PR rather than pushing onto an open predecessor. It ships
two safe fixes — the `uv sync` / `--no-sync` workflow change and the
duplicate-test wiring fix — alongside the snapshot, this report and the ledger.
New evidence was posted to
[#317](https://github.com/meridianlabs-ai/inspect_ai/issues/317) (docs
render-cache index),
[#308](https://github.com/meridianlabs-ai/inspect_ai/issues/308) (its fix is
undone by `uv run`'s implicit sync) and
[#298](https://github.com/meridianlabs-ai/inspect_ai/issues/298) (the `workflow`
scope blocker is cleared; the other two stand).
