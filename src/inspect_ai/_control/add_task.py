"""Task-add directive for the control channel (phase 3).

Submits a *task spec* (registry name or file path, ``-T`` args, an optional
model) to the running eval process: the spec resolves in-process against the
run's models/config and the resulting tasks run under the same ``run_id`` as
new sibling evals. ``design/ctl/task-add.md`` owns the semantics this
resolver implements, including the decision table.

Shaped like :mod:`inspect_ai._control.requeue` and runs on the eval's own
loop. The eval runner registers an :class:`AddTaskCapability` alongside the
run (a process-global slot mirroring the keep-alive latch — *not* the
enqueuer ContextVar, which is scoped to the eval's async context); the
``POST /tasks`` route validates the body and invokes :func:`add_task`, so
the control layer never reaches into the runner. Results: an
:class:`AddTaskRejected` maps to a 409; :class:`AddTaskInvalid` (and any
exception out of spec resolution, which runs user code) maps to a 400;
otherwise the result carries ``changed`` — ``True`` for a fresh accept,
``False`` for a ``request_id`` replay echoing the original rows.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable, Literal

from pydantic import BaseModel, ConfigDict
from typing_extensions import TypedDict

if TYPE_CHECKING:
    from inspect_ai._eval.task.resolved import ResolvedTask

# Resolve one submitted spec — (spec, task_args, model) — to concrete
# ``ResolvedTask``s against the run's models / config. Built by the eval
# runner; raises on anything it can't resolve (unimportable spec, a task arg
# no resolved task consumes, an unconstructible model).
ResolveSpecFn = Callable[[str, "dict[str, Any]", "str | None"], "list[ResolvedTask]"]


class AddTaskBody(BaseModel):
    """The ``POST /tasks`` JSON body.

    ``extra="forbid"`` is explicit: pydantic's default *ignores* unknown
    fields, which would silently drop a newer client's parameter and recreate
    the partial-apply hazard the strict-mutations dependency closes for
    query-param routes — same fail-loud contract, different mechanism.
    """

    model_config = ConfigDict(extra="forbid")

    spec: str
    """Registry name or file path (``@task`` selector optional).

    File paths should be absolute — the server's cwd is the launch cwd, not
    the client's (the CLI absolutizes before sending)."""

    task_args: dict[str, Any] = {}
    """Task creation args (the ``-T`` map)."""

    model: str | None = None
    """Model to run against (defaults to every launch model, one task each)."""

    request_id: str | None = None
    """Optional idempotency key — a repeat of an accepted id echoes the
    original result with ``changed: false`` instead of enqueueing again."""

    dry_run: bool = False
    """Resolve and validate without enqueueing (nothing durable is created)."""


class AddTaskInvalid(Exception):
    """A malformed request (the route maps it to a 400)."""


class AddTaskRejected(TypedDict):
    """A rejection from the decision table (the route maps it to a 409)."""

    ok: Literal[False]
    error: str


class AddTaskRow(TypedDict):
    """One resolved task the add minted (or, under dry-run, would mint)."""

    task_id: str
    task_name: str
    model: str
    samples: int
    epochs: int


class AddTaskAccepted(TypedDict):
    """An accepted add (or, under ``dry_run``, what would be added).

    Returns ``task_id``s rather than ``eval_id``s: ``task_id`` is minted at
    spec resolution and is the selector the whole ctl surface takes, while
    ``eval_id`` is minted at logger init after this path has returned (see
    ``design/ctl/task-add.md`` "Identity").
    """

    ok: Literal[True]
    dry_run: bool
    changed: bool
    tasks: list[AddTaskRow]


AddTaskResult = AddTaskRejected | AddTaskAccepted


@dataclass
class AddTaskCapability:
    """The running eval's task-add entry points, registered process-globally.

    Closes over the run's spec resolution and (pre-resolved) enqueue, plus
    the ``request_id`` map. Registered by ``eval_async`` alongside the run
    and closed/cleared in its ``finally`` — in-memory and run-scoped like
    every control intent (a process restart forgets it, which is honest:
    the tasks it deduplicated are gone too).
    """

    run_id: str
    resolve: ResolveSpecFn
    enqueue: Callable[["list[ResolvedTask]"], None]
    eval_set: bool = False
    """Whether the run is an eval-set (task add rejects — see the doc's Scope)."""
    run_epochs: int | None = None
    """The run-level epochs override, if any (reported in the response rows)."""
    _closed: bool = False
    # accepted request_ids -> (canonical body key, original result). Only
    # accepted adds are recorded: a rejection enqueued nothing, so a caller
    # who fixes the condition and retries the same id gets a real attempt.
    _seen: dict[str, tuple[str, AddTaskAccepted]] = field(default_factory=dict)

    def close(self) -> None:
        """Stop accepting adds (the run is tearing down — cancellation wins)."""
        self._closed = True


# The active run's capability. Process-global (module-level) rather than a
# ContextVar because the control server task is not a child of the eval's
# async context — mirroring the keep-alive latch. Single-slot: only one
# eval_async runs per process (enforced in eval.py).
_capability: AddTaskCapability | None = None


def register_add_task(capability: AddTaskCapability) -> None:
    """Install ``capability`` as the active run's task-add entry point."""
    global _capability
    _capability = capability


def clear_add_task() -> None:
    """Remove the active capability (called at the run boundary)."""
    global _capability
    _capability = None


def get_add_task() -> AddTaskCapability | None:
    return _capability


def _reject(error: str) -> AddTaskRejected:
    return {"ok": False, "error": error}


def _body_key(spec: str, task_args: dict[str, Any], model: str | None) -> str:
    """Canonical form of the add-defining fields, for request_id mismatch checks.

    ``default=str`` keeps exotic (YAML-parsed) arg values from breaking the
    canonicalization — the key only needs to be stable per body, not
    reversible.
    """
    return json.dumps(
        {"spec": spec, "task_args": task_args, "model": model},
        sort_keys=True,
        default=str,
    )


def add_task(
    spec: str,
    *,
    task_args: dict[str, Any],
    model: str | None,
    request_id: str | None,
    dry_run: bool,
) -> AddTaskResult:
    """Add a task spec to the running eval (``POST /tasks``).

    Evaluated synchronously on the eval's loop — no await between the checks
    and the enqueue, so the decision table's rows are race-free (the same
    argument as requeue's accept path). Resolution failures (unimportable
    spec, bad ``-T`` arg, unknown/uncredentialed model) propagate to the
    route, which maps them to a 400; every rejection row reports its error
    under ``dry_run`` too, so an agent can probe safely.
    """
    from inspect_ai._control.server import keep_alive_intent

    capability = _capability
    if capability is None or capability._closed:
        return _reject(
            "not addable — no eval run is accepting task additions in this "
            "process (the run may be finishing or tearing down)"
        )
    if capability.eval_set:
        return _reject(
            "task add is not supported for eval-set processes yet — add "
            "against a standalone `inspect eval` process"
        )
    if not keep_alive_intent():
        return _reject(
            "not addable — launch with `--ctl-server=keep`, or run "
            "`inspect ctl process keep` while the eval is running"
        )

    # request_id replay: echo the original rows with changed=false. Dry-run
    # requests bypass the map entirely (neither recorded nor consulted) — a
    # dry-run enqueues nothing, so there is nothing to deduplicate, and
    # consulting it would let a probe-then-commit caller reusing one id get
    # the dry-run result echoed while the real add is silently never enqueued.
    key = _body_key(spec, task_args, model)
    if request_id is not None and not dry_run:
        seen = capability._seen.get(request_id)
        if seen is not None:
            seen_key, seen_result = seen
            if seen_key != key:
                raise AddTaskInvalid(
                    f"request_id '{request_id}' was already used for a "
                    "different add — echoing that result would be a wrong "
                    "answer shaped like success; use a fresh request_id"
                )
            return {**seen_result, "changed": False}

    # resolve (runs user code — task imports, dataset construction); errors
    # propagate to the route's 400 mapping
    resolved = capability.resolve(spec, task_args, model)

    from inspect_ai._util.constants import DEFAULT_EPOCHS

    rows: list[AddTaskRow] = [
        {
            "task_id": r.id,
            "task_name": r.task.name,
            "model": str(r.model),
            "samples": len(r.task.dataset),
            "epochs": capability.run_epochs or r.task.epochs or DEFAULT_EPOCHS,
        }
        for r in resolved
    ]
    result: AddTaskAccepted = {
        "ok": True,
        "dry_run": dry_run,
        "changed": True,
        "tasks": rows,
    }
    if dry_run:
        return result

    capability.enqueue(resolved)
    if request_id is not None:
        capability._seen[request_id] = (key, result)
    return result
