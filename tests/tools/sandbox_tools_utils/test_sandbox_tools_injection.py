"""Tests for sandbox tools injection."""

import os
import shutil
import subprocess
from contextlib import asynccontextmanager
from io import BytesIO
from typing import AsyncIterator, BinaryIO, Literal, overload

import anyio
import pytest

from inspect_ai.tool._sandbox_tools_utils import sandbox as sandbox_tools
from inspect_ai.util._sandbox._cli import SANDBOX_CLI
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
        self.writes: list[tuple[str, str | bytes]] = []
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
        if cmd[:2] == ["sh", "-c"] and 'test -e "$1"' in cmd[2]:
            return ExecResult(success=False, returncode=1, stdout="", stderr="")
        if cmd[:2] == ["sh", "-c"] and 'test -d "$1"' in cmd[2]:
            return ExecResult(success=False, returncode=1, stdout="", stderr="")
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
        self.writes.append((file, contents))

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
    sandbox.validation_results = [True, True]

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
        _tools_dir: str,
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
    start_index = sandbox.exec_calls.index(([SANDBOX_CLI, "start-server"], None))
    validation_index = next(
        index
        for index, (command, _) in enumerate(sandbox.exec_calls)
        if command[:2] == ["sh", "-c"] and "validate_file()" in command[2]
    )
    assert validation_index < start_index


async def test_injection_accepts_trusted_installation_appearing_late(
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

    detector_results = iter((False, True))

    async def fake_detector(_sandbox: SandboxEnvironment) -> bool:
        return next(detector_results)

    async def tools_dir_exists(_sandbox: SandboxEnvironment) -> bool:
        return True

    monkeypatch.setattr(sandbox_tools, "detect_sandbox_os", fake_detect_sandbox_os)
    monkeypatch.setattr(
        sandbox_tools, "_open_executable_for_arch", fake_open_executable_for_arch
    )
    monkeypatch.setattr(sandbox_tools, "_sandbox_tools_detector", fake_detector)
    monkeypatch.setattr(sandbox_tools, "_tools_dir_exists", tools_dir_exists)

    await sandbox_tools._inject_container_tools_code(sandbox)

    assert all(command != ["id", "-u"] for command, _ in sandbox.exec_calls)


def test_publish_command_rejects_destination_created_during_move(
    tmp_path: os.PathLike[str],
) -> None:
    base = os.fspath(tmp_path)
    staging = os.path.join(base, "staging")
    destination = os.path.join(base, "tools")
    bin_dir = os.path.join(base, "bin")
    os.mkdir(staging)
    os.mkdir(bin_dir)
    real_mv = shutil.which("mv")
    assert real_mv is not None
    mv_wrapper = os.path.join(bin_dir, "mv")
    with open(mv_wrapper, "w") as wrapper:
        wrapper.write(f'#!/bin/sh\nmkdir -- "$3"\nexec "{real_mv}" "$@"\n')
    os.chmod(mv_wrapper, 0o700)
    command = sandbox_tools._publish_tools_command(staging)
    command[5] = os.path.join(base, "lock")
    command[6] = destination

    result = subprocess.run(
        command,
        check=False,
        env={**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"},
    )

    assert result.returncode == 17


async def test_write_archive_uses_provider_file_transfer() -> None:
    sandbox = RootProbeRaisesSandbox()
    archive_path = "/var/tmp/.inspect-sandbox-tools-test.tgz"

    await sandbox_tools._write_archive(sandbox, archive_path, b"archive")

    assert sandbox.exec_calls == []
    assert sandbox.writes == [(archive_path, b"archive")]


async def test_secure_archive_copies_and_verifies_as_install_user() -> None:
    sandbox = RootProbeRaisesSandbox()

    await sandbox_tools._secure_archive(
        sandbox, "/var/tmp/upload", "/protected/archive", b"archive", "root"
    )

    command, user = sandbox.exec_calls[-1]
    assert user == "root"
    assert command[:2] == ["sh", "-c"]
    assert "sha256sum -c" in command[2]
    assert command[4:6] == ["/var/tmp/upload", "/protected/archive"]


async def test_extract_cancellation_removes_uploaded_archive() -> None:
    class CancelledUploadSandbox(RootProbeRaisesSandbox):
        def __init__(self) -> None:
            super().__init__()
            self.write_started = anyio.Event()

        async def write_file(self, file: str, contents: str | bytes) -> None:
            self.writes.append((file, contents))
            self.write_started.set()
            await anyio.sleep_forever()

    sandbox = CancelledUploadSandbox()

    async with anyio.create_task_group() as task_group:
        task_group.start_soon(
            sandbox_tools._extract_tools_tree,
            sandbox,
            "tools",
            b"archive",
            "root",
            "/protected/install",
        )
        await sandbox.write_started.wait()
        task_group.cancel_scope.cancel()

    cleanup_calls = [
        command
        for command, user in sandbox.exec_calls
        if command[:3] == ["rm", "-f", "--"] and user == "root"
    ]
    assert len(cleanup_calls) == 1
    assert cleanup_calls[0][-1] == "/protected/install/archive.tgz"


async def test_injection_staging_is_concurrent_across_sandbox_instances(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    installed = False
    active_installers = 0
    maximum_active_installers = 0

    async def detector(_sandbox: SandboxEnvironment) -> bool:
        return installed

    async def injector(_sandbox: SandboxEnvironment) -> None:
        nonlocal active_installers, installed, maximum_active_installers
        active_installers += 1
        maximum_active_installers = max(maximum_active_installers, active_installers)
        await anyio.sleep(0)
        installed = True
        active_installers -= 1

    monkeypatch.setattr(sandbox_tools, "_sandbox_tools_detector", detector)
    monkeypatch.setattr(sandbox_tools, "_inject_container_tools_code_impl", injector)

    async with anyio.create_task_group() as task_group:
        task_group.start_soon(
            sandbox_tools._inject_container_tools_code, RootProbeRaisesSandbox()
        )
        task_group.start_soon(
            sandbox_tools._inject_container_tools_code, RootProbeRaisesSandbox()
        )

    assert maximum_active_installers == 2


def test_launcher_validator_checks_filesystem_state(tmp_path: os.PathLike[str]) -> None:
    tools_dir = os.fspath(tmp_path)
    launcher = os.path.join(tools_dir, "launcher")
    with open(launcher, "w") as file:
        file.write("#!/bin/sh\n")
    os.chmod(launcher, 0o700)
    command = sandbox_tools._sandbox_cli_validation_command()
    command[4] = launcher

    assert subprocess.run(command, check=False).returncode == 0

    os.chmod(launcher, 0o722)
    assert subprocess.run(command, check=False).returncode != 0
    os.chmod(launcher, 0o700)

    os.unlink(launcher)
    os.symlink("missing", launcher)
    assert subprocess.run(command, check=False).returncode != 0
