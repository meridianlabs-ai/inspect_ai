"""Unit tests for `sandbox_agent_bridge` startup against fake sandboxes."""

from pathlib import Path
from typing import Any

import anyio
import pytest

from inspect_ai.agent import sandbox_agent_bridge
from inspect_ai.agent._bridge.sandbox import bridge as bridge_module
from inspect_ai.tool._sandbox_tools_utils import sandbox as sandbox_tools
from inspect_ai.util._sandbox._cli import SANDBOX_TOOLS_BASE_NAME
from inspect_ai.util._sandbox.exec_remote import ExecCompleted, ExecOutput
from inspect_ai.util._sandbox.local import LocalSandboxEnvironment


class FakeProxyProcess:
    """Stands in for the `model_proxy` process: runs until killed, then exits cleanly."""

    def __init__(self) -> None:
        self._killed = anyio.Event()
        self._completed = False

    def __aiter__(self) -> "FakeProxyProcess":
        return self

    async def __anext__(self) -> ExecOutput:
        if self._completed:
            raise StopAsyncIteration
        await self._killed.wait()
        self._completed = True
        return ExecCompleted(exit_code=0)

    async def kill(self) -> None:
        self._killed.set()


async def test_sandbox_bridge_starts_proxy_with_local_launcher(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tools_dir = tmp_path / "tools"
    monkeypatch.setattr(
        sandbox_tools, "local_sandbox_tools_dir", lambda: str(tools_dir)
    )
    sandbox = LocalSandboxEnvironment()
    remote_commands: list[list[str]] = []

    async def fake_exec_remote(cmd: list[str], **_kwargs: Any) -> FakeProxyProcess:
        remote_commands.append(cmd)
        return FakeProxyProcess()

    async def fake_injected_tools(**_kwargs: Any) -> LocalSandboxEnvironment:
        return sandbox

    async def fake_run_model_service(*args: Any) -> None:
        started: anyio.Event = args[-1]
        started.set()

    monkeypatch.setattr(sandbox, "exec_remote", fake_exec_remote)
    monkeypatch.setattr(
        bridge_module, "sandbox_with_injected_tools", fake_injected_tools
    )
    monkeypatch.setattr(bridge_module, "run_model_service", fake_run_model_service)

    try:
        async with sandbox_agent_bridge():
            pass
    finally:
        sandbox.directory.cleanup()

    # the server spawns this command itself, so it must name the host-local launcher
    assert remote_commands == [
        [str(tools_dir / SANDBOX_TOOLS_BASE_NAME), "model_proxy"]
    ]
