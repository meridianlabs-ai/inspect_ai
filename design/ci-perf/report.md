# CI performance report — 2026-09-05

Data: 200 PR runs, 2026-09-04 07:47 .. 2026-09-05 01:25 UTC (**17.6h**, 11.3
runs/hour, 53 pushes). Snapshot: `history/2026-09-05.json`. Previous: 2026-09-03
(200 runs over 12.5h, ending 09:13 UTC). **22.6h between the two windows is
uncovered by either snapshot** (proposal 9). Largest gap inside this window
3.6h, at the old edge. The collector's stale-page bug bit for a **fifth** time —
attempt 1 returned a 471h window with a 417h hole; a plain re-run gave the clean
window above. Produced by the unattended scheduled run
([workflow run](https://github.com/meridianlabs-ai/actions/actions/runs/33957366017)).

## Summary

**A new job is on the critical path: `slow-tests (checkpoint)`.** It ran twice
last window and **16 times this one**, at **362s median / 458s p90**, and it is
now the last-finishing job in **7 of 45 successful Build runs — by a 104s median
margin**, more than five times the 12–19s margin the `test` legs bind by. It
runs `pytest --runslow -m slow tests/checkpoint/` with no `-n`, so **20 tests
take 421.7s single-process** on a 4-vCPU runner (proposal 3).

**The docs render cache fix landed, and its mechanism works — but this window
gave it nothing to hit.** Upstream [#5220](https://github.com/UKGovernmentBEIS/inspect_ai/pull/5220)
merged 2026-09-03 21:48Z, keying on the PR's own `src/inspect_ai` delta instead
of the merged tree, and closed
[#317](https://github.com/meridianlabs-ai/inspect_ai/issues/317). Two hits
landed in the 10h between that merge and this window's start — both same-branch
re-pushes, **the case that had stopped hitting entirely** under the old key. In
the window itself: **0 hits in 7 `docs` jobs**, none of which was an eligible
repeat. The duplicate-key waste is gone by construction: **0 of the 17
post-merge keys exist under both a PR ref and `refs/heads/main`**, against 36 of
129 before.

**Queue went back to normal, but the burst was not re-tested.** p90 **5s**
(was 57s), max **67s** (was 157s), 5 waits over a minute (was 65). The window's
densest 16-minute stretch is 24 runs against the 47 that produced the
2026-09-03 burst, so this is *absence of the trigger*, not evidence the pool now
absorbs one (proposal 5).

**A 4-hour external stall cost the Viewer workflow 64 runner-minutes.** Between
07:47 and 11:48 UTC, `pnpm/action-setup@v6` took **94–426s** in 13 jobs against
a 5–6s median, turning 76s Viewer runs into 470s ones. That single incident is
the whole of the Viewer workflow's 133 → 202 runner-minute rise.

**This run ships one safe fix**: `filter: "blob:none"` on the
`Suppressions comment` checkout — the last unfiltered `fetch-depth: 0` in any
per-PR workflow, and **28.0s of a 33.5s job**, run 54 times this window (more
often than Build). It is compute, not wall clock: the job finishes long before
Build's 360s, so nobody waits on it. **The fresh-context review pass earned its
keep on it** — see "What the review caught" below.

## Queue vs execution

Median execution / queue over successful jobs, this window against the last.
Queue is measured from run start for independent jobs and from the predecessor's
completion for dependent ones (`needs` map read from `.github/workflows/build.yml`:
`docs`/`sandbox-tools-unit` ← `changes`; `check-version-bump`/`slow-tests` ←
`detect-slow`; `slow-tool-tests-{dev,release}` ← `detect-slow` +
`check-version-bump`). p90 is linear interpolation, as in every prior report.

| workflow | job | n | exec med | prev | exec p90 | queue med | queue p90 |
|---|---|---:|---:|---:|---:|---:|---:|
| Build | slow-tool-tests-dev | 1 | 945 | 912 | 945 | 2 | 2 |
| Build | docs | 6 | 390 | 386 | 410 | 3 | 3 |
| Build | **slow-tests (checkpoint)** | **16** | **362** | — | **458** | 2 | 3 |
| Build | test (3.10) | 46 | 338 | 333 | 358 | 3 | 7 |
| Build | test (3.11) | 46 | 338 | 336 | 360 | 3 | 9 |
| Build | mypy (3.10) | 45 | 92 | 92 | 102 | 3 | 7 |
| Build | mypy (3.11) | 45 | 87 | 91 | 97 | 3 | 7 |
| Viewer | viewer-tests | 46 | 71 | 69 | **124** | 3 | 4 |
| Viewer | check-schema-and-types | 46 | 59 | 55 | **96** | 3 | 5 |
| Viewer | dist-validation | 47 | 36 | 34 | **56** | 3 | 5 |
| Build | pre-commit | 47 | 34 | 33 | 39 | 3 | 4 |
| Build | package | 47 | 31 | 32 | 35 | 3 | 5 |
| Suppressions | suppressions | 48 | 16 | 16 | 19 | 3 | 4 |
| Build | ruff | 47 | 11 | 11 | 13 | 3 | 4 |
| Build | detect-slow | 47 | 9 | 8 | 10 | 3 | 10 |
| Build | check-version-bump | 2 | 8 | 9 | 10 | 3 | 3 |
| Viewer | submodule-on-main | 46 | 8 | 8 | 9 | 3 | 4 |
| Build | changes | 47 | 7 | 7 | 8 | 3 | 4 |
| Changelog Lint | entries-under-unreleased | 37 | 7 | 6 | 8 | 3 | 3 |

Execution medians are within 1–5s of last window everywhere. The queue p90
column has collapsed from 62–73s back to 4–10s. The three Viewer p90s are the
pnpm incident, not job growth — their medians moved 1–4s. `package` is recorded
as `Build & inspect the package.` in the snapshot.

`Suppressions comment` is a `pull_request_target` workflow, so the collector
never sees it (proposal 9); measured directly over its 20 most recent runs it is
**33.5s median / 40.0s p90**, of which **28.0s median / 33.1s p90** is
`actions/checkout` and **1.0s** is the work.

Workflow wall clock, successful runs only:

| workflow | n | wall med | prev | wall p90 |
|---|---:|---:|---:|---:|
| Build | 45 | **360** | 370 | 442 |
| Validate Embedded Viewer | 44 | 76 | 76 | **129** |
| Suppressions | 48 | 21 | 22 | 25 |
| Changelog Lint | 37 | 10 | 11 | 12 |

Split by what the push touched:

| class | n | wall med | p90 | Build runner-min/run |
|---|---:|---:|---:|---:|
| sandbox-tools | 1 | 991 | 991 | 38.5 |
| docs only | 1 | 421 | 421 | 11.7 |
| **code + checkpoint slow-tests** | **16** | **412** | 473 | 22.0 |
| code + docs | 3 | 396 | 426 | 21.9 |
| code only | 20 | **352** | 388 | 15.9 |
| design/md only | 4 | **101** | 110 | 4.7 |

The `slow-tests` row is new and is a third of the window's successful Build
runs: touching `src/inspect_ai/util/_checkpoint/**` costs **+60s of wall and
+6.1 Build runner-minutes** over a plain code push. A push costs **19.1
runner-minutes** across the four PR workflows the snapshot sees (median over 53
pushes), against 18.9 last window.

### Critical path

Binding (last-finishing) job across the 45 successful Build runs:

| binding job | runs | wall median | median margin over runner-up |
|---|---:|---:|---:|
| `test (3.10)` | 18 | 351s | 12s |
| `test (3.11)` | 12 | 358s | 19s |
| **`slow-tests (checkpoint)`** | **7** | **448s** | **104s** |
| `mypy (3.10)` | 4 | 101s | 7s |
| `docs` | 3 | 421s | 75s |
| `slow-tool-tests-dev` | 1 | 991s | 560s |

The `test` legs still bind most runs, and still by a margin (12–19s) too small
for any per-leg saving to pay off twice. What is new is the middle row: on the
two branches that touched `src/inspect_ai/util/_checkpoint/**` this window,
`slow-tests` added **52–151s** of Build wall on top of whatever the `test` legs
cost. The four `mypy` bindings are the design/md-only pushes (#299 working as
intended — `mypy` is the long pole once `test` no-ops).

### Queue

687 independent-job samples: median **3s**, p90 **5s**, p95 15s, p99 39s, **max
67s**; 13 samples above 30s, **5 above 60s** (last window: p90 57s, max 157s, 65
above 60s). Per hour:

| hour (UTC) | runs | job samples | queue med | p90 | max |
|---|---:|---:|---:|---:|---:|
| 09-04 15 | 25 | 98 | 3s | 17s | 67s |
| 09-04 16 | 19 | 74 | 3s | 14s | 36s |
| 09-04 18 | 11 | 40 | 3s | 25s | 61s |
| 09-05 01 | 24 | 90 | 3s | 11s | 40s |
| everything else | ≤31 | ≤120 | 2–4s | 3–5s | ≤12s |

The honest reading: the window's densest 16-minute stretch is **24 runs**
(median wait 3s, p90 29s, max 67s), against the **47 runs in 16 minutes** that
produced last window's +68s of Build wall. Nothing here re-tested the burst, so
proposal 5 stays open rather than resolved.

## Where the pytest step actually goes

Timestamps from the raw job log of `test (3.10)` in upstream run 33936100151:

| phase | seconds | prev | note |
|---|---:|---:|---|
| `uv run` project re-sync | 5.4 | 5.3 | still present on `main` — the fix is in the open fork PR |
| startup + collection (5 interpreters) | 55.7 | 58.6 | assertion rewriting; scales with the suite |
| test execution | 244.8 | 247.6 | 875.5 worker-seconds over 4 workers |
| reporting (durations, summary) | 0.3 | 0.2 | `-ra`, holding |
| **step total** | **306.3** | 311.8 | window median 311 (3.10) / 315 (3.11) |

The raw log is **5,899 lines** against ~31,000 before #5075. The re-sync line is
still `Uninstalled 50 packages … Installed 53 packages` — `uv sync` +
`uv run --no-sync` is committed on this PR's branch but has not been merged
upstream, so `main` still pays it (proposal 4).

## Worker balance (`--dist worksteal`, #4948)

Tenth window holding, from both report-log artifacts of run 33936100151:
per-worker test seconds **215.8 / 213.2 / 215.5 / 231.1** on 3.10 (imbalance
+12.2s, efficiency 94.7%) and **236.3 / 239.1 / 236.7 / 241.0** on 3.11
(+2.7s, 98.9%). No stragglers on either leg; the 3.10 spread is the widest since
the fix landed but is a fifth of the 76–80s stragglers it replaced.

## Slowest tests

Median seconds per test across the 20 legs mined this window
(`--durations=50 --durations-min=1`, `call` + `setup` + `teardown` summed). 148
tests captured; per-leg tail total **217.2s** (median; 172.9–258.8), against
216.4s last window.

| s | test | classification |
|---:|---|---|
| 11.3 | `test_eval_set_selection.py::test_eval_set_selection_concurrent_workers` | genuinely heavy — three real subprocesses |
| 10.7 | `test_eval_set_scanner.py::test_scout_scan_resume_reruns_failed_scans` | genuinely heavy |
| 9.9 | `test_eval_set.py::test_retry_attempt_killed_mid_sweep_leaves_completed_samples_reusable` | genuinely heavy — kills a live attempt mid-sweep |
| 9.9 | `_control/test_launch_handoff.py::test_eval_detach_via_dotenv_detaches_exactly_once` | subprocess launch; pays `import inspect_ai` (#311) |
| 9.9 | `_control/test_launch_handoff.py::test_eval_detach_hands_off_and_leaves_eval_running` | subprocess launch (#311) |
| 7.8 | `_control/test_launch_handoff.py::test_eval_json_redirects_subprocess_stdout_to_stderr` | subprocess launch (#311) |
| 7.2 | `model/test_parse_tool_call.py::test_parse_error_on_deeply_nested_yaml_arguments` | guards a real uninterruptible hang (#5095); the cost is the point |
| 7.1 | `agent/test_agent_bridge.py::test_google_bridge_computer_use_incompatible_model` | ~3.9s is `traceback_ansi` rendering (#374) |
| 6.3 | `agent/test_agent_bridge.py::test_google_bridge_streaming_not_supported` | ~3.9s is `traceback_ansi` rendering (#374) |
| 6.3 | `test_eval_set_scanner.py::test_scanner_resume_accumulates_summary_…[s3]` | moto S3 + full eval-set resume |
| 6.1 | `test_eval_set.py::test_eval_set_previous_task_args` | ~5s real sleep around `keyboard_interrupt(2)` |
| 6.0 | `log/test_eval_log_config.py::test_eval_log_run_config_round_trip` | genuinely heavy |
| 5.7 | `_control/test_launch_handoff.py::test_eval_detach_fails_when_control_bind_fails` | subprocess launch (#311) |
| 4.4 | `test_sample_shuffle.py::test_sample_shuffle` | runs the whole `popularity()` dataset twice; `test_sample_shuffle_limit` asserts the same property on 20 samples |
| 4.3 | `_control/test_pause.py::test_eval_hard_pause_time_limit_reap_reparks_grader` | timer-bound |

Heaviest files by total worker time in the 3.10 report log (not just the tail):
`test_eval_set_scanner.py` 60.1s, `test_eval_set.py` 59.0s,
`_control/test_launch_handoff.py` 48.6s, `_control/test_eval_set_integration.py`
42.1s, `test_sample_limits.py` 28.0s, `test_eval_set_selection.py` 21.9s,
`agent/deepagent/test_deepagent_background.py` 20.3s,
`agent/test_agent_bridge.py` 19.0s.

### No per-test regression

Diffing per-test medians against the 2026-09-03 snapshot over the 96 tests in
both tails: sum **334.8 → 338.2s**, largest increase **+1.4s** (`test_eval_retry`
and `test_eval_set_limit_slices`), largest decrease **−1.8s**
(`test_retry_attempt_killed_mid_sweep_leaves_completed_samples_reusable`). 52
tests entered the tail and 61 left it, all near the 1s cutoff — ranking-boundary
churn, not new cost.

### Docker-trap sweep

Unchanged for eight runs: **6** test functions pair `skip_if_no_docker` with no
`@pytest.mark.slow` — `util/sandbox/test_docker_compose_config.py` ×3 (ungated,
never start a container), `tools/test_think_tool.py` ×2 and
`agent/test_agent_docs.py::test_agent_collect` (gated by `skip_if_no_anthropic` /
`skip_if_no_openai`). No new offenders.

## Suite size

| snapshot | collected items | pytest wall (median leg) | Build wall (success) |
|---|---:|---:|---:|
| 2026-08-25 | 13,449 | 304.8 | 353.0 |
| 2026-08-27 | 14,123 | 328.7 | 390.0 |
| 2026-08-29 | 14,674 | 287.1 | 342.0 |
| 2026-08-31 | 14,950 | 290.3 | 338.0 |
| 2026-09-01 | 15,220 | 289.6 | 346.0 |
| 2026-09-03 | 15,895 | 300.4 | 370.0 |
| 2026-09-05 | **16,415** | **302.9** | **360.0** |

+520 collected items in two days. Top-level test functions on `main`
(`^(async )?def test_` under `tests/`, re-derived per date):

| date | test functions | Δ |
|---|---:|---|
| 2026-08-31 | 8,468 | — |
| 2026-09-01 | 8,663 | +195 (1d) |
| 2026-09-02 | 8,934 | +271 (1d) |
| 2026-09-03 | 9,056 | +122 (1d) |
| 2026-09-04 | 9,151 | +95 (1d) |
| 2026-09-05 | **9,240** | **+89 (1d)** |

**+184 test functions in the two days this report covers**, roughly half the
previous window's +391. Growth has not stopped, it has returned to trend.

### Where the time sits (3.10 report log, 16,428 tests, 875.5 worker-seconds)

| band | tests | worker-s | share | prev share |
|---|---:|---:|---:|---:|
| ≥5s | 13 | 101.4 | 11.6% | 11.7% |
| 1–5s | 159 | 277.9 | 31.7% | 30.5% |
| **0.1–1s** | **1,152** | **403.4** | **46.1%** | 48.2% |
| <0.1s | 15,104 | 92.9 | 10.6% | 9.5% |

Phases: call 817.7s, setup 36.9s, teardown 20.9s. 11,521 passed, 4,907 skipped —
30% of collected items never run in the PR gate. Worker time **906.7 → 875.5s**
(3.10) while the suite grew 520 items: the first two-day window in this series
where measured test time went *down*, and it is inside the ±60s noise band, so
read it as flat rather than as an improvement. The 3.11 leg of the same run is
953.0s.

The shape is stable across six windows: the ~92% of tests under 0.1s are ~10% of
the time, and the 0.1–1s band is just under half.

### Duplicate-coverage and low-value sampling

The AST sweep (identical decorators + signature + body) finds **3 groups / 7
tests**, down from 5 groups / 11 tests: the pair that was a genuine defect is
fixed on this branch. The remaining three are coincidental — the same one-line
assertion in three provider `test_known_models_not_latest` files, two CLI
`test_omitted_returns_none` flag tests, and a pair of nested `async def
test_func` helpers pytest never collects. Nothing worth deleting.

## Regressions since last report

**No per-job execution regression.** Every job median is within 1–5s of last
window; the three Viewer p90 rises are one external incident (see Waste). Test
worker time fell 906.7 → 875.5s across +520 collected items, and the matched
per-test tail moved +3.4s over 96 tests.

**Queue recovered** — p90 57 → 5s, max 157 → 67s — but see the critical-path
note: the window contained no comparable burst.

Red checks a contributor actually sees — **3 failed job records in 200 runs**,
the cleanest window in the series: `submodule-on-main` 2, `entries-under-unreleased`
1. Zero `test`-leg failures. Last window's changelog-relocation cluster (14
failures across 13 branches after the 0.3.261 release) has fully cleared.

## Waste

- **The `pnpm/action-setup@v6` stall: 64 runner-minutes.** 13 Viewer jobs
  between 09-04 07:47 and 11:48 UTC took **94–426s** in that step against a
  5–6s median (all three Viewer jobs affected, across 2 branches / 5 pushes);
  worst case a `check-schema-and-types` job of 470s against a 59s median. That
  is the entire Viewer compute rise (133 → 202 runner-min) and the entire Viewer
  wall p90 rise (100 → 129s). The action resolves and downloads pnpm from the
  npm registry on every job, so this is an external dependency with no local
  cache — the same failure shape `blob:none` was introduced to fix for git.
  One incident; recorded, not yet a proposal.
- **`Suppressions comment` full-history checkout: 25.2 runner-minutes.** 54 runs
  × 28.0s of `fetch-depth: 0` for a job whose actual work is 1.0s. **Fixed in
  this PR**: driving `actions/checkout`'s own refspec end to end, the job's git
  sequence goes **22.54s → 5.36s** and `.git` **421MB → 37MB**, with byte-identical
  merged-tree OIDs and exit statuses against a full clone on nine upstream PRs
  (six clean, three conflicted). See the note below on what the review pass
  caught.
- **Cancelled superseded jobs: 16.0 runner-minutes** across 13 jobs / 4
  cancelled runs (was 44.0 over 16), led by `mypy` 5.6 and `viewer-tests` 2.1.
- **Failed jobs: 0.4 runner-minutes** over 3 jobs (was 8.2 over 22).
- **Duplicated Quarto renders: none observed.** 0 of the 17 `docs-render-*` keys
  created since #5220 exist under both a PR ref and `refs/heads/main` (was 36 of
  129). By construction: a PR's key now carries its own non-empty source delta,
  which `main`'s key never has.
- **`uv run` re-sync: 5.4s per `test` leg, ~3.5s per `mypy` leg**, plus the
  `slow-tests`, `slow-tool-tests-*` and `docs` sites — still on `main`, fixed for
  `test`/`mypy` on this branch (proposal 4).
- **Compute: 1,012 runner-minutes** per 200 PR runs (Build 792, Viewer 202,
  Suppressions 13, Changelog Lint 4), plus **203 runner-minutes** the collector
  never fetches — 9 push-event Build runs (138.9), 54 `Suppressions comment`
  runs (29.8), 9 push Viewer runs (25.1), 14 `PR Gate` runs (1.9) and a
  Dependabot run (4.7). **1,215 runner-minutes over 17.6h** (~69/hour).
- **Runs that never ran: 15 `action_required`** (was 0) — first-time-contributor
  approval gates; no runner time, but 15 pushes got no feedback until a
  maintainer approved.
- **Overhead-dominated jobs:** `changes` 7s, `detect-slow` 9s,
  `submodule-on-main` 8s, `ruff` 11s, `suppressions` 16s — ~51s of runner time
  per push, none of it on the critical path.

## What the review caught

Worth recording, because it is the first time this skill's mandatory
fresh-context review pass found a real defect in a fix the run was about to
ship, and because the failure mode generalises to every future `blob:none`
conversion.

`filter: "blob:none"` is not purely a fetch optimisation for this job: it turns
one existing line into a *network* read. When the PR itself edits
`suppressions.json`, the merged tree's entry for that path is the PR's own blob,
and `git merge-tree` never opens it (a one-sided change is taken by OID), so it
is not local. The workflow's

```sh
git show "$merge_tree:suppressions.json" > … 2>/dev/null || echo '{}' > …
```

therefore acquires a second reason to fail that it cannot distinguish from the
first. Under a full clone the only way that `git show` fails is the ledger being
absent from the merged tree, for which `{}` is the right answer. Under
`blob:none` a promisor-fetch failure lands in the same branch — and `{}` renders
as **"Suppression ledger changed: 788 → 0 (-788)"**, a 392-line false
burn-down listing every rule in the repo as removed, which is precisely what
`suppressions_pr_delta.py`'s own docstring says must never be produced.
Reproduced end to end here.

The fix, in the same commit: tree objects are always local under `blob:none`, so
`git rev-parse --verify -q "$merge_tree:suppressions.json"` answers "does this
path exist" without the network. The deletion fallback is preserved; a genuine
fetch failure now fails the job (verified: exit 128 under `GIT_NO_LAZY_FETCH=1`,
against the silent 392-line comment the old form produced under the same
conditions).

The other lazy-fetch site was already safe: `git merge-tree` exits 128 on a
promisor failure, which the existing `-ne 0 && -ne 1` guard turns into a red
job.

One consequence for the next run's impact check: `suppressions-comment.yml` is a
`pull_request_target` workflow, so it always runs the *base* repo's copy. The
PR carrying this change cannot exercise it, and the saving will only become
observable once it merges — and then only by fetching those job records by hand,
since the collector does not see `pull_request_target` at all (proposal 9).

## Impact verification (previous runs' changes)

- **#5220 / #317 (docs render cache key) — merged 2026-09-03 21:48Z, mechanism
  confirmed, hit rate not yet demonstrated.** The cache API is the only place
  the hits are visible (a hit updates an entry's `last_accessed_at`; it creates
  nothing): **2 of the 17 post-merge keys were re-read**, both same-branch
  re-pushes 3 and 7 minutes apart, on PRs 5233 and 5236 — and that is exactly the
  case the 2026-09-01 report recorded as having *stopped* hitting under the old
  key. Inside this snapshot's window, **0 of 7 `docs` jobs hit**: five were first
  renders for their branch, one followed a *cancelled* render (which saves no
  marker, by design), and one was a docs-only PR's first push. The
  duplicate-key waste that dominated the last four reports is gone: **0 of 17
  post-merge keys are duplicated across a PR ref and `main`**, against 36 of 129.
  The prediction in #5220 — "roughly 23%, not 100%" — is neither confirmed nor
  refuted by 7 jobs; the next window should have enough `docs` runs to say.
- **#5075 (`-rA` → `-ra`) — holding, sixth window.** Reporting phase 0.3s, raw
  test-leg log 5,899 lines.
- **#4948 (`--dist worksteal`) — holding, tenth window** (+12.2s / 94.7% on 3.10,
  +2.7s / 98.9% on 3.11, no stragglers).
- **#4760 (`test_package` pre-installed) — holding**: the 11 `test_extensions`
  tests cost 0.48s in total (3.11 report log) and no mid-run install appears in
  the raw `test (3.10)` log.
- **#4935 (`blob:none` checkouts) — holding.** Checkout is 2–6s median in every
  Build and Viewer job. The one always-run step whose p90 exceeds 2× its median
  is the `test`-leg checkout (2s median, 7s p90) — 5s of absolute spread, not
  worth chasing. This report extends the same change to the last per-PR job
  without it.
- **#299 (`design/**` excluded from the test filter) — holding**, 4 observations
  on `epatey/agents-md-provider-slow-tests`: `test` legs 4–7s each, Build wall
  **96–114s** against a 357s code-only median, 4.2–4.8 Build runner-min per push.
  `mypy` binds all four.
- **#393 (control-server startup) — filed 2026-09-01, no action yet.** Both
  candidate fixes remain maintainer decisions.
- **#374 (traceback rendering) — unchanged, still open.** The two Google bridge
  tests are 7.1s and 6.3s.
- **The 2026-09-03 burst (proposal 5) — no recurrence, and no re-test.** See
  "Queue" above: the densest 16-minute stretch this window was 24 runs against
  47, so the pool was never asked the same question.

## Proposals (ranked)

1. **Stop paying 214ms of control-server startup on every `eval()`.** Carried,
   unchanged, and still the largest measured lever in this series. A one-sample
   `mockllm` eval is 249ms with the control channel and 35ms without; the 214ms
   splits 30ms building an identical 28-route FastAPI app, 30ms binding, and
   100ms waiting out uvicorn's fixed 0.1s `should_exit` poll. 796 test functions
   across 153 files call `eval()`/`eval_set()` directly, and a full-suite A/B on
   4 workers runs **725.8 → 529.0s of wall (−27%)** with **381 tests leaving the
   0.1–1s band** — the band that is still 46% of CI test time. Extrapolated to a
   CI leg (245s of execution over 4 workers, 876 worker-seconds), on the order of
   **50–60s off both `test` legs**, the only lever large enough to clear the
   12–19s binding margin on both at once. Not shipped: the test-side fix moves
   coverage of the *default* configuration out of the bulk of the suite, and the
   product-side fix changes eval teardown semantics. Status: carried,
   [#393](https://github.com/meridianlabs-ai/inspect_ai/issues/393).

2. **Cache pytest's assertion-rewrite bytecode across runs.** Carried.
   Startup+collection is **55.7s of the 306.3s step (18%)**, essentially flat on
   last window's 58.6s of 311.8s despite +520 collected items. Fix shape: restore
   `**/__pycache__` from `actions/cache` keyed on a hash of `src/**/*.py` +
   `tests/**/*.py`, normalizing source mtimes deterministically after checkout in
   both the producing and consuming run (pytest validates a rewritten pyc against
   source mtime + size). Est. ~30s off both `test` legs. Pushable since the
   `workflow` scope cleared, but still **needs one CI experiment before it is a
   safe fix** — the mtime normalization is the part that can silently no-op, and
   a no-op that *looks* like a hit is worse than no cache. What does not work,
   both measured on 2026-08-29: `compileall` and `uv`'s `compile-bytecode`.
   Status: carried, next in line to ship once measured.

3. **`slow-tests` runs its Docker checkpoint tests serially, and is now the
   third binding job class.** NEW. The job was a two-run curiosity last window;
   this one it ran **16 times** at **362s median / 458s p90** and was the
   last-finishing job in **7 of 45 successful Build runs by a 104s median
   margin** (52–151s) — five times the 12–19s the `test` legs bind by. Touching
   `src/inspect_ai/util/_checkpoint/**` now costs **412s of Build wall against
   352s** for a plain code push, and **22.0 Build runner-minutes against 15.9**.
   The cause is one missing flag: it runs `uv run pytest --runslow -m slow
   tests/<area>/` with no `-n`, so its tests execute one at a time on a 4-vCPU
   runner. Upstream job 101121776112: **20 passed, 11 skipped, 456 deselected in
   421.7s**.

   Measured both ways in this sandbox, which reproduces the CI job exactly
   (identical 20/11/456 counts):

   | arm | result | wall |
   |---|---|---:|
   | serial (today's CI) | 20 passed, 11 skipped | **446.2s** |
   | `-n 4` | **4 failed**, 16 passed, 11 skipped | **224.2s** |

   So the prize is real — **−222s, half the job, and ~100s of Build wall on
   every checkpoint push** — and it is **not a safe fix**, because the four
   failures are a genuine isolation defect rather than flakiness. Every one is a
   test asserting on *global* Docker state: `expected the killed attempt to leak
   its sandbox container / assert []` (×2), `Ctrl-C should tear down the sandbox;
   leaked ['51c0c3b0be60', 'c88ba6cfec9b']`, and a container-count assertion
   returning 4 where 2 was expected. Concurrent tests see each other's
   containers. Making the job parallel therefore requires first scoping those
   assertions to the containers a given test owns (e.g. a per-test compose
   project or label filter) — a product-adjacent test change with real
   correctness content, not workflow hygiene. Filed for a maintainer decision.
   Status: **new**, filed as
   [#444](https://github.com/meridianlabs-ai/inspect_ai/issues/444).

4. **`uv run` re-syncs the environment the install step just built.** Carried
   from last window, where the `test`/`mypy` half shipped in this PR's first
   commit — but the PR has not been promoted upstream, so `main` still pays
   5.4s per `test` leg and ~3.5s per `mypy` leg. The remainder is unchanged and
   deliberately unshipped: for `docs` and `sandbox-tools-unit` dropping the sync
   would change *which dependency versions the job tests against* (14 packages
   differ for the injectable, including the `mcp 2.1.1 → 2.0.0` that
   [#308](https://github.com/meridianlabs-ai/inspect_ai/issues/308) was filed
   about), and `slow-tool-tests-release` *depends* on the sync replacing its
   wheel install with the lockfile's editable one so `_binaries_dir()` resolves
   to the tree the published binaries were downloaded into. Status: carried,
   half shipped, **awaiting promotion**.

5. **Burst contention costs measurable wall clock — unresolved, not disproved.**
   Last window's single observation stands: 47 runs across 6 branches in 16
   minutes cost each of them **+68s of Build wall (350 → 418s) with test
   execution unchanged**. This window's queue is back to a 3s median and a 5s
   p90, but its densest 16-minute stretch was only 24 runs, so nothing re-tested
   it. What the data still does *not* support is buying runners — concurrency
   reached 35 on the burst evening, so the pool ramps. The lever, if one is
   wanted, is jobs-per-push: a push produces 13–21 job records across four PR
   workflows plus a `Suppressions comment` run, and this report's safe fix takes
   22s off the most frequent of them. Status: carried, **awaiting a second
   observation**.

6. **Defer the `acp.schema` import.** Re-measured today on `main` in this
   sandbox: `import inspect_ai` is **1,147ms**, of which `acp.schema` is
   **299ms of self-time (26%)** — **6.5×** the next-largest module
   (`inspect_ai.log._log`, 46ms) — reached eagerly through
   `inspect_ai._eval.eval` → `agent._acp.server`. Absolute numbers are lower
   than the 483ms/1.70s recorded on 2026-08-29 (different sandbox load); the
   share is unchanged. Paid by 5 interpreters per leg plus the four
   `_control/test_launch_handoff.py` tests that hold slots 4, 5, 6 and 13 of the
   tail. Product change with a public-API surface. Status: carried,
   [#311](https://github.com/meridianlabs-ai/inspect_ai/issues/311).

7. **Test-volume policy — it is the 0.1–1s band that matters.** Sixth window
   confirming it: 15,104 tests under 0.1s are 10.6% of test time; 1,152 tests
   between 0.1s and 1s are 46.1%. Growth returned to trend this window (+184 test
   functions in two days, against +391) and measured worker time went *down*
   31s — the first two-day window in the series where it did. Proposal 1 remains
   the sharp form of the question: much of that band is not what the tests
   *assert*, it is what `eval()` *costs*. Structural. Status: carried.

8. **`tests/util/test_display_counter.py` sleeps 6 × 1.1s for 2 throttle paths.**
   Carried at **10.12s of worker time** in this window's 3.11 report log (n=6).
   Re-examined for a mock-clock fix on 2026-09-01 and rejected:
   `inspect_ai.util._throttle` reads `time.time()` directly *and* schedules a real
   `anyio.sleep(remaining)` trailing-edge fire in a background task, so faking the
   clock without also faking the sleep changes what the test exercises. The honest
   options remain a coverage judgement (drop the sleep for the params whose
   `@throttle(5)` a 1.1s sleep can never fire) or an injectable throttle window
   (product change). Status: carried.

9. **Collector: fetch more than 200 runs, and fetch more than `pull_request`.**
   Carried, and it cost real accuracy again this run. (a) At 11.3 runs/hour a
   200-run snapshot spans 17.6h against a ~2-day cadence, so **22.6h between this
   window and the last is uncovered**. (b) The `event=pull_request` filter hides
   **203 runner-minutes over 97 runs and 250 job records** this window — including
   the `Suppressions comment` job this report's safe fix targets, which had to be
   measured by hand because the snapshot cannot see it. (c) The stale-page bug
   fired for the **fifth** time (attempt 1: a 471h window with a 417h hole);
   `warn_on_time_gap` caught it, but the fix is to retry rather than to print.
   All three fixes are one file,
   `.claude/skills/ci-perf/scripts/collect_ci_data.py`, **still unwritable by the
   agent's edit tooling** — re-probed today, refused as a sensitive file
   (proposal 10). Status: carried.

10. **Unblock the scheduled run — two of three blockers cleared, one stands.**
    Re-probed today:
    - *`workflow` scope* — **CLEARED** (2026-09-03), and used for the first time
      this run: the safe fix below is a `.github/workflows/**` change.
    - *No upstream write* — still blocked. `repos/UKGovernmentBEIS/inspect_ai`
      reports `{"admin":false,"maintain":false,"pull":true,"push":false,
      "triage":false}` for this token; PR creation attempted at the end of this
      run (result recorded in `prs.md`).
    - *`.claude/**` unwritable by the agent's edit tooling* — still blocked; a
      plain write under `.claude/skills/ci-perf/` was refused as a sensitive
      file. Blocks proposal 9.

    Status: carried, updated on
    [#298](https://github.com/meridianlabs-ai/inspect_ai/issues/298).

11. **Pin or cache `pnpm/action-setup`'s pnpm download.** NEW, report-only on one
    observation. 13 Viewer jobs in a 4-hour band took 94–426s in that step
    against a 5–6s median — 64 runner-minutes, and 6× Viewer wall for the two
    branches that were pushing at the time. The action fetches pnpm from the npm
    registry on every job with no local cache, which is the same
    external-dependency variance pattern `filter: "blob:none"` was introduced to
    fix for git. Worth watching for a second occurrence before proposing a
    mitigation; a single registry incident is not a workflow defect.
    Status: **new**, report-only.

12. **Merge the 4 Viewer jobs into 1–2** — required-check rename. Same standing
    argument, with a new data point: all three Viewer jobs paid the pnpm stall
    *independently*, three times over, because each sets up its own toolchain.
    Structural. Status: carried, low.

13. **Duplicate and near-duplicate test cleanups.** The strict AST sweep is down
    to 3 groups / 7 tests, all coincidental one-liners with no cleanup value,
    after the genuine defect was fixed on this branch. The judgement-based
    candidates are unchanged: `test_sample_shuffle` (4.4s) runs the full
    `popularity()` dataset twice to assert the property `test_sample_shuffle_limit`
    already asserts on 20 samples, and `test_eval_set_previous_task_args` (6.1s)
    is ~5s of real sleep. Both are coverage judgements, not safe fixes.
    Status: carried, low.

14. **Policy consistency: docker tests without `@pytest.mark.slow`.** Still six,
    still ~0.05s combined; the right fix is probably to drop `skip_if_no_docker`
    from the three ungated ones rather than to mark them slow. Zero wall-clock
    impact. Status: carried.

Nothing dropped this report. Last report's proposal 15
(changelog-entry relocation as the top source of red checks) is **resolved**:
zero `entries-under-unreleased` failures beyond a single one this window, the
release-relocation cluster having cleared.

## PRs opened by this skill

See `prs.md`. The previous run's PR
([meridianlabs-ai/inspect_ai#408](https://github.com/meridianlabs-ai/inspect_ai/pull/408))
is still open and green, so this run pushes onto its branch rather than opening
a second PR, per the unattended rule. It adds the snapshot, this report, the
ledger row, and one safe fix (`filter: "blob:none"` on the `Suppressions
comment` checkout).

One new issue was filed —
[#444](https://github.com/meridianlabs-ai/inspect_ai/issues/444), the serial
`slow-tests` job, with the measured `-n 4` result and the four isolation
failures that block it — and new evidence was posted to
[#317](https://github.com/meridianlabs-ai/inspect_ai/issues/317) (first
measurement after #5220) and
[#298](https://github.com/meridianlabs-ai/inspect_ai/issues/298) (blockers
re-probed: `workflow` scope cleared and used for the first time; `.claude/**`
and upstream write still blocked).
