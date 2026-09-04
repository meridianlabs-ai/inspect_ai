# Fail on Refusal — terminate a sample when a model returns `content_filter`

> **Status: proposed** (design only, not implemented). Originating issue:
> meridianlabs-ai/inspect_ai#437. Builds on the existing `StopReason`
> value `"content_filter"` rather than introducing a new signal.

## Summary

Add a `fail_on_refusal` generate-config option. When enabled,
`Model.generate()` raises a new `ModelRefusalError` after a generation ends
with `stop_reason == "content_filter"`. The sample then fails as an ordinary
sample error, so it shows up loudly in the log, in the display, and in
`fail_on_error` accounting. Because it lives in `GenerateConfig`, it can be
set eval-wide, per task, per model, per model role, or per call, using
plumbing that already exists.

## Problem

Capabilities teams want to run arbitrary tasks and agents and be informed
loudly if a sample is hit by a model refusal. Today the agent quietly exits
after a few refusals and the sample is scored zero, which is
indistinguishable from a genuine failure of the task. Falling back to another
model is not appropriate for this use case: the run should stop and say why.

## What happens today

- `StopReason` already has `"content_filter"`
  (`src/inspect_ai/model/_model_output.py`). Every provider maps refusals and
  safety filters onto it, including the cases where a 4xx is converted into a
  refusal output (`_providers/anthropic.py`, `_providers/_openai.py`,
  `_providers/google.py`, `_providers/bedrock.py`). `stop_details` carries the
  category when the provider reports one. This is the signal the Slack thread
  asked us to build on, and it is the only trigger this design uses.
- `Model.generate()` notices the refusal only to count it and optionally log a
  warning via `--log-refusals` (`report_refusal()` in
  `src/inspect_ai/model/_model.py`, `src/inspect_ai/log/_refusal.py`).
  Nothing stops the sample.
- `react()` retries a refusal `retry_refusals` times (`_model_generate` in
  `src/inspect_ai/agent/_react.py`), then after 3 consecutive refusals
  silently breaks out of the loop. The sample proceeds to scoring and is
  typically scored zero. This is the "quietly exits" behaviour in the issue.
  The bridge does the same (`bridge_generate` in
  `src/inspect_ai/agent/_bridge/util.py`).
- `fallback_models` is Anthropic-only and server-side. It changes which model
  answers, which the issue says is not appropriate here.

## Proposal

### 1. New `GenerateConfig` field

```python
fail_on_refusal: bool | None = Field(default=None)
"""Raise a ModelRefusalError (failing the sample) when the model returns
stop_reason="content_filter". Defaults to False."""
```

Added to both `GenerateConfigArgs` and `GenerateConfig` in
`src/inspect_ai/model/_generate_config.py`. Default `None` means off, so
existing behaviour is unchanged.

Why `GenerateConfig` rather than a new `eval()` argument like `log_refusals`:
the request wants global, per-model and per-role control. Generate config
already layers exactly that way (eval, task, model, role, call) and already
flows through `--model-role`, `--model-spec`, `--run-config`, `ModelConfig`
for roles, and the eval spec in the log. A standalone eval option would need
all of that built again.

### 2. New public exception

```python
class ModelRefusalError(Exception):
    """Raised by Model.generate() when fail_on_refusal is set and the
    model returned stop_reason="content_filter"."""
    output: ModelOutput   # full output, so stop_details/category are available
    model: str
    role: str | None
```

Exported from `inspect_ai.model`. Message format:
`Model refusal (<model>[, role <role>][, category <category>]): <first ~200 chars of completion>`.
Including the category and a stable `Model refusal` prefix makes these errors
easy to filter in the dataframe `error` column and in `inspect view`.

### 3. Raise point

In the outer frame of `Model.generate()`, immediately after
`report_refusal()`, using the resolved config for that call. Raising there,
rather than inside the retry loop or in the provider, means:

- The `ModelEvent` is already completed with the refusal output, so the
  transcript shows the refusal and then the `ErrorEvent`.
- Usage, turn counting, the refusal counter, and telemetry all still run.
- Provider retries and the (non-)caching of refusals are unaffected. Refusals
  are already never cached (`src/inspect_ai/model/_cache.py`).
- Every caller is covered: the `generate()` solver, `react()`, `deepagent`,
  bridged scaffolds, custom agents, compaction, and model-graded scorers.

The trigger predicate is
`not output.empty and output.stop_reason == "content_filter"`, identical to
the one `report_refusal` and `retry_refusals` use (first choice). Keeping the
three in lockstep avoids a state where the counter says "refusal" but the
sample did not fail.

### 4. Sample outcome

`ModelRefusalError` is a plain `Exception`, so the task runner's generic
handler turns it into an `EvalError` (`src/inspect_ai/_eval/task/run.py`).
Consequences, all intentional:

- The sample is marked errored rather than scored. `fail_on_error` thresholds
  count it, so the eval can be configured to abort on the first refusal.
- `score_on_error` still works for users who want a score alongside the
  error.
- `retry_on_error` sample retries apply as to any other error. A fresh attempt
  is often what you want at a filter decision boundary. If that proves
  wasteful for deterministic refusals we can exclude the error type later.

It is deliberately not a `LimitExceededError`. Limits mean "stopped early,
still scored, counted as success", which is the opposite of what the issue
asks for.

### 5. Interaction with `retry_refusals`

Without care, the raise would defeat `retry_refusals`, because the first
refused attempt would already raise. So `react()`'s `_model_generate` loop and
`bridge_generate` catch `ModelRefusalError` while retry attempts remain and
re-raise the final one. Net effect: `retry_refusals=N` plus
`fail_on_refusal=True` means "N+1 consecutive refusals in one step fails the
sample". The existing hardcoded 3-consecutive break becomes unreachable when
the option is on and can stay as-is. Every other refusal-retry loop in an
extension gets the same shape: catch, retry, re-raise.

### 6. Scope and precedence

| Where set | How | Wins over |
|---|---|---|
| Eval-wide | `eval(..., fail_on_refusal=True)`, `--fail-on-refusal`, `INSPECT_EVAL_FAIL_ON_REFUSAL` | nothing (lowest) |
| Task | `Task(config=GenerateConfig(fail_on_refusal=True))` | eval-wide |
| Model role | `--model-role grader="{model: openai/gpt-4o, fail_on_refusal: false}"` or `get_model(..., config=GenerateConfig(fail_on_refusal=False))` in `model_roles` | eval-wide, task |
| Per model in a multi-model eval | `--model-spec "{model: ..., fail_on_refusal: true}"` | eval-wide, task |
| Per call | `model.generate(..., config=GenerateConfig(fail_on_refusal=False))` | everything |

One change is needed for the eval-wide case to reach role models. Today
eval-wide generate config applies only to the active model; non-active models
inherit just a handful of operational fields (`Model._resolve_config` in
`src/inspect_ai/model/_model.py`). `fail_on_refusal` joins that inherited
set, but with role-wins precedence: it is copied from the eval config only
when the role model's own config leaves it unset. The existing operational
fields go the other way (eval-wide overrides the role), which would make it
impossible to say "fail everywhere except the grader". The
`GenerateConfig.merge()` semantics (non-`None` in `other` wins) support this
with a two-line conditional.

Typical configurations this enables:

```bash
# fail any sample where any model refuses
inspect eval task.py --model anthropic/claude-fable-5 --fail-on-refusal

# fail on refusals from the agent, but let a grader refuse normally
inspect eval task.py --fail-on-refusal \
  --model-role grader="{model: openai/gpt-5, fail_on_refusal: false}"
```

### 7. Eval-set identity

The field stays in the task identifier hash (it is not added to
`GENERATE_CONFIG_FIELDS_TO_EXCLUDE` in `src/inspect_ai/_eval/evalset.py`)
because it changes sample outcomes. Flipping it produces a new task rather
than silently reusing logs.

### 8. CLI

`--fail-on-refusal` as a boolean flag on `inspect eval` and
`inspect eval-set`, env var `INSPECT_EVAL_FAIL_ON_REFUSAL`, converted in
`src/inspect_ai/_util/generate_config_args.py` the same way `logprobs` is (a
`False` flag becomes `None` so it does not clobber file config).

## Alternatives considered

- **A `react()`-only option** (`on_refusal="error"`). Rejected: does not cover
  custom agents, bridged scaffolds, or scorers, and the issue explicitly wants
  arbitrary tasks and agents.
- **A new sample limit** (`refusal_limit(n)`, `EvalSampleLimit` type
  `"refusal"`). Rejected as the primary mechanism because limits still score
  the sample and count as success. It would be a reasonable follow-up if a
  team wants "stop and score" rather than "stop and error", and nothing here
  precludes it.
- **A generalized `fail_on_stop_reason: list[StopReason]`.** Rejected for
  now: `model_length` and `max_tokens` have in-band recovery paths (overflow
  handling, compaction, truncation) that a raise inside `generate()` would
  break. `content_filter` is the only stop reason with no recovery path. The
  bool name does not block adding a list-valued sibling later.
- **A separate `eval()` argument** mirroring `log_refusals`. Rejected: no
  per-role or per-call granularity without new plumbing.

## Files touched

- `src/inspect_ai/model/_generate_config.py`: field on `GenerateConfigArgs`
  and `GenerateConfig`.
- `src/inspect_ai/model/_model.py`: `ModelRefusalError`, raise after
  `report_refusal`, inheritance in `_resolve_config`.
- `src/inspect_ai/model/__init__.py`: export the error.
- `src/inspect_ai/agent/_react.py`, `src/inspect_ai/agent/_bridge/util.py`:
  catch while retries remain.
- `src/inspect_ai/_cli/eval.py`, `src/inspect_ai/_util/generate_config_args.py`:
  flag and env var.
- Docs: `docs/fallbacks.qmd` (the refusals page), `docs/react-agent.qmd`
  (refusals section), `docs/eval-logs.qmd` next to `--log-refusals`,
  `docs/options.qmd`, `docs/handling-errors.qmd`.
- `CHANGELOG.md` entry under Unreleased.
- The `GenerateConfig` change regenerates `inspect-openapi.json` and the
  ts-mono types, so landing follows the `land-ts-mono` skill.

## Test plan

- `tests/model/test_refusal_display.py` (already covers refusal reporting):
  mockllm refusal with `fail_on_refusal=True` yields an errored sample whose
  error message carries the `Model refusal` prefix and category; `ErrorEvent`
  follows the `ModelEvent`; default is off; per-call `False` overrides an
  eval-wide `True`.
- Role precedence: eval-wide `True` with a role set to `False` leaves that
  role's generate working; eval-wide unset with a role set to `True` fails
  only on that role.
- `tests/agent/test_agent_react.py`: `retry_refusals=2` with three refusals
  errors; with two refusals then success it succeeds.
- Bridge equivalent in `tests/agent/`.
- `fail_on_error=False` records the sample error but the eval succeeds;
  `score_on_error=True` still scores.
- CLI parsing of `--fail-on-refusal` and the env var.

## Open questions

1. Should eval-wide `fail_on_refusal` reach grader roles by default? The
   design says yes for uniform loudness, with per-role opt-out. The
   alternative is to inherit only into the active model and require roles to
   opt in.
2. With `num_choices > 1`, should any refused choice trigger, or only the
   first? The design follows `report_refusal` (first choice) for consistency.
3. Should `retry_on_error` skip `ModelRefusalError`? Left as ordinary error
   behaviour for now.
