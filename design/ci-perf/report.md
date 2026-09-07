# CI performance report — 2026-09-07

Data: 200 PR runs, 2026-09-04 14:49 .. 2026-09-07 09:17 UTC (**66.5h**, 3.0
runs/hour, 55 pushes). Snapshot: `history/2026-09-07.json`. Previous: 2026-09-05
(200 runs over 17.6h, ending 01:25 UTC). For the first time in this series the
two windows **overlap** (by 10.6h) instead of leaving a hole, because the
weekend cut throughput from 11.3 to 3.0 runs/hour — the same 200-run limit that
covered 17.6h last window covers 66.5h of this one (proposal 9). The largest
gap inside the window is **14.2h**, and it is a genuine weekend lull, not the
collector's stale-page bug: a direct API query over 2026-09-06 10:00 ..
2026-09-07 00:30 returns **zero** PR runs, so `warn_on_time_gap`'s fixed 12h
threshold false-positived for the first time. Produced by the unattended
scheduled run
([workflow run](https://github.com/meridianlabs-ai/actions/actions/runs/34105444190)).

## Summary

**This run ships one safe fix, and it closes a cache bug that has been costing
43s of every `docs` job since the job existed.** `astral-sh/setup-uv`'s default
cache key is shared by every Python-3.11 job in `build.yml`, so whichever
finishes first — `mypy (3.11)` at ~90s — saves its 25MB pruned dev cache under
that key, and every later job logs `Cache hit occurred on key …, not saving
cache`. The `docs` job's dependency set is a *different* set, and one of its
members is the problem: `quarto-cli` publishes **no wheel on PyPI**, only a 4.7KB
sdist whose build downloads the ~150MB quarto release. So `docs` restores 25MB of
dev packages it does not need, spends **43.3s building quarto-cli from sdist**,
prunes its cache down to the 207MB that `uv cache prune --ci` keeps *precisely*
so a built wheel survives — and then cannot save it. Every run, forever. The fix
is one input, `cache-suffix: docs`, and two review passes were needed to arrive
at exactly that — see proposal 1 for what the drafts got wrong and for the one
residual the fix does not close.

**The window is a weekend, and the suite stopped growing.** 3.0 runs/hour against
11.3; `main` has not moved since 2026-09-06 07:20Z. Test functions under `tests/`
went 9,240 → **9,246 (+6)** and collected items 16,415 → **16,416 (+1)**,
against +184/+520 and +391/+675 in the two windows before. Both deltas are
report-to-report (2026-09-05 → 09-07); because this window reaches back to
09-04, growth *inside* its span is +95 test functions, almost all of it on the
Friday before the weekend. Read every trend line in
this report as "flat over a quiet weekend" rather than as an improvement; the
noise floor (±60s of worker time) is wider than most of the deltas.

**`docs` is the second binding job class, and it binds by a very wide margin.**
It was last-finishing in **5 of 39 successful Build runs** at a **303s median
margin** over the runner-up — but that margin is #299 working as designed, not a
regression: on a docs-only push the `test` legs no-op in 4s and `docs` stands
alone. The load-bearing number is the class split: a docs-touching push costs
**421s** of Build wall against **350s** for code-only, and `docs` exec is **396s**
of which the render is 330s and the install this run fixes is 49.5s.

**#5220 (docs render cache key) — mechanism confirmed, 0 hits this window, and
the duplicate-render waste is effectively closed.** 8 `docs` jobs ran, all 8
rendered; every miss traces to a genuine change in a hashed input, not to the key
design. The only 2 hits ever observed sit in the 10h between the merge and the
previous window's start, so **no snapshot has yet contained one** — 0 of 7 last
window, 0 of 8 this one, which is evidence against #5220's predicted ~23% rate
even though the cache index shows the mechanism working. The duplicate-key
problem that dominated four prior reports is down to **2 of 19 post-merge keys** existing under both a PR ref and `refs/heads/main`, and both
are **concurrent** PR/main renders (77s and 305s apart, overlapping) that no
cache design could have deduplicated.

**Two kinds of red check that nothing in the code caused.** `test (3.10)` on
`fix/math-symbol-assumptions` failed after the suite **passed**: the
`Upload test order log` step hit `Failed to FinalizeArtifact: … (403) Forbidden`
and took a required check down with it (first observation; report-only,
proposal 12). And **6 runs concluded `failure` with zero jobs** — approval-gate
runs on two first-contributor PRs, marked failed the second the PR closed or
merged. Prior reports have counted only *job* records, so their red-check
figures, including last window's "cleanest in the series", undercount by this
class.

**Queue is a non-issue for the ninth consecutive window** — median 3s, p90 8s,
max 71s over 651 records — and, as last window, the burst was not re-tested:
peak concurrency was 19 jobs and the densest 16-minute stretch 24 runs.

## Queue vs execution

Median execution / queue over successful jobs, this window against the last.
Queue is measured from run start for independent jobs and from the predecessor's
completion for dependent ones (`needs` map read from `.github/workflows/build.yml`:
`docs`/`sandbox-tools-unit` ← `changes`; `check-version-bump`/`slow-tests` ←
`detect-slow`; `slow-tool-tests-{dev,release}` ← `detect-slow` +
`check-version-bump`). p90 is linear interpolation, as in every prior report.

| workflow | job | n | exec med | prev | exec p90 | queue med | queue p90 |
|---|---|---:|---:|---:|---:|---:|---:|
| Build | slow-tool-tests-dev | 3 | 1109 | 945 | 1135 | 2 | 4 |
| Build | **docs** | 8 | **396** | 390 | 412 | 3 | 6 |
| Build | slow-tests (checkpoint) | 6 | 393 | 362 | 475 | 2 | 3 |
| Build | test (3.11) | 43 | 338 | 338 | 358 | 3 | 11 |
| Build | test (3.10) | 42 | 334 | 338 | 357 | 3 | 4 |
| Build | sandbox-tools-unit | 2 | 132 | — | 133 | 2 | 2 |
| Build | mypy (3.10) | 42 | 92 | 92 | 99 | 3 | 5 |
| Build | mypy (3.11) | 42 | 89 | 87 | 97 | 3 | 10 |
| Viewer | viewer-tests | 43 | 68 | 71 | 74 | 3 | 7 |
| Viewer | check-schema-and-types | 43 | 58 | 59 | 68 | 3 | 10 |
| Viewer | dist-validation | 44 | 34 | 36 | 39 | 3 | 9 |
| Build | pre-commit | 44 | 30 | 34 | 37 | 3 | 5 |
| Build | package | 44 | 30 | 31 | 34 | 3 | 4 |
| Suppressions | suppressions | 44 | 16 | 16 | 19 | 3 | 4 |
| Build | ruff | 44 | 10 | 11 | 12 | 3 | 10 |
| Build | check-version-bump | 4 | 9 | 8 | 10 | 3 | 3 |
| Build | detect-slow | 44 | 8 | 9 | 10 | 3 | 13 |
| Viewer | submodule-on-main | 44 | 8 | 8 | 9 | 3 | 5 |
| Changelog Lint | entries-under-unreleased | 29 | 7 | 7 | 8 | 3 | 3 |
| Build | changes | 44 | 7 | 7 | 8 | 3 | 7 |

Every median is within 4s of last window except `slow-tool-tests-dev` (n=3
against n=1) and `slow-tests` (n=6 against n=16 — both small samples on the same
two checkpoint branches). The three Viewer p90s have come back down (124 → 74,
96 → 68, 56 → 39): last window's rises were the `pnpm/action-setup` incident, and
that step is back to a 5–6s median with an 8–10s p90 in all three jobs
(proposal 11). `package` is recorded as `Build & inspect the package.` in the
snapshot.

`Suppressions comment` is a `pull_request_target` workflow, so the collector
never sees it (proposal 9); measured directly over the window's **56 runs** it is
**31.0s median / 36.0s p90**, of which **27.0s** is `actions/checkout` and
**1.0s** is the work — the baseline for the fix already committed on this branch,
which cannot take effect until it merges (see "Impact verification").

Workflow wall clock, successful runs only:

| workflow | n | wall med | prev | wall p90 |
|---|---:|---:|---:|---:|
| Build | 39 | **351** | 360 | 441 |
| Validate Embedded Viewer | 42 | 74 | 76 | **84** |
| Suppressions | 44 | 21 | 21 | 24 |
| Changelog Lint | 29 | 10 | 10 | 12 |

Split by what the push touched:

| class | n | wall med | p90 | Build runner-min/run |
|---|---:|---:|---:|---:|
| sandbox-tools | 1 | 991 | 991 | 38.5 |
| code + checkpoint slow-tests | 6 | 446 | 489 | 23.7 |
| **docs only** | **3** | **421** | 427 | 11.3 |
| code + docs | 3 | 396 | 426 | 21.9 |
| code only | 21 | **350** | 369 | 15.6 |
| design/md only | 5 | **101** | 109 | 4.7 |

**8** of the 39 successful Build runs rendered docs; the 6 in the two
docs-named rows above are the slowest normal class at 396–421s against 350s
code-only (the other two are classed by their heavier component — one
sandbox-tools push at 991s, one checkpoint push at 421s). A push costs **18.2 runner-minutes** across the four PR
workflows the snapshot sees (median over 55 pushes, against 19.1 last window),
or ~18.8 counting its `Suppressions comment` and `PR Gate` runs.

### Critical path

Binding (last-finishing) job across the 39 successful Build runs:

| binding job | runs | wall median | median margin over runner-up |
|---|---:|---:|---:|
| `test (3.10)` | 13 | 351s | 13s |
| `test (3.11)` | 12 | 348s | 18s |
| **`docs`** | **5** | **421s** | **303s** |
| `mypy (3.10)` | 5 | 101s | 11s |
| `slow-tests (checkpoint)` | 3 | 475s | 140s |
| `slow-tool-tests-dev` | 1 | 991s | 560s |

The `test` legs still bind most runs (25 of 39) and still by a margin (13–18s)
too small for any per-leg saving to pay off on both. The two wide-margin rows are
the ones worth attention. `docs`'s 303s margin is a consequence of #299 — on the
three docs-only pushes the `test` legs finish in 4s, so the runner-up is `mypy`
at ~118s and `docs` runs alone for five minutes; the fix this run ships takes
~30s off exactly those runs. `slow-tests` binds by 140s on the two checkpoint
branches, unchanged in cause from last window (proposal 3) but on a quarter of
the sample. The five `mypy (3.10)` bindings are the design/md-only pushes.

### Queue

651 independent-job samples: median **3s**, p90 **8s**, p95 17s, p99 40s, **max
71s**, and **6 waits over 60 seconds** (was 5). The longest is a `detect-slow` at
71s; the next four are the four Viewer jobs of a single run at 65–67s. Peak
concurrency across the whole snapshot is **19 simultaneous jobs**, against 35 on
the burst evening of 2026-09-03, and the densest 16-minute stretch holds 24 runs
against that evening's 47. So the pool was never asked the question that
produced proposal 6, and this window is again *absence of the trigger* rather
than evidence about the pool.

## Where the pytest step actually goes

Timestamps from the raw job log of `test (3.10)` in upstream run 34019435477
(the newest leg the collector mined):

| phase | seconds | prev | note |
|---|---:|---:|---|
| `uv run` project re-sync | 5.4 | 5.4 | still on `main`; fixed on this branch, unmerged |
| startup + collection (5 interpreters) | 55.9 | 55.7 | assertion rewriting; see proposal 5 |
| test execution | 246.5 | 244.8 | 896.9 worker-seconds over 4 workers |
| reporting (durations, summary) | 0.2 | 0.3 | `-ra`, holding |
| **step total** | **308.0** | 306.3 | window median 307 (3.10) |

The raw log is **5,880 lines** (was 5,899). The re-sync line is still
`Uninstalled 51 packages … Installed 54 packages`.

## Worker balance (`--dist worksteal`, #4948)

Eleventh window holding, from both report-log artifacts of run 34019435477:
per-worker test seconds **222.6 / 218.5 / 222.1 / 233.7** on 3.10 (imbalance
+9.5s, efficiency 95.9%) and **220.1 / 218.4 / 218.5 / 223.0** on 3.11 (+3.0s,
98.7%). No stragglers on either leg.

## Slowest tests

Median seconds per test across the 14 legs mined this window
(`--durations=50 --durations-min=1`, `call` + `setup` + `teardown` summed). 127
tests captured; per-leg tail total **215.0s** (median; 180.1–226.4), against
217.2s last window.

| s | test | classification |
|---:|---|---|
| 11.2 | `test_eval_set_selection.py::test_eval_set_selection_concurrent_workers` | genuinely heavy — three real subprocesses |
| 10.5 | `test_eval_set_scanner.py::test_scout_scan_resume_reruns_failed_scans` | genuinely heavy |
| 9.9 | `test_eval_set.py::test_retry_attempt_killed_mid_sweep_leaves_completed_samples_reusable` | genuinely heavy — kills a live attempt mid-sweep |
| 9.7 | `_control/test_launch_handoff.py::test_eval_detach_hands_off_and_leaves_eval_running` | subprocess launch; pays `import inspect_ai` (#311) |
| 9.6 | `_control/test_launch_handoff.py::test_eval_detach_via_dotenv_detaches_exactly_once` | subprocess launch (#311) |
| 7.9 | `_control/test_launch_handoff.py::test_eval_json_redirects_subprocess_stdout_to_stderr` | subprocess launch (#311) |
| 6.9 | `agent/test_agent_bridge.py::test_google_bridge_computer_use_incompatible_model` | ~3.9s is `traceback_ansi` rendering (#374) |
| 6.7 | `model/test_parse_tool_call.py::test_parse_error_on_deeply_nested_yaml_arguments` | guards a real uninterruptible hang (#5095); the cost is the point |
| 6.2 | `test_eval_set_scanner.py::test_scanner_resume_accumulates_summary_…[s3]` | moto S3 + full eval-set resume |
| 6.2 | `agent/test_agent_bridge.py::test_google_bridge_streaming_not_supported` | ~3.9s is `traceback_ansi` rendering (#374) |
| 6.1 | `test_eval_set.py::test_eval_set_previous_task_args` | ~5s real sleep around `keyboard_interrupt(2)` |
| 5.8 | `log/test_eval_log_config.py::test_eval_log_run_config_round_trip` | genuinely heavy |
| 5.6 | `_control/test_launch_handoff.py::test_eval_detach_fails_when_control_bind_fails` | subprocess launch (#311) |
| 4.3 | `_control/test_pause.py::test_eval_hard_pause_time_limit_reap_reparks_grader` | timer-bound |
| 4.3 | `test_retry.py::test_eval_retry` | genuinely heavy |

Heaviest files by total worker time in the 3.10 report log (not just the tail):
`test_eval_set.py` 63.0s, `test_eval_set_scanner.py` 60.6s,
`_control/test_launch_handoff.py` 49.2s, `_control/test_eval_set_integration.py`
44.5s, `test_sample_limits.py` 28.3s, `test_eval_set_selection.py` 21.3s,
`scorer/test_model_graded.py` 20.4s, `agent/test_agent_bridge.py` 20.1s.

### No per-test regression

Diffing per-test medians against the 2026-09-05 snapshot over the **106** tests
in both tails: sum **364.8 → 353.6s**, largest increase **+1.3s**
(`test_task_retry_detaches_superseded_attempt_live`), largest decrease **−1.3s**
(`test_eval_config_overrides_do_not_mutate_reused_task`). 21 tests entered the
tail and 42 left it, all near the 1s cutoff — ranking-boundary churn.

### Docker-trap sweep

Unchanged for nine runs: **6** test functions pair `skip_if_no_docker` with no
`@pytest.mark.slow` — `util/sandbox/test_docker_compose_config.py` ×3 (ungated,
never start a container), `tools/test_think_tool.py` ×2 and
`agent/test_agent_docs.py::test_agent_collect` (gated by `skip_if_no_anthropic` /
`skip_if_no_openai`). No new offenders.

## Suite size

| snapshot | collected items | pytest wall (median leg) | Build wall (success) |
|---|---:|---:|---:|
| 2026-08-27 | 14,123 | 328.7 | 390.0 |
| 2026-08-29 | 14,674 | 287.1 | 342.0 |
| 2026-08-31 | 14,950 | 290.3 | 338.0 |
| 2026-09-01 | 15,220 | 289.6 | 346.0 |
| 2026-09-03 | 15,895 | 300.4 | 370.0 |
| 2026-09-05 | 16,415 | 302.9 | 360.0 |
| 2026-09-07 | **16,416** | **300.1** | **351.0** |

Both pytest columns are the **median mined leg** of each snapshot, as in every
prior report (this snapshot's 14 legs span 16,409–16,428 collected items).

**+1 collected item in two days.** Top-level test functions on `main`
(`^(async )?def test_` under `tests/`, re-derived per date; `main` has not moved
since 2026-09-06 07:20Z):

| date | test functions | Δ |
|---|---:|---|
| 2026-09-03 | 9,054 | — |
| 2026-09-04 | 9,151 | +97 (1d) |
| 2026-09-05 | 9,240 | +89 (1d) |
| 2026-09-06 | 9,246 | +6 (1d) |
| 2026-09-07 | 9,246 | 0 (1d) |

**+6 test functions between this report and the last**, against +184 and +391.
That is the weekend, not a policy change: measured across this window's own span
(which starts 2026-09-04 14:49) growth is **+95**, essentially all of it on the
Friday. The same caveat applies to the collected-items column, whose two
snapshots share only one mined leg (run 33936100151) — the +1 is measured over
adjacent, nearly disjoint leg sets about 1.3 days apart.

### Where the time sits (3.10 report log, 16,428 tests, 896.9 worker-seconds)

| band | tests | worker-s | share | prev share |
|---|---:|---:|---:|---:|
| ≥5s | 14 | 112.6 | 12.6% | 11.6% |
| 1–5s | 158 | 269.9 | 30.1% | 31.7% |
| **0.1–1s** | **1,155** | **416.4** | **46.4%** | 46.1% |
| <0.1s | 15,101 | 98.0 | 10.9% | 10.6% |

Phases: call 835.0s, setup 39.1s, teardown 22.9s. 11,521 passed, 4,907 skipped —
30% of collected items never run in the PR gate. Worker time **875.5 → 896.9s**
(3.10) against a suite that grew by 1, which is noise inside the ±60s band; the 3.11
leg of the same run is 880.0s (was 953.0s), which is the same noise in the other
direction. The shape is stable across seven windows: the ~92% of tests under 0.1s
are ~11% of the time, and the 0.1–1s band is just under half.

### Duplicate-coverage and low-value sampling

The strict AST sweep (identical decorators + signature + body) finds the same
**3 groups / 7 tests** as last window, all coincidental: the same one-line
assertion in three provider `test_known_models_not_latest` files, two CLI
`test_omitted_returns_none` flag tests, and a pair of nested `async def test_func`
helpers pytest never collects. Nothing worth deleting. With +6 test functions in
the window there was nothing new to sample.

## Regressions since last report

**No per-job execution regression.** Every job median is within 4s of last window
apart from two small-sample slow-test job classes. Test worker time and the
matched per-test tail both moved inside noise (see above), and the Viewer p90
rises of last window have fully reverted.

**No per-test regression** — largest matched increase +1.3s over 106 tests.

Red checks a contributor actually sees — **6 failed job records in 200 runs**
(was 3): `slow-tool-tests-release` 2 (the published-binary gate failing fast at
37–40s, by design), `entries-under-unreleased` 2, `submodule-on-main` 1, and
**`test (3.10)` 1 — which is not a test failure.** Its suite passed; the job died
in `Upload test order log` with
`Failed to FinalizeArtifact: … (403) Forbidden: Error from intermediary`, so a
transient artifact-service error took down a required check on a green PR and
cost the contributor a 343s re-run. Checking the four other `test`-leg failures
in the snapshots since 2026-08-29 (jobs 98822279296, 99634284444, 99625902383,
100488661057), all four were genuine `exit code 1` from pytest — so this is a
first observation, not a pattern (proposal 12).

**Six more runs concluded `failure` with no jobs at all, and prior reports have
been counting only job records.** All six are approval-gate runs on two
first-contributor PRs from the same author: all three PR workflows on
`docs/doubled-word-typos` (PR 5253, `run_started_at` 18:22:14Z,
`updated_at` 19:13:54Z) and on `docs/fix-design-doc-anchors` (PR 5252,
18:22:05Z → next-day 19:33:15Z). Each run's `updated_at` is **one second after
its PR closed or merged**: a run held at `action_required` is marked `failure`
when the PR terminates, with zero jobs and zero runner time. So the honest
contributor-visible red-check count for this window is **6 failed jobs plus 6
zero-job run failures**, and the same omission inflates last window's "3 …
cleanest window in the series" — that comparison should be read as
job-records-only on both sides. This is proposal 13's phenomenon, not a CI
defect: `docs/fix-design-doc-anchors` waited **25.2h** for approval, then ran
green in ~2 minutes and merged.

## Waste

- **Every `docs` job rebuilds `quarto-cli` from sdist: 43.3s × 8 jobs = 5.8
  runner-minutes this window, and it has been true for every `docs` job ever
  run.** Cause and fix in the Summary and proposal 1. Worth stating plainly why
  the existing cache does not help: the job *does* get a cache hit (25MB, 1.9s)
  and *does* prune correctly, and the post-job log then says `Cache hit occurred
  on key …, not saving cache` — the one job whose cache contents are worth
  keeping is the one job that can never write them.
- **Duplicated Quarto renders: 2, both concurrent.** `docs-render-e4f9e3bd`
  exists under `refs/pull/5250/merge` (19:40:41Z) and `refs/heads/main`
  (19:41:58Z); `docs-render-b156e253` under `refs/pull/5204/merge` (19:43:31Z) and
  `refs/heads/main` (19:48:36Z). Both are docs-only PRs, so their
  `src/inspect_ai` delta is empty and their key equals `main`'s — and GitHub
  cache scoping forbids `refs/heads/main` from reading a PR-scoped entry. But
  both pairs *overlap in time* (the merge build started before the PR's own
  render finished), so no cache design could have deduplicated them. ~11
  runner-minutes, down from ~165 two windows ago.
- **Cancelled superseded jobs: 16.0 runner-minutes** across 13 jobs / 5
  cancelled runs (was 16.0 over 13 jobs / 4 runs).
- **Failed jobs: 7.3 runner-minutes** over 6 jobs (was 0.4 over 3), 5.7 of which
  is the false-red `test (3.10)` above.
- **Runs that never ran: 29 `action_required` plus 6 zero-job `failure`s** (was
  15 and uncounted), across 10 branches — first-time-contributor approval gates.
  No runner time, but those contributors got no feedback at all until a
  maintainer approved, up to **25.2h** in one case, and the 6 that expired at
  PR close/merge showed up as red (proposal 13).
- **`uv run` re-sync: 5.4s per `test` leg, ~3.5s per `mypy` leg**, plus the
  `slow-tests`, `slow-tool-tests-*` and `docs` sites — still on `main`, fixed for
  `test`/`mypy` on this branch (proposal 4).
- **`Suppressions comment` full-history checkout: 25.2 runner-minutes** (56 runs
  × 27.0s for a job whose work is 1.0s) — fixed on this branch, unmerged.
- **Compute: 844 runner-minutes** per 200 PR runs, latest attempt only (Build
  702, Viewer 127,
  Suppressions 12, Changelog Lint 3), plus **212 runner-minutes** the collector
  never fetches — 11 push-event Build runs (145.8), 56 `Suppressions comment`
  runs (30.5), 11 push Viewer runs (30.4), 11 push Suppressions runs (3.3), 22
  `PR Gate` runs (2.4) and 2 `Stale PRs` (0.2). **1,057 runner-minutes over
  66.5h** (~16/hour, against ~69/hour last window — the weekend, not an
  efficiency gain).
- **Overhead-dominated jobs:** `changes` 7s, `entries-under-unreleased` 7s,
  `detect-slow` 8s, `submodule-on-main` 8s, `ruff` 10s, `suppressions` 16s — ~56s
  of runner time per push, none of it on the critical path.

## Impact verification (previous runs' changes)

- **#5220 / #317 (docs render cache key) — mechanism confirmed, but no snapshot
  has yet contained a hit; the duplicate-render waste is closed.** This window: 8
  `docs` jobs, `Render docs` ran in all 8, **0 hits**. Every miss is a genuine
  input change rather than a key-design failure. Branches 4895, 4896 and 5234
  each rendered once in this window under a *different* key from their previous
  render, and 5250 rendered twice in-window under two different keys — i.e. their
  docs or their `src/inspect_ai` delta moved between pushes, which is exactly
  when a re-render is correct. The two hits recorded last window (PRs 5233 and 5236)
  remain the only ones ever observed, and both fall in the 10h between the merge
  and the previous window's start — outside either snapshot. So across the 15
  `docs` jobs the two snapshots *do* cover, the observed rate is **0**, and
  #5220's own "roughly 23%, not 100%" prediction is still untested rather than
  refuted: a 23% rate over 15 jobs would have produced 3–4 hits, so this is
  evidence against 23% but not yet against the mechanism, which the cache index
  shows working. The duplicate-key half is measurably done: **2
  of 19** post-merge keys exist under both a PR ref and `main` (was 36 of 129),
  both are docs-only PRs whose empty source delta makes their key equal `main`'s,
  and both pairs are concurrent. Remaining exposure is ~11 runner-minutes per
  weekend window, and it is a cache-scoping rule, not a key bug.
- **This branch's own two fixes are still unmerged upstream, and both baselines
  re-confirmed today.** The `uv sync` + `uv run --no-sync` change: run
  34019435477's `test (3.10)` still shows `Uninstalled 51 packages … Installed 54
  packages`, 5.4s. The `Suppressions comment` `blob:none` checkout: 56 runs at
  31.0s median with 27.0s of `actions/checkout`. Neither can move until PR #408
  merges, and the second is a `pull_request_target` workflow that always runs the
  *base* repo's copy, so it cannot be exercised by the PR carrying it.
- **#5075 (`-rA` → `-ra`) — holding, seventh window.** Reporting phase 0.2s, raw
  test-leg log 5,880 lines.
- **#4948 (`--dist worksteal`) — holding, eleventh window** (+9.5s / 95.9% on
  3.10, +3.0s / 98.7% on 3.11, no stragglers).
- **#4760 (`test_package` pre-installed) — holding**: no `test_extensions` test
  appears anywhere in the durations tail and no mid-run install appears in the raw
  `test (3.10)` log.
- **#4935 (`blob:none` checkouts) — holding.** Checkout is 2–6s median in every
  Build and Viewer job. The `test`-leg checkout is again the one always-run step
  whose p90 exceeds 2× its median (0s median, because it is split across two
  mutually-exclusive gated steps, against a 6.7s p90) — 7s of absolute spread,
  not worth chasing.
- **#299 (`design/**` excluded from the test filter) — holding**, 5 observations:
  `test` legs 4–7s each, Build wall **100–114s** against a 350s code-only median,
  4.7 Build runner-min per push, `mypy` binding all five.
- **#393 (control-server startup), #374 (traceback rendering), #311
  (`acp.schema` import), #444 (serial `slow-tests`) — filed, no action yet.** All
  four remain maintainer decisions.
- **The 2026-09-03 burst (proposal 6) — no recurrence, and no re-test.** Peak
  concurrency 19 jobs, densest 16-minute stretch 24 runs.

## Proposals (ranked)

1. **Give the `docs` job its own uv cache scope.** NEW, and **shipped in this
   PR**. `astral-sh/setup-uv`'s cache key is
   `setup-uv-<v>-<arch>-<os>-<python>-<hash of 13 dependency files>`, identical
   for every 3.11 job in `build.yml`. `mypy (3.11)` finishes first at ~90s and
   saves a 25MB pruned dev cache under it; `docs`, 300s later, restores that
   25MB, finds none of its own dependency set in it, and its post-job step logs
   `Cache hit occurred on key …, not saving cache`. `quarto-cli` has **no wheel
   on PyPI** (checked: every release from 1.8.25 to 1.10.18 is sdist-only, a
   4.7KB sdist that downloads the ~150MB quarto release at build time), so the
   job pays a source build every single run.

   Measured, from upstream job 101364042001 (`docs`, run 33987585533):

   | log line | timestamp | Δ |
   |---|---|---:|
   | `Building quarto-cli==1.10.18` | 19:37:11.85 | |
   | `Built quarto-cli==1.10.18` | 19:37:55.17 | **43.3s** |
   | `Prepared 160 packages in 44.78s` | 19:37:56.02 | |
   | step `Install dependencies` total | | **48.8s** |

   And reproduced end to end in this sandbox, which is the whole argument for the
   fix — `uv cache prune --ci` keeps a wheel built from an sdist *precisely* so
   this is cacheable:

   | arm | build line present? | total |
   |---|---|---:|
   | empty uv cache | `Building quarto-cli==1.10.18` | **28.34s** |
   | after `uv cache prune --ci` (207MB, 165MB compressed) | *absent* | **4.77s** |

   with `quarto --version` → `1.10.18` in the second arm, so nothing is lost.
   The fix is one input: `cache-suffix: docs`, with the dependency glob left at
   its default. Two fresh-context review passes were needed to get there, and
   both corrections are worth recording because they generalise to any future
   `cache-suffix` change.

   *The first draft narrowed the glob* to `requirements-doc.txt` +
   `pyproject.toml`, on the cache-footprint grounds below. *The second draft
   justified keeping the default glob by claiming it hashes `uv.lock`, which
   pins the resolved quarto version.* Both are wrong in the same way: **this job
   does not resolve from `uv.lock` at all.** `uv pip install -r
   requirements-doc.txt .` resolves fresh against PyPI, honouring
   `quarto-cli>=1.9.36`, so the key — under any glob — hashes the *request*, not
   the *resolution*. When quarto publishes 1.10.19, the key is unchanged, the
   restore is an exact hit, uv builds the new sdist (43s), and setup-uv skips the
   save because it hit. **This is a real residual the fix does not close**, and
   the only difference the glob makes is how long it lasts: under the default
   glob any of 13 dependency files rotates the hash (13 distinct hashes in the
   last 7 days, so ~weekly self-healing); under the narrow one it could persist
   indefinitely, since re-reading the entry every run also keeps it from being
   LRU-evicted. Hence the default. Closing the residual properly means pinning
   `quarto-cli==` in `requirements-doc.txt` — a dependency-policy change, not
   cache tuning, so it is not bundled here.

   *One concern raised and refuted*: a cancelled `docs` job skips
   `Minimize uv cache` (gated on `!cancelled()`) but would still run setup-uv's
   post step, persisting an unpruned multi-hundred-MB cache under the new
   docs-only key that later exact hits could never replace. `setup-uv@v10.0.1`
   declares `post-if: success()`, so the save does not run on a cancelled or
   failed job — every entry it writes is the pruned one.

   Est. impact, stated as a range because the hit rate is the uncertain term.
   On a hit: `Install dependencies` **49.5 → ~8–12s**, less ~9s for the larger
   restore, i.e. **−25 to −35s of `docs` exec** (396 → ~365s) and the same off
   Build wall on the runs `docs` binds — 5 of 39 this window. On a miss: the
   43s build is unchanged and the job pays **~15s more** to save. So the sign of
   the change pays as long as roughly **one docs job in three hits** (30s saved
   against 15s spent breaks even at a 33% hit rate). The reason to expect at
   least that: `docs` also runs on pushes to `main` (3 `main`-ref renders in
   the post-#5220 cache index), and a cache written on the default branch is
   readable by *every* PR branch, so one `main` docs render warms the entry for
   all open PRs that have not touched a hashed dependency file — which is most
   of them. Against it: `docs` is gated on `docs/**` changes, so the entry is
   warmed far less often than the shared key it replaces. **Next run should
   measure the hit rate directly rather than assume it** — the evidence is
   `Install dependencies` step times in the `docs` job records, which the
   snapshot already carries. Footprint accepted: ~165MB per *missing* docs job
   (worst case 8 × 165MB ≈ 1.3GB/window) against a repo at 2.69GB of a 10GB
   LRU budget; the `docs-render-*` markers #5220 depends on are 178 bytes each
   and are re-read every docs job, so they are not plausible eviction victims.
   Disruption: safe fix (cache tuning, one job, one input).
   Status: **new, shipped in this PR**.

2. **Stop paying 214ms of control-server startup on every `eval()`.** Carried,
   unchanged, and still the largest measured lever in this series. A one-sample
   `mockllm` eval is 249ms with the control channel and 35ms without; the 214ms
   splits 30ms building an identical 28-route FastAPI app, 30ms binding, and
   100ms waiting out uvicorn's fixed 0.1s `should_exit` poll. 796 test functions
   across 153 files call `eval()`/`eval_set()` directly, and a full-suite A/B on
   4 workers runs **725.8 → 529.0s of wall (−27%)** with **381 tests leaving the
   0.1–1s band** — still 46.4% of CI test time this window. Extrapolated to a CI
   leg (246.5s of execution over 4 workers, 897 worker-seconds), on the order of
   **50–60s off both `test` legs**, the only lever large enough to clear the
   13–18s binding margin on both at once. Not shipped: the test-side fix moves
   coverage of the *default* configuration out of the bulk of the suite, and the
   product-side fix changes eval teardown semantics. Status: carried,
   [#393](https://github.com/meridianlabs-ai/inspect_ai/issues/393).

3. **`slow-tests` runs its Docker checkpoint tests serially.** Carried. Six runs
   this window (was 16) at **393s median / 475s p90**, binding **3 of 39**
   successful Build runs by a **140s median margin**. Cause unchanged: the job
   runs `uv run pytest --runslow -m slow tests/<area>/` with no `-n`, so 20 tests
   execute one at a time on a 4-vCPU runner. The 2026-09-05 measurement stands —
   serial 446.2s against `-n 4` 224.2s, but with **4 failures**, every one a test
   asserting on *global* Docker state (leaked-container and container-count
   assertions that see a concurrent test's containers). Parallelising therefore
   requires scoping those assertions to the containers a test owns first: a
   product-adjacent test change with real correctness content, not workflow
   hygiene. Status: carried,
   [#444](https://github.com/meridianlabs-ai/inspect_ai/issues/444).

4. **`uv run` re-syncs the environment the install step just built.** Carried;
   the `test`/`mypy` half is committed on this branch but unmerged, so `main`
   still pays 5.4s per `test` leg and ~3.5s per `mypy` leg (re-measured today).
   The remainder is unchanged and deliberately unshipped: for `docs` and
   `sandbox-tools-unit` dropping the sync would change *which dependency versions
   the job tests against* (14 packages differ for the injectable, including the
   `mcp 2.1.1 → 2.0.0` that
   [#308](https://github.com/meridianlabs-ai/inspect_ai/issues/308) was filed
   about), and `slow-tool-tests-release` *depends* on the sync replacing its wheel
   install with the lockfile's editable one so `_binaries_dir()` resolves to the
   tree the published binaries were downloaded into. One new detail for the docs
   case: its render step's `uv run` re-sync costs 4.8s and uninstalls 16 of the
   161 packages the install step just placed, so the two steps disagree about the
   environment by construction — but resolving that disagreement is a decision
   about what the docs job should validate against, not hygiene.
   Status: carried, half shipped, **awaiting promotion**.

5. **Cache pytest's assertion-rewrite bytecode across runs.** Carried from last
   window as "next in line to ship once measured" — and this run **declines it**,
   on two grounds that did not need a CI experiment.

   *The prize is a third of what the header number suggests.* Startup+collection
   is 55.9s of the 308.0s step, but only the assertion-rewriting part is
   cacheable, and that was measured on 2026-08-29 at ~18–20s (cold collection
   32.6s against 14.9s under `--assert=plain`, and 12.7s warm). So the ceiling on
   a perfect hit is ~20s per leg, not ~30s.

   *The safe form of the fix cannot hit, and the form that can hit is not safe.*
   pytest validates a rewritten `.pyc` against the source's **mtime + size**, and
   writes its own `*-pytest-9.1.1.pyc` rather than a PEP 552 hash-based pyc. Any
   scheme that normalizes mtimes so a *partial* restore can serve unchanged files
   will also serve a **stale rewritten pyc** for a file whose content changed and
   whose size happens to match — silently running different assertions than the
   source says, in the PR gate. The only variant that is safe by construction is
   a strict key over the exact content of every `.py` file, with no
   `restore-keys` fallback. But then a hit requires `src/**/*.py` and
   `tests/**/*.py` to be byte-identical to a previous run, which for a code push
   never happens, and for a push that touches no Python at all means either a
   docs/design-only push (where `test` already no-ops to 4s under #299) or the
   rare workflow/config-only push. Expected value on the runs that would benefit
   is approximately zero.
   Status: **declined** (was "next in line to ship"). What does not work, both
   measured on 2026-08-29: `compileall` and `uv`'s `compile-bytecode`.

6. **Burst contention costs measurable wall clock — unresolved, not disproved.**
   The single 2026-09-03 observation still stands alone: 47 runs across 6 branches
   in 16 minutes cost each of them **+68s of Build wall (350 → 418s) with test
   execution unchanged**. Two windows have now passed without a comparable burst
   — this one peaked at 19 concurrent jobs and 24 runs per 16 minutes — so
   nothing has re-tested it. What the data still does *not* support is buying
   runners; concurrency reached 35 on the burst evening, so the pool ramps. The
   lever, if one is wanted, is jobs-per-push: a push produces 13–21 job records
   across four PR workflows plus `Suppressions comment` and `PR Gate`.
   Status: carried, **awaiting a second observation** (two windows and counting).

7. **Defer the `acp.schema` import.** Carried, not re-measured this window (the
   absolute sandbox numbers move with load and the share has been stable at 26%
   across three measurements). `import inspect_ai` is ~1.15s, of which
   `acp.schema` is ~300ms of self-time — 6.5× the next-largest module — reached
   eagerly through `inspect_ai._eval.eval` → `agent._acp.server`. Paid by 5
   interpreters per leg plus the four `_control/test_launch_handoff.py` tests that
   hold slots 4, 5, 6 and 13 of this window's tail. Product change with a
   public-API surface. Status: carried,
   [#311](https://github.com/meridianlabs-ai/inspect_ai/issues/311).

8. **Test-volume policy — it is the 0.1–1s band that matters.** Seventh window
   confirming it: 15,101 tests under 0.1s are 10.9% of test time; 1,155 tests
   between 0.1s and 1s are 46.4%. This window adds a control rather than new
   evidence: growth was **+6 test functions** and measured worker time moved
   +21s on 3.10 and −73s on 3.11, so the two-day noise band is wider than two
   days of normal growth. Proposal 2 remains the sharp form of the question:
   much of that band is not what the tests *assert*, it is what `eval()` *costs*.
   Structural. Status: carried.

9. **Collector: fetch a time window, not a run count — and retry on a suspected
   stale page.** Carried, and this window inverted the symptom in a way that
   sharpens the fix. (a) At 3.0 runs/hour a 200-run snapshot spans **66.5h** and
   *overlaps* the previous window by 10.6h, where at 11.3 runs/hour it spanned
   17.6h and left a 22.6h hole. A run count cannot track a 2-day cadence through
   a 4× throughput swing; a `--since` window can. (b) `warn_on_time_gap`'s fixed
   12h threshold **false-positived for the first time** — it flagged the 14.2h
   Sunday lull as a stale page, and only a direct API query over that range
   (which returned zero PR runs) distinguished the two. So the fix is not a
   bigger threshold but a retry-and-compare: re-fetch page 1 and check whether
   the newest run moved. (c) The `event=pull_request` filter still hides **212
   runner-minutes over 113 runs** this window, including the `Suppressions
   comment` job whose fix is on this branch and which therefore has to be
   measured by hand every run. All three fixes are one file,
   `.claude/skills/ci-perf/scripts/collect_ci_data.py`, **still unwritable** —
   re-probed today, refusal unchanged (proposal 10).
   Status: carried, re-framed.

10. **Unblock the scheduled run — one blocker left, and it is the important
    one.** Re-probed today:
    - *`workflow` scope* — **CLEARED** and exercised again: proposal 1 is a
      `.github/workflows/build.yml` change pushed by this run's token.
    - *No upstream write* — still blocked. `repos/UKGovernmentBEIS/inspect_ai`
      reports `push: false` for this token; PR creation attempted at the end of
      this run (result recorded in `prs.md`). This is the blocker that matters:
      every report since 2026-08-21 has had to be promoted upstream by hand.
    - *`.claude/**` unwritable by the agent's edit tooling* — still blocked. The
      filesystem permits it (`test -w` passes); a one-token edit to
      `.claude/skills/ci-perf/scripts/collect_ci_data.py` was refused as a
      "sensitive file". So it remains a harness permission-policy change, not a
      token one. Blocks proposal 9.

    Status: carried, updated on
    [#298](https://github.com/meridianlabs-ai/inspect_ai/issues/298).

11. **Pin or cache `pnpm/action-setup`'s pnpm download.** Carried at one
    observation and **no recurrence**: the step is back to a 5–6s median with an
    8–10s p90 across all three Viewer jobs, and the Viewer wall p90 has reverted
    129 → 84s. One incident in two windows is a registry incident, not a
    workflow defect. Status: carried, report-only, and due to be dropped if a
    third window is clean.

12. **A transient artifact upload takes down a required check.** NEW,
    report-only on one observation. `test (3.10)` on
    `fix/math-symbol-assumptions` (job 101598457874) ran the full suite to a
    clean `short test summary info` and then failed in `Upload test order log`
    with `Failed to FinalizeArtifact: … (403) Forbidden: Error from
    intermediary`. The artifact is purely diagnostic — the `--report-log` output
    that `scripts/pytest_bisect.py` reads offline — but the step carries no
    `continue-on-error`, so a GitHub artifact-service blip turns a passing PR red
    and costs the contributor a 343s re-run. The one-line fix
    (`continue-on-error: true` on that step) is deliberately **not** shipped
    here: it is one observation (the four other `test`-leg failures in the last
    five snapshots were all genuine `exit code 1` from pytest), and choosing to
    let a diagnostic upload fail silently is a maintainer's call about what a
    required check should assert, not workflow hygiene. Worth watching for a
    second occurrence. Status: **new**, report-only.

13. **First-contributor approval gates are the longest feedback wait in the
    data.** NEW, report-only, and outside CI's control. **29 of 200 runs (14.5%)
    ended `action_required`**, across 8 branches at 3–4 runs each — up from 15
    last window. Those pushes consumed no runner time and produced no signal:
    the contributor saw nothing until a maintainer approved the workflow run, and
    three of the eight branches were pushing over the weekend. A further **6
    runs** (two more first-contributor PRs, 5252 and 5253) sat at
    `action_required` and were then marked **`failure` with zero jobs** the
    second their PR closed or merged — so the gate also manufactures red checks.
    The sharpest number in the window is PR 5252: a docs-only change that waited
    **25.2h** for approval, then ran green in ~2 minutes and merged. Measured
    against this report's primary metric (push to all-checks-green), the
    approval gate is now by far the largest single term for the contributors it
    applies to — an order of magnitude above the 350s Build wall everything else
    in this report is trying to shave. No fix belongs in a workflow file, which
    is why this is recorded rather than proposed.
    Status: **new**, report-only.

14. **Merge the 4 Viewer jobs into 1–2** — required-check rename. Same standing
    argument; the three pnpm-using jobs each spend ~5s on `pnpm/action-setup` and
    ~5s on `setup-node` independently, ~30s of duplicated toolchain setup per
    push. Structural. Status: carried, low.

15. **Duplicate and near-duplicate test cleanups.** The strict AST sweep is
    unchanged at 3 groups / 7 tests, all coincidental one-liners with no cleanup
    value. The judgement-based candidates are unchanged: `test_sample_shuffle`
    (4.1s) runs the full `popularity()` dataset twice to assert the property
    `test_sample_shuffle_limit` already asserts on 20 samples, and
    `test_eval_set_previous_task_args` (6.1s) is ~5s of real sleep. Both are
    coverage judgements, not safe fixes. Status: carried, low.

16. **`tests/util/test_display_counter.py` sleeps 6 × 1.1s for 2 throttle
    paths.** Carried, and out of the mined tail this window (no phase of it
    cleared the 1s `--durations-min` cutoff in these legs). Re-examined for a
    mock-clock fix on 2026-09-01 and rejected: `inspect_ai.util._throttle` reads
    `time.time()` directly *and* schedules a real `anyio.sleep(remaining)`
    trailing-edge fire in a background task, so faking the clock without also
    faking the sleep changes what the test exercises. The honest options remain a
    coverage judgement (drop the sleep for the params whose `@throttle(5)` a 1.1s
    sleep can never fire) or an injectable throttle window (product change).
    Status: carried.

17. **Policy consistency: docker tests without `@pytest.mark.slow`.** Still six,
    still ~0.05s combined; the right fix is probably to drop `skip_if_no_docker`
    from the three ungated ones rather than to mark them slow. Zero wall-clock
    impact. Status: carried.

Nothing dropped this report. Last report's proposal 2 (assertion-rewrite
bytecode cache) is **declined** rather than dropped — see proposal 5 for the
reasoning, which is the substantive analytical result of this run alongside the
`docs` cache finding.

## PRs opened by this skill

See `prs.md`. The previous run's PR
([meridianlabs-ai/inspect_ai#408](https://github.com/meridianlabs-ai/inspect_ai/pull/408))
is still open and green, so this run pushes onto its branch rather than opening a
second PR, per the unattended rule. It adds the 2026-09-07 snapshot, this report,
the ledger row, and one safe fix (`cache-suffix: docs` on the `docs` job's
`setup-uv`), on top of the two fixes and two reports already on the branch.
