import base64
import concurrent.futures
import re
import subprocess
import sys
import time
from argparse import Namespace
from io import StringIO
from pathlib import Path

import pytest
from test_helpers.utils import skip_if_no_docker

from inspect_ai import Task, eval
from inspect_ai.agent._human import install
from inspect_ai.agent._human.agent import human_cli
from inspect_ai.agent._human.commands import submit
from inspect_ai.agent._human.commands.submit import QuitCommand, SubmitCommand
from inspect_ai.util._subprocess import ExecResult


async def test_install_human_agent_repairs_legacy_root_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A safe legacy root installation is repaired and reused."""
    calls: list[tuple[list[str], str | None]] = []

    class FakeSandbox:
        async def exec(
            self, cmd: list[str], *, user: str | None = None, **kwargs: object
        ) -> ExecResult[str]:
            calls.append((cmd, user))
            return ExecResult(True, 0, "existing\n", "")

    monkeypatch.setattr(install, "sandbox", lambda: FakeSandbox())

    await install.install_human_agent("root", [], None, False)

    assert len(calls) == 1
    command, user = calls[0]
    assert user == "root"
    assert "chmod 700" in command[2]


async def test_install_human_agent_falls_back_when_root_exec_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A rootless provider may raise while probing the root installation."""
    calls: list[str | None] = []

    class FakeSandbox:
        async def exec(
            self, cmd: list[str], *, user: str | None = None, **kwargs: object
        ) -> ExecResult[str]:
            calls.append(user)
            if user == "root":
                raise PermissionError("root execution unavailable")
            return ExecResult(True, 0, "existing\n", "")

    monkeypatch.setattr(install, "sandbox", lambda: FakeSandbox())

    await install.install_human_agent("agent", [], None, False)

    assert calls == ["root", "agent"]


async def test_session_logs_are_read_as_configured_user(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Private recordings are listed and read through their owning identity."""
    calls: list[tuple[list[str], str | None]] = []

    class FakeSandbox:
        read_calls = 0

        async def exec(
            self, cmd: list[str], *, user: str | None = None, **kwargs: object
        ) -> ExecResult[str]:
            calls.append((cmd, user))
            if cmd[0] == "ls":
                stdout = "session.log\n"
            elif self.read_calls == 0:
                stdout = base64.b64encode(b"recording").decode()
                self.read_calls += 1
            else:
                stdout = ""
            return ExecResult(True, 0, stdout, "")

    monkeypatch.setattr(submit, "sandbox", lambda: FakeSandbox())

    logs = await SubmitCommand(True, "alice")._read_session_logs()

    assert logs == {"session.log": "recording"}
    assert [user for _, user in calls] == ["alice", "alice"]
    assert [cmd[0] for cmd, _ in calls] == ["ls", "sh"]


async def test_checked_write_file_runs_as_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Staged files are written and made executable without privilege changes."""
    calls: list[tuple[list[str], str | None]] = []

    async def fake_checked_exec(
        cmd: list[str],
        input: str | bytes | None = None,
        cwd: str | None = None,
        user: str | None = None,
    ) -> str:
        calls.append((cmd, user))
        return ""

    monkeypatch.setattr(install, "checked_exec", fake_checked_exec)

    await install.checked_write_file("/tmp/staged", "contents", True, "agent")

    assert calls == [
        (["tee", "--", "/tmp/staged"], "agent"),
        (["chmod", "+x", "/tmp/staged"], "agent"),
    ]


@pytest.mark.parametrize(
    ("command", "args", "expected_calls"),
    [
        (QuitCommand(False), Namespace(), []),
        (
            SubmitCommand(False),
            Namespace(answer=None),
            [("validate", {"answer": None})],
        ),
    ],
)
def test_session_end_commands_decline_on_eof(
    command: QuitCommand | SubmitCommand,
    args: Namespace,
    expected_calls: list[tuple[str, dict[str, object]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, dict[str, object]]] = []

    def call_human_agent(method: str, **params: object) -> None:
        calls.append((method, params))

    monkeypatch.setattr(submit, "call_human_agent", call_human_agent)
    monkeypatch.setattr(sys, "stdin", StringIO())

    command.cli(args)

    assert calls == expected_calls


@pytest.mark.slow
@skip_if_no_docker
@pytest.mark.parametrize("user", ["root", "nonroot", None])
def test_human_cli(capsys: pytest.CaptureFixture[str], user: str | None):
    def run_eval():
        task = Task(
            solver=human_cli(user=user),
            sandbox=(
                "docker",
                (Path(__file__).parent / "compose.human.yaml").as_posix(),
            ),
        )
        return eval(task, display="plain")[0]

    with concurrent.futures.ThreadPoolExecutor() as executor:
        future = executor.submit(run_eval)

        out = ""
        container_name = None
        for _ in range(10):
            out += capsys.readouterr().out
            if match := re.search(r"inspect-task-\S+-default-1", out):
                container_name = match.group(0)
                break
            time.sleep(1)

        if not container_name:
            raise Exception("Failed to find container name")

        docker_exec = [
            "docker",
            "exec",
            *(["-u", user] if user else []),
            container_name,
            "bash",
            "-l",
            "-c",
        ]

        human_agent_found = False
        for _ in range(10):
            result = subprocess.run(
                docker_exec
                + ["ls /var/tmp/sandbox-services/human_agent/human_agent.py"]
            )
            if result.returncode == 0:
                human_agent_found = True
                break
            time.sleep(1)

        if not human_agent_found:
            raise Exception("Human agent sandbox service not found")

        subprocess.check_call(docker_exec + ["python3 /opt/human_agent/task.py start"])
        subprocess.check_call(
            docker_exec
            + [
                'echo -e "y\\n" | python3 /opt/human_agent/task.py submit "test"',
            ],
        )

        done, _ = concurrent.futures.wait([future], timeout=20)
        if future in done:
            log = future.result()
            assert log.status == "success"
            assert log.samples[0].output.choices[0].message.content == "test"
        else:
            raise Exception("eval() did not complete within timeout")


@pytest.mark.slow
@skip_if_no_docker
def test_human_cli_submit_no_answer(capsys: pytest.CaptureFixture[str]):
    """Test that submitting without an answer completes the task when answer=False."""

    def run_eval():
        task = Task(
            solver=human_cli(answer=False),
            sandbox=(
                "docker",
                (Path(__file__).parent / "compose.human.yaml").as_posix(),
            ),
        )
        return eval(task, display="plain")[0]

    with concurrent.futures.ThreadPoolExecutor() as executor:
        future = executor.submit(run_eval)

        out = ""
        container_name = None
        for _ in range(10):
            out += capsys.readouterr().out
            if match := re.search(r"inspect-task-\S+-default-1", out):
                container_name = match.group(0)
                break
            time.sleep(1)

        if not container_name:
            raise Exception("Failed to find container name")

        docker_exec = [
            "docker",
            "exec",
            container_name,
            "bash",
            "-l",
            "-c",
        ]

        human_agent_found = False
        for _ in range(10):
            result = subprocess.run(
                docker_exec
                + ["ls /var/tmp/sandbox-services/human_agent/human_agent.py"]
            )
            if result.returncode == 0:
                human_agent_found = True
                break
            time.sleep(1)

        if not human_agent_found:
            raise Exception("Human agent sandbox service not found")

        subprocess.check_call(docker_exec + ["python3 /opt/human_agent/task.py start"])
        # Submit without an answer - this should complete the task when answer=False
        subprocess.check_call(
            docker_exec
            + [
                'echo -e "y\\n" | python3 /opt/human_agent/task.py submit',
            ],
        )

        done, _ = concurrent.futures.wait([future], timeout=5)
        if future in done:
            log = future.result()
            assert log.status == "success"
            assert log.samples[0].output.choices[0].message.content == ""
        else:
            raise Exception("eval() did not complete within timeout")
