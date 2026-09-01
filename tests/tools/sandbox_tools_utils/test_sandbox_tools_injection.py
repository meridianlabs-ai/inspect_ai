"""Tests for sandbox tools injection."""

from contextlib import asynccontextmanager
from io import BytesIO
from typing import AsyncIterator, BinaryIO, Literal, overload

import pytest

from inspect_ai.tool._sandbox_tools_utils import sandbox as sandbox_tools
from inspect_ai.util._sandbox.environment import (
    SandboxEnvironment,
    SandboxEnvironmentConfigType,
)
from inspect_ai.util._sandbox.recon import Architecture, SupportedContainerOSInfo
from inspect_ai.util._subprocess import ExecResult


class RootProbeRaisesSandbox(SandboxEnvironment):
    def __init__(self) -> None:
        super().__init__()
        self.exec_calls: list[tuple[list[str], str | None]] = []
        self.extracted_as_user: str | None | object = object()

    async def exec(
        self,
        cmd: list[str],
        input: str | bytes | None = None,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        user: str | None = None,
        timeout: int | None = None,
        timeout_retry: bool = True,
        concurrency: bool = True,
    ) -> ExecResult[str]:
        self.exec_calls.append((cmd, user))
        if cmd == ["id", "-u"] and user == "root":
            raise RuntimeError("runuser: may not be used by non-root users")
        return ExecResult(success=True, returncode=0, stdout="", stderr="")

    async def write_file(self, file: str, contents: str | bytes) -> None:
        pass

    @overload
    async def read_file(self, file: str, text: Literal[True] = True) -> str: ...

    @overload
    async def read_file(self, file: str, text: Literal[False]) -> bytes: ...

    async def read_file(self, file: str, text: bool = True) -> str | bytes:
        raise NotImplementedError

    @classmethod
    async def sample_cleanup(
        cls,
        task_name: str,
        config: SandboxEnvironmentConfigType | None,
        environments: dict[str, SandboxEnvironment],
        interrupted: bool,
    ) -> None:
        pass


async def test_inject_container_tools_falls_back_when_root_probe_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sandbox = RootProbeRaisesSandbox()

    async def fake_detect_sandbox_os(
        _sandbox: SandboxEnvironment,
    ) -> SupportedContainerOSInfo:
        return {"architecture": "amd64", "libc": "glibc"}

    @asynccontextmanager
    async def fake_open_executable_for_arch(
        _arch: Architecture,
        _musl: bool,
    ) -> AsyncIterator[tuple[str, BinaryIO]]:
        yield "inspect-sandbox-tools", BytesIO(b"binary")

    async def fake_extract_tools_tree(
        _sandbox: SandboxEnvironment,
        _name: str,
        _gz_bytes: bytes,
        user: str | None,
    ) -> None:
        sandbox.extracted_as_user = user

    monkeypatch.setattr(sandbox_tools, "detect_sandbox_os", fake_detect_sandbox_os)
    monkeypatch.setattr(
        sandbox_tools, "_open_executable_for_arch", fake_open_executable_for_arch
    )
    monkeypatch.setattr(sandbox_tools, "_extract_tools_tree", fake_extract_tools_tree)

    await sandbox_tools._inject_container_tools_code(sandbox)

    assert sandbox._tools_user is None
    assert sandbox.extracted_as_user is None
    assert (["id", "-u"], "root") in sandbox.exec_calls
    assert (sandbox_tools._ensure_tools_dir_command(), None) in sandbox.exec_calls
    assert (
        ["find", sandbox_tools.SANDBOX_TOOLS_DIR, "-mindepth", "1", "-delete"],
        None,
    ) in sandbox.exec_calls
    assert (
        ["rm", "-rf", "--", sandbox_tools._SANDBOX_TOOLS_CHUNK_DIR],
        None,
    ) in sandbox.exec_calls
    generation_calls = [
        cmd
        for cmd, user in sandbox.exec_calls
        if user is None and sandbox_tools._SANDBOX_TOOLS_GENERATION_FILE in " ".join(cmd)
    ]
    assert generation_calls
    assert ([sandbox_tools.SANDBOX_CLI, "stop-server"], None) in sandbox.exec_calls
    stop_index = sandbox.exec_calls.index(
        ([sandbox_tools.SANDBOX_CLI, "stop-server"], None)
    )
    ensure_index = sandbox.exec_calls.index(
        (sandbox_tools._ensure_tools_dir_command(), None)
    )
    assert stop_index < ensure_index


async def test_tools_directory_reuse_check_does_not_repair_permissions() -> None:
    sandbox = RootProbeRaisesSandbox()

    assert await sandbox_tools._tools_dir_is_verified(sandbox, None)
    command = sandbox.exec_calls[-1][0]
    assert "chmod" not in command[2]


async def test_tools_reuse_checks_launcher_trust_and_generation() -> None:
    sandbox = RootProbeRaisesSandbox()

    assert await sandbox_tools._tools_install_is_current(sandbox, "root")
    launcher_command = sandbox.exec_calls[-2][0][2]
    generation_command = sandbox.exec_calls[-1][0][2]
    assert "test ! -L" in launcher_command
    assert "stat -c %u" in launcher_command
    assert "& 022" in launcher_command
    assert sandbox_tools._get_sandbox_tools_version() in generation_command


async def test_existing_server_is_stopped_only_through_trusted_launcher() -> None:
    sandbox = RootProbeRaisesSandbox()

    await sandbox_tools._stop_trusted_existing_server(sandbox, "root")

    assert sandbox.exec_calls[-1] == (
        [sandbox_tools.SANDBOX_CLI, "stop-server"],
        "root",
    )


async def test_legacy_server_without_stop_command_uses_verified_pid_fallback() -> None:
    class LegacySandbox(RootProbeRaisesSandbox):
        async def exec(
            self,
            cmd: list[str],
            input: str | bytes | None = None,
            cwd: str | None = None,
            env: dict[str, str] | None = None,
            user: str | None = None,
            timeout: int | None = None,
            timeout_retry: bool = True,
            concurrency: bool = True,
        ) -> ExecResult[str]:
            self.exec_calls.append((cmd, user))
            if cmd == [sandbox_tools.SANDBOX_CLI, "stop-server"]:
                return ExecResult(
                    success=False,
                    returncode=2,
                    stdout="",
                    stderr="invalid choice: 'stop-server'",
                )
            return ExecResult(success=True, returncode=0, stdout="", stderr="")

    sandbox = LegacySandbox()

    await sandbox_tools._stop_trusted_existing_server(sandbox, None)

    assert any(
        cmd[:2] == ["sh", "-c"]
        and sandbox_tools.SANDBOX_CLI in cmd
        and "python" not in cmd[1]
        for cmd, _ in sandbox.exec_calls
    )


async def test_legacy_directory_trust_allows_safe_non_private_mode() -> None:
    sandbox = RootProbeRaisesSandbox()

    assert await sandbox_tools._legacy_tools_dir_is_trusted(sandbox, None)
    command = sandbox.exec_calls[-1][0][2]
    assert "& 022" in command
    assert "0700" not in command
