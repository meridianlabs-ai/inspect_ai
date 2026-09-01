import concurrent.futures
import re
import subprocess
import sys
import time
from argparse import Namespace
from dataclasses import dataclass, field
from io import StringIO
from pathlib import Path
from typing import Any, cast

import pytest
from test_helpers.utils import skip_if_no_docker

from inspect_ai import Task, eval
from inspect_ai.agent._human import install as human_install
from inspect_ai.agent._human.agent import human_cli
from inspect_ai.agent._human.commands import submit
from inspect_ai.agent._human.commands.submit import QuitCommand, SubmitCommand
from inspect_ai.util import SandboxEnvironment
from inspect_ai.util._subprocess import ExecResult


@dataclass
class _InstallSandbox:
    calls: list[tuple[list[str], str | None]] = field(default_factory=list)
    human_directory_created: bool = False

    async def exec(
        self, cmd: list[str], *, user: str | None = None, **kwargs: Any
    ) -> ExecResult[str]:
        self.calls.append((cmd, user))
        if cmd[:2] == ["sh", "-c"] and human_install.HUMAN_AGENT_DIR in cmd[2]:
            if not self.human_directory_created:
                self.human_directory_created = True
                return ExecResult(True, 0, "created\n", "")
            if user == "root":
                return ExecResult(False, 1, "", "")
            return ExecResult(True, 0, "existing\n", "")
        return ExecResult(True, 0, "", "")


async def test_human_agent_reuses_directory_owned_by_selected_user(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _InstallSandbox()
    monkeypatch.setattr(
        human_install, "sandbox", lambda: cast(SandboxEnvironment, fake)
    )

    await human_install.install_human_agent("agent", [], None, False)
    await human_install.install_human_agent("agent", [], None, False)

    directory_checks = [
        user
        for cmd, user in fake.calls
        if cmd[:2] == ["sh", "-c"] and human_install.HUMAN_AGENT_DIR in cmd[2]
    ]
    assert directory_checks == ["root", "root", "agent"]
    assert (["bash", f"./{human_install.INSTALL_SH}"], "agent") in fake.calls
    assert (["rm", "-rf", human_install.INSTALL_DIR], "root") in fake.calls


async def test_session_logs_are_read_as_selected_user(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    @dataclass
    class SessionSandbox:
        calls: list[tuple[list[str], str | None]] = field(default_factory=list)

        async def exec(
            self, cmd: list[str], *, user: str | None = None, **kwargs: Any
        ) -> ExecResult[str]:
            self.calls.append((cmd, user))
            if cmd[0] == "ls":
                return ExecResult(True, 0, "agent_session.output\n", "")
            return ExecResult(True, 0, "session contents", "")

    fake = SessionSandbox()
    monkeypatch.setattr(submit, "sandbox", lambda: cast(SandboxEnvironment, fake))

    logs = await SubmitCommand(True, "agent")._read_session_logs()

    assert logs == {"agent_session.output": "session contents"}
    assert [user for _, user in fake.calls] == ["agent", "agent"]
    assert fake.calls[1][0][-2:] == [
        f"{human_install.RECORD_SESSION_DIR}/agent_session.output",
        str(100 * 1024**2),
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
