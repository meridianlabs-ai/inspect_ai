"""Tests for sandbox tools injection."""

import base64
import os
import subprocess
from contextlib import asynccontextmanager
from io import BytesIO
from typing import AsyncIterator, BinaryIO, Literal, overload

import pytest

from inspect_ai.tool._sandbox_tools_utils import sandbox as sandbox_tools
from inspect_ai.util._sandbox._cli import SANDBOX_CLI, SANDBOX_TOOLS_DIR
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
        self.exec_inputs: list[str | bytes | None] = []
        self.extracted_as_user: str | None | object = object()
        self.validation_results: list[bool] = []

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
        self.exec_inputs.append(input)
        if cmd == ["id", "-u"] and user == "root":
            raise RuntimeError("runuser: may not be used by non-root users")
        if cmd[:2] == ["sh", "-c"] and "validate_file()" in cmd[2]:
            success = (
                self.validation_results.pop(0) if self.validation_results else False
            )
            return ExecResult(
                success=success,
                returncode=0 if success else 1,
                stdout="",
                stderr="",
            )
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
    sandbox.validation_results = [False, True]

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
    stop_index = sandbox.exec_calls.index(([SANDBOX_CLI, "stop-server"], None))
    clear_index = next(
        index
        for index, (command, _) in enumerate(sandbox.exec_calls)
        if command[:2] == ["sh", "-c"] and "-mindepth" in command[2]
    )
    start_index = sandbox.exec_calls.index(([SANDBOX_CLI, "start-server"], None))
    release_index = next(
        index
        for index, (command, _) in enumerate(sandbox.exec_calls)
        if command[:2] == ["sh", "-c"] and 'cat "$1/token"' in command[2]
    )
    assert stop_index < clear_index < start_index < release_index


async def test_write_archive_stages_inside_tools_dir_as_extraction_user() -> None:
    sandbox = RootProbeRaisesSandbox()
    archive_path = f"{SANDBOX_TOOLS_DIR}/.pkg.tgz"

    await sandbox_tools._write_archive(sandbox, archive_path, b"archive", "root")

    assert sandbox.exec_calls[-1] == (
        ["sh", "-c", 'base64 -d > "$1"', "sh", archive_path],
        "root",
    )
    assert base64.b64decode(str(sandbox.exec_inputs[-1])) == b"archive"


def test_launcher_validator_checks_filesystem_state(tmp_path: os.PathLike[str]) -> None:
    tools_dir = os.fspath(tmp_path)
    launcher = os.path.join(tools_dir, "launcher")
    marker = os.path.join(tools_dir, "generation")
    identity = "artifact sha256:1234"
    with open(launcher, "w") as file:
        file.write("#!/bin/sh\n")
    with open(marker, "w") as file:
        file.write(f"{identity}\n")
    os.chmod(launcher, 0o700)
    os.chmod(marker, 0o600)

    command = sandbox_tools._sandbox_cli_validation_command(identity)
    command[4:6] = [launcher, marker]

    assert subprocess.run(command, check=False).returncode == 0

    os.chmod(launcher, 0o722)
    assert subprocess.run(command, check=False).returncode != 0
    os.chmod(launcher, 0o700)

    with open(marker, "w") as file:
        file.write("different artifact\n")
    assert subprocess.run(command, check=False).returncode != 0

    os.unlink(launcher)
    os.symlink("generation", launcher)
    assert subprocess.run(command, check=False).returncode != 0


def test_artifact_identity_includes_name_and_content_digest() -> None:
    identity = sandbox_tools._artifact_identity("inspect-sandbox-tools-v1-dev", b"a")

    assert identity.startswith("inspect-sandbox-tools-v1-dev sha256:")
    assert identity != sandbox_tools._artifact_identity(
        "inspect-sandbox-tools-v1-dev", b"b"
    )
