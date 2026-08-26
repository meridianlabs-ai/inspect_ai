# Sample Timeline

Feature design for a **Sample Timeline** view in the log viewer: the
sample-scoped analog of the task-level **Timeline** tab. Where the task
Timeline answers "what happened over the course of this run?" (sample
terminations, concurrency, config retunes, connection throttling), the
Sample Timeline answers "what happened over the course of this sample?" —
key events plotted along the sample's own time axis, with a filterable list
of the newsworthy moments.

Status: draft — data inventory and general design. No implementation yet.

## Relationship to existing surfaces

Three related things already exist; the design should complement, not
duplicate, them:

1. **Task Timeline tab**
   (`ts-mono/apps/inspect/src/app/log-view/tabs/timeline/`): a wall-clock
   chart (activity series, termination dots, full-height ◆ markers) paired
   with a categorized, filterable **History** list. Its vocabulary — time
   window, markers, `HistoryRow` categories, guide segments for limits,
   `fmtDuration` — is the paradigm this feature transplants to a sample.

2. **Transcript swimlanes**
   (`ts-mono/packages/inspect-components/src/transcript/timeline/`):
   the agent-centric swimlane header already embedded at the top of the
   sample **Transcript** tab (`TranscriptLayout`). It is time-mapped
   (piecewise-linear wall clock with >5-min gaps compressed to zero width)
   and structure-oriented: lanes are agents/spans, and its job is
   navigation of the transcript. It deliberately elides "boring" time and
   does not plot quantities (tokens, limits) or surface a scannable event
   list.

3. **Python `Timeline` model** (`src/inspect_ai/event/_timeline.py`,
   stored as `EvalSample.timelines`): a hierarchical, agent-centric span
   tree built from the flat event stream (`timeline_build()`), with
   start/end times, token totals, and idle-time computation per span. The
   TS transcript pipeline mirrors it. This gives Sample Timeline its
   structural grouping (which agent was active when) for free.

The gap the Sample Timeline fills: a *time-and-quantity* centric view of a
single sample — where the time went, how resources burned toward limits,
and a curated history of key events — rather than a structure-centric
transcript navigator.

## Data inventory

What we can plot comes from two layers: the sample envelope
(`EvalSample` / `EvalSampleSummary`, `src/inspect_ai/log/_log.py`) and the
per-event stream (`EvalSample.events`, union in
`src/inspect_ai/event/_event.py`).

### Sample envelope (frame + summary strip)

| Field | Timeline use |
| --- | --- |
| `started_at` / `completed_at` | The chart's time window (task Timeline uses the run's window the same way). |
| `total_time` vs `working_time` | Headline split: elapsed vs productive time. The difference is waiting (retries, rate limits, other samples sharing the connection pool). |
| `error` (`EvalError`) | Terminal error marker at sample end. |
| `error_retries` (`list[EvalRetryError]`) | Prior failed attempts. Each carries its own partial `events` (back to the last `ModelEvent`), so retried attempts can render as faded pre-history segments before the final attempt. |
| `limit` (`EvalSampleLimit`: `type`, `limit`, `reason`) | Why the sample stopped: message/token/time/working/turn/cost/operator/custom. Pairs with a guide line (see below). |
| `token_limit`, `token_limit_type`, `token_limit_usage` | The configured token ceiling and metered usage — a horizontal guide the cumulative-tokens series burns toward (same idiom as the task chart's violet `guideSegments`). |
| `model_usage` / `role_usage` (`ModelUsage`) | End-of-sample totals for the summary strip; per-model split for series coloring. |
| `model_fallbacks` | Fallback markers (model → fallback_model × count); the task History already renders these per sample. |
| `turn_count`, `message_count` | Denominators for a turn ruler / progress axis. |
| `scores` | Final score annotation at the right edge. |
| `invalidation` | Badge that the sample was invalidated post-hoc. |
| `timelines` (`list[Timeline]`) | Precomputed agent-centric span tree: which agent/span owned each stretch of time (band coloring or a collapsed structure lane). |

### Base event fields (every event)

Every event (`BaseEvent`, `src/inspect_ai/event/_base.py`) carries:

- `timestamp` — wall-clock position (all events are plottable).
- `working_start` — the sample's *working-time* offset when the event
  occurred. This gives a second, alternative x-axis: plotting by working
  time collapses waiting; the divergence between the two axes *is* the
  waiting, and can be rendered explicitly (e.g. hatched "stalled" bands
  where wall clock advances but working time doesn't).
- `span_id` — attribution to the span/agent tree (lane assignment).
- `uuid` — deep-link key into the Transcript tab (the transcript already
  resolves event deep links); absent on old logs, so linking degrades.
- `pending` — live-view flag: the event is still in flight.

### Interval events (have `completed` and `working_time`)

These render as bars/segments, not points:

- **`ModelEvent`** — the workhorse. `timestamp`→`completed` span; `model`,
  `role` (color/lane key); `output.usage` (input/output/cache-read/
  cache-write tokens → the cumulative token series and per-call context
  size); `retries`; `error` (including cancel sentinels); `cache`
  read/write; `pending` + `progress` (streamed `output_tokens`,
  `last_progress_at`) for live rendering.
- **`ToolEvent`** — `function`, `arguments`, `result`/`error`/`failed`,
  `truncated`; nested `events` (its own sub-transcript); `agent` +
  `agent_span_id` mark subagent invocations (handoffs) — these become
  labeled sub-trajectory segments; `cancelled`.
- **`SubtaskEvent`** — named subtask with nested `events`; same treatment
  as tool-spawned work.
- **`SandboxEvent`** — `action` (exec/read_file/write_file), `cmd`/`file`,
  exit `result`, `completed`. Dense sandbox activity is often where
  agentic time actually goes; render as a thin activity band with
  slow-command outliers surfaced individually.

### Point events (the ◆ markers / Key Events rows)

- **`SampleInitEvent`** — start anchor.
- **`SampleLimitEvent`** — `type`, `message`, `limit`: the moment a limit
  fired (may precede sample end if caught).
- **`ScoreEvent`** — final *and* `intermediate=True` scores. Intermediate
  scores are a first-class marker: progress-over-time within a sample.
  Also `scorer` name and scorer `model_usage` (scoring cost attribution —
  the scorers span shows scoring time distinctly from solving time).
- **`ScoreEditEvent`** — post-hoc score edits (often after `completed_at`;
  same "post-run" clamping treatment as the task chart's post-run rows).
- **`ErrorEvent`** — errors caught mid-sample.
- **`ApprovalEvent`** — `approver`, `decision`
  (approve/modify/reject/escalate/terminate) on a tool call — human/policy
  intervention points.
- **`InputEvent`** — blocking human-input requests with `outcome`; these
  typically *explain* long wall-clock gaps and should be attached to them.
- **`InterruptEvent`** — `source` (user_cancel/limit/system) and what was
  interrupted (generate/tool_call/between_turns), with pointers to the
  interrupted event.
- **`CompactionEvent`** — `type` (summary/edit/trim), `tokens_before` →
  `tokens_after`: context-management moments; the per-call context-size
  series visibly drops here, and the marker labels why.
- **`CheckpointEvent`** / **`AnchorEvent`** / **`BranchEvent`** —
  checkpoint/fork/branch points; branches become parallel trajectory
  segments (the swimlane pipeline already models branch spans).
- **`InfoEvent`** — app-defined milestones (`source`, `data`) — the
  extensibility hook: solver authors can inject their own timeline
  markers today with no new API.
- **`LoggerEvent`** — python log records; warning+ levels are marker-worthy,
  info/debug are list-only under a filter.
- **`SpanBeginEvent`/`SpanEndEvent`** (+ legacy `StepEvent`) — structure:
  init/solvers/scorers phases, agent/tool/branch spans. Not markers
  themselves, but the banding/lane machinery.
- **`StateEvent`/`StoreEvent`** — jsonpatch mutations of `TaskState`/store.
  Default-off noise, but store writes to well-known keys could be
  surfaced later; not in scope for v1 markers.

### Derived series (chart-able quantities)

Computable client-side from the above with a single pass over events:

1. **Cumulative tokens over time** — step series from each `ModelEvent`'s
   usage at its `completed` time, split by model or role; with the
   `token_limit` guide line, this is the burn-down story ("hit the token
   limit 80% of the way through turn 12").
2. **Context size per call** — input tokens per `ModelEvent`: the growth
   curve of the conversation, with cliffs at `CompactionEvent`s.
3. **Activity/concurrency** — count of overlapping model/tool/sandbox/
   subtask intervals (parallel subagents), the sample-level analog of the
   task chart's active-samples series.
4. **Working vs waiting** — from `working_start` deltas vs wall-clock
   deltas; stalled stretches get hatched bands, attributable to retries
   (`ModelEvent.retries`), rate limits, or `InputEvent` waits.
5. **Turn ruler** — top-level `ModelEvent`s as tick marks (matches
   `turn_count`), giving an "agent progress" axis alongside wall time.

## General design

A new **Timeline** tab in the sample dialog (alongside Transcript /
Messages / Scoring / Metadata / JSON), mirroring the task Timeline tab's
two-part anatomy:

1. **Chart** — x = wall clock over `started_at`→`completed_at` (toggle:
   working time; reuse the swimlane gap-compression `TimeMapping` so idle
   gaps don't flatten the interesting parts). Content, top to bottom:
   - phase/agent bands from the `Timeline` span tree (init / solving,
     per-agent / scoring);
   - an activity band of model/tool/sandbox intervals (or the concurrency
     step series when subagents run in parallel);
   - the cumulative-token series with the token-limit guide;
   - full-height ◆ markers for key events, clustered when dense, each
     linked to its Key Events row (the task chart's marker idiom).
2. **Key Events list** — the `HistoryList` idiom scoped to the sample:
   time-ordered rows, category filter pills, free-text search, expandable
   detail. Candidate categories (analogous to `HistoryCategory`):
   *lifecycle* (init, retried attempts, completion), *errors & retries*
   (ErrorEvent, model retries, fallbacks), *limits* (SampleLimitEvent,
   limit-guide crossings), *scoring* (intermediate + final scores, score
   edits), *intervention* (approvals, input, interrupts), *context*
   (compaction, checkpoint, branch), *sandbox* (slow/failed commands),
   *subagents* (handoffs, subtasks), *log* (warning+ LoggerEvents, InfoEvents).
   Every row deep-links to its event in the Transcript tab via event
   `uuid`.

### Data availability and degradation

- Everything needed is already in the loaded sample: the Transcript tab
  already fetches full `events`, so no new API or log format is required.
  (A summary-only variant is *not* possible: `EvalSampleSummary` has the
  window and totals but no events.)
- Live samples: `pending` events and `ModelEventProgress` let the chart
  grow in place, like the task chart during a running eval.
- Old logs degrade: missing `uuid` disables deep links for those events;
  missing `started_at`/`working_start` falls back to the first/last event
  timestamps and wall-clock-only axis.
- Large samples: series building is a single O(events) pass; the Key
  Events list is already a filtered subset (State/Store and info-level
  logger noise excluded by default), and the list virtualizes like
  HistoryList.

## Open questions

1. **Tab vs. panel**: a separate Timeline tab, or an expanded mode of the
   existing swimlane header inside Transcript? A tab keeps the chart +
   list pairing coherent and matches the task-level precedent; the
   swimlane header stays the lightweight navigator.
2. **Pipeline reuse**: build on the TS transcript-timeline pipeline
   (`useTimelinePipeline`, spans/branches/retry grouping already ported)
   vs. consuming Python-precomputed `EvalSample.timelines` when present.
   Leaning TS pipeline (works for all logs), using stored `timelines` only
   as alternative named views (the model explicitly supports multiple
   interpretations).
3. **Retried attempts**: render `error_retries` pre-history on the same
   axis (their events can predate `started_at` of the final attempt) or
   list-only rows?
4. **Cost**: `cost` is a limit type; if/when per-call cost lands in usage
   data, it becomes a second burn-down series — design the series slot to
   be quantity-agnostic (tokens now, cost later).
5. **Cross-navigation**: should the task Timeline's sample rows (error/
   limit/fallback) deep-link into the sample Timeline tab, making the two
   views a drill-down pair?
