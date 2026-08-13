# ctl CLI refactor: split `_cli/ctl.py` into a package

> **Status: design.** Originating issue: meridianlabs-ai/inspect_ai#220.
> Companion to [`control-channel.md`](control-channel.md), which owns the
> control-channel architecture and the CLI command hierarchy this module
> implements; this doc owns only the *code organization* of the client. It
> proposes no behavior, surface, or JSON-shape changes — a pure move.

`src/inspect_ai/_cli/ctl.py` is 6,229 lines — every `inspect ctl` command,
its runner, the HTTP/discovery client, the `--json` error envelope, and all
human-output rendering in one module. It is the largest file in the package
by a wide margin, and each new directive (requeue, pause/resume, config
knobs) grows it further. The internal structure is already disciplined —
clearly-marked layer sections with one-way dependencies — so the refactor is
mostly mechanical: promote the existing sections to modules in a
`_cli/ctl/` package.

## Current structure

The file is organized by layer, with explicit section markers:

| Lines (approx) | Section |
|---|---|
| 1–420 | Module docstring, imports, tuning constants, knob tables, click param types, `_NounGroup` / option-mirroring infrastructure, root `ctl` group, shared echo/exit helpers |
| 420–1430 | Noun command groups (thin click wrappers): `task`, `sample`, `config`, `process`, `model` |
| 1430–1600 | Hidden deprecated aliases (the old flat spellings) |
| 1600–1830 | `--json` error envelope: `_CtlFailure`, `_fail`, `_classify`, `_structured_failures`, `_envelope_failures` |
| 1830–4100 | Command runners (shared by canonical commands and aliases): listings, show/events/messages, mutations, config compose/scope resolution |
| 4100–5400 | Server discovery + HTTP client: `_resolve_target_server`, `_ServerUnreachable`/`_ServerBusy`, busy narration, retry budgets, `_request_json`, per-resource fetches, version gates, `_exec_limits` |
| 5400–6230 | Rendering: every `_print_*` / `_format_*` helper, tables, footers, summaries |

Dependencies already flow one way: click commands → runners → client →
envelope, with rendering a leaf used by runners. There are no cycles to
untangle; the sections just need to become importable units.

## Constraints

- **Import path stability.** `inspect_ai._cli.main` does
  `from .ctl import ctl_command`; prose in `_control/{__init__,server,discovery}.py`
  and several `design/ctl/*.md` docs reference `inspect_ai._cli.ctl`.
  Converting the module to a *package* of the same name keeps every
  `inspect_ai._cli.ctl` reference valid.
- **Test coupling.** `tests/_control/test_ctl.py` (5,616 lines) plus
  `test_limits.py`, `test_server.py`, `test_buffer.py`, and
  `test_eval_set_integration.py` import ~35 private symbols from
  `inspect_ai._cli.ctl` and `monkeypatch.setattr` string targets of the form
  `"inspect_ai._cli.ctl.<name>"` at 112 sites. Both must be migrated
  mechanically and must fail loudly (not silently patch a dead alias) if a
  target goes stale.
- **Import-lightness.** The CLI deliberately avoids importing the core
  package (`TYPE_CHECKING` guard on `inspect_ai.log._samples`, see the
  comment at the guard). The split must not add eager heavy imports.
- **Behavior freeze.** No CLI surface, output, exit-code, or JSON-shape
  change. The agent output contract (see "Agent output contract" in
  [`control-channel.md`](control-channel.md)) and the discoverability
  docstrings ([`agent-discoverability.md`](agent-discoverability.md)) move
  verbatim.

## Proposed layout

`src/inspect_ai/_cli/ctl.py` becomes the package `src/inspect_ai/_cli/ctl/`.
Module boundaries follow the existing section markers; the two oversized
sections (runners, client) split along lines the code already draws — the
runners by resource noun, the client into transport vs. per-resource
fetches. Approximate sizes are from today's section extents, plus per-module
import boilerplate:

| Module | Contents | ~lines |
|---|---|---|
| `__init__.py` | Imports every command module for registration side effects; `__all__ = ["ctl_command"]`. Carries the current module docstring (the noun-group overview). | 80 |
| `_group.py` | Root `ctl` click group; `_NounGroup`, `_forward_group_options`, `_mirror_list_options`, `_json_option`, `_IntOrClearType`, `_deprecation_note`; shared echo/exit helpers (`_echo_no_running_evals`, `_busy_note`, `_anomalies_pointer`, `_exit_all_busy`). | 400 |
| `_failure.py` | The `--json` error envelope section: `_ErrorKind`, `_CtlFailure`, `_fail`, `_classify`, `_structured_failures`, `_envelope_failures`. | 230 |
| `_http.py` | Transport + targeting: `_resolve_target_server`, `_ServerUnreachable`/`_ServerBusy`, `_BusyNarrator`, retry budgets (`_REQUEST_ATTEMPTS`, `_DEGRADED_READ_ATTEMPTS`, `_MAX_CONCURRENT_READS`, timeouts), `_get_with_retry_async`, `_request_json`, `_handler_404`, `_run_async`, `_collect_reads`, `_exit_busy`, `_unreachable_failure`, error-detail helpers. | 810 |
| `_fetch.py` | Per-resource reads/writes over `_http`: `_fetch_summaries`, `_read_task_rows` / `_read_all_task_rows`, `_fetch_samples*`, `_fetch_sample_detail` / `_fetch_sample_events` / `_fetch_sample_messages`, `_post_flush`; target-eval resolution (`_resolve_target_eval`, `_match_by_task_name`, `_exit_ambiguous`). | 650 |
| `_mutate.py` | Shared mutation machinery: `_mutation_envelope`, `_run_sample_mutation`, `_paused_sources`, `_still_held_note`. | 150 |
| `_task.py` | `task` group commands + their runners: `_run_task_list`, `_run_task_cancel`, `_run_task_pause_resume`, `_run_log_flush`. | 550 |
| `_sample.py` | `sample` group commands (list/errors/show/events/messages/cancel/requeue) + the mutation runners (`_run_sample_cancel`, `_run_sample_requeue`) and option-validation helpers (`_validate_cursor`, `_validate_from_start`, `_normalized_types`, `_exit_removed_since`). | 750 |
| `_sample_read.py` | Sample read runners: `_list_sample_rows`, `_read_all_eval_samples`, `_run_sample_list` / `_run_sample_errors` / `_run_sample_listing`, `_run_sample_show`, `_run_sample_events`, `_run_sample_messages`, idle/truncation footers, and their tuning constants (`_DEFAULT_EVENTS_TAIL`, `_DEFAULT_MESSAGES_TAIL`, `_IDLE_POINTER_MIN_SECONDS`). | 850 |
| `_config.py` | `config` command + everything config-specific: `_KNOB_SCOPE` / `_KNOB_SINCE` / `_PROVENANCE_SINCE`, `_run_config`, `_compose_config`, `_resolve_scope` / `_DirectiveScope`, `_applied_knob_names`, `_gate_knob_support` / `_gate_provenance_support`, `_exec_limits`, `_ConfigResult`, process-scope notes. | 950 |
| `_process.py` | `process` group commands + runners: `_run_process_list`, `_run_keep_alive`, `_run_process_pause_resume`, `_run_process_anomalies` (+ `_trace_file_for_pid`, `_PidAnomalies` — the `_cli/trace.py` integration). | 500 |
| `_model.py` | `model` group commands + `_run_model_pause_resume`. | 160 |
| `_aliases.py` | The hidden deprecated flat spellings, unchanged thin delegations to the runners. | 190 |
| `_render.py` | The whole rendering section: `_print_*`, `_format_*`, `_render_table`, `_short_id` / `_SHORT_ID_LEN`, `_truncate`, event/message summaries. | 880 |

Notes on placement judgment calls:

- **Runners live with their noun's commands** (not in a separate `runners`
  layer) — a runner has exactly one canonical command plus at most one
  hidden alias as callers, so co-location is where a reader looks first.
  The exception is `sample`, whose read runners are large enough to warrant
  the `_sample.py` / `_sample_read.py` split (commands + mutations vs. read
  runners); if implementation finds the seam awkward, collapsing them into
  one ~1,500-line `_sample.py` is acceptable — still a 4× reduction and the
  noun boundary is the one that must hold.
- **`_render.py` stays one module.** The formatters are small, uniform, and
  heavily shared across nouns (the samples table serves `sample list`,
  `task list` footers, and the errors table); splitting them per-noun would
  force either duplication or a shared-formatters module that recreates
  today's grab-bag.
- **`_mutate.py` is deliberately tiny.** Only the machinery *shared* across
  nouns; noun-specific mutation runners stay in their noun module.
- **`_unreachable_failure` moves to `_http.py`, not `_failure.py`**, even
  though today it sits in the envelope section: it runtime-dispatches on
  `isinstance(exc, _ServerBusy)`, so keeping it beside `_CtlFailure` would
  make `_failure` import `_http` — the one cycle the layering forbids. Its
  natural home is next to the exception types it translates.
- **Root group in `_group.py`, not `__init__.py`.** Noun modules attach via
  `@task_group.command(...)` decorators at import time, so they need the
  parent group importable without importing the package `__init__`
  (avoiding an import cycle). `__init__.py` then imports the noun modules
  purely for their registration side effects and re-exports `ctl_command`.

### Dependency layering

One-way, matching today's section order (leaf → top):

```
_render, _failure                    (leaves)
_group  → _failure                   (shared exit helpers raise _CtlFailure)
_http   → _failure
_fetch  → _http, _failure
_mutate → _http, _fetch, _failure
noun modules (_task, _sample, _sample_read,
  _config, _process, _model, _aliases)
        → _group, _mutate, _fetch, _http, _failure, _render
__init__ → _group + every noun module (registration)
```

A cycle would be an implementation bug; the extraction order below makes one
impossible to introduce silently (each extracted module must import cleanly
before the next extraction starts).

### Patchable seams: import modules, not names

`monkeypatch.setattr` only affects lookups through the patched namespace, so
after the split a test patching `inspect_ai._cli.ctl._fetch.` +
`_fetch_samples_async` must actually intercept the runner's call. The rule
that keeps every existing patch site a one-line mechanical rewrite:

**Cross-module references to functions that tests patch go through the
module object** — `from inspect_ai._cli.ctl import _fetch` then
`_fetch._fetch_samples_async(...)` — never `from ._fetch import
_fetch_samples_async`. Each seam then has exactly one canonical patch target
(its defining module) that intercepts *every* consumer. Types, constants,
and never-patched helpers may be imported by name as usual. The seams tests
patch today: `list_discovered_servers`, `httpx` client classes,
`_get_with_retry_async`, `_fetch_samples` / `_fetch_samples_async`,
`_fetch_summaries`, `_exec_limits`, `read_trace_file`, and the retry-budget
constants. A short comment at each module-object import notes it is a patch
seam, so a later cleanup doesn't "simplify" it back into a name import.

Symbol names are **kept verbatim** (leading underscores included) even
though module-level privacy now also comes from the path. Renaming would
turn a `sed`-able migration into a semantic one and break the
grep-discoverability of every symbol cited in `design/ctl/*.md`. A
follow-up may drop underscores; not this refactor.

### `__init__.py` re-exports nothing private

Only `ctl_command` is exported. Test imports and patch targets are updated
to the defining modules rather than served by a compatibility facade: a
facade would keep stale patch targets *importable* while making them
silently ineffective (patching the facade's alias, not the name the runner
looks up) — the worst failure mode, a test that passes for the wrong
reason. With no facade, any missed migration fails loudly at import or
`setattr` time.

## Test migration

Mechanical, in the same commit as each extraction (the suite stays green at
every commit):

1. **Imports** — rewrite `from inspect_ai._cli.ctl import X` to the
   defining module per the table above (~35 symbols across 5 test files,
   both the top-of-file blocks and the in-test lazy imports).
2. **Patch targets** — rewrite `"inspect_ai._cli.ctl.<name>"` to
   `"inspect_ai._cli.ctl.<module>.<name>"` (112 sites). The module-object
   seam rule above guarantees the defining module is always the correct
   target.
3. **No assertion changes.** Any test whose *assertions* need touching is a
   red flag that the move changed behavior.

`tests/_control/test_ctl.py` itself (5,616 lines) is out of scope here; a
natural follow-up splits it along the same noun/module lines once the
source layout settles.

## Migration plan

One PR, reviewable commit by commit, suite green after each:

1. `git mv src/inspect_ai/_cli/ctl.py src/inspect_ai/_cli/ctl/__init__.py`
   — a pure rename commit (100% similarity), preserving history through the
   package conversion.
2. Extract leaves: `_render.py`, `_failure.py`.
3. Extract `_group.py`, `_http.py`, `_fetch.py`, `_mutate.py`.
4. Extract noun modules: `_config.py`, `_sample.py` + `_sample_read.py`,
   `_task.py`, `_process.py`, `_model.py`, then `_aliases.py`.
5. Shrink `__init__.py` to registration + docstring; update the file-path
   references in `design/ctl/*.md` (`agent-discoverability.md`,
   `config-log-persistence.md`, `sample-requeue.md`, `pause-resume.md`)
   to the new module paths.

Each extraction commit moves code verbatim (plus the import block), moves
the section's tests' imports/patch targets, and nothing else. Reviewers can
verify move-only commits with `git diff --color-moved=dimmed-zebra`.

Verification gate per commit: `pytest tests/_control tests/_cli`, then
`ruff check` and `mypy` over `src/inspect_ai/_cli/ctl/` at the end. The
5,616-line ctl test suite is the behavioral safety net — it exercises every
command's human and `--json` output, the retry/busy paths, and the version
gates.

## Risks

- **Silent behavior drift during a move.** Mitigated by move-only commits,
  `--color-moved` review, the no-assertion-changes rule, and the breadth of
  the existing suite.
- **Missed patch-target migration.** Fails loudly by design (no facade;
  `monkeypatch.setattr` raises on a missing attribute).
- **Import cycles.** The layering is acyclic today; leaf-first extraction
  order surfaces any accidental inversion immediately as an `ImportError`.
- **`git log --follow` across the split.** History for extracted modules
  needs `git log --follow` from the extraction commit back through
  `__init__.py` to `ctl.py`; the rename-first commit keeps that chain
  intact for the file bulk. Accepted cost, standard for any split.

## Non-goals

- No CLI surface, output, exit-code, or JSON-shape changes.
- No symbol renames (see above) and no de-underscoring.
- No restructuring of `tests/_control/test_ctl.py` (follow-up).
- No changes to the deprecated-alias transition policy — aliases move as-is
  and retire on their own schedule.
