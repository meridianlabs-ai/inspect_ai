"""Tests for sandbox tools injection."""

import stat
import subprocess
from contextlib import asynccontextmanager
from io import BytesIO
from pathlib import Path
from typing import AsyncIterator, BinaryIO, Literal, overload

import pytest

from inspect_ai.tool._sandbox_tools_utils import sandbox as sandbox_tools
from inspect_ai.util._sandbox.environment import (
    SandboxEnvironment,
    SandboxEnvironmentConfigType,
)
from inspect_ai.util._sandbox.recon import Architecture, SupportedContainerOSInfo
from inspect_ai.util._subprocess import ExecResult


def test_tools_directory_repairs_legacy_private_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tools_dir = tmp_path / "sandbox-tools"
    tools_dir.mkdir(mode=0o755)
    monkeypatch.setattr(sandbox_tools, "SANDBOX_TOOLS_DIR", str(tools_dir))

    subprocess.run(sandbox_tools._ensure_tools_dir_command(), check=True)

    assert stat.S_IMODE(tools_dir.stat().st_mode) == 0o700


def test_tools_directory_rejects_legacy_writable_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tools_dir = tmp_path / "sandbox-tools"
    tools_dir.mkdir(mode=0o777)
    tools_dir.chmod(0o777)
    monkeypatch.setattr(sandbox_tools, "SANDBOX_TOOLS_DIR", str(tools_dir))

    result = subprocess.run(sandbox_tools._ensure_tools_dir_command(), check=False)

    assert result.returncode != 0


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
