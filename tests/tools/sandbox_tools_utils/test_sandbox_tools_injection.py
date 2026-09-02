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
from inspect_ai.util._sandbox._cli import (
    SANDBOX_CLI,
    SANDBOX_TOOLS_BASE_NAME,
    SANDBOX_TOOLS_DIR,
)
from inspect_ai.util._sandbox.environment import (
    SandboxEnvironment,
    SandboxEnvironmentConfigType,
    SandboxUnavailableError,
)
from inspect_ai.util._sandbox.events import SandboxEnvironmentProxy
from inspect_ai.util._sandbox.local import LocalSandboxEnvironment
from inspect_ai.util._sandbox.recon import Architecture, SupportedContainerOSInfo
from inspect_ai.util._subprocess import ExecResult


@pytest.fixture
def local_tools_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point local sandbox tools at a temp directory instead of the real home dir.

    The real resolver would find whatever is installed under the developer's
    ``$HOME`` / ``$XDG_RUNTIME_DIR`` (and cache it process-wide).
    """
    tools_dir = tmp_path / "tools"
    monkeypatch.setattr(
        sandbox_tools, "local_sandbox_tools_dir", lambda: str(tools_dir)
    )
    monkeypatch.setattr(sandbox_cli, "local_sandbox_tools_dir", lambda: str(tools_dir))
    return tools_dir


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
    # staging validation, then post-publication validation
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

    monkeypatch.setattr(sandbox_tools, "detect_sandbox_os", fake_detect_sandbox_os)
    monkeypatch.setattr(
        sandbox_tools, "_open_executable_for_arch", fake_open_executable_for_arch
    )
    monkeypatch.setattr(sandbox_tools, "_extract_tools_tree", fake_extract_tools_tree)

    sandbox = ConcurrentPublisherSandbox()
    sandbox.validation_results = [True]  # staging validation

    await sandbox_tools._inject_container_tools_code_impl(sandbox)

    validated_dirs = [
        command[4]
        for command, _ in sandbox.exec_calls
        if command[:2] == ["sh", "-c"] and "validate_owned()" in command[2]
    ]
    assert any(".install-" in tools_dir for tools_dir in validated_dirs)
    assert validated_dirs[-1] == SANDBOX_TOOLS_DIR


async def test_injection_removes_published_tree_that_fails_verification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class TransientRootProbeSandbox(RootProbeRaisesSandbox):
        """Root `id -u` raises (transiently), but root exec works afterwards."""

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
            if (
                user == "root"
                and cmd[:2] == ["sh", "-c"]
                and "validate_owned()" in cmd[2]
            ):
                self.exec_calls.append((cmd, user))
                # root is available now, and rejects the user-owned tree
                return ExecResult(
                    success=False,
                    returncode=1,
                    stdout="0\n",
                    stderr=f"{cmd[4]} is owned by uid 1000",
                )
            if cmd[:3] == ["rm", "-rf", "--"]:
                self.exec_calls.append((cmd, user))
                if SANDBOX_TOOLS_DIR in cmd[3:]:
                    self.tools_dir_exists = False
                return ExecResult(success=True, returncode=0, stdout="", stderr="")
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

    monkeypatch.setattr(sandbox_tools, "detect_sandbox_os", fake_detect_sandbox_os)
    monkeypatch.setattr(
        sandbox_tools, "_open_executable_for_arch", fake_open_executable_for_arch
    )
    monkeypatch.setattr(sandbox_tools, "_extract_tools_tree", fake_extract_tools_tree)

    sandbox = TransientRootProbeSandbox()
    sandbox.validation_results = [True]  # staging validation as the default user

    with pytest.raises(
        sandbox_tools.SandboxInjectionError,
        match="Published sandbox tools installation failed validation",
    ):
        await sandbox_tools._inject_container_tools_code(sandbox)

    # the tree this call published is removed (as its owner) rather than left in
    # place for every later detector pass to reject
    removals = [
        command for command, _ in sandbox.exec_calls if command[:2] == ["rm", "-rf"]
    ]
    assert removals == [["rm", "-rf", "--", SANDBOX_TOOLS_DIR]]
    assert not sandbox.tools_dir_exists
    assert all(
        command != [SANDBOX_CLI, "start-server"] for command, _ in sandbox.exec_calls
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

    async def trusted_verification(
        _sandbox: SandboxEnvironment, _tools_dir: str
    ) -> sandbox_tools._InstallVerification:
        return sandbox_tools._InstallVerification(trusted=True, reason="")

    async def tools_dir_exists(_sandbox: SandboxEnvironment, _tools_dir: str) -> bool:
        return True

    monkeypatch.setattr(sandbox_tools, "detect_sandbox_os", fake_detect_sandbox_os)
    monkeypatch.setattr(
        sandbox_tools, "_open_executable_for_arch", fake_open_executable_for_arch
    )
    monkeypatch.setattr(sandbox_tools, "_verify_installation", trusted_verification)
    monkeypatch.setattr(sandbox_tools, "_tools_dir_exists", tools_dir_exists)

    await sandbox_tools._inject_container_tools_code(sandbox)

    assert all(command != ["id", "-u"] for command, _ in sandbox.exec_calls)


async def test_injection_error_names_untrusted_installation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sandbox = RootProbeRaisesSandbox()
    sandbox.tools_dir_exists = True

    async def untrusted_verification(
        _sandbox: SandboxEnvironment, tools_dir: str
    ) -> sandbox_tools._InstallVerification:
        return sandbox_tools._InstallVerification(
            trusted=False, reason=f"{tools_dir} is owned by uid 1000"
        )

    monkeypatch.setattr(sandbox_tools, "_verify_installation", untrusted_verification)

    with pytest.raises(sandbox_tools.SandboxInjectionError) as excinfo:
        await sandbox_tools._inject_container_tools_code(sandbox)

    message = str(excinfo.value)
    assert SANDBOX_TOOLS_DIR in message
    assert "is owned by uid 1000" in message


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
    # a single round trip establishes both root availability and trust
    assert len(sandbox.exec_calls) == 1
    command, user = sandbox.exec_calls[0]
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
            # root is available (uid 0 printed) but the install fails validation
            return ExecResult(
                success=False,
                returncode=1,
                stdout="0\n",
                stderr="/var/tmp/tools is owned by uid 1000",
            )

    sandbox = ExplicitRootSandbox()

    assert not await sandbox_tools._sandbox_tools_detector(sandbox)
    # no fallback to the default user: that could adopt a pre-positioned launcher
    assert [user for _, user in sandbox.exec_calls] == ["root"]


async def test_detector_falls_back_to_default_user_when_root_is_unavailable() -> None:
    class RootlessSandbox(RootProbeRaisesSandbox):
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
            if user == "root":
                return ExecResult(
                    success=False,
                    returncode=1,
                    stdout="",
                    stderr="runuser: may not be used by non-root users",
                )
            return ExecResult(success=True, returncode=0, stdout="1000\n", stderr="")

    sandbox = RootlessSandbox()

    assert await sandbox_tools._sandbox_tools_detector(sandbox)
    assert sandbox._tools_user is None
    assert [user for _, user in sandbox.exec_calls] == ["root", None]


async def test_detector_does_not_warn_about_user_for_local_sandbox(
    local_tools_dir: Path,
) -> None:
    sandbox = LocalSandboxEnvironment()
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            assert not await sandbox_tools._sandbox_tools_detector(sandbox)
    finally:
        sandbox.directory.cleanup()


async def test_detector_does_not_warn_about_user_for_proxied_local_sandbox(
    local_tools_dir: Path,
) -> None:
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

    assert sandbox.root_probes == 1


def test_local_sandbox_tools_install_uses_resolved_local_path() -> None:
    sandbox = LocalSandboxEnvironment()
    try:
        tools_dir = sandbox_tools._sandbox_tools_dir(sandbox)
    finally:
        sandbox.directory.cleanup()

    assert tools_dir == sandbox_cli.local_sandbox_tools_dir()


def test_proxied_local_sandbox_tools_install_uses_resolved_local_path() -> None:
    local = LocalSandboxEnvironment()
    sandbox = SandboxEnvironmentProxy(local)
    try:
        tools_dir = sandbox_tools._sandbox_tools_dir(sandbox)
    finally:
        local.directory.cleanup()

    assert tools_dir == sandbox_cli.local_sandbox_tools_dir()


def test_local_sandbox_tools_path_is_not_resolved_at_import() -> None:
    # Resolution probes the filesystem (writes and executes a shell script), which
    # must only happen when a local sandbox actually needs the path.
    assert not hasattr(sandbox_cli, "LOCAL_SANDBOX_TOOLS_DIR")
    assert callable(sandbox_cli.local_sandbox_tools_dir)


def test_local_sandbox_tools_path_supports_spaces(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home with spaces"
    home.mkdir(mode=0o700)
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    monkeypatch.setenv("HOME", os.fspath(home))

    tools_dir = sandbox_cli._local_sandbox_tools_dir()

    assert Path(tools_dir).parent == home


def test_local_sandbox_tools_path_ignores_relative_runtime_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("XDG_RUNTIME_DIR", "runtime")
    monkeypatch.setenv("HOME", os.fspath(home))

    tools_dir = sandbox_cli._local_sandbox_tools_dir()

    assert Path(tools_dir).parent == home

    monkeypatch.setenv("XDG_RUNTIME_DIR", os.fspath(runtime))
    assert Path(sandbox_cli._local_sandbox_tools_dir()).parent == runtime


def test_local_sandbox_tools_path_fails_without_suitable_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    monkeypatch.setenv("HOME", os.fspath(tmp_path / "missing"))

    with pytest.raises(RuntimeError, match="Local sandbox tools require"):
        sandbox_cli._local_sandbox_tools_dir()

    assert list(tmp_path.iterdir()) == []


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


def test_publish_command_reports_move_failure_distinctly(
    tmp_path: os.PathLike[str],
) -> None:
    base = os.fspath(tmp_path)
    staging = os.path.join(base, "staging")
    destination = os.path.join(base, "tools")
    bin_dir = os.path.join(base, "bin")
    os.mkdir(staging)
    os.mkdir(bin_dir)
    mv_wrapper = os.path.join(bin_dir, "mv")
    with open(mv_wrapper, "w") as wrapper:
        wrapper.write("#!/bin/sh\necho 'mv: unrecognized option' >&2\nexit 1\n")
    os.chmod(mv_wrapper, 0o700)
    command = sandbox_tools._publish_tools_command(staging)
    command[5] = os.path.join(base, "lock")
    command[6] = destination

    result = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        env={**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"},
    )

    # not reported as a collision: the real cause reaches the error message
    assert result.returncode not in (0, 17, 18)
    assert "unrecognized option" in result.stderr
    assert os.path.isdir(staging)
    assert not os.path.exists(destination)


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


def test_publish_command_does_not_expire_live_lock(
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
    process_fields = Path(f"/proc/{os.getpid()}/stat").read_text().split(") ", 1)[1]
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

    assert result.returncode == 18
    assert os.path.isdir(staging)
    assert not os.path.exists(destination)
    assert os.path.isdir(lock)


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
        with open(owner_file, "w") as owner:
            owner.write("999999999:1\n")
    finally:
        os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=5)
    assert Path(owner_file).read_text() == "999999999:1\n"


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

    def validate() -> subprocess.CompletedProcess[str]:
        return subprocess.run(command, check=False, capture_output=True, text=True)

    result = validate()
    assert result.returncode == 0
    assert result.stdout.strip() == str(os.getuid())

    os.chmod(launcher, 0o722)
    result = validate()
    assert result.returncode != 0
    assert result.stdout.strip() == str(os.getuid())
    assert f"{launcher} is writable by other users" in result.stderr
    os.chmod(launcher, 0o600)
    assert f"{launcher} is not executable" in validate().stderr
    os.chmod(launcher, 0o700)

    os.unlink(launcher)
    os.symlink("missing", launcher)
    result = validate()
    assert result.returncode != 0
    assert f"{launcher} is a symbolic link" in result.stderr

    os.unlink(launcher)
    assert f"{launcher} is missing or not a regular file" in validate().stderr


async def test_local_sandbox_injection_skips_root_probe(
    local_tools_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tools_dir = local_tools_dir

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
        staging_dir: str,
    ) -> None:
        assert user is None
        launcher = Path(staging_dir) / SANDBOX_TOOLS_BASE_NAME
        launcher.write_text("#!/bin/sh\nexit 0\n")
        launcher.chmod(0o700)

    monkeypatch.setattr(sandbox_tools, "detect_sandbox_os", fake_detect_sandbox_os)
    monkeypatch.setattr(
        sandbox_tools, "_open_executable_for_arch", fake_open_executable_for_arch
    )
    monkeypatch.setattr(sandbox_tools, "_extract_tools_tree", fake_extract_tools_tree)

    sandbox = LocalSandboxEnvironment()
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", UserWarning)
            await sandbox_tools._inject_container_tools_code(sandbox)
            assert await sandbox_tools._sandbox_tools_detector(sandbox)
    finally:
        sandbox.directory.cleanup()

    assert sandbox._tools_user is None
    assert (tools_dir / SANDBOX_TOOLS_BASE_NAME).is_file()
    assert list(tools_dir.parent.iterdir()) == [tools_dir]


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
