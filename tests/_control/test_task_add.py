"""Tests for the control channel's task-add directive (``POST /tasks``).

Covers the accept/reject decision table and ``request_id`` idempotency at the
resolver level (``inspect_ai._control.add_task``), the route's body handling
via the FastAPI app, the CLI verb's targeting/spec handling, and an
end-to-end park-restart: a task added to a keep-alive-parked eval runs in a
fresh session under the same ``run_id``. See ``design/ctl/task-add.md``.
"""

import os
from types import SimpleNamespace
from typing import Any

import anyio
import httpx
import pytest
from test_helpers.utils import skip_if_trio

from inspect_ai import Task
from inspect_ai._control.add_task import (
    AddTaskCapability,
    AddTaskInvalid,
    add_task,
    clear_add_task,
    close_add_task,
    register_add_task,
)
from inspect_ai._control.server import (
    request_keep_alive,
    request_release,
    reset_keep_alive,
)
from inspect_ai.dataset import Sample
from inspect_ai.solver import generate


def _fake_resolved(
    name: str = "added", task_id: str = "tid-1", dataset: Any = None
) -> Any:
    return SimpleNamespace(
        id=task_id,
        task=SimpleNamespace(
            name=name,
            dataset=dataset if dataset is not None else [1, 2],
            epochs=None,
            sample_source=None,
        ),
        model="mockllm/model",
        task_args={},
    )


class _Capability:
    """Builds an AddTaskCapability around recording stubs."""

    def __init__(
        self,
        *,
        eval_set: bool = False,
        resolve_error: Exception | None = None,
        resolved: list[Any] | None = None,
        run_limit: Any = None,
        run_sample_id: Any = None,
    ) -> None:
        self.enqueued: list[list[Any]] = []
        self.resolve_calls: list[tuple[str, dict[str, Any], str | None]] = []
        self._resolve_error = resolve_error
        self._resolved = resolved if resolved is not None else [_fake_resolved()]

        def resolve(
            spec: str, task_args: dict[str, Any], model: str | None
        ) -> list[Any]:
            self.resolve_calls.append((spec, task_args, model))
            if self._resolve_error is not None:
                raise self._resolve_error
            return self._resolved

        self.capability = AddTaskCapability(
            run_id="run-1",
            resolve=resolve,
            enqueue=self.enqueued.append,
            eval_set=eval_set,
            run_limit=run_limit,
            run_sample_id=run_sample_id,
        )


@pytest.fixture(autouse=True)
def _clean_registrations():
    reset_keep_alive()
    clear_add_task()
    yield
    reset_keep_alive()
    clear_add_task()


def test_add_task_rejects_without_capability() -> None:
    request_keep_alive()
    result = add_task(
        "some_task", task_args={}, model=None, request_id=None, dry_run=False
    )
    assert result["ok"] is False
    assert "not addable" in result["error"]


def test_add_task_rejects_closed_capability() -> None:
    request_keep_alive()
    cap = _Capability()
    register_add_task(cap.capability)
    # the teardown hook (fired at the start of the run's exception unwind)
    close_add_task()
    result = add_task(
        "some_task", task_args={}, model=None, request_id=None, dry_run=False
    )
    assert result["ok"] is False
    assert cap.enqueued == []
    # a contained park-session failure re-opens (the process is still addable)
    cap.capability.reopen()
    reopened = add_task(
        "some_task", task_args={}, model=None, request_id=None, dry_run=False
    )
    assert reopened["ok"] is True


def test_add_task_rejects_eval_set() -> None:
    request_keep_alive()
    cap = _Capability(eval_set=True)
    register_add_task(cap.capability)
    result = add_task(
        "some_task", task_args={}, model=None, request_id=None, dry_run=False
    )
    assert result["ok"] is False
    assert "eval-set" in result["error"]


def test_add_task_rejects_without_keep_intent() -> None:
    cap = _Capability()
    register_add_task(cap.capability)
    result = add_task(
        "some_task", task_args={}, model=None, request_id=None, dry_run=False
    )
    assert result["ok"] is False
    # the error names both fixes
    assert "--ctl-server=keep" in result["error"]
    assert "ctl process keep" in result["error"]
    assert cap.enqueued == []


def test_add_task_accepts_and_enqueues() -> None:
    request_keep_alive()
    cap = _Capability()
    register_add_task(cap.capability)
    result = add_task(
        "some_task",
        task_args={"difficulty": "hard"},
        model="mockllm/model",
        request_id=None,
        dry_run=False,
    )
    assert result["ok"] is True
    assert result["changed"] is True
    assert result["tasks"] == [
        {
            "task_id": "tid-1",
            "task_name": "added",
            "model": "mockllm/model",
            "samples": 2,
            "epochs": 1,
        }
    ]
    # the exact resolved tasks were enqueued (resolve once — the response's
    # task_ids must be the enqueued ones)
    assert cap.enqueued == [cap._resolved]
    assert cap.resolve_calls == [("some_task", {"difficulty": "hard"}, "mockllm/model")]


def test_add_task_dry_run_resolves_without_enqueueing() -> None:
    request_keep_alive()
    cap = _Capability()
    register_add_task(cap.capability)
    result = add_task(
        "some_task", task_args={}, model=None, request_id=None, dry_run=True
    )
    assert result["ok"] is True
    assert result["dry_run"] is True
    assert result["changed"] is True
    assert len(result["tasks"]) == 1
    assert cap.enqueued == []


def test_add_task_request_id_replay_is_idempotent() -> None:
    request_keep_alive()
    cap = _Capability()
    register_add_task(cap.capability)
    first = add_task(
        "some_task", task_args={}, model=None, request_id="rid-1", dry_run=False
    )
    replay = add_task(
        "some_task", task_args={}, model=None, request_id="rid-1", dry_run=False
    )
    assert first["ok"] is True and first["changed"] is True
    assert replay["ok"] is True and replay["changed"] is False
    assert replay["tasks"] == first["tasks"]
    # enqueued exactly once
    assert len(cap.enqueued) == 1


def test_add_task_request_id_body_mismatch_is_invalid() -> None:
    request_keep_alive()
    cap = _Capability()
    register_add_task(cap.capability)
    add_task("some_task", task_args={}, model=None, request_id="rid-1", dry_run=False)
    with pytest.raises(AddTaskInvalid):
        add_task(
            "other_task", task_args={}, model=None, request_id="rid-1", dry_run=False
        )


def test_add_task_dry_run_bypasses_request_id_map() -> None:
    """A probe-then-commit caller reusing one id must get a real add."""
    request_keep_alive()
    cap = _Capability()
    register_add_task(cap.capability)
    probe = add_task(
        "some_task", task_args={}, model=None, request_id="rid-1", dry_run=True
    )
    commit = add_task(
        "some_task", task_args={}, model=None, request_id="rid-1", dry_run=False
    )
    assert probe["ok"] is True and commit["ok"] is True
    assert probe["dry_run"] is True and probe["changed"] is True
    assert commit["dry_run"] is False and commit["changed"] is True
    assert len(cap.enqueued) == 1
    # and a dry-run after the accept is not recorded either: the recorded
    # accept still echoes for a *real* replay
    replay = add_task(
        "some_task", task_args={}, model=None, request_id="rid-1", dry_run=False
    )
    assert replay["ok"] is True and replay["changed"] is False
    assert len(cap.enqueued) == 1


def test_add_task_request_id_map_is_bounded() -> None:
    """The request_id map evicts oldest-first past its cap.

    A long-lived keep-alive-parked worker accrues one entry per accepted add;
    the cap bounds that. A replay of an evicted id is a fresh attempt (it
    enqueues again) while a still-recorded id keeps echoing.
    """
    from inspect_ai._control.add_task import SEEN_REQUEST_IDS_MAX

    request_keep_alive()
    cap = _Capability()
    register_add_task(cap.capability)
    for i in range(SEEN_REQUEST_IDS_MAX + 1):
        result = add_task(
            f"task_{i}", task_args={}, model=None, request_id=f"rid-{i}", dry_run=False
        )
        assert result["ok"] is True and result["changed"] is True
    assert len(cap.capability._seen) == SEEN_REQUEST_IDS_MAX
    # the newest id still echoes...
    newest = add_task(
        f"task_{SEEN_REQUEST_IDS_MAX}",
        task_args={},
        model=None,
        request_id=f"rid-{SEEN_REQUEST_IDS_MAX}",
        dry_run=False,
    )
    assert newest["ok"] is True and newest["changed"] is False
    # ...while the evicted oldest gets a genuine re-add
    enqueues_before = len(cap.enqueued)
    oldest = add_task(
        "task_0", task_args={}, model=None, request_id="rid-0", dry_run=False
    )
    assert oldest["ok"] is True and oldest["changed"] is True
    assert len(cap.enqueued) == enqueues_before + 1


def test_add_task_rejections_are_not_recorded() -> None:
    """A caller who fixes the condition and retries the same id gets a real add."""
    cap = _Capability()
    register_add_task(cap.capability)
    rejected = add_task(
        "some_task", task_args={}, model=None, request_id="rid-1", dry_run=False
    )
    assert rejected["ok"] is False
    request_keep_alive()
    retried = add_task(
        "some_task", task_args={}, model=None, request_id="rid-1", dry_run=False
    )
    assert retried["ok"] is True and retried["changed"] is True
    assert len(cap.enqueued) == 1


def test_add_task_resolution_error_maps_to_invalid() -> None:
    """Resolution failures are wrapped in AddTaskInvalid (the route's 400).

    The exception type distinguishes pre-enqueue failures from a fault after
    the add was applied (which propagates, rather than lying "not resolved").
    """
    request_keep_alive()
    cap = _Capability(resolve_error=ValueError("no such task"))
    register_add_task(cap.capability)
    with pytest.raises(
        AddTaskInvalid, match="could not resolve 'missing'.*no such task"
    ):
        add_task("missing", task_args={}, model=None, request_id=None, dry_run=False)
    assert cap.enqueued == []


def test_add_task_samples_honors_run_limit() -> None:
    """The response rows' `samples` reflects the run's --limit slice.

    The added task runs under the run's resolved config, so the count matches
    what will run — the way `epochs` honors the run-level override.
    """
    request_keep_alive()
    cap = _Capability(run_limit=1)
    register_add_task(cap.capability)
    result = add_task(
        "some_task", task_args={}, model=None, request_id=None, dry_run=True
    )
    assert result["ok"] is True
    assert result["tasks"][0]["samples"] == 1


def test_add_task_samples_honors_run_sample_id() -> None:
    from inspect_ai.dataset import MemoryDataset

    dataset = MemoryDataset(
        [Sample(input="a", id="s1"), Sample(input="b", id="s2")], name="added_ds"
    )
    request_keep_alive()
    cap = _Capability(resolved=[_fake_resolved(dataset=dataset)], run_sample_id="s1")
    register_add_task(cap.capability)
    result = add_task(
        "some_task", task_args={}, model=None, request_id=None, dry_run=True
    )
    assert result["ok"] is True
    assert result["tasks"][0]["samples"] == 1
    # an added dataset that can't satisfy the run's filter fails at accept
    # (fail-loud at the 400, rather than a contained task error later)
    cap_no_match = _Capability(
        resolved=[_fake_resolved(dataset=dataset)], run_sample_id="nope"
    )
    register_add_task(cap_no_match.capability)
    with pytest.raises(AddTaskInvalid, match="No matches"):
        add_task("some_task", task_args={}, model=None, request_id=None, dry_run=True)
    assert cap_no_match.enqueued == []


# ---------------------------------------------------------------------------
# Route tests (FastAPI app over ASGI transport)
# ---------------------------------------------------------------------------


def _app() -> Any:
    from inspect_ai._control.server import ControlServer

    return ControlServer(run_id="test")._build_app()


@skip_if_trio
async def test_add_route_rejects_non_object_body() -> None:
    """A missing, malformed, or non-object body 400s with the channel's error shape."""
    request_keep_alive()
    cap = _Capability()
    register_add_task(cap.capability)
    transport = httpx.ASGITransport(app=_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        for content in (None, b"[1, 2]", b'"spec"', b"not json {"):
            response = await client.post(
                "/tasks",
                content=content,
                headers={"content-type": "application/json"},
            )
            assert response.status_code == 400
            assert "error" in response.json()
    assert cap.enqueued == []


@skip_if_trio
async def test_add_route_rejects_unknown_body_field() -> None:
    """extra="forbid": an unknown field 400s instead of being silently dropped."""
    request_keep_alive()
    cap = _Capability()
    register_add_task(cap.capability)
    transport = httpx.ASGITransport(app=_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/tasks", json={"spec": "some_task", "not_a_field": 1}
        )
    assert response.status_code == 400
    assert "error" in response.json()
    assert cap.enqueued == []


@skip_if_trio
async def test_add_route_maps_results() -> None:
    request_keep_alive()
    cap = _Capability()
    register_add_task(cap.capability)
    transport = httpx.ASGITransport(app=_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        # accepted
        ok = await client.post("/tasks", json={"spec": "some_task"})
        assert ok.status_code == 200
        body = ok.json()
        assert body["changed"] is True
        assert body["tasks"][0]["task_id"] == "tid-1"
        # not addable → 409
        request_release()
        conflict = await client.post("/tasks", json={"spec": "some_task"})
        assert conflict.status_code == 409
        assert "error" in conflict.json()


@skip_if_trio
async def test_add_route_maps_resolution_error_to_400() -> None:
    request_keep_alive()
    cap = _Capability(resolve_error=ValueError("bad -T arg"))
    register_add_task(cap.capability)
    transport = httpx.ASGITransport(app=_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/tasks", json={"spec": "some_task"})
    assert response.status_code == 400
    assert "bad -T arg" in response.json()["error"]


# ---------------------------------------------------------------------------
# CLI verb
# ---------------------------------------------------------------------------


def test_absolute_spec_absolutizes_paths_only(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from inspect_ai._cli.ctl._task import _absolute_spec

    # registry / package names pass through
    assert _absolute_spec("arc_easy") == "arc_easy"
    assert _absolute_spec("inspect_evals/gpqa") == "inspect_evals/gpqa"
    assert _absolute_spec("hf/some/task") == "hf/some/task"
    # file specs absolutize, @task selector preserved
    assert _absolute_spec("evals/arc.py") == os.path.abspath("evals/arc.py")
    assert (
        _absolute_spec("evals/arc.py@arc_easy")
        == f"{os.path.abspath('evals/arc.py')}@arc_easy"
    )
    # explicitly path-shaped directory spellings absolutize
    assert _absolute_spec(".") == os.path.abspath(".")
    assert _absolute_spec("evals/") == os.path.abspath("evals")
    assert _absolute_spec("./evals") == os.path.abspath("evals")
    # a bare name passes through even when a same-named local path exists:
    # the server resolves registry-first (inspect eval parity), so the
    # client's cwd must not shadow a registry task name
    (tmp_path / "arc_easy").mkdir()
    monkeypatch.chdir(tmp_path)
    assert _absolute_spec("arc_easy") == "arc_easy"


def test_cli_task_add_sends_body_and_renders(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from pathlib import Path

    from inspect_ai._cli.ctl import _http, _task

    server = SimpleNamespace(pid=123, socket_path=Path("/tmp/ctl.sock"))
    monkeypatch.setattr(
        _http, "list_discovered_servers", lambda: [server], raising=True
    )

    sent: dict[str, Any] = {}

    def fake_request_json(socket_path: str, path: str, **kwargs: Any) -> Any:
        sent["path"] = path
        sent["json_body"] = kwargs.get("json_body")
        return {
            "ok": True,
            "dry_run": False,
            "changed": True,
            "tasks": [
                {
                    "task_id": "tid-1",
                    "task_name": "added",
                    "model": "mockllm/model",
                    "samples": 2,
                    "epochs": 1,
                }
            ],
        }

    monkeypatch.setattr(_http, "_request_json", fake_request_json, raising=True)

    _task._run_task_add(
        "some_task",
        model="mockllm/model",
        task_arg=("difficulty=hard",),
        dry_run=False,
        pid=None,
        as_json=False,
        terse=False,
    )
    out = capsys.readouterr().out
    assert sent["path"] == "/tasks"
    body = sent["json_body"]
    assert body["spec"] == "some_task"
    assert body["task_args"] == {"difficulty": "hard"}
    assert body["model"] == "mockllm/model"
    assert body["request_id"]  # always sent
    assert "dry_run" not in body
    assert "Added 1 task" in out
    assert "tid-1" in out


def test_cli_task_add_ambiguous_without_addable_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pathlib import Path

    from inspect_ai._cli.ctl import _fetch, _http, _task
    from inspect_ai._cli.ctl._failure import _CtlFailure

    servers = [
        SimpleNamespace(pid=1, socket_path=Path("/tmp/a.sock")),
        SimpleNamespace(pid=2, socket_path=Path("/tmp/b.sock")),
    ]
    monkeypatch.setattr(_http, "list_discovered_servers", lambda: servers, raising=True)
    monkeypatch.setattr(
        _fetch,
        "_fetch_summaries",
        lambda s: SimpleNamespace(
            summaries=[
                {"socket_path": "/tmp/a.sock", "keep_alive": False},
                {"socket_path": "/tmp/b.sock", "keep_alive": False},
            ]
        ),
        raising=True,
    )
    with pytest.raises(_CtlFailure) as exc_info:
        _task._resolve_addable_server(None)
    assert exc_info.value.kind == "ambiguous"


def test_cli_task_add_defaults_to_sole_addable_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pathlib import Path

    from inspect_ai._cli.ctl import _fetch, _http, _task

    servers = [
        SimpleNamespace(pid=1, socket_path=Path("/tmp/a.sock")),
        SimpleNamespace(pid=2, socket_path=Path("/tmp/b.sock")),
    ]
    monkeypatch.setattr(_http, "list_discovered_servers", lambda: servers, raising=True)
    monkeypatch.setattr(
        _fetch,
        "_fetch_summaries",
        lambda s: SimpleNamespace(
            summaries=[
                {"socket_path": "/tmp/a.sock", "keep_alive": False},
                {"socket_path": "/tmp/b.sock", "keep_alive": True},
            ]
        ),
        raising=True,
    )
    assert _task._resolve_addable_server(None).pid == 2


# ---------------------------------------------------------------------------
# End-to-end: park restart
# ---------------------------------------------------------------------------


@skip_if_trio
async def test_task_add_park_restart_runs_added_task(tmp_path) -> None:
    """A task added over HTTP to a keep-parked eval runs under the same run_id.

    Runs a real `eval_async(..., ctl_server="keep")` as a background task,
    waits for the initial task to finish, POSTs an add over the eval's own
    control socket (a file spec, resolved in-process), waits for the added
    task to complete, then releases the park and checks the combined logs.
    """
    from inspect_ai._control.discovery import list_discovered_servers
    from inspect_ai._control.eval_state import get_eval_states
    from inspect_ai._eval.eval import eval_async

    task_file = tmp_path / "park_added_task.py"
    task_file.write_text(
        "from inspect_ai import Task, task\n"
        "from inspect_ai.dataset import Sample\n"
        "from inspect_ai.solver import generate\n"
        "\n"
        "@task\n"
        "def park_added():\n"
        '    return Task(dataset=[Sample(input="hi")], solver=[generate()],'
        ' name="park_added")\n'
    )

    parent = Task(
        dataset=[Sample(input="hi", target="ok")],
        solver=[generate()],
        name="park_parent",
    )

    logs: list[Any] = []

    async def run_eval() -> None:
        logs.extend(
            await eval_async(
                parent,
                model="mockllm/model",
                ctl_server="keep",
                log_dir=str(tmp_path / "logs"),
            )
        )

    async def wait_for(predicate: Any) -> None:
        with anyio.fail_after(30):
            while not predicate():
                await anyio.sleep(0.05)

    async with anyio.create_task_group() as tg:
        tg.start_soon(run_eval)

        def parent_finished() -> bool:
            return any(
                s.task == "park_parent" and s.completed_at is not None
                for s in get_eval_states()
            )

        await wait_for(parent_finished)

        # filter to this process: under pytest-xdist the discovery dir is
        # shared, so another worker's eval may be listed too
        servers = [s for s in list_discovered_servers() if s.pid == os.getpid()]
        assert len(servers) == 1
        transport = httpx.AsyncHTTPTransport(uds=str(servers[0].socket_path))
        async with httpx.AsyncClient(
            transport=transport, base_url="http://localhost", timeout=10.0
        ) as client:
            response = await client.post(
                "/tasks", json={"spec": f"{task_file}@park_added"}
            )
            assert response.status_code == 200, response.text
            rows = response.json()["tasks"]
            assert [r["task_name"] for r in rows] == ["park_added"]

            def added_finished() -> bool:
                return any(
                    s.task == "park_added" and s.completed_at is not None
                    for s in get_eval_states()
                )

            await wait_for(added_finished)

            release = await client.post("/release")
            assert release.status_code == 200

    # the added task's log is in the run's results, as a sibling of the
    # original (same run_id, own log)
    assert sorted(log.eval.task for log in logs) == ["park_added", "park_parent"]
    assert all(log.status == "success" for log in logs)
    assert len({log.eval.run_id for log in logs}) == 1


@skip_if_trio
async def test_task_add_park_session_is_source_free(tmp_path, monkeypatch) -> None:
    """A task added while parked never fires callbacks on the run's TaskSource.

    Park sessions run their batches with no task_source (``run_session`` is
    source-free): the added task's completion must not fire sample_complete /
    task_complete on the run's original source — a source whose run ended
    must not be resurrected by an unrelated add-while-parked whose callback
    returns follow-up tasks.
    """
    from inspect_ai import TaskSource
    from inspect_ai._control.discovery import list_discovered_servers
    from inspect_ai._control.eval_state import get_eval_states
    from inspect_ai._control.server import ControlServer
    from inspect_ai._eval.eval import eval_async

    task_file = tmp_path / "park_added_task.py"
    task_file.write_text(
        "from inspect_ai import Task, task\n"
        "from inspect_ai.dataset import Sample\n"
        "from inspect_ai.solver import generate\n"
        "\n"
        "@task\n"
        "def park_added():\n"
        '    return Task(dataset=[Sample(input="hi")], solver=[generate()],'
        ' name="park_added")\n'
    )

    parent = Task(
        dataset=[Sample(input="hi", target="ok")],
        solver=[generate()],
        name="park_parent",
    )

    completed: list[str] = []

    async def on_task_complete(log: Any) -> list[Task] | None:
        completed.append(log.eval.task)
        if log.eval.task == "park_added":
            # only reachable if a park session leaked the source: the
            # follow-up would resurrect it after its run ended
            return [
                Task(
                    dataset=[Sample(input="hi")],
                    solver=[generate()],
                    name="resurrected",
                )
            ]
        return None

    source = TaskSource.from_tasks([parent], task_complete=on_task_complete)

    # deterministic park signal: the park loop calls wait_for_release_or_add
    # only once the body is over and the add buffer is empty. An add POSTed
    # merely after the parent *completed* could still drain into the live
    # run — where source callbacks do fire, by design — making the test
    # race-dependent.
    parked = anyio.Event()
    orig_wait = ControlServer.wait_for_release_or_add

    async def wait_and_signal(self: Any, pending: Any) -> None:
        parked.set()
        await orig_wait(self, pending)

    monkeypatch.setattr(ControlServer, "wait_for_release_or_add", wait_and_signal)

    logs: list[Any] = []

    async def run_eval() -> None:
        logs.extend(
            await eval_async(
                source,
                model="mockllm/model",
                ctl_server="keep",
                log_dir=str(tmp_path / "logs"),
            )
        )

    async with anyio.create_task_group() as tg:
        tg.start_soon(run_eval)

        with anyio.fail_after(30):
            await parked.wait()

        servers = [s for s in list_discovered_servers() if s.pid == os.getpid()]
        assert len(servers) == 1
        transport = httpx.AsyncHTTPTransport(uds=str(servers[0].socket_path))
        async with httpx.AsyncClient(
            transport=transport, base_url="http://localhost", timeout=10.0
        ) as client:
            response = await client.post(
                "/tasks", json={"spec": f"{task_file}@park_added"}
            )
            assert response.status_code == 200, response.text

            def added_finished() -> bool:
                return any(
                    s.task == "park_added" and s.completed_at is not None
                    for s in get_eval_states()
                )

            with anyio.fail_after(30):
                while not added_finished():
                    await anyio.sleep(0.05)

            release = await client.post("/release")
            assert release.status_code == 200

    # the source saw only its own run's task: the added task completed
    # without firing task_complete, so no "resurrected" task ever ran
    assert completed == ["park_parent"]
    assert sorted(log.eval.task for log in logs) == ["park_added", "park_parent"]
    assert all(log.status == "success" for log in logs)


async def _run_add_while_source_blocked(tmp_path: Any, max_tasks: int) -> None:
    """An add dispatched while the source poll blocks must not be starved.

    Runs a real `eval_async(..., ctl_server="keep")` whose TaskSource's
    `next_tasks()` parks on an event, waits until the poll is genuinely in
    flight (the run fully idle, `next_tasks()` entered), POSTs an add over
    the eval's control socket, and requires the added task to complete while
    the poll is still blocked — pre-race an accepted add sat in the buffer
    until `next_tasks()` returned (which here never would), so this wait
    hung. `max_tasks` selects the poll shape under test: the dispatcher's
    `feed.next()` race (`parallel > 1`) or `run_batches`' follow-up-batch
    race (`parallel == 1`).
    """
    from inspect_ai import TaskSource
    from inspect_ai._control.discovery import list_discovered_servers
    from inspect_ai._control.eval_state import get_eval_states
    from inspect_ai._eval.eval import eval_async

    task_file = tmp_path / "blocked_poll_added.py"
    task_file.write_text(
        "from inspect_ai import Task, task\n"
        "from inspect_ai.dataset import Sample\n"
        "from inspect_ai.solver import generate\n"
        "\n"
        "@task\n"
        "def blocked_poll_added():\n"
        '    return Task(dataset=[Sample(input="hi")], solver=[generate()],'
        ' name="blocked_poll_added")\n'
    )

    poll_entered = anyio.Event()
    source_release = anyio.Event()

    class _BlockedSource(TaskSource):
        def initial_tasks(self) -> list[Task]:
            return [
                Task(dataset=[Sample(input="hi")], solver=[generate()], name="seed")
            ]

        async def next_tasks(self) -> list[Task] | None:
            poll_entered.set()
            with anyio.fail_after(30):
                await source_release.wait()
            return None

    logs: list[Any] = []

    async def run_eval() -> None:
        logs.extend(
            await eval_async(
                _BlockedSource(),
                model="mockllm/model",
                max_tasks=max_tasks,
                ctl_server="keep",
                log_dir=str(tmp_path / "logs"),
            )
        )

    async with anyio.create_task_group() as tg:
        tg.start_soon(run_eval)

        with anyio.fail_after(30):
            await poll_entered.wait()

        servers = [s for s in list_discovered_servers() if s.pid == os.getpid()]
        assert len(servers) == 1
        transport = httpx.AsyncHTTPTransport(uds=str(servers[0].socket_path))
        async with httpx.AsyncClient(
            transport=transport, base_url="http://localhost", timeout=10.0
        ) as client:
            response = await client.post(
                "/tasks", json={"spec": f"{task_file}@blocked_poll_added"}
            )
            assert response.status_code == 200, response.text

            def added_finished() -> bool:
                return any(
                    s.task == "blocked_poll_added" and s.completed_at is not None
                    for s in get_eval_states()
                )

            with anyio.fail_after(30):
                while not added_finished():
                    await anyio.sleep(0.05)
            # the added task ran to completion with the poll still blocked
            assert not source_release.is_set()

            # exhaust the source (the blocked poll returns None), then
            # release the keep-alive park
            source_release.set()
            release = await client.post("/release")
            assert release.status_code == 200

    assert sorted(log.eval.task for log in logs) == ["blocked_poll_added", "seed"]
    assert all(log.status == "success" for log in logs)
    assert len({log.eval.run_id for log in logs}) == 1


@skip_if_trio
async def test_task_add_runs_while_source_poll_blocked_parallel(tmp_path) -> None:
    """Dispatcher shape (`parallel > 1`): the add dispatches on free capacity."""
    await _run_add_while_source_blocked(tmp_path, max_tasks=2)


@skip_if_trio
async def test_task_add_runs_while_source_poll_blocked_serial(tmp_path) -> None:
    """`run_batches` shape (`parallel == 1`): the add runs as a follow-up batch."""
    await _run_add_while_source_blocked(tmp_path, max_tasks=1)
