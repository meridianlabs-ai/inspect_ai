# Chaining a retry's sample source across attempt logs

Design for [meridianlabs-ai/inspect_ai#420](https://github.com/meridianlabs-ai/inspect_ai/issues/420)
(upstream [UKGovernmentBEIS/inspect_ai#5213](https://github.com/UKGovernmentBEIS/inspect_ai/issues/5213)).
Follows on from [retry-deferred-destination-log.md](retry-deferred-destination-log.md)
(the hard-kill variant, #4933) and the reuse-sweep machinery in
[retry-reused-sample-flush.md](retry-reused-sample-flush.md).

## Problem

A retry attempt seeds itself from exactly one prior log. Every planned
`(id, epoch)` is looked up in that log by the reuse sweep in `run_sample`
(`_eval/task/run.py`): a clean completed sample is re-logged into the new
attempt with `write_through=True`, an errored one seeds `PreviousError`
history, and anything else runs live. Live samples do not wait for the sweep,
and the sweep's prior-log body reads are bounded to 25 at a time, so on a
large prior log on remote storage the sweep can run for minutes while live
samples are already executing.

If the attempt *errors* while the sweep is still running — a live sample fails
and `fail_on_error` tears the task down, a `TerminateTaskError` from
`inspect ctl`, a Ctrl-C, or the prior-log read itself raising a transport
error out of `read_from_file` — `task_run`'s terminal branches call
`finish_task_log`, which writes whatever the sweep had re-logged so far plus
the live results. That log is a legitimate error/cancelled record of the
attempt, and it is now the task's newest log. It is also missing every prior
completed sample the sweep never reached.

The next attempt then uses only that partial log:

- eval_set (`retry_immediate=False`) picks the newest log per task by mtime
  in `latest_completed_task_eval_logs` (`_eval/evalset.py`) and builds the
  source from it in `as_previous_tasks` → `resolve_previous_task`
  (`_eval/loader.py`).
- The in-process retry (`retry_immediate=True`, or `eval(retry_attempts=)`)
  builds the source directly from `options.logger.location` — the attempt
  that just errored — in `_run_task`'s retry branch (`_eval/run.py`).

Both paths re-run from scratch every sample that completed in the older log
but is absent from the newer one. In the production incident that was 34 of
47 samples (25 of them cost-capped). With the default `retry_cleanup=True`
the older log is deleted at the start of the next pass
(`list_latest_eval_logs(..., cleanup_older=retry_cleanup)` removes every
non-`started` log that isn't the newest), so the completed results are gone
before the retry even starts — duplicated spend becomes permanent loss.

### Why #4933 doesn't cover this

That fix enforces "no destination write until the reuse sweep settles", so a
hard kill mid-sweep leaves no file and the next retry chains to the older
log. `TaskLogger.log_finish` is deliberately exempt: it is the attempt's own
terminal record and must always land. The graceful-error path therefore still
produces a newest log with a partial reused set. The #240 design filed this
as an accepted "bounded re-spend, no data loss" trade-off; the incident shows
the bound is the whole prior log and, with cleanup on, it *is* data loss.

The `carry_forward_unlogged_samples` teardown step doesn't help either: it
re-logs only samples with prior *error* history (`PreviousError`), probing
`error_history_ids()` — never clean completed samples — and it was
deliberately narrowed to that set because probing every planned sample at
teardown stalled Ctrl-C shutdown of large remote retries for minutes.

## Goal

> **A retry never re-runs a sample that a prior attempt of the same task
> completed cleanly, regardless of how the intervening attempt ended.**

Two ways to get there: make each attempt's log complete before it is
finalized (copy the unreached samples at teardown), or let the retry source
see past a partial newest log to the older one that has them. This design
takes the second, for reasons laid out under "Alternatives considered": the
teardown copy is exactly the slow (or failing) work that prevented the sweep
from finishing, so it cannot be the guarantee — it needs a fallback, and the
fallback alone is sufficient.

## Approach

Make the retry sample source a **chain** of the task's prior logs, newest
first, with one precedence rule:

> The newest log that holds *any* record for `(id, epoch)` is authoritative
> for it. Only a key **absent** from a log falls through to the next older
> log.

"Record" means any sample entry — clean, errored, invalidated, or cancelled.
This is sound because a newer attempt reproduces every prior record it
reaches: the sweep copies clean samples, live re-runs replace errored /
invalidated / absent ones, and carry-forward copies unreached errored ones.
So a record in a newer log always supersedes the older log's record for the
same key, and absence in the newer log means precisely "this attempt never
got to it". An invalidated sample in the newest log is a record, so it does
*not* fall through to an older clean copy — the user's invalidation stands
and the sample re-runs, exactly as today.

Two consequences follow, both required for the guarantee to hold:

1. **Cleanup must not delete a log that the chain still needs.** An older
   log is deleted only once it is *superseded* — every sample key it holds is
   present in the newest log.
2. **The in-process retry chains too.** `_run_task` already holds the
   previous attempt's source in `options.sample_source`; the next attempt's
   source is the errored attempt's log chained onto it.

The #4933 destination-write hold stays as is. It keeps the hard-kill case
from ever producing a misleading newest file (which recovery, the viewer, and
cleanup all key off), and it remains the reason a killed attempt has nothing
for the chain to skip over. The chain is defense in depth for every shape the
hold cannot prevent: graceful errors, cancels, read failures, and shapes not
yet anticipated.

## Mechanism

### `eval_log_sample_source` gains a `fallback`

`eval_log_sample_source(eval_log, eval_log_info, dataset,
eval_checkpoints_dir, fallback: EvalSampleSource | None = None)`
(`_eval/task/run.py`). `EvalSampleSource` itself is unchanged — the chain is
built by composition, so `run_sample`, `carry_forward_unlogged_samples`, and
the throttle logic see one source as before.

- **`lookup` (file path, `read_from_file`)**: absence is already signalled —
  `read_eval_log_sample_async` raises `IndexError` for a missing entry and
  `FileNotFoundError` for a missing file (the held-attempt-that-never-wrote
  case). Today that branch returns `_resume_if_checkpointed(this log's
  checkpoints dir)`. New order on absence: (1) this attempt's checkpoint, if
  any — a checkpoint means this attempt *started* the sample live, which is
  newer work than anything in an older log; (2) `fallback.lookup(id, epoch)`
  when a fallback exists; (3) `None`. A present record — clean, errored,
  invalidated — resolves from this log exactly as today and never consults
  the fallback. Any other exception (a transport error) propagates as today:
  an error is not absence, and the chain must not silently skip a log it
  couldn't read.
- **`lookup` (in-memory path, `read_from_memory`)**: `match is None` is
  absence → same (1)–(3) order.
- **`prior_exists`**: `this.prior_exists(id, epoch) or
  await fallback.prior_exists(id, epoch)`. Both are index-only (cached
  central-directory membership), so this stays cheap; a hit anywhere in the
  chain means a body read will happen and must take the read throttle.
- **`error_history_ids`**: the union across the chain. Carry-forward then
  probes each candidate through the chained `lookup`, so precedence applies
  (a candidate that errored in the older log but completed cleanly in the
  newest resolves to the clean sample and is skipped, as today).
- **Ineligible member**: when this log fails the existing eligibility checks
  (shuffled without ids, dataset size changed, no samples and no file and no
  checkpoints) the function currently returns the no-op source. It now
  returns `fallback` if one was given (an unusable link is skipped, not a
  chain terminator), else the no-op source. The warning it logs today still
  fires.

The fallback's own checkpoints dir is baked into the fallback when it was
built (each log's checkpoints live under its own basename via
`eval_checkpoints_dir_from_config`), so checkpoint lookup composes with no
extra plumbing.

### eval_set: hand every same-task log to the retry

`latest_completed_task_eval_logs` currently groups `Log`s by
`eval.task_id`, mtime-sorts each group, and returns only `[0]`. Split its two
jobs:

- A grouping helper returns the mtime-ordered groups (newest first) per task
  id. `list_latest_eval_logs` takes `[0]` of each group for the
  complete/incomplete classification as today, and passes the tail along
  with each incomplete `Log`. `latest_completed_task_eval_logs(logs,
  cleanup_older)` keeps its signature and `list[Log]` return as a thin
  wrapper over the helper (grouping, then cleanup, then `[0]` per group):
  `cleanup_older_eval_logs` and two existing tests call it directly (see
  Testing).
- `as_previous_tasks` receives, per failed task, the newest `Log` plus its
  ordered older siblings, and stores the siblings on `PreviousTask` as a new
  defaulted field `fallback_logs: list[PriorAttemptLog]`
  (`_eval/task/task.py`), where `PriorAttemptLog` is a two-field
  `NamedTuple(log: EvalLog, info: EvalLogInfo)` declared beside
  `PreviousTask` — named fields rather than a bare tuple so the
  `EvalLog`/`EvalLogInfo` slots can't be swapped positionally, mirroring
  `PreviousTask`'s own `log`/`log_info` pair. `evalset.Log` is not reused
  because `evalset.py` imports from `task/`, so `task.py` importing `Log`
  back would be circular; `as_previous_tasks` converts each sibling `Log`
  to `PriorAttemptLog(log=header, info=info)`. `PreviousTask` is private,
  and both other constructors — `eval_retry` in `_eval/eval.py` and the
  explicit-resume path in `evalset.py` — keep the default.
  `_recover_crashed_log` applies only to the newest log as today; older
  `started` logs are chained as they are on disk (whatever they hold is
  usable; nothing more is attempted).
- `resolve_previous_task` (`_eval/loader.py`) folds the chain from oldest to
  newest: `source = None; for link in reversed(fallback_logs): source =
  eval_log_sample_source(link.log, link.info, ..., fallback=source)`, then
  the newest with `fallback=source`. Each link runs its own eligibility
  checks against `loaded_task.dataset`.

Logs in one group already share task identity (`task_identifier` includes
task args, model, and the eval-set config that participates in identity), so
chain members are exactly the logs eval_set already deems reuse-compatible
with each other. Epoch-count changes between attempts need no special
handling: lookups are keyed by `(id, epoch)`, and eval_set already treats an
`epochs_changed` newest log as the retry seed for the epochs it does hold.

### In-process retry: chain onto the previous attempt's source

In `_run_task`'s retry branch (`_eval/run.py`), the new source becomes
`eval_log_sample_source(result, failed_log_info, dataset, checkpoints_dir,
fallback=options.sample_source)`. `options.sample_source` is the source this
attempt ran with — for an eval_set `retry_immediate=True` retry, the
eval_set-built chain; for a fresh `eval(retry_attempts=)`, `None`. If the
errored attempt left no file (a held attempt whose `log_finish` also failed),
its link resolves every key as absent and the chain falls through, instead
of today's degrade-to-no-reuse.

### Cleanup deletes only superseded logs

`latest_completed_task_eval_logs(cleanup_older=True)` — called at pass start
via `list_latest_eval_logs` and at the end of a successful set via
`cleanup_older_eval_logs` — currently removes every older non-`started` log
in the group. New rule, per older log:

- `started` logs are kept, as today (post-mortem).
- If the newest log's status is `success`, the older log is deleted (today's
  behaviour, no extra reads — a completed task needs no chain; if eval_set
  still classifies that success log incomplete for config reasons it retries
  from it alone, as it does now).
- Otherwise the older log is deleted only if
  `keys(older) ⊆ keys(newest)`, where each key set is `{(id, epoch)}` from
  `read_eval_log_sample_summaries` — one bounded summaries read per log in a
  group of two or more. A log that isn't superseded is kept and the reason is
  logged at info level, so an operator seeing extra logs in the dir can tell
  why.

The summaries reads happen only when a task has more than one log, i.e.
after a non-success attempt — never on the steady-state pass where each task
has one log. A normal errored attempt that finished its sweep holds every key
of its predecessor (reused + live + carried-forward), so it supersedes it and
cleanup behaves exactly as today. Only a partial attempt keeps its
predecessor alive, and only until an attempt supersedes both. `invalidate_log_sample_summaries`
is called on deletion as today.

`docs/eval-sets.qmd`'s `--no-retry-cleanup` row should say failed logs are
removed once a later attempt supersedes them. This is a visible behaviour
change for anything that assumes one log per task in an eval-set directory
after a pass — `bundle_log_dir` output, viewer listings, scripts iterating
`list_eval_logs` — triggered by any partial attempt. It is accepted (those
logs hold results nothing else has, and keeping is the conservative
direction), and the implementation PR's CHANGELOG entry should say in one
sentence that a failed attempt's older logs now remain until a later
attempt holds every sample they do.

### Observability at the partial finish

When a retry attempt finishes with a non-success status while some planned
runs never resolved their prior-attempt lookup, log a warning naming the
count: "N planned samples had not resolved their prior-attempt lookup when
this attempt ended; the next retry will consult the prior attempt's log for
them." This costs nothing and turns a silent condition into a diagnosable
one — the incident was only reconstructed by reading three logs by hand.

The count is **not** `_ReuseSweepCountdown._remaining`. That counter tracks
*settled* runs, and settling deliberately includes cancellation: the
`settle_one()` call sits in `run_sample`'s `finally` (`_eval/task/run.py`)
precisely so a cancelled `run_sample` still releases the destination-write
hold. On every teardown shape in the Problem section the task group is
cancelled, each pending `await sample_source.lookup(...)` raises the
cancellation exception, and its `finally` decrements the count — so by the
time `task_run`'s terminal branches reach `finish_task_log`, `_remaining` is
already `0`. A warning keyed on it would never fire in exactly the
scenarios it exists to diagnose.

The warning needs a resolved-vs-settled distinction: `_ReuseSweepCountdown`
gains a `resolved` counter incremented only when the lookup block completes
normally, via `settle_one(resolved: bool)` — `run_sample` sets a local flag
at the end of the `try` body (after the lookup and any re-log) and passes it
from the `finally`, so a lookup that raised or was cancelled settles but
does not resolve. A `run_sample` with no source, or whose key the source
lacks, completes the block trivially and counts as resolved; requeued
re-runs are excluded exactly as they are from settling. The warning is
keyed on `planned − resolved > 0` (planned includes any runs added by
`add()`), and fires only when `options.sample_source` is not `None` — a
fresh eval has no prior attempt to consult, so the message would be
misleading there.

Where it fires: from the `finish_task_log` closure, which every terminal
branch already goes through, when `status != "success"`. `reuse_settle` is
currently bound inside the `try`, a few statements in (after
`create_sample_semaphore`, alongside the read throttle), so an exception
raised before that line — `create_sample_semaphore` itself, say — reaches
the `BaseException` branch and its `finish_task_log` call with the name
unbound. Declare it as `_ReuseSweepCountdown | None = None` before the
`try` so the closure can read it, and skip the warning when it is still
`None`.

## Failure analysis

- **Graceful error / cancel mid-sweep** (the issue): the partial log is
  finalized as today and is newest. Cleanup keeps the older log (not
  superseded). The next attempt's chain resolves reached keys from the
  partial log and unreached ones from the older log. Nothing re-runs that
  didn't need to. The final successful attempt supersedes both; cleanup
  removes them. Both `retry_immediate` values take the same chain.
- **Prior-log read raises mid-sweep** (the issue's second trigger): the
  attempt still tears down and leaves a partial log — this design does not
  change how a read failure inside the sweep is handled (see follow-ups).
  What changes is the consequence: the next attempt chains past the partial
  log, and if the older log is readable again, reuse is complete.
- **Hard kill mid-sweep**: unchanged from #4933 — no file, so the chain has
  one link fewer.
- **A chain member is unreadable at retry time**: `read_from_file` and the
  presence probe surface the error as today (the probe degrades to
  "unthrottled" after three failures; `lookup` propagates). The chain never
  treats an error as absence, so a transient failure in an older log cannot
  silently cause a re-run — it surfaces the same way a failing single-log
  source does now.
- **Cleanup's summaries read fails**: keep the log (deleting on an unknown is
  the unsafe direction) and warn. The next pass retries the comparison.
- **Repeated partial attempts**: each keeps its predecessors alive; the chain
  grows by one per partial attempt, bounded by `retry_attempts`. Each link
  costs one lazy central-directory fetch the first time an absent key reaches
  it.

## Trade-offs (accepted)

1. **Retry semantics become "newest record wins" across logs instead of
   "newest log is the whole truth".** The precedence rule above makes the
   two coincide whenever the newest log is complete, so behaviour changes
   only in the case that is a bug today. Consumers other than the retry
   source (viewer, recovery, manifest) still see the newest log as the
   task's latest attempt, which it is.
2. **Un-superseded older logs stay in the log dir** until an attempt
   supersedes them (or forever if the set never succeeds). This is the point:
   those logs hold results nothing else has. The info-level message explains
   the extra file, and the cleanup section names the consumers that see it.
3. **Extra summaries reads at pass start**, only for tasks with more than one
   log — one per log in the group. Bounded and rare.
4. **`PreviousTask` grows a field.** Private dataclass, defaulted; the
   `eval_retry` and explicit-resume constructors are unaffected.
5. **The partial attempt's own error history for unreached samples is thin.**
   Its log lacks the older log's errored records that carry-forward didn't
   reach (carry-forward also stops when the task is torn down mid-probe), so
   an operator reading the partial log alone under-counts retries for those
   samples. The *chain* preserves them — the next attempt seeds
   `PreviousError` from the older log — so the surviving sample's history is
   intact, which is the property `PreviousError` exists to protect.

## Alternatives considered

- **Complete the sweep at teardown** (the issue's first suggestion): extend
  `carry_forward_unlogged_samples` to copy unreached *clean* samples too,
  from the same summaries read that identifies the prior sample set. This
  keeps each log self-contained and needs no selection or cleanup change.
  Rejected as the primary mechanism for two reasons. (a) It is the same slow
  work the sweep didn't finish, now run at teardown inside the Ctrl-C
  cancellation shield — the exact stall that motivated narrowing
  carry-forward to error candidates; a time budget would be needed, and a
  budget that expires leaves a partial log again. (b) When the trigger is
  the prior-log read failing, the copy fails for the same reason. Either way
  a fallback is required, and the fallback (this design) is sufficient on its
  own. The teardown copy could be layered on later as an optimization that
  makes cleanup supersede sooner; it is not needed for correctness.
- **Flag the partial log in its header and skip flagged logs when choosing a
  retry source**: a new persisted field on a public log record (producers:
  `log_finish`; consumers: eval_set selection, in-process retry, cleanup),
  and it discards the partial attempt's live results and error increments
  rather than using them. The chain keeps them and needs no schema change.
- **Don't write the destination at all on a mid-sweep non-success finish**:
  mirrors the hard-kill shape, but loses the attempt's error record, breaks
  the in-process retry (which reads the attempt's location), and returns an
  `EvalLog` whose `location` doesn't exist to `eval()` callers.
- **Defer all cleanup to the final successful sweep**: simplest cleanup
  change, but leaves every attempt's log around for the whole set, and
  doesn't compose with an eval_set re-invoked in a dir whose set never
  succeeded. The supersession rule keeps today's tidiness in the common case.

## Edge cases

- **Fresh eval / no prior log**: no chain; byte-for-byte unchanged.
- **Newest log complete (sweep settled, then errored on a live sample)**: it
  holds every key of its predecessor; no lookup ever falls through, and
  cleanup deletes the predecessor as today.
- **Invalidated in the newest log**: a record → resolves `None` from the
  newest → re-runs fresh. Never reaches an older clean copy.
- **Invalidated in an older log after a newer attempt reused it clean**: the
  newer clean copy wins — same as today, where the older log isn't consulted
  at all. Invalidate in the newest log.
- **Sample absent from every link**: `None` after each link's checkpoint
  check → runs fresh.
- **`sample_id` / `limit` filters**: `run_sample` looks up only planned keys;
  extra keys in older logs are never consulted. The cleanup subset rule
  compares whole key sets, so a narrowed plan can keep an older log alive
  until a success finalizes the task — acceptable (trade-off 2).
- **`eval-retry` on a named log**: the user chose the file; no chain is
  built (`log_info=None`, in-memory source). Unchanged; a user hit by this
  shape can name the older log instead.
- **Explicit `resume=` log in eval_set**: same — the user named it;
  unchanged.
- **JSON logs**: `read_from_file` on a `.json` log reads the whole file per
  lookup today; absence surfaces the same way (`IndexError`) so the chain
  composes; presence stays `_never_prior_exists` for `.json` links.
- **Dynamic `SampleSource` tasks**: injected samples look up through the
  same chained source; nothing feed-specific changes.
- **Two attempts with the same mtime**: chain order is mtime order,
  inherited from today's newest-log selection, and stores with
  seconds-granularity mtimes (S3) can give two attempts that finish within
  the same second an ambiguous order. The worst case is a key present in
  both logs resolving from the older one — one lost error-history
  increment, or a clean sample shadowing an errored re-run of it — never a
  re-run of a completed sample or a lost result. `retry_wait` makes it
  unlikely and the existing single-log selection has the same ambiguity, so
  no tie-break is added; if one is ever wanted, the header's
  `eval.created` timestamp is the natural secondary key.

## Testing

Unit (`tests/test_task_retry_error_history.py`, which already covers
`eval_log_sample_source` and carry-forward; or `tests/test_retry.py`):

- Chain lookup: absent in newest → fallback's clean sample; errored in
  newest → `PreviousError` from newest even when the fallback has a clean
  copy; invalidated in newest → `None`, fallback not consulted; a checkpoint
  in the newest's dir takes precedence over the fallback; absent everywhere →
  `None`.
- `prior_exists` is the OR across links; `error_history_ids` is the union.
- An ineligible link (dataset size mismatch) is skipped, not terminal.
- A transport error from a link's read propagates rather than falling
  through.
- Cleanup: with a non-success newest log, an older log with keys the newest
  lacks is kept, a superseded one is deleted, `started` logs are kept; with a
  success newest log every older non-`started` log is deleted; a failing
  summaries read keeps the log.
- Existing callers of `latest_completed_task_eval_logs` must keep passing:
  `tests/test_eval_set.py::test_latest_completed_task_eval_logs` (two
  `.json` fixtures for one task, `success` and `error`, both holding no
  samples — an empty key set is trivially superseded, so the older log is
  deleted under either mtime order) and
  `tests/_control/test_eval_state.py`'s summaries-memo invalidation test
  (`success` newest, so the shortcut path with no summaries read; its
  header is a `SimpleNamespace` stub exposing only `status`,
  `eval.task_id` and `eval.eval_id`, which the success path must keep
  sufficient).
- The mid-sweep non-success warning fires with the *unresolved* count when
  `run_sample`s are cancelled mid-lookup (settled but not resolved), is
  silent when every lookup resolved before the failure, and is silent on a
  fresh eval with no sample source.

Eval-level (`tests/test_eval_set.py`):

- The issue's repro, parametrized over `retry_immediate`: attempt 1 completes
  s1–s3 and errors on s4; attempt 2's reads of s2/s3 hang and s4 errors
  again; assert s2 and s3 run exactly once overall, attempt 2's log holds
  `{s1, s4}`, attempt 1's log still exists when attempt 3 starts with
  `retry_cleanup=True`, and after success only the final log remains.
- Same shape with `eval(retry_attempts=2)` (no eval_set) to cover the
  `_run_task` chain directly.

Run the async tests with `--runtrio` as well.

## Follow-ups (out of scope)

- **Prior-log read failures inside the sweep tear the whole task down.**
  `read_from_file` catches only `IndexError` and `FileNotFoundError`; a
  transport error escapes `run_sample` and fails the attempt (the issue's
  second trigger). With the chain in place that is no longer catastrophic,
  but a per-lookup retry with backoff — and a defined, warned fallback for a
  persistently unreadable prior sample — would avoid burning a retry attempt
  on a storage blip. Worth its own design; it touches the `retry_on_error`
  and `fail_on_error` accounting.
- **Teardown copy of unreached clean samples** as an optimization so a
  partial attempt supersedes its predecessor sooner (see Alternatives).
- **`eval-retry` chaining** over same-task logs in the named log's directory.
