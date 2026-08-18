# CI performance report — 2026-08-18

Data: 200 PR runs, 2026-08-18 00:47 .. 19:10 UTC. Snapshot:
`history/2026-08-18.json`. Previous: 2026-08-12. Durations mined from 40
test jobs (20 Build runs). Prompted by meridianlabs-ai/inspect_ai#253
("CI tests are slow" — pytest was ~5 min, now ~7).

## Summary

Build median wall clock 498 s (8.3 min), of which the `test` job is 442 s
exec + 414 s of that is the pytest step. The slowest-tests table is now
flat — the 43 s docker test fixed in #4848 is gone and nothing exceeds
7 s — yet the pytest step did not move (415 → 414 s median): the tail of
~13,000 sub-second tests grew to absorb the savings, and the top-50
tests (>1 s) account for only ~150 of ~830 worker-seconds. Test-level
fixes are exhausted as a lever. The dominant finding this run: **CI runs
pytest-xdist with only 2 workers on a 4-vCPU runner** — `-n auto`
resolves to *physical* cores (2) because `psutil` (a runtime dependency)
is installed; `ubuntu-latest` is 4 vCPU / 2 physical. Benchmarked on an
identical 4-vCPU/16 GB runner, `-n logical` (4 workers) cuts the full
suite from 464 s to 419 s (see proposal 1 and the benchmark section for
oversubscription data and noise caveats).

## Queue vs execution

Median execution / queue, successful runs. Queue for dependent jobs is
measured from predecessor completion (`needs` map read from the workflow
files).

| Workflow | Job | n | exec med | exec p90 | queue med | queue p90 |
|---|---|---|---|---|---|---|
| Build | slow-tool-tests-dev | 13 | 790s | 987s | 86s* | 308s* |
| Build | test (per matrix leg) | 111 | 442s | 497s | 4s | 131s |
| Build | docs (when docs change) | 17 | 379s | 414s | 14s | 127s |
| Build | mypy (per matrix leg) | 116 | 89s | 95s | 4s | 158s |
| Viewer | viewer-tests | 63 | 58s | 68s | 3s | 69s |
| Viewer | check-schema-and-types | 63 | 54s | 63s | 3s | 103s |
| Build | pre-commit | 63 | 33s | 37s | 4s | 141s |
| Build | package | 63 | 30s | 35s | 4s | 140s |
| Viewer | dist-validation | 63 | 28s | 33s | 3s | 70s |
| Build | ruff | 60 | 11s | 14s | 3s | 188s |
| Build | detect-slow | 63 | 8s | 10s | 3s | 127s |
| Build | changes | 63 | 7s | 9s | 4s | 161s |

\* wait measured from run start (predecessor adjustment not applied in
this table for the slow-tools chain).

Queue medians remain ~3–4 s, but p90 crept back to 130–190 s (was
9–17 s on 08-12) — mild batch-push contention returned. Still second-order
next to the 414 s pytest step.

`check-schema-and-types` dropped 74 → 54 s median and the 216 s checkout
tail is gone — the `filter: blob:none` fix from #4848, verified.

### Critical path

- **Ordinary PR:** `test` — 4 s queue + 442 s exec vs 498 s Build wall.
  Inside `test`: 414 s pytest, 10 s deps install, ~15 s everything else.
- **Sandbox-tools PR:** `detect-slow` → `check-version-bump` →
  `slow-tool-tests-dev` (790 s) → `slow-tool-tests-release`, serialized by
  `needs` (proposal 3, carried).

## Slowest tests

Median seconds across 40 CI test jobs, `call`+`setup`+`teardown` combined,
asyncio/trio variants merged. 172 tests captured at `--durations-min=1`.
The table is now flat: the top 15 sum to ~70 s spread across 2 workers,
and the top-50 sum (~150 s) is ~18% of total worker time — the other ~82%
is ~13,000 sub-second tests plus collection/imports.

| Median | Test | Classification |
|---|---|---|
| 6.7s | `test_eval_set_scanner.py::test_scout_scan_resume_reruns_failed_scans` | genuinely heavy (was 8.8s, improved) |
| 6.5s | `test_launch_handoff.py::test_eval_detach_via_dotenv_detaches_exactly_once` | real CLI subprocess (inherent, examined 08-12) |
| 6.5s | `test_launch_handoff.py::test_eval_detach_hands_off_and_leaves_eval_running` | (as above) |
| 5.9s | `test_eval_set.py::test_eval_set_previous_task_args` | multi-eval eval-set flow |
| 5.5s | `test_eval_set_scanner.py::test_scanner_resume_...[s3]` | moto S3 + eval-set resume |
| 4.8s | `test_launch_handoff.py::test_eval_json_redirects_subprocess_stdout_to_stderr` | real CLI subprocess (needs real fds) |
| 4.7s | `test_eval_log_config.py::test_eval_log_run_config_round_trip` | not yet examined |
| 4.6s | `test_agent_bridge.py::test_google_bridge_streaming_not_supported` | not yet examined |
| 4.5s | `test_agent_bridge.py::test_google_bridge_computer_use_incompatible_model` | not yet examined |
| 4.3s | `test_pause.py::test_eval_hard_pause_time_limit_reap_reparks_grader` | new; real pause/reap timing |
| 4.2s | `test_sample_limits.py::test_working_limit` | real waits inherent to limit tests |
| 4.0s | `test_launch_handoff.py::test_eval_detach_fails_when_control_bind_fails` | real CLI subprocess |
| 3.9s | `test_sample_shuffle.py::test_sample_shuffle` | not yet examined |
| 3.4s | `test_sample_limits.py::test_solver_timeout_not_scored` | real waits |
| 3.3s | `test_sample_limits.py::test_working_limit_reporting` | real waits |

No test is a slow-suite candidate by policy (no docker, no unmocked
external services in the PR-gate list), and none is big enough to move
the needle alone.

## Worker-count benchmark (2026-08-18)

Full suite on a 4-vCPU/16 GB GitHub Actions runner (same spec as CI),
CI flags minus report-log. Every run had the identical 11 pre-existing
failures (runner-environment artifacts: stderr capture under the action
harness, a workload-identity env var breaking one Anthropic test) — no
new failures at any worker count, including the timing-sensitive
working-limit/timeout tests.

| Workers | Wall | CPU (user+sys) | Avg cores busy |
|---|---|---|---|
| `-n auto` = 2 (current CI) | 463.9s | 661s | 1.4 |
| `-n logical` = 4 | 418.8s | 856s | 2.0 |
| `-n 8` | 372.0s | 1181s | 3.2 |
| `-n 12` | 399.4s | 1255s | 3.1 |
| `-n 8` (repeat) | 480.0s | 1240s | 2.6 |

Reading the data honestly:

- **Run-to-run noise is large.** The two `-n 8` runs differ by 29%
  (372 vs 480 s) at near-identical CPU-seconds (1181 vs 1240) — the
  slow run had ~0.6 fewer cores available on average, i.e. external
  contention on the shared runner host. CI's own step data shows the
  same band (414 s median, 468 s p90). Single-run deltas smaller than
  ~±13% are not conclusive.
- **The suite is IO/sleep-bound**: 2 workers keep only 1.4 cores busy.
  `-n logical` raises utilization to 2.0 cores and is the only option
  that is a pure config fix (use the CPUs we're already paying for).
- **Oversubscription buys wall clock but costs real CPU**: total work
  grows 661 → ~1200 CPU-s from 2 → 8 workers, mostly per-worker
  import+collection of the ~13k-item suite (with `--doctest-modules`
  importing all of src). That growing fixed cost is why `-n 12` is
  slower than `-n 8`. On a contended host the extra CPU demand also
  makes wall clock *more* variable (see the repeat run) and raises the
  flake risk for timing-sensitive tests.

## Impact verification (previous run's PRs)

#4848 (merged 08-12) — mixed:

- **43 s docker `read_file` test** — gone from the durations data;
  confirmed marked slow.
- **`check-schema-and-types` blob:none** — 74 → 54 s median, 216 s max
  tail gone. Held.
- **Predicted −15 to −40 s on `test` exec** — did **not** hold: pytest
  step 415 → 414 s. The freed worker time was absorbed by suite growth
  (122 → 172 tests captured at the 1 s cutoff; upstream merges added
  tests throughout the week). Honest miss: with 2 workers and a flat
  tail, removing one 40 s test only helps if it was on the critical
  worker at the end of the schedule.

## Regressions since last report

- None at test level (biggest mover: `test_sample_shuffle` 3.3 → 3.9 s).
  Suite-level: captured-test count grew 122 → 172; total pytest time flat
  despite the docker fix, i.e. steady organic growth (~40 worker-seconds
  this week).
- Queue p90 regressed 9–17 s → 130–190 s (batch-push contention; medians
  unchanged).

## Waste

- Cancelled superseded runs: 3/200, 10 runner-min. Negligible.
- Compute: 1,608 runner-min total (up from 1,353; more sandbox-tools
  runs in window: 13 slow-tool-tests-dev at 790 s median).
- **The tooling failure that hid this run's data:** `gh` ≥ 2.9x refuses
  to print job logs containing ANSI escape sequences without
  `--allow-escape-sequences`; pytest runs with `--color=yes`, so every
  log fetch in the collector failed silently (caught
  `CalledProcessError` → skip) and the snapshot's `pytest_durations` came
  back empty. Durations for this report were re-mined with a patched
  fetch. Fix for the collector (blocked from direct edit this run by
  sandbox permissions on `.claude/`): in `gh_api_text`, add
  `--allow-escape-sequences` to the `gh api` invocation and strip
  `\x1b\[[0-9;]*m` from the returned text before parsing; also make
  `collect_durations` print a warning instead of silently returning `{}`
  when every fetch errors.

## Proposals (ranked)

1. **Run pytest with `-n logical` instead of `-n auto`** (build.yml test
   job). `-n auto` = physical cores (2) when psutil is importable —
   psutil is a runtime dependency of inspect_ai — so CI has been running
   at half the runner's parallelism. `-n logical` = logical cores (4).
   Measured 464 → 419 s (−10%) on an identical runner; single sample,
   but the utilization data (1.4 → 2.0 cores busy) supports a real
   improvement. Est. −40 s on the 414 s CI pytest step; verify against
   next snapshot. Risk: timing-sensitive tests see more contention at 4
   workers — no failures in the benchmark runs, watch flake rate after
   merge. Safe fix. Status: **new — this run's PR**.
1b. **Evaluate `-n 8` after 1 lands** — best single run 372 s (−20%) but
   29% run-to-run noise, +80% CPU cost, and higher flake exposure. Only
   worth it with a week of CI data at `-n logical` as baseline. The
   cleaner long-term lever is cutting per-worker collection cost (see
   proposal 7). Status: new, experiment.
2. **Collector fix for gh ≥ 2.9x ANSI refusal** (see Waste). Without it
   every future snapshot silently loses test-level data. Safe fix, but
   `.claude/` edits were permission-blocked this run — apply next run or
   by hand. Status: new.
3. **Un-serialize `slow-tool-tests-release` from `slow-tool-tests-dev`** —
   would cut ~13 min from sandbox-tools PR wall clock (29–34 min). The
   sequence is deliberate (`design/sandbox-tools-ci-gates.md`), so
   maintainer call. Structural. Status: carried.
4. **Larger runners (8-core) for the `test` job** — both repos are
   public: standard runners are free, larger runners are paid per-minute
   with no free allotment (~$0.032/min × ~8 min × 2 legs × dozens of
   runs/day ≈ meaningful spend). Unlike oversubscription, real cores
   would cut wall clock without the contention/flake risk — with the
   suite at 2.0/4 cores busy under `-n logical`, an 8-core runner at
   `-n logical` (8 workers, ~1200 CPU-s of work) plausibly lands near
   3–4 min. The issue (#253) explicitly floats this; it is a
   maintainer cost decision. Structural/cost. Status: carried, option
   if proposal 1 isn't enough.
5. **Merge the 4 Viewer jobs into 1–2** — compute/batch-resilience
   argument only; queue medians are still ~3 s. Structural. Status:
   carried, low priority.
6. **Cache the Quarto render for `docs`** — 379 s on docs PRs; second
   longest job but only on docs changes, and below `test`. Structural.
   Status: carried, low priority.
7. **Suite-growth watch** — the tail grows ~40 worker-seconds/week;
   at 4 workers that's ~10 s/week of wall clock. If growth continues,
   the next levers are collection cost (`--doctest-modules` imports all
   of src for a handful of doctests) and splitting the suite into
   sharded jobs. Status: new, monitor.

## PRs opened by this skill

- #4746 — add `--durations=50` to Build pytest (2026-08-04, **merged**,
  impact verified)
- #4747 — remove `changes` → `test` serialization (2026-08-04, **merged**,
  impact verified)
- #4748 — the ci-perf skill itself (2026-08-04, **merged**)
- #4760 — two slow-test fixes + `blob:none` checkouts + slow-test policy
  docs (2026-08-05, **merged**, impact verified)
- #4848 — mark the 43 s docker test slow, `blob:none` on the Viewer
  checkout, collector hardening (2026-08-12, **merged**; docker +
  checkout verified, test-exec estimate missed — see impact section)
- PR_PLACEHOLDER — `-n logical` for the Build pytest step (2026-08-18,
  this run)
