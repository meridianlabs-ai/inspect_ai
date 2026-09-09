# CI performance report — 2026-09-09

Data: 200 PR runs, 2026-09-08 12:46 .. 2026-09-09 09:02 UTC (**20.3h**, 9.9
runs/hour, 53 pushes). Snapshot: `history/2026-09-09.json`. Previous: 2026-09-07
(200 runs over 66.5h, ending 09:17 UTC). The two windows leave a **27.5h hole**
(09-07 09:17 → 09-08 12:46) — the mirror image of last window's 10.6h overlap,
and the same cause: a fixed 200-run limit against a throughput that swung 3.0 →
9.9 runs/hour when the working week resumed (proposal 12). The largest gap
*inside* this window is 6.5h (the 09-09 01:21 → 07:52 overnight lull) and the
collector raised no stale-page warning. Produced by the unattended scheduled run
([workflow run](https://github.com/meridianlabs-ai/actions/actions/runs/34333735299)).

## Summary

**One test file is now 10.8% of the PR gate, and this run ships the fix.**
`tests/checkpoint/test_sandbox_egress_restic.py` arrived with
[#5248](https://github.com/UKGovernmentBEIS/inspect_ai/pull/5248) and drives
`egress_sandbox` against a **real restic binary** — `resolve_restic()` downloads
it on first use, and each of its 24 tests pays a real `restic init` plus real
backup/restore invocations. From the `--report-log` artifacts of run
34332424502: **116.4s of test worker time on both legs** (of 1079.0s on 3.10 and
1100.9s on 3.11), against **66.7s for the next-largest file**, and 120.05s of
wall on its own in a single process. It carries no `@pytest.mark.slow`, while its
sibling `tests/checkpoint/test_restore_repo.py` — same directory, same real
binary, same subsystem — does. Marking it moves the coverage to the `slow-tests`
checkpoint leg, which #5293 has just made parallel, and to the ~2h scheduled
suite. Predicted: **~30s off each `test` leg** (proposal 3).

**The runner ceiling is 20 concurrent jobs, and that is why bursts hurt.**
Proposal 6 has been "unresolved, awaiting a second observation" for three
windows. It got one: at 00:50–02:00 UTC **8 branches pushed 32 runs**, and queue
went from a 4s median (p90 24s) to a **47.5s median (p90 171s, max 214s)** while
code-only Build wall went **361 → 472s (+111s)**. The mechanism is no longer a
guess. Counting *every* workflow event in that window rather than only
`pull_request`, concurrency **peaked at exactly 20**, sat at 20 for 158s and at
19–20 for 278s, and never reached 21 — and peak concurrency across the last four
snapshots is **19, 19, 19, 20**. That is a hard account-level ceiling, not a pool
that ramps, which **withdraws the 2026-09-03 report's note that "concurrency
reached 35 … so the pool ramps"** (that snapshot's own peak is 19). A push fans
out 13–21 job records, so **two simultaneous pushes saturate the account** and
everything behind them queues. Filed as
[#462](https://github.com/meridianlabs-ai/inspect_ai/issues/462); the only lever
inside this repo is jobs-per-push (proposals 2 and 15).

**`slow-tool-tests-dev` is now the largest single item in CI, and it is
serial.** Its test step has gone **723 → 757 → 874 → 910 → 959 → 1356s** across
six windows, and it ran 11 times here (against 1–4 before) because the
sandbox-tools work landed. The log is unambiguous: **246 passed, 195 skipped in
1446.08s (24m 6s)**, one Docker test at a time on a 4-vCPU runner, because the
step is `pytest --runslow -m slow tests/tools/ -x` with no `-n`. It binds **8 of
34** successful Build runs at a **1071s median wall** (p90 1593s) and a 690s
margin over the runner-up. #5293 just did exactly this work for the checkpoint
area, with the result below, so the template exists (proposal 1, filed as
[#461](https://github.com/meridianlabs-ai/inspect_ai/issues/461)).

**#444 is verified, and it is the cleanest hit in the series.** #5293 isolated
the checkpoint containers' cleanup and added `-n logical`; `slow-tests
(checkpoint)` exec went **393 → 221s** and its test step **366 → 195s**, with no
failures. The issue predicted serial 446s → 224s under `-n 4`; measured 195s.

**#5220 is verified too — the docs render cache hit 4 of 15 jobs (26.7%)**, the
first hits any snapshot has contained, against its own predicted "roughly 23%".
On a hit the whole job is 8–9s against a 354s median for the 11 misses. And the
duplicate-key waste that dominated four prior reports is **closed**: of the 21
`docs-render-*` entries created since 2026-09-06, **21 are distinct keys and none
exists under two refs** (36 of 129 at its worst).

## Queue vs execution

Median execution / queue over successful jobs, this window against the last.
Queue is measured from run start for independent jobs and from the predecessor's
completion for dependent ones (`needs` map read from `.github/workflows/build.yml`:
`docs`/`sandbox-tools-unit` ← `changes`; `check-version-bump`/`slow-tests` ←
`detect-slow`; `slow-tool-tests-{dev,release}` ← `detect-slow` +
`check-version-bump`). p90 is linear interpolation, as in every prior report.

| workflow | job | n | exec med | prev | exec p90 | queue med | queue p90 |
|---|---|---:|---:|---:|---:|---:|---:|
| Build | slow-tool-tests-release | 1 | 1603 | — | 1603 | 3 | 3 |
| Build | **slow-tool-tests-dev** | 11 | **1489** | 1109 | 1724 | 3 | 4 |
| Build | test (3.11) | 41 | 354 | 338 | 381 | 4 | 52 |
| Build | test (3.10) | 42 | 352 | 334 | 382 | 4 | 59 |
| Build | **docs** | 15 | **325** | 396 | 417 | 3 | 8 |
| Build | **slow-tests (checkpoint)** | 3 | **221** | 393 | 222 | 4 | 30 |
| Build | sandbox-tools-unit | 7 | 132 | 132 | 145 | 3 | 9 |
| Build | mypy (3.10) | 44 | 94 | 92 | 101 | 4 | 56 |
| Build | mypy (3.11) | 44 | 91 | 89 | 102 | 5 | 56 |
| Viewer | viewer-tests | 45 | 70 | 68 | 76 | 4 | 36 |
| Viewer | check-schema-and-types | 45 | 56 | 58 | 62 | 4 | 28 |
| Viewer | dist-validation | 45 | 34 | 34 | 42 | 4 | 39 |
| Build | pre-commit | 43 | 33 | 30 | 42 | 4 | 50 |
| Build | package | 43 | 30 | 30 | 33 | 4 | 58 |
| Suppressions | suppressions | 45 | 17 | 16 | 22 | 4 | 18 |
| Build | ruff | 44 | 10 | 10 | 12 | 4 | 49 |
| Build | detect-slow | 44 | 9 | 8 | 10 | 4 | 83 |
| Build | check-version-bump | 14 | 8 | 9 | 10 | 3 | 19 |
| Viewer | submodule-on-main | 44 | 8 | 8 | 9 | 4 | 54 |
| Changelog Lint | entries-under-unreleased | 44 | 7 | 7 | 8 | 3 | 20 |
| Build | changes | 44 | 7 | 7 | 10 | 4 | 49 |

Two things break the pattern of every prior window. **Every queue p90 is 10–20×
its median** — that is the burst, and it is concentrated in one hour rather than
spread across the window (see "Queue"). And `test` exec is up 16–18s while
`docs` is down 71s and `slow-tests` down 172s: the first is the new restic file
plus burst-hour slowdown, the other two are #5220 and #5293 landing. `package`
is recorded as `Build & inspect the package.` in the snapshot.

`Suppressions comment` is a `pull_request_target` workflow, so the collector
never sees it (proposal 12); measured directly over the window's **40**
successful runs it is **33.5s median / 51.0s p90**, of which `actions/checkout`
is 21–31s (median ~24s) and the work is 1–2s. That is the baseline for the
`blob:none` fix already committed on this branch and still unmerged (proposal 7).

Workflow wall clock, successful runs only:

| workflow | n | wall med | prev | wall p90 |
|---|---:|---:|---:|---:|
| Build | 34 | 404 | 351 | 1079 |
| Validate Embedded Viewer | 44 | 76 | 74 | 114 |
| Suppressions | 45 | 23 | 21 | 39 |
| Changelog Lint | 44 | 11 | 10 | 29 |

Build's 404s median and 1079s p90 are both `slow-tool-tests-dev`, not a change
in ordinary pushes. Split by what the push touched, and by whether it landed
inside the burst:

| class | n | wall med | p90 | Build runner-min/run |
|---|---:|---:|---:|---:|
| sandbox-tools (burst) | 1 | 1982 | 1982 | 45.2 |
| sandbox-tools | 7 | 1071 | 1593 | 35.3 |
| code + docs | 5 | 409 | 488 | 24.2 |
| code only (burst) | 7 | **472** | 576 | 16.9 |
| code only | 14 | **361** | 395 | 16.0 |

There were **no docs-only and no design/md-only pushes** in the window, so #299
gets no snapshot observation (this PR's own push is one — see impact
verification). A push costs **19.8 runner-minutes** end to end (median over 53
pushes, all four workflows the snapshot sees), against 18.2 last window; the
increase is `slow-tool-tests-dev` firing on 11 runs.

### Critical path

Binding (last-finishing) job across the 34 successful Build runs:

| binding job | runs | wall median | median margin over runner-up |
|---|---:|---:|---:|
| `test (3.11)` | 13 | 362s | 13s |
| `test (3.10)` | 10 | 404s | 38s |
| **`slow-tool-tests-dev`** | **8** | **1077s** | **690s** |
| `docs` | 3 | 439s | 59s |

The two `test` legs still bind two thirds of runs, and still by a small margin
(13–38s) — which is exactly why proposal 3's ~30s matters: it is large enough to
clear both legs at once. But the *worst* Build runs are all
`slow-tool-tests-dev`: 8 runs whose median wall is **2.6× the code-only
median**, with 690s of slack over the next job. That is proposal 1.

### Queue

666 independent-job samples: median **4s**, p90 46s, p95 105s, p99 188s, **max
214s**; 115 samples above 30s and **51 above 60s** — against 651 samples at a 3s
median, 8s p90, 71s max and 6 samples over 60s last window. Per hour:

| hour (UTC) | runs | job samples | queue med | queue p90 | max |
|---|---:|---:|---:|---:|---:|
| **09-09 01** | **32** | **120** | **48s** | **171s** | **214s** |
| 09-08 15 | 8 | 15 | 38s | 42s | 67s |
| 09-08 14 | 28 | 90 | 4s | 37s | 49s |
| 09-08 17 | 15 | 59 | 4s | 30s | 55s |
| 09-09 00 | 8 | 30 | 4s | 21s | 57s |
| 09-09 09 | 7 | 21 | 12s | 33s | 61s |
| everything else | ≤20 | ≤75 | 3–4s | ≤17s | ≤39s |

Note that 28 runs in the 09-08 14 hour held a 4s median while 32 runs in the
09-09 01 hour did not: the 14:00 pushes were spread across the hour, the 01:00
ones arrived together. **Arrival shape, not hourly volume, is what saturates a
fixed 20-job ceiling** — which is why "runs per hour" in eight prior reports
never predicted a queue. Full analysis in proposal 2.

## Where the pytest step actually goes

Timestamps from the raw job log of `test (3.10)` in upstream run 34332424502
(the newest leg the collector mined; 16,609 collected items, 4 workers):

| phase | seconds | prev | note |
|---|---:|---:|---|
| `uv run` project re-sync | 5.6 | 5.4 | still on `main`; fixed on this branch, unmerged (proposal 5) |
| startup + collection (5 interpreters) | 59.7 | 55.9 | assertion rewriting (proposal 14) |
| test execution | 292.9 | 246.5 | 1079.0 worker-seconds over 4 workers |
| reporting (durations, summary) | 0.15 | 0.2 | `-ra`, holding |
| **step total** | **~358** | 308.0 | window median 352 (3.10) |

The raw log is **5,936 lines** with **zero** `Captured stdout` blocks. Test
execution is where the whole regression lives: +46.4s, of which
`test_sandbox_egress_restic.py` is 116.4s of worker time — 29.1s per worker.

## Worker balance (`--dist worksteal`, #4948)

Twelfth window holding, from both `--report-log` artifacts of run 34332424502:
per-worker test seconds **275.1 / 266.0 / 268.3 / 269.5** on 3.10 (imbalance
+5.3s, efficiency **98.1%**) and **272.7 / 275.3 / 275.0 / 277.9** on 3.11
(+2.7s, **99.0%**). No stragglers on either leg — the restic file's heaviest
test is 8.6s, well under the ~76s stragglers that motivated the fix.

## Slowest tests

Median seconds per test+phase across the 20 mined `test` legs (`--durations=50
--durations-min=1`, so only phases ≥1s appear).

| s | phase | test | classification |
|---:|---|---|---|
| 11.8 | call | `test_eval_set_selection.py::test_eval_set_selection_concurrent_workers` | genuinely heavy — three real subprocesses |
| 11.2 | call | `test_eval_set.py::test_retry_attempt_killed_mid_sweep_leaves_completed_samples_reusable` | genuinely heavy |
| 10.9 | call | `test_eval_set_scanner.py::test_scout_scan_resume_reruns_failed_scans` | genuinely heavy |
| 10.0 | call | `_control/test_launch_handoff.py::test_eval_detach_hands_off_and_leaves_eval_running` | subprocess launch; pays `import inspect_ai` (#311) |
| 10.0 | call | `_control/test_launch_handoff.py::test_eval_detach_via_dotenv_detaches_exactly_once` | subprocess launch (#311) |
| **8.8** | call | `checkpoint/test_sandbox_egress_restic.py::test_egress_recovers_after_failed_transfer` | **real restic — marked slow in this PR** |
| 8.4 | call | `_control/test_launch_handoff.py::test_eval_json_redirects_subprocess_stdout_to_stderr` | subprocess launch (#311) |
| 8.2 | call | `model/test_parse_tool_call.py::test_parse_error_on_deeply_nested_yaml_arguments` | guards a real uninterruptible hang (#5095); the cost is the point |
| **7.8** | call | `checkpoint/test_sandbox_egress_restic.py::test_egress_sweep_spares_sibling_sandbox_in_flight_transfer` | **real restic — marked slow in this PR** |
| 7.3 | call | `agent/test_agent_bridge.py::test_google_bridge_computer_use_incompatible_model` | ~3.9s is `traceback_ansi` rendering (#374) |
| 6.8 | call | `agent/test_agent_bridge.py::test_google_bridge_streaming_not_supported` | ~3.9s is `traceback_ansi` rendering (#374) |
| 6.5 | call | `test_eval_set_scanner.py::test_scanner_resume_accumulates_summary_…[s3]` | moto S3 + full eval-set resume |
| 6.3 | call | `log/test_eval_log_config.py::test_eval_log_run_config_round_trip` | genuinely heavy |
| 6.1 | call | `test_eval_set.py::test_eval_set_previous_task_args` | ~5s real sleep (proposal 19) |
| 6.0 | call | `_control/test_launch_handoff.py::test_eval_detach_fails_when_control_bind_fails` | subprocess launch (#311) |
| **5.8** | call | `checkpoint/test_sandbox_egress_restic.py::test_egress_ships_unrecorded_extra_snapshot_as_orphan` | **real restic — marked slow in this PR** |
| **5.0** | setup | `checkpoint/test_sandbox_egress_restic.py::test_egress_ships_deltas_and_records_host_verified_id` | **real restic — marked slow in this PR** |

Heaviest files by total worker time in the 3.10 report log (not just the tail):
**`checkpoint/test_sandbox_egress_restic.py` 116.4s**, `test_eval_set_scanner.py`
66.7s, `test_eval_set.py` 65.8s, `_control/test_launch_handoff.py` 54.7s,
`_control/test_eval_set_integration.py` 45.5s, `test_sample_limits.py` 28.5s,
`test_eval_set_selection.py` 22.6s, `agent/test_agent_bridge.py` 21.9s.

### The regression is one file

The whole mined tail is **312s per leg**, against 262s last window. Per file the
change is a single entry: `test_sandbox_egress_restic.py` contributes **+61.3s**
and everything else nets **−11s**. The `--durations-min=1` cutoff understates it
badly — 18 of its 24 tests pay a ~2.4s fixture setup that never clears the
cutoff — which is why proposal 3 is estimated from the `--report-log` total
(116.4s) rather than from the tail.

### Docker-trap sweep

Unchanged for ten runs: **6** test functions pair `skip_if_no_docker` with no
`@pytest.mark.slow` — `util/sandbox/test_docker_compose_config.py` ×3 (ungated,
never start a container), `tools/test_think_tool.py` ×2 and
`agent/test_agent_docs.py::test_agent_collect` (gated by `skip_if_no_anthropic` /
`skip_if_no_openai`). No new offenders. The sweep looks for `skip_if_no_docker`,
so it could not have found this window's actual offender: proposal 3's file uses
a *downloaded binary* rather than Docker, which is the other half of the same
policy clause.

## Suite size

| snapshot | collected items | pytest wall (median leg) | Build wall (code-only) |
|---|---:|---:|---:|
| 2026-08-31 | 14,950 | 290.3 | 338.0 |
| 2026-09-01 | 15,220 | 289.6 | 346.0 |
| 2026-09-03 | 15,895 | 300.4 | 370.0 |
| 2026-09-05 | 16,415 | 302.9 | 360.0 |
| 2026-09-07 | 16,416 | 300.1 | 351.0 |
| **2026-09-09** | **16,608** | **338.7** | **361.0** |

Both pytest columns are the median mined leg of each snapshot, as in every prior
report. Top-level test functions on `main` (`^(async )?def test_` under
`tests/`): **9,246 → 9,331 (+85)** in two days; collected items **+192**; pytest
`passed` per leg **11,515 → 11,633 (+118)**.

**Per-leg wall is up 12.9% for a 1.0% rise in test count**, and the attribution
is almost entirely one file. From the two `--report-log` artifacts of run
34332424502:

| leg | test worker time | over 4 workers | span (first start → last stop) | pytest wall |
|---|---:|---:|---:|---:|
| 3.10 | 1079.0s | 269.8s | 281.5s | 350.5s |
| 3.11 | 1100.9s | 275.2s | 284.0s | 346.9s |

`test_sandbox_egress_restic.py` is 116.4s of both. Worker time is up 896.9 →
1079.0s on 3.10 (+182.1s) against a ±60s noise band, so this is the first
window since 2026-08-27 with a *real* worker-time regression rather than noise —
and 116.4s of the 182.1s is the one file.

### Where the time sits (3.10 report log, 16,609 items, 1079.0 worker-seconds)

| band | tests | worker-s | share | prev share |
|---|---:|---:|---:|---:|
| ≥10s | 6 | 72.6 | 6.7% | — |
| 1–10s | 197 | 461.8 | 42.8% | 42.7% (1–5s + ≥5s) |
| **0.1–1s** | **1,207** | **437.5** | **40.5%** | 46.4% |
| 0.01–0.1s | 2,086 | 55.1 | 5.1% | — |
| <0.01s | 13,113 | 52.1 | 4.8% | 10.9% (<0.1s) |

The 0.1–1s band has been 40–49% of test time in every window measured, and
15,199 tests under 0.1s are still under 10% of it. Its *share* fell this window
only because the 1–10s band grew — the restic file is a third of that band by
itself. Once it is gone the 0.1–1s band is the only thing left that matters, and
proposal 4 is what it is made of.

### Duplicate-coverage and low-value sampling

The strict AST sweep (identical decorators + signature + body) finds the same
**3 groups / 7 tests** as the last three windows, all coincidental one-liners.
The two files that grew most in the window were sampled instead:
`test_sandbox_egress_restic.py` (+649 lines) has 24 tests with no overlapping
assertions — the fix there is the mark, not deletion — and
`test_sandbox_egress_extract.py` (+361/−49) is fully mocked and costs **0.4s** of
worker time, i.e. exactly what a test of that subject should cost. The
judgement-based candidates are unchanged (proposal 19).

## Regressions since last report

- **`test` leg exec +16/+18s, pytest wall +38.6s, worker time +182.1s** — the
  new real-restic file. Proposal 3, fixed in this PR.
- **`slow-tool-tests-dev` test step 959 → 1356s (+41%)** — new Docker tests
  under `tests/tools/` (`test_tool_user_param.py`, the rootless and nogzip
  compose cases, +430/−38 in `sandbox_tools_utils/test_sandbox_tools.py`)
  multiplied by serial execution. Proposal 1.
- **Queue p90 8 → 46s, max 71 → 214s** — one 70-minute burst against a 20-job
  ceiling. Proposal 2. Not a regression in CI's configuration; a regression in
  what eight contributors felt that hour.
- **No recurrence of the artifact-upload failure** (proposal 17): this window's
  single `test (3.10)` failure is a genuine pytest `exit code 1`, and no job
  failed in `Upload test order log`.
- **No per-job regression anywhere else.** Every other median is within 4s of
  last window.

## Waste

- **4 cancelled Build runs held 151.8 runner-minutes** before dying — well above
  the usual few minutes, because two were `fix/sandbox-tools-v30` pushes
  cancelled mid-`slow-tool-tests-*` (3986s and 2787s of summed job exec).
  Superseded-run cancellation works as designed; the cost *per* cancellation now
  scales with proposal 1.
- **21 runs (10.5%) ended `action_required`**, across 4 first-contributor
  branches (8/6/4/3 runs each) — down from 29 across 8 branches. Zero runner
  time, zero signal, and the contributor sees nothing until a maintainer
  approves (proposal 18).
- **Zero zero-job `failure` runs**, against 6 last window: every `failure` here
  has real failing jobs — 3 × `slow-tool-tests-release` (the published-binary
  gate failing fast, by design), 1 each of `pre-commit`, `test (3.10)`,
  `package`, `submodule-on-main` and `entries-under-unreleased`.
- **Overhead-dominated jobs.** `Suppressions comment` spends ~24s of checkout on
  1–2s of work (proposal 7, fix committed, unmerged). The `pre-commit` job spent
  12s installing 200+ packages no hook reads, behind a 4s full-history checkout
  (proposal 8, fixed in this PR). Everything else is within a few seconds of its
  useful work.
- **Cache effectiveness.** `docs-render-*`: 4 of 15 jobs hit; 21 distinct keys
  and 0 duplicates since 09-06 (#5220 verified). `setup-uv`: the `docs` job still
  restores a cache it cannot save into, which is proposal 6.

## Proposals (ranked)

1. **Parallelize the `tests/tools/` slow suite.** NEW, and the largest
   wall-clock item in CI. `slow-tool-tests-dev` runs `uv run pytest --runslow -m
   slow tests/tools/ -x` with no `-n`, so **246 tests execute one at a time**:
   1446.08s of pytest wall (24m 6s) inside a 1489s job (p90 1724s), binding **8
   of 34** successful Build runs at a **1071s median wall** — 2.6× the code-only
   median — with a 690s margin over the runner-up. The step has grown 723 → 757
   → 874 → 910 → 959 → **1356s** over six windows as sandbox-tools tests were
   added, so it compounds. **The template already exists**:
   [#5293](https://github.com/UKGovernmentBEIS/inspect_ai/pull/5293) did
   precisely this for the checkpoint area — isolate each test's container
   cleanup, then add `-n logical` — and cut that job 393 → 221s.

   Why this is an issue rather than a fix here, unchanged from #444: the
   2026-09-05 experiment on the checkpoint area found 4 failures under `-n 4`,
   every one a test asserting on *global* Docker state (leaked-container and
   container-count assertions that see a concurrent test's containers), and
   scoping those assertions is product-adjacent test work with real correctness
   content. `-x` also interacts with xdist (it stops scheduling after the first
   failure but lets in-flight tests finish), which is a deliberate choice about
   fail-fast behaviour on a 24-minute job, not something to change silently.
   Est. impact: **−10 to −18 min** of wall on tools-touching pushes (22% of
   pushes here), using the checkpoint area's measured 1.9× as the conservative
   anchor. Disruption: structural (test-content correctness).
   Status: **new**, filed as
   [#461](https://github.com/meridianlabs-ai/inspect_ai/issues/461).

2. **The runner ceiling is 20 concurrent jobs.** Was proposal 6 ("burst
   contention — unresolved, not disproved") for three windows; now **confirmed,
   with the mechanism identified**, which changes what to do about it. Second
   observation, 2026-09-09 00:50–02:00 UTC, 8 branches × 4 runs:

   | | burst | rest of window |
   |---|---:|---:|
   | queue median | **47.5s** | 4.0s |
   | queue p90 | **171s** | 24s |
   | Build wall, code-only | **472s** | 361s |
   | `test (3.10)` exec | 371s | 349s |

   The ceiling is the finding. Counting **all** workflow events in that window
   (`pull_request` and `pull_request_target`, 41 runs / 132 job intervals),
   concurrency peaked at **exactly 20**, spent 158s at 20 and 278s at 19–20, and
   never reached 21. Peak concurrency in the last four snapshots is **19, 19,
   19, 20**. So the pool does not ramp, and the 2026-09-03 report's "concurrency
   reached 35" is unsupported by its own snapshot (peak 19) — read that claim as
   withdrawn. Twenty concurrent jobs is GitHub's standard limit for this account
   class, and one push produces 13–21 job records across four PR workflows plus
   `Suppressions comment` and `PR Gate`: **two pushes saturate it.** That also
   explains why eight prior reports found no queue at 10–20 runs/hour — arrival
   shape decides, not hourly volume (28 runs in one hour queued 4s here; 32
   arriving together queued 48s).

   Note the execution slowdown too: `test` exec was 371/376s inside the burst
   against 349/354s outside, on runners that are nominally dedicated — so ~20s of
   the burst's cost is not queue at all, and no repo-side change addresses it.
   The levers, in order of how much they need a maintainer: raise the limit
   (plan or larger-runner purchase — outside this repo); cut jobs per push
   (proposal 15 merges 4 Viewer jobs into 1–2; `Changelog Lint` and
   `Suppressions` are one 7s and one 17s job each in workflows of their own); or
   accept it and stop attributing burst-hour wall clock to test speed.
   Status: **new evidence**, filed as
   [#462](https://github.com/meridianlabs-ai/inspect_ai/issues/462).

3. **Mark the real-restic sandbox egress tests slow.** NEW, and **shipped in
   this PR**. `tests/checkpoint/test_sandbox_egress_restic.py` drives
   `egress_sandbox` against a real restic binary that `resolve_restic()`
   downloads on first use; each of its 24 tests pays a real `restic init` plus
   real backup/restore invocations. That is the repo's own definition of a slow
   test (Docker *or* a real unmocked external service), and its sibling
   `tests/checkpoint/test_restore_repo.py` — same directory, same real binary,
   same subsystem — already carries `pytestmark = pytest.mark.slow` for exactly
   this reason.

   Measured, from the `--report-log` artifacts of upstream run 34332424502:

   | | 3.10 leg | 3.11 leg |
   |---|---:|---:|
   | this file's worker time | **116.4s** | **116.4s** |
   | whole leg's worker time | 1079.0s | 1100.9s |
   | share | **10.8%** | **10.6%** |
   | next-largest file | 66.7s | 76.5s |

   plus 120.05s of wall on its own in a single process (`pytest <file>`, 24
   passed). It is also the entire suite-size regression this window: the mined
   tail went 262 → 312s per leg and this file is +61.3s of that.

   Est. impact: worker time 1079.0 → 962.6s; at the measured 98.1% worker
   efficiency and 4.3% span-over-average overhead that is a span of ~251s
   (from 281.5s), **pytest wall ~350 → ~320s and `test` leg exec ~391 → ~360s**
   — **~30s off both legs**, above the 13–38s margin by which they bind, so it
   should show as ~30s off code-only Build wall (361 → ~331s).

   Where the coverage goes, stated precisely, because two of the three
   review findings on this change were about exactly that.
   `tests/checkpoint/**` is a `slow-tests` matrix area, so **on a pull request**
   touching `src/inspect_ai/util/_checkpoint/**`,
   `src/inspect_ai/util/_restic/**` or `tests/checkpoint/**`, the checkpoint leg
   runs them; `detect-slow` is gated on `pull_request`, so **pushes to `main`
   lose them**, and there they are covered only by the ~2h scheduled suite. Nor
   is that trigger set the full blast radius: `egress.py` and `_copy.py` import
   `util/_sandbox/{environment,limits}.py`, which no trigger path covers (though
   the unmarked `test_snapshot_strategy.py` keeps `copy_out` and
   `LocalShellSandbox` in the gate). And the move *loads the job that binds
   checkpoint PRs*: `slow-tests (checkpoint)` bound 3 of 39 Build runs by 140s
   last window. It is still net-positive only because #5293 landed first —
   116.4s of worker time over that job's 4 workers is ~+29s, taking it 221 →
   ~250s, comfortably under the ~320s the `test` legs will sit at. Had this fix
   landed a week earlier it would have moved wall clock from one binding job to
   another for no gain.

   One test does not belong in the slow set:
   `test_remove_files_unwinds_last_written_first` needs no restic binary, costs
   milliseconds, and is the only test of `_remove_files` anywhere. It moves to
   `test_sandbox_egress_extract.py` (same module under test, also restic-free)
   rather than being swept along.

   Validation: `pytest <file>` is 47 skipped in 0.17s before the move and 23
   passed in 119.8s with `--runslow` after it; the two files together are 34
   passed / 46 skipped in 1.24s in the PR gate's configuration.
   Disruption: safe fix (one `pytestmark`, policy-mandated, with an
   in-directory precedent).
   Status: **new, shipped in this PR — and measured in CI within the hour;
   prediction hit.** See "Measured in CI" below.
   

4. **Stop paying 214ms of control-server startup on every `eval()`.** Carried,
   unchanged, and still the largest measured *test-side* lever. A one-sample
   `mockllm` eval is 249ms with the control channel and 35ms without; the 214ms
   splits 30ms building an identical 28-route FastAPI app, 30ms binding, and
   100ms waiting out uvicorn's fixed 0.1s `should_exit` poll. 796 test functions
   across 153 files call `eval()`/`eval_set()` directly, and a full-suite A/B on
   4 workers runs **725.8 → 529.0s of wall (−27%)** with 381 tests leaving the
   0.1–1s band — **40.5%** of test worker time this window. Extrapolated to a CI
   leg (1079.0 worker-seconds), on the order of **50–60s off both `test` legs**.
   Not shipped: the test-side fix moves coverage of the *default* configuration
   out of the bulk of the suite, and the product-side fix changes eval teardown
   semantics. Status: carried,
   [#393](https://github.com/meridianlabs-ai/inspect_ai/issues/393).

5. **`uv run` re-syncs the environment the install step just built.** Carried;
   the `test`/`mypy` half is committed on this branch and still unmerged, so
   `main` still pays it. Re-measured today on run 34332424502's `test (3.10)`:
   the step opens `Uninstalled 52 packages … Installed 55 packages`, **5.6s**,
   after an `Install dependencies` step that already took 11s. The remainder is
   unchanged and deliberately unshipped: for `docs` and `sandbox-tools-unit`
   dropping the sync would change *which dependency versions the job tests
   against* ([#308](https://github.com/meridianlabs-ai/inspect_ai/issues/308)),
   and `slow-tool-tests-release` depends on the sync replacing its wheel install
   with the lockfile's editable one so `_binaries_dir()` resolves to the tree the
   published binaries land in. Status: carried, half shipped, **awaiting
   promotion**.

6. **Give the `docs` job its own uv cache scope.** Carried, committed on this
   branch, unmerged — and this window revises the estimate **down**, because
   #5220 now skips the install entirely on a render-cache hit. Of 15 `docs` jobs,
   4 hit (8–9s jobs, no install at all) and 11 paid `Install dependencies` at a
   **45s median** (28–50s), of which the `quarto-cli` sdist build is ~43s. So the
   fix applies to ~73% of docs jobs rather than all: on the runs it applies to,
   `Install dependencies` 45 → ~8–12s, i.e. **−25 to −35s of `docs` exec**, still
   breaking even at a ~33% setup-uv hit rate. The residual it does not close is
   unchanged: the key hashes the *request* (`quarto-cli>=1.9.36`), not the
   resolution, so a new quarto release above the floor re-pays the 43s build
   until some hashed file rotates the key. Pinning `quarto-cli==` would close it
   and is a dependency-policy change, not cache tuning.
   Status: carried, shipped on this branch, **awaiting promotion**.

7. **`blob:none` on the `Suppressions comment` checkout.** Carried, committed on
   this branch, unmerged. Re-measured today over 40 successful runs: **33.5s
   median wall / 51.0s p90**, of which `actions/checkout` is 21–31s and the work
   is 1–2s. Because it is `pull_request_target` the *base* repo's copy always
   runs, so the PR carrying the fix can never exercise it — it can only be
   verified after merge. Status: carried, shipped on this branch, **awaiting
   promotion**.

8. **Drop the unused dev install from the `pre-commit` job.** NEW, and
   **shipped in this PR**. The job ran `uv pip install --system .[dev]` (200+
   packages) behind a full-history `fetch-depth: 0` checkout. Nothing consumes
   either: all four repos in `.pre-commit-config.yaml` are remote, pre-commit
   builds each hook's environment itself, and the `uv-lock` hook ships its own
   `uv`. Nothing consumes a setuptools_scm version either — the `uv-lock` hook
   *does* build root metadata (review finding: the first draft of the comment
   claimed no version was computed at all), but `uv.lock` records this project
   as a versionless editable root, so the tagless fallback a shallow clone
   yields never reaches the file. Verified by
   running the job's exact hook set against this tree in a venv containing
   nothing but pre-commit, with no `uv` on `PATH` and the project not installed:
   all 9 non-skipped hooks pass (`ruff` is skipped in CI too, via `SKIP`).
   Measured cost removed, over this window's 43 `pre-commit` jobs:
   `Install dependencies` 8s, `Install uv` 3s, `Minimize uv cache` 1s, and the
   checkout 4s → ~1s — **~15s of the job's 33s**. Off the critical path, so this
   is runner time (~15s of a push's 19.8 runner-min), not wall clock. One side
   effect worth recording, since proposal 6 is about the same cache:
   `setup-uv`'s default key is shared by every 3.11 job here and only the first
   job to finish saves it. That was plausibly this 33s job; it is now `mypy
   (3.11)` at ~90s, saving the locked dependency set rather than an unlocked
   `.[dev]` resolution. No job loses a cache — `test`, `mypy`, `slow-tests` and
   `sandbox-tools-unit` all still write that key. Disruption: safe fix (workflow
   hygiene). Status: **new, shipped in this PR — measured in CI at 33 → 17s,
   prediction hit.** See "Measured in CI" below.

9. **Rendering `EvalError.traceback_ansi` costs ~60s of worker time per leg.**
   Carried, not re-measured this window; the two `test_agent_bridge.py` Google
   tests still hold slots 10 and 11 of the tail at 7.3s and 6.8s, and the file is
   21.9s of worker time. Status: carried,
   [#374](https://github.com/meridianlabs-ai/inspect_ai/issues/374).

10. **Defer the `acp.schema` import.** Carried, not re-measured (the share has
    been stable at ~26% across three measurements). `import inspect_ai` is
    ~1.15s, of which `acp.schema` is ~300ms of self-time — 6.5× the next-largest
    module — reached eagerly through `inspect_ai._eval.eval` →
    `agent._acp.server`. Paid by 5 interpreters per leg plus the four
    `_control/test_launch_handoff.py` tests that hold slots 4, 5, 7 and 15 of
    this window's tail (54.7s of worker time for the file). Product change with a
    public-API surface. Status: carried,
    [#311](https://github.com/meridianlabs-ai/inspect_ai/issues/311).

11. **Test-volume policy — it is the 0.1–1s band that matters.** Eighth window
    confirming the shape: 15,199 tests under 0.1s are 9.9% of test worker time;
    1,207 tests between 0.1s and 1s are 40.5%. This window adds a caveat to the
    framing rather than new evidence: a *single* 116s file moved per-leg wall by
    38.6s — more than four windows of ordinary growth combined — so the slow tail
    is not finished as a source of regressions even though the body is where the
    steady-state time lives. The two are different problems: the body needs
    proposal 4, the tail needs the slow-test policy applied when files land.
    Structural. Status: carried.

12. **Collector: fetch a time window, not a run count.** Carried, and this
    window produced the third distinct symptom in three runs. (a) At 9.9
    runs/hour a 200-run snapshot spans 20.3h and leaves a **27.5h hole** between
    this window and the last — where last window, at 3.0 runs/hour, it spanned
    66.5h and *overlapped* by 10.6h. Same limit, opposite failure, one working
    week's throughput swing. A `--since` window fixes both. (b)
    `warn_on_time_gap` was correctly silent here (largest internal gap 6.5h),
    but its fixed 12h threshold false-positived on a weekend lull last window, so
    the retry-and-compare fix stands. (c) The `event=pull_request` filter still
    hides `Suppressions comment` — 40 runs and ~22 runner-minutes this window —
    which is why proposal 7's baseline has to be measured by hand every run. All
    three are one file, `.claude/skills/ci-perf/scripts/collect_ci_data.py`,
    **still unwritable** (proposal 13). Status: carried.

13. **Unblock the scheduled run — the upstream-write blocker is the one that
    matters.** Re-probed today:
    - *`workflow` scope* — **CLEARED**, and exercised again: proposal 8 is a
      `.github/workflows/build.yml` change pushed by this run's token.
    - *No upstream write* — `repos/UKGovernmentBEIS/inspect_ai` still reports
      `push: false` for this token. PR creation was attempted at the end of this
      run and the result is recorded in `prs.md`. Every report since 2026-08-21
      has had to be promoted upstream by hand.
    - *`.claude/**` unwritable by the agent's edit tooling* — still blocked, so
      proposal 12 cannot ship. The filesystem permits it; the tooling refuses the
      path, which makes it a harness policy change rather than a token one.

    Status: carried, [#298](https://github.com/meridianlabs-ai/inspect_ai/issues/298).

14. **Cache pytest's assertion-rewrite bytecode across runs.** **Declined**
    (2026-09-07), and this window's numbers support the decline: 59.7s of
    startup+collection, of which only the ~18–20s assertion-rewriting part is
    cacheable. The safe form of the fix (a strict key over every `.py` file, no
    `restore-keys`) can only hit on a push that changes no Python, where `test`
    already no-ops in 4s under #299; the form that can hit is unsafe, because
    pytest validates a rewritten `.pyc` on mtime+size and would silently run
    stale assertions in the PR gate. Status: **declined**, retained for the
    record.

15. **Merge the 4 Viewer jobs into 1–2.** Carried, and **raised** on the
    strength of proposal 2: under a hard 20-job ceiling every job removed from
    the fan-out is capacity for the next push, not merely saved runner time. The
    three pnpm-using jobs each spend ~5s on `pnpm/action-setup` and ~5s on
    `setup-node` independently (re-measured across 45 runs of each), ~30s of
    duplicated toolchain setup per push, and Viewer wall is 76s against
    `check-schema-and-types`'s 56s of exec. Requires a required-check rename.
    Structural. Status: carried, **raised**.

16. **Pin or cache `pnpm/action-setup`'s pnpm download.** **Dropped.** Third
    clean window: 5–6s median and 7.6–10s p90 across all three Viewer jobs (the
    Viewer wall p90 of 114s is the burst, not pnpm). Per the previous report's own
    criterion, one incident in three windows is a registry incident, not a
    workflow defect. Status: **dropped**.

17. **A transient artifact upload takes down a required check.** Carried at one
    observation, **no recurrence**. The one-line fix
    (`continue-on-error: true` on `Upload test order log`) remains deliberately
    unshipped: letting a diagnostic upload fail silently is a maintainer's call
    about what a required check should assert. Status: carried, report-only.

18. **First-contributor approval gates are the longest feedback wait in the
    data.** Carried and improving: **21 of 200 runs (10.5%)** ended
    `action_required` across 4 branches, against 29 across 8 last window. Still
    an order of magnitude above the 361s Build wall everything else in this
    report is trying to shave, and still no fix that belongs in a workflow file.
    Status: carried, report-only.

19. **Duplicate and near-duplicate test cleanups.** Unchanged: the strict AST
    sweep finds 3 groups / 7 tests, all coincidental one-liners with no cleanup
    value. The judgement-based candidates are unchanged — `test_sample_shuffle`
    (4.5s) runs the full `popularity()` dataset twice to assert a property
    `test_sample_shuffle_limit` already asserts on 20 samples, and
    `test_eval_set_previous_task_args` (6.1s) is ~5s of real sleep. Both are
    coverage judgements, not safe fixes. Status: carried, low.

20. **`tests/util/test_display_counter.py` sleeps 6 × 1.1s for 2 throttle
    paths.** Carried, and back in view at **9.1s of worker time** in the 3.10
    report log (no phase of it clears `--durations-min=1`, so the mined tail hides
    it). The 2026-09-01 analysis stands: `_throttle` reads `time.time()` directly
    *and* schedules a real `anyio.sleep(remaining)` trailing-edge fire in a
    background task, so faking the clock without faking the sleep changes what
    the test exercises. Options remain a coverage judgement (drop the sleep for
    the params whose `@throttle(5)` a 1.1s sleep can never fire) or an injectable
    throttle window. Status: carried.

21. **Policy consistency: docker tests without `@pytest.mark.slow`.** Still
    exactly six, still ~0.05s combined; the right fix is probably to drop
    `skip_if_no_docker` from the three ungated ones rather than to mark them
    slow. Zero wall-clock impact — unlike proposal 3, which is the same policy
    clause with a 116s price tag, and which the `skip_if_no_docker`-shaped sweep
    could not see. Worth widening the sweep to real-binary/real-service
    resolvers (`resolve_restic`, and anything else that downloads on first use)
    the next time the collector is writable. Status: carried.

Dropped this report: proposal 16 (pnpm), after three clean windows.

## Measured in CI: this run's two fixes

Both fixes touch code the PR gate runs, so this PR's own CI measured them
before the run ended. Comparing the `--report-log` artifacts of this PR's Build
run (34337355178, on the fork) against upstream run 34332424502:

| | before | after | Δ |
|---|---:|---:|---:|
| `test_sandbox_egress_restic.py` worker time | 116.4s | **0.1s** | −116.3s |
| 3.10 leg worker time | 1079.0s | **889.2s** | −189.8s |
| 3.11 leg worker time | 1100.9s | **897.4s** | −203.5s |
| 3.10 span (first test start → last stop) | 281.5s | **235.9s** | −45.6s |
| 3.11 span | 284.0s | **237.8s** | −46.2s |
| `call` phases collected | 11,626 | 11,602 | −24 |
| `pre-commit` job exec | 33s (window median) | **17s** | −16s |
| `slow-tests (checkpoint)` step | 195s / 20 passed | **183.3s / 44 passed** | −11.7s, +24 tests |

**Fix 1 (proposal 3): prediction hit, and the honest arithmetic matters.** The
marked file is gone from the leg (0.1s is the one test that moved to
`test_sandbox_egress_extract.py`), exactly the 116.3s predicted. The leg-level
span fell 45.6s, which is *more* than the ~30s predicted — but only ~29s of it
is this fix. Diffing per-file worker time across the two runs, the other
**−73.3s is spread thinly over 612 files** in a uniformly faster direction
(`test_eval_set.py` −8.3s, `test_eval_set_scanner.py` −5.9s,
`test_launch_handoff.py` −4.6s, and hundreds of sub-2s deltas), which is a
faster/less-loaded runner, not anything the change did: 116.3/4 = 29.1s plus
73.3/4 = 18.3s accounts for the 45.6s observed. So the fix delivers **~29s per
leg**, against ~30s predicted.

**Fix 2 (proposal 8): prediction hit.** `pre-commit` ran in **17s** against a
33s window median — 16s removed, against ~15s predicted.

**The cost side of fix 1 did not materialise, and that is the interesting
result.** The report predicted ~+29s on `slow-tests (checkpoint)` from absorbing
116.4s of worker time across 4 workers. Measured on this PR's own run (the first
push to exercise it): **44 passed / 34 skipped in 183.26s**, against 20 passed
at a ~195s step median. The leg took **24 more tests and 116s more worker time
for 11.7s less wall** — the checkpoint slow suite had that much idle worker
capacity, because it is 20 Docker tests whose durations are very uneven. Two
consequences: the move is unambiguously net-positive rather than
marginally so, and #5293's `-n logical` bought more headroom than its own
measurement showed. Small samples on both sides (3 runs before, 1 after), so
the next snapshot should re-read it.

## Impact verification

- **#444 / #5293 (`slow-tests` parallel) — verified, prediction hit.** The issue
  predicted serial 446.2s → 224.2s under `-n 4`, conditional on scoping the
  global-Docker-state assertions first; #5293 did both (isolated container
  cleanup + `-n logical`). Measured: `slow-tests (checkpoint)` exec **393 →
  221s**, test step **366 → 195s**, p90 475 → 222s, and the 4 failures the
  experiment saw are gone — all 3 runs here green. #444 is closed.
- **#5220 (docs render cache key) — verified, prediction hit.** First snapshot
  ever to contain hits: **4 of 15 `docs` jobs** (26.7%) against #5220's predicted
  "roughly 23%". A hit is an **8–9s job** against a 354s median for the 11 misses
  (render 298s, install 45s). Independently confirmed in the cache index: 7
  `docs-render-*` keys have ever been re-read and **4 of those reads fall inside
  this window** (PRs 5291, 5294, 5297, 5298). The duplicate-key waste four prior
  reports tracked is closed — of 21 entries created since 2026-09-06, **21
  distinct keys, 0 under two refs** (worst was 36 of 129).
  [#317](https://github.com/meridianlabs-ai/inspect_ai/issues/317) can stay
  closed.
- **#5075 (`-ra`) — holding, eighth window.** Raw `test (3.10)` job log 5,936
  lines, **zero** `Captured stdout` blocks, reporting phase 0.15s.
- **#4948 (`--dist worksteal`) — holding, twelfth window.** +5.3s imbalance at
  98.1% efficiency (3.10) and +2.7s at 99.0% (3.11); no stragglers.
- **#4935 (`blob:none` checkouts) — holding.** Checkout is 3–6s median in every
  Build and Viewer job; the `test`-leg checkout is again the one always-run step
  whose p90 exceeds 2× its median (3s → 7s), i.e. 4s of absolute spread.
  `slow-tool-tests-release` executed *successfully* for the first time in the
  series (1603s), so that leg's checkout is finally observed, at 6s.
- **#4760 (`test_package` pre-installed) — holding.** No `test_extensions` test
  appears in the mined tail or in the top 25 files of either report log.
- **#299 (`design/**` excluded from the test filter) — no snapshot
  observation** (no docs-only or design-only push landed in the window). This
  PR's own push is the sixth observation and is recorded in `prs.md`.
- **This branch's three unmerged fixes — baselines all re-confirmed today.**
  `uv sync` + `--no-sync` (proposal 5): the pytest step still opens with
  `Uninstalled 52 packages … Installed 55 packages`, 5.6s. `cache-suffix: docs`
  (proposal 6): 11 of 15 docs jobs paid a 45s install with the ~43s quarto sdist
  build inside it. `Suppressions comment` `blob:none` (proposal 7): 40 runs at
  33.5s median with 21–31s of checkout.
- **#393, #374, #311 — filed, no action yet.** All three remain maintainer
  decisions.

## PRs opened by this skill

See `prs.md`. The previous run's PR
([meridianlabs-ai/inspect_ai#408](https://github.com/meridianlabs-ai/inspect_ai/pull/408))
is still open and green, so this run pushes onto its branch rather than opening
a second PR, per the unattended rule. It adds the 2026-09-09 snapshot, this
report, the ledger row, and two safe fixes (`@pytest.mark.slow` on the
real-restic egress tests; the `pre-commit` job's dead dev install), on top of the
three fixes and three reports already on the branch.
