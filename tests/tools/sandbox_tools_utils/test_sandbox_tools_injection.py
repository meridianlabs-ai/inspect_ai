"""Tests for sandbox tools injection."""

import os
import shutil
import signal
import subprocess
import time
import warnings
from contextlib import asynccontextmanager
from io import BytesIO
from pathlib import Path
from typing import AsyncIterator, BinaryIO, Literal, overload

import anyio
import pytest

from inspect_ai.tool._sandbox_tools_utils import sandbox as sandbox_tools
from inspect_ai.util._sandbox import _cli as sandbox_cli
from inspect_ai.util._sandbox._cli import SANDBOX_CLI
from inspect_ai.util._sandbox.environment import (
    SandboxEnvironment,
    SandboxEnvironmentConfigType,
    SandboxUnavailableError,
)
from inspect_ai.util._sandbox.events import SandboxEnvironmentProxy
from inspect_ai.util._sandbox.local import LocalSandboxEnvironment
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
        self.tools_dir_exists = False

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
        if user == "root" and (
            cmd == ["id", "-u"]
            or (cmd[:2] == ["sh", "-c"] and "validate_owned()" in cmd[2])
        ):
            raise RuntimeError("runuser: may not be used by non-root users")
        if cmd[:2] == ["sh", "-c"] and 'test -e "$1"' in cmd[2]:
            return ExecResult(
                success=self.tools_dir_exists,
                returncode=0 if self.tools_dir_exists else 1,
                stdout="",
                stderr="",
            )
        if cmd[:2] == ["sh", "-c"] and "source_id=$(stat" in cmd[2]:
            self.tools_dir_exists = True
            return ExecResult(success=True, returncode=0, stdout="", stderr="")
        if cmd[:2] == ["sh", "-c"] and 'test -d "$1"' in cmd[2]:
            return ExecResult(
                success=self.tools_dir_exists,
                returncode=0 if self.tools_dir_exists else 1,
                stdout="",
                stderr="",
            )
        if cmd[:2] == ["sh", "-c"] and "validate_owned()" in cmd[2]:
            if self.validation_results:
                success = self.validation_results.pop(0)
                return ExecResult(
                    success=success,
                    returncode=0 if success else 1,
                    stdout="1000\n" if success else "",
                    stderr="",
                )
            return ExecResult(
                success=self.tools_dir_exists,
                returncode=0 if self.tools_dir_exists else 1,
                stdout="1000\n" if self.tools_dir_exists else "",
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
    sandbox.validation_results = [False, True, True]

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
    assert any(user == "root" for _, user in sandbox.exec_calls)
    start_index = sandbox.exec_calls.index(([SANDBOX_CLI, "start-server"], None))
    validation_index = next(
        index
        for index, (command, _) in enumerate(sandbox.exec_calls)
        if command[:2] == ["sh", "-c"] and "validate_owned()" in command[2]
    )
    assert validation_index < start_index


async def test_injection_accepts_valid_install_after_publication_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class ConcurrentPublisherSandbox(RootProbeRaisesSandbox):
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
            if cmd[:2] == ["sh", "-c"] and "source_id=$(stat" in cmd[2]:
                self.exec_calls.append((cmd, user))
                self.tools_dir_exists = True
                return ExecResult(
                    success=False,
                    returncode=2,
                    stdout="",
                    stderr="publication lock disappeared",
                )
            return await super().exec(
                cmd, input, cwd, env, user, timeout, timeout_retry, concurrency
            )

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
        _user: str | None,
        _tools_dir: str,
    ) -> None:
        pass

    async def trusted_staging(
        _sandbox: SandboxEnvironment,
        _user: str | None,
        *,
        tools_dir: str,
    ) -> bool:
        assert ".install-" in tools_dir
        return True

    monkeypatch.setattr(sandbox_tools, "detect_sandbox_os", fake_detect_sandbox_os)
    monkeypatch.setattr(
        sandbox_tools, "_open_executable_for_arch", fake_open_executable_for_arch
    )
    monkeypatch.setattr(sandbox_tools, "_extract_tools_tree", fake_extract_tools_tree)
    monkeypatch.setattr(sandbox_tools, "_sandbox_cli_is_trusted", trusted_staging)

    await sandbox_tools._inject_container_tools_code_impl(
        ConcurrentPublisherSandbox()
    )


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

    async def tools_dir_exists(_sandbox: SandboxEnvironment, _tools_dir: str) -> bool:
        return True

    monkeypatch.setattr(sandbox_tools, "detect_sandbox_os", fake_detect_sandbox_os)
    monkeypatch.setattr(
        sandbox_tools, "_open_executable_for_arch", fake_open_executable_for_arch
    )
    monkeypatch.setattr(sandbox_tools, "_sandbox_tools_detector", fake_detector)
    monkeypatch.setattr(sandbox_tools, "_tools_dir_exists", tools_dir_exists)

    await sandbox_tools._inject_container_tools_code(sandbox)

    assert all(command != ["id", "-u"] for command, _ in sandbox.exec_calls)


async def test_detector_uses_root_identity_when_root_is_available() -> None:
    class TrustedRootSandbox(RootProbeRaisesSandbox):
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
            return ExecResult(success=True, returncode=0, stdout="0\n", stderr="")

    sandbox = TrustedRootSandbox()

    assert await sandbox_tools._sandbox_tools_detector(sandbox)
    assert sandbox._tools_user == "root"
    assert len(sandbox.exec_calls) == 2
    command, user = sandbox.exec_calls[1]
    assert user == "root"
    assert "validate_owned()" in command[2]


async def test_detector_rejects_default_user_install_when_root_is_available() -> None:
    class ExplicitRootSandbox(RootProbeRaisesSandbox):
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
            success = cmd == ["id", "-u"]
            return ExecResult(
                success=success,
                returncode=0 if success else 1,
                stdout="0\n" if success else "",
                stderr="",
            )

    sandbox = ExplicitRootSandbox()

    assert not await sandbox_tools._sandbox_tools_detector(sandbox)
    assert [user for _, user in sandbox.exec_calls] == ["root", "root"]


async def test_detector_does_not_warn_about_user_for_local_sandbox() -> None:
    sandbox = LocalSandboxEnvironment()
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            assert not await sandbox_tools._sandbox_tools_detector(sandbox)
    finally:
        sandbox.directory.cleanup()


async def test_detector_does_not_warn_about_user_for_proxied_local_sandbox() -> None:
    local = LocalSandboxEnvironment()
    sandbox = SandboxEnvironmentProxy(local)
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            assert not await sandbox_tools._sandbox_tools_detector(sandbox)
    finally:
        local.directory.cleanup()


async def test_injection_preserves_sandbox_unavailable_error() -> None:
    class UnavailableSandbox(RootProbeRaisesSandbox):
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
            raise SandboxUnavailableError("sandbox stopped")

    with pytest.raises(SandboxUnavailableError, match="sandbox stopped"):
        await sandbox_tools._inject_container_tools_code(UnavailableSandbox())


async def test_injection_preserves_unavailability_during_root_staging_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class UnavailableDuringStagingSandbox(RootProbeRaisesSandbox):
        def __init__(self) -> None:
            super().__init__()
            self.root_probes = 0

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
            if cmd == ["id", "-u"] and user == "root":
                self.root_probes += 1
                if self.root_probes == 2:
                    raise SandboxUnavailableError("sandbox stopped during staging")
            return await super().exec(
                cmd, input, cwd, env, user, timeout, timeout_retry, concurrency
            )

    @asynccontextmanager
    async def fake_open_executable_for_arch(
        _arch: Architecture,
        _musl: bool,
    ) -> AsyncIterator[tuple[str, BinaryIO]]:
        yield "inspect-sandbox-tools", BytesIO(b"binary")

    async def fake_detect_sandbox_os(
        _sandbox: SandboxEnvironment,
    ) -> SupportedContainerOSInfo:
        return {"architecture": "amd64", "libc": "glibc"}

    monkeypatch.setattr(sandbox_tools, "detect_sandbox_os", fake_detect_sandbox_os)
    monkeypatch.setattr(
        sandbox_tools, "_open_executable_for_arch", fake_open_executable_for_arch
    )

    sandbox = UnavailableDuringStagingSandbox()
    with pytest.raises(SandboxUnavailableError, match="sandbox stopped during staging"):
        await sandbox_tools._inject_container_tools_code(sandbox)

    assert sandbox.root_probes == 2


def test_local_sandbox_tools_install_is_namespaced_per_os_user() -> None:
    sandbox = LocalSandboxEnvironment()
    try:
        tools_dir = sandbox_tools._sandbox_tools_dir(sandbox)
    finally:
        sandbox.directory.cleanup()

    assert tools_dir == sandbox_cli.local_sandbox_tools_dir(
        sandbox_cli.local_sandbox_tools_namespace()
    )
    assert Path(tools_dir).parent == Path.home()


def test_proxied_local_sandbox_tools_install_is_namespaced_per_os_user() -> None:
    local = LocalSandboxEnvironment()
    sandbox = SandboxEnvironmentProxy(local)
    try:
        tools_dir = sandbox_tools._sandbox_tools_dir(sandbox)
    finally:
        local.directory.cleanup()

    assert tools_dir == sandbox_cli.local_sandbox_tools_dir(
        sandbox_cli.local_sandbox_tools_namespace()
    )


def test_local_sandbox_tools_namespace_without_getuid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delattr(os, "getuid")

    namespace = sandbox_cli.local_sandbox_tools_namespace()

    assert len(namespace) == 16
    assert namespace.isalnum()


async def test_cancelled_root_staging_creation_cleans_up_as_root(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class NoSuperSandbox(RootProbeRaisesSandbox):
        def __init__(self) -> None:
            self.exec_calls = []

    sandbox = NoSuperSandbox()
    creation_started = anyio.Event()
    cleanup_users: list[str | None] = []

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

    async def fake_tools_dir_exists(
        _sandbox: SandboxEnvironment, _tools_dir: str
    ) -> bool:
        return False

    async def cancelled_root_creation(_sandbox: SandboxEnvironment, _path: str) -> bool:
        creation_started.set()
        await anyio.sleep_forever()
        raise AssertionError("sleep_forever returned")

    async def record_cleanup(
        _sandbox: SandboxEnvironment,
        user: str | None,
        *_paths: str,
        recursive: bool = True,
    ) -> None:
        cleanup_users.append(user)

    monkeypatch.setattr(sandbox_tools, "detect_sandbox_os", fake_detect_sandbox_os)
    monkeypatch.setattr(
        sandbox_tools, "_open_executable_for_arch", fake_open_executable_for_arch
    )
    monkeypatch.setattr(sandbox_tools, "_tools_dir_exists", fake_tools_dir_exists)
    monkeypatch.setattr(
        sandbox_tools, "_create_tools_dir_as_root", cancelled_root_creation
    )
    monkeypatch.setattr(sandbox_tools, "_cleanup_paths", record_cleanup)

    async with anyio.create_task_group() as task_group:
        task_group.start_soon(sandbox_tools._inject_container_tools_code_impl, sandbox)
        await creation_started.wait()
        task_group.cancel_scope.cancel()

    assert cleanup_users == ["root"]


def test_validation_command_rejects_missing_installation(
    tmp_path: os.PathLike[str],
) -> None:
    command = sandbox_tools._sandbox_tools_validation_command()
    command[4] = os.path.join(os.fspath(tmp_path), "missing-tools")
    command[5] = os.path.join(command[4], "inspect-sandbox-tools")

    result = subprocess.run(command, check=False, capture_output=True, text=True)

    assert result.returncode != 0


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
        wrapper.write(f'#!/bin/sh\nmkdir -- "$4"\nexec "{real_mv}" "$@"\n')
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
    assert os.path.isdir(staging)
    assert os.listdir(destination) == []


def test_publish_command_recovers_stale_lock(tmp_path: os.PathLike[str]) -> None:
    base = os.fspath(tmp_path)
    staging = os.path.join(base, "staging")
    destination = os.path.join(base, "tools")
    lock = os.path.join(base, "lock")
    os.mkdir(staging)
    os.mkdir(lock)
    os.chmod(lock, 0o700)
    with open(os.path.join(lock, "owner"), "w") as owner:
        owner.write("999999999:1\n")
    os.chmod(os.path.join(lock, "owner"), 0o600)
    command = sandbox_tools._publish_tools_command(staging)
    command[5] = lock
    command[6] = destination

    result = subprocess.run(command, check=False, capture_output=True, text=True)

    assert result.returncode == 0
    assert os.path.isdir(destination)
    assert not os.path.exists(lock)


def test_publish_command_expires_live_looking_lock(
    tmp_path: os.PathLike[str],
) -> None:
    base = os.fspath(tmp_path)
    staging = os.path.join(base, "staging")
    destination = os.path.join(base, "tools")
    lock = os.path.join(base, "lock")
    bin_dir = os.path.join(base, "bin")
    os.mkdir(staging)
    os.mkdir(lock)
    os.chmod(lock, 0o700)
    os.mkdir(bin_dir)
    process_fields = (
        Path(f"/proc/{os.getpid()}/stat").read_text().split(") ", 1)[1]
    )
    process_start = process_fields.split()[19]
    with open(os.path.join(lock, "owner"), "w") as owner:
        owner.write(f"{os.getpid()}:{process_start}\n")
    os.chmod(os.path.join(lock, "owner"), 0o600)
    sleep_wrapper = os.path.join(bin_dir, "sleep")
    with open(sleep_wrapper, "w") as wrapper:
        wrapper.write("#!/bin/sh\nexit 0\n")
    os.chmod(sleep_wrapper, 0o700)
    command = sandbox_tools._publish_tools_command(staging)
    command[5] = lock
    command[6] = destination

    result = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        env={**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"},
    )

    assert result.returncode == 0
    assert os.path.isdir(destination)
    assert not os.path.exists(lock)


def test_publish_command_creates_owner_with_restrictive_mode(
    tmp_path: os.PathLike[str],
) -> None:
    base = os.fspath(tmp_path)
    staging = os.path.join(base, "staging")
    destination = os.path.join(base, "tools")
    lock = os.path.join(base, "lock")
    bin_dir = os.path.join(base, "bin")
    os.mkdir(staging)
    os.mkdir(bin_dir)
    mv_wrapper = os.path.join(bin_dir, "mv")
    with open(mv_wrapper, "w") as wrapper:
        wrapper.write("#!/bin/sh\nsleep 30\n")
    os.chmod(mv_wrapper, 0o700)
    command = sandbox_tools._publish_tools_command(staging)
    command[5] = lock
    command[6] = destination

    def permissive_umask() -> None:
        os.umask(0o002)

    process = subprocess.Popen(
        command,
        env={**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"},
        start_new_session=True,
        preexec_fn=permissive_umask,
    )
    owner_file = os.path.join(lock, "owner")
    try:
        for _ in range(100):
            if os.path.exists(owner_file):
                break
            assert process.poll() is None
            time.sleep(0.01)
        assert os.stat(owner_file).st_mode & 0o777 == 0o600
    finally:
        os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=5)


def test_publish_command_recovers_empty_owner_file(
    tmp_path: os.PathLike[str],
) -> None:
    base = os.fspath(tmp_path)
    staging = os.path.join(base, "staging")
    destination = os.path.join(base, "tools")
    lock = os.path.join(base, "lock")
    os.mkdir(staging)
    os.mkdir(lock)
    os.chmod(lock, 0o700)
    owner_file = os.path.join(lock, "owner")
    with open(owner_file, "w"):
        pass
    os.chmod(owner_file, 0o600)
    command = sandbox_tools._publish_tools_command(staging)
    command[5] = lock
    command[6] = destination

    result = subprocess.run(command, check=False, capture_output=True, text=True)

    assert result.returncode == 0
    assert os.path.isdir(destination)
    assert not os.path.exists(lock)


def test_publish_command_rejects_lock_symlink_without_deleting_target(
    tmp_path: os.PathLike[str],
) -> None:
    base = os.fspath(tmp_path)
    staging = os.path.join(base, "staging")
    destination = os.path.join(base, "tools")
    lock_target = os.path.join(base, "lock-target")
    lock = os.path.join(base, "lock")
    owner_file = os.path.join(lock_target, "owner")
    os.mkdir(staging)
    os.mkdir(lock_target)
    with open(owner_file, "w") as owner:
        owner.write("999999999\n")
    os.symlink(lock_target, lock)
    command = sandbox_tools._publish_tools_command(staging)
    command[5] = lock
    command[6] = destination

    result = subprocess.run(command, check=False, capture_output=True, text=True)

    assert result.returncode == 18
    assert os.path.isfile(owner_file)
    assert not os.path.exists(destination)


def test_publish_command_rejects_malformed_lock_without_retrying(
    tmp_path: os.PathLike[str],
) -> None:
    base = os.fspath(tmp_path)
    staging = os.path.join(base, "staging")
    destination = os.path.join(base, "tools")
    lock = os.path.join(base, "lock")
    os.mkdir(staging)
    os.mkdir(lock)
    os.chmod(lock, 0o700)
    with open(os.path.join(lock, "unexpected"), "w") as unexpected:
        unexpected.write("content")
    command = sandbox_tools._publish_tools_command(staging)
    command[5] = lock
    command[6] = destination

    result = subprocess.run(
        command, check=False, capture_output=True, text=True, timeout=2
    )

    assert result.returncode == 18
    assert not os.path.exists(destination)


def test_publish_command_cancellation_releases_lock(
    tmp_path: os.PathLike[str],
) -> None:
    base = os.fspath(tmp_path)
    staging = os.path.join(base, "staging")
    destination = os.path.join(base, "tools")
    lock = os.path.join(base, "lock")
    bin_dir = os.path.join(base, "bin")
    os.mkdir(staging)
    os.mkdir(bin_dir)
    mv_wrapper = os.path.join(bin_dir, "mv")
    with open(mv_wrapper, "w") as wrapper:
        wrapper.write("#!/bin/sh\nsleep 30\n")
    os.chmod(mv_wrapper, 0o700)
    command = sandbox_tools._publish_tools_command(staging)
    command[5] = lock
    command[6] = destination
    process = subprocess.Popen(
        command,
        env={**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"},
        start_new_session=True,
    )
    try:
        for _ in range(100):
            if os.path.exists(os.path.join(lock, "owner")):
                break
            assert process.poll() is None
            time.sleep(0.01)
        assert os.path.exists(os.path.join(lock, "owner"))
        os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=5)
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()

    assert not os.path.exists(lock)


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
    cleanup_paths = cleanup_calls[0][3:]
    assert sandbox.writes[0][0] in cleanup_paths
    assert "/protected/install/archive.tgz" in cleanup_paths


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
    command = sandbox_tools._sandbox_tools_validation_command(tools_dir)
    command[5] = launcher

    assert subprocess.run(command, check=False).returncode == 0

    os.chmod(launcher, 0o722)
    assert subprocess.run(command, check=False).returncode != 0
    os.chmod(launcher, 0o700)

    os.unlink(launcher)
    os.symlink("missing", launcher)
    assert subprocess.run(command, check=False).returncode != 0


def test_launcher_validator_accepts_safe_root_owned_install_for_nonroot_user(
    tmp_path: os.PathLike[str],
) -> None:
    if os.getuid() != 0:
        pytest.skip("root ownership fixture requires root")
    tools_dir = os.fspath(tmp_path)
    launcher = os.path.join(tools_dir, "launcher")
    with open(launcher, "w") as file:
        file.write("#!/bin/sh\n")
    os.chmod(tools_dir, 0o755)
    os.chmod(launcher, 0o755)
    bin_dir = os.path.join(tools_dir, "bin")
    os.mkdir(bin_dir)
    id_wrapper = os.path.join(bin_dir, "id")
    with open(id_wrapper, "w") as wrapper:
        wrapper.write("#!/bin/sh\nprintf '1000\\n'\n")
    os.chmod(id_wrapper, 0o700)
    command = sandbox_tools._sandbox_tools_validation_command(tools_dir)
    command[5] = launcher

    result = subprocess.run(
        command,
        check=False,
        env={**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"},
    )

    assert result.returncode == 0
