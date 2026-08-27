"""Tests for adding tasks to a running eval via `enqueue_task`.

`enqueue_task` is the in-process primitive: a task added while the eval runs is
picked up after the current batch completes and run under the same run_id. Here
the trigger is a solver (the simplest in-run caller); the ergonomic trigger
surface — hooks, a callback, etc. — is layered on top and tested separately.
"""

import pytest

from inspect_ai import Task, eval, task
from inspect_ai._eval.loader import resolve_tasks
from inspect_ai._eval.task.enqueue import enqueue_task
from inspect_ai._util.content import ContentImage
from inspect_ai.dataset import Sample
from inspect_ai.model import ChatMessageUser, get_model
from inspect_ai.solver import Generate, Solver, TaskState, generate, solver


@task
def child() -> Task:
    return Task(
        dataset=[Sample(input="hi", target="ok")],
        solver=[generate()],
        name="child",
    )


@solver
def enqueue_child():
    """A solver that adds a `child` task to the running eval, once."""

    async def solve(state: TaskState, generate: Generate) -> TaskState:
        enqueue_task(child())
        return state

    return solve


def test_enqueue_task_runs_added_task_in_same_run() -> None:
    parent = Task(
        dataset=[Sample(input="hi", target="ok")],
        solver=[enqueue_child()],
        name="parent",
    )

    logs = eval(parent, model="mockllm/model", display="none")

    # the enqueued child ran alongside the parent, in the same run
    assert sorted(log.eval.task for log in logs) == ["child", "parent"]
    assert all(log.status == "success" for log in logs)
    assert len({log.eval.run_id for log in logs}) == 1


def test_enqueue_task_chains_across_batches() -> None:
    # each task enqueues the next up to a depth, exercising repeated draining:
    # parent -> gen1 -> gen2, all under one run
    seen: list[str] = []

    @solver
    def spawn_next(depth: int):
        async def solve(state: TaskState, generate: Generate) -> TaskState:
            seen.append(state.metadata.get("name", "?"))
            if depth > 0:
                enqueue_task(
                    Task(
                        dataset=[Sample(input="hi", target="ok")],
                        solver=[spawn_next(depth - 1)],
                        name=f"gen{depth - 1}",
                        metadata={"name": f"gen{depth - 1}"},
                    )
                )
            return state

        return solve

    parent = Task(
        dataset=[Sample(input="hi", target="ok")],
        solver=[spawn_next(2)],
        name="parent",
        metadata={"name": "parent"},
    )

    logs = eval(parent, model="mockllm/model", display="none")
    assert sorted(log.eval.task for log in logs) == ["gen0", "gen1", "parent"]
    assert len({log.eval.run_id for log in logs}) == 1


def test_enqueued_task_media_is_inline_only(tmp_path) -> None:
    secret = tmp_path / "secret.png"
    secret.write_bytes(b"runtime-selected")
    seen: list[str] = []

    @solver
    def child_solver() -> Solver:
        async def solve(state: TaskState, generate: Generate) -> TaskState:
            assert not isinstance(state.input, str)
            content = state.input[0].content
            assert isinstance(content, list)
            image = content[0]
            assert isinstance(image, ContentImage)
            seen.append(image.image)
            return state

        return solve

    @solver
    def enqueue_child() -> Solver:
        async def solve(state: TaskState, generate: Generate) -> TaskState:
            enqueue_task(
                Task(
                    dataset=[
                        Sample(
                            id="child",
                            input=[
                                ChatMessageUser(
                                    content=[ContentImage(image=str(secret))]
                                )
                            ],
                        )
                    ],
                    solver=child_solver(),
                    name="child-media",
                )
            )
            return state

        return solve

    logs = eval(
        Task(
            dataset=[Sample(id="parent", input="parent")],
            solver=enqueue_child(),
            name="parent-media",
        ),
        model="mockllm/model",
        display="none",
    )

    assert seen == [str(secret)]
    child_sample = next(
        sample for log in logs for sample in (log.samples or []) if sample.id == "child"
    )
    content = child_sample.messages[-1].content
    assert isinstance(content, list)
    image = content[0]
    assert isinstance(image, ContentImage)
    assert image.image == str(secret)


def test_enqueued_resolved_task_media_is_inline_only(tmp_path) -> None:
    secret = tmp_path / "secret.png"
    secret.write_bytes(b"runtime-selected")
    seen: list[str] = []

    @solver
    def child_solver() -> Solver:
        async def solve(state: TaskState, generate: Generate) -> TaskState:
            assert not isinstance(state.input, str)
            content = state.input[0].content
            assert isinstance(content, list)
            image = content[0]
            assert isinstance(image, ContentImage)
            seen.append(image.image)
            return state

        return solve

    child = Task(
        dataset=[
            Sample(
                id="child",
                input=[ChatMessageUser(content=[ContentImage(image=str(secret))])],
            )
        ],
        solver=child_solver(),
        name="child-resolved-media",
    )
    resolved_child = resolve_tasks(
        child,
        {},
        get_model("mockllm/model"),
        None,
        None,
        None,
    )[0]
    assert resolved_child.input_media_policy == "inline_only"

    @solver
    def enqueue_resolved_child() -> Solver:
        async def solve(state: TaskState, generate: Generate) -> TaskState:
            enqueue_task(resolved_child)
            return state

        return solve

    eval(
        Task(
            dataset=[Sample(id="parent", input="parent")],
            solver=enqueue_resolved_child(),
            name="parent-resolved-media",
        ),
        model="mockllm/model",
        display="none",
    )

    assert seen == [str(secret)]
    assert resolved_child.input_media_policy == "inline_only"


def test_enqueue_task_does_not_leak_active_model() -> None:
    # enqueue_task() resolves the added task against every model in the run,
    # calling init_active_model() per model. That resolution must not leak into
    # the caller: a solver running under one model must still see that model via
    # get_model() after enqueuing. Two models make the leak observable — the
    # resolution loop would otherwise leave the *last* model active.
    mismatches: list[tuple[str, str]] = []

    @solver
    def enqueue_and_check():
        async def solve(state: TaskState, generate: Generate) -> TaskState:
            before = str(get_model())
            enqueue_task(child())
            after = str(get_model())
            if before != after:
                mismatches.append((before, after))
            return state

        return solve

    parent = Task(
        dataset=[Sample(input="hi", target="ok")],
        solver=[enqueue_and_check()],
        name="parent",
    )

    logs = eval(parent, model=["mockllm/model", "mockllm/model2"], display="none")
    assert mismatches == [], f"active model leaked after enqueue_task: {mismatches}"
    assert all(log.status == "success" for log in logs)


def test_enqueue_task_outside_run_raises() -> None:
    with pytest.raises(RuntimeError, match="while an eval is running"):
        enqueue_task(child())


def test_enqueuer_marks_obligations_distinctly() -> None:
    """Obligation entries are tracked apart from plain ones; drain clears both.

    The keep-alive park gate keys on ``has_pending_obligation()`` — buffered
    control-channel adds (accepted-means-will-run) open the park even with
    keep intent off, where plain ``enqueue_task`` leftovers must not.
    """
    from typing import Any, cast

    from inspect_ai._eval.task.enqueue import create_task_enqueuer

    enqueuer = create_task_enqueuer("run-1", lambda tasks: [])
    plain = cast(Any, object())
    enqueuer.enqueue_resolved([plain])
    assert enqueuer.has_pending()
    assert not enqueuer.has_pending_obligation()
    enqueuer.enqueue_resolved([plain], obligation=True)
    assert enqueuer.has_pending_obligation()
    assert len(enqueuer.drain()) == 2
    assert not enqueuer.has_pending()
    assert not enqueuer.has_pending_obligation()


def test_cancelled_run_drops_buffered_additions(monkeypatch) -> None:
    """A run ended by a cancelled batch drops plain enqueue_task() leftovers.

    Regression: the keep-alive park gate widened to honor buffered
    control-channel adds; keyed on the whole enqueuer buffer, it resurrected
    an ``enqueue_task()`` follow-up after a cancellation in a process that
    never asked for keep-alive. The gate keys on obligations now, so the
    leftover is dropped exactly as before the widening.
    """
    from typing import Any

    from inspect_ai._eval.task.run import task_run as original_task_run

    async def cancelling_task_run(options: Any, task_cancel: Any = None) -> Any:
        # emulate external cancellation of the batch (the shape the display's
        # cancel binding produces): the log reports cancelled but no exception
        # unwinds the run, so the eval proceeds to the keep-alive park gate
        result = await original_task_run(options, task_cancel=task_cancel)
        result.status = "cancelled"
        return result

    monkeypatch.setattr("inspect_ai._eval.run.task_run", cancelling_task_run)

    parent = Task(
        dataset=[Sample(input="hi", target="ok")],
        solver=[enqueue_child()],
        name="parent",
    )
    logs = eval(parent, model="mockllm/model", display="none")

    # the buffered child never ran: a cancelled run without keep-alive intent
    # exits instead of executing leftover additions
    assert [log.eval.task for log in logs] == ["parent"]
    assert logs[0].status == "cancelled"
