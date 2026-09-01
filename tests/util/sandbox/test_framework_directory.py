import os
import subprocess
from pathlib import Path

import pytest

from inspect_ai.util._sandbox._framework_directory import (
    framework_directory_command,
)


def run_framework_directory(
    path: Path, mode: int = 0o700
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        framework_directory_command(str(path), mode=mode),
        check=False,
        capture_output=True,
        text=True,
    )


def test_framework_directory_creates_and_adopts_secure_directory(
    tmp_path: Path,
) -> None:
    directory = tmp_path / "framework"

    assert run_framework_directory(directory).returncode == 0
    assert directory.is_dir()
    assert directory.stat().st_mode & 0o777 == 0o700
    assert run_framework_directory(directory).returncode == 0


@pytest.mark.parametrize("entry_type", ["file", "symlink"])
def test_framework_directory_rejects_non_directory(
    tmp_path: Path, entry_type: str
) -> None:
    directory = tmp_path / "framework"
    if entry_type == "file":
        directory.touch()
    else:
        directory.symlink_to(tmp_path)

    assert run_framework_directory(directory).returncode != 0


def test_framework_directory_migrates_legacy_secure_mode(tmp_path: Path) -> None:
    directory = tmp_path / "framework"
    directory.mkdir(mode=0o755)
    os.chmod(directory, 0o755)

    assert run_framework_directory(directory).returncode == 0
    assert directory.stat().st_mode & 0o777 == 0o700


@pytest.mark.parametrize("mode", [0o720, 0o702, 0o777])
def test_framework_directory_rejects_writable_legacy_mode(
    tmp_path: Path, mode: int
) -> None:
    directory = tmp_path / "framework"
    directory.mkdir(mode=mode)
    os.chmod(directory, mode)

    assert run_framework_directory(directory).returncode != 0
    assert directory.stat().st_mode & 0o777 == mode


@pytest.mark.parametrize("mode", [0o720, 0o702, 0o1000])
def test_framework_directory_rejects_invalid_requested_mode(mode: int) -> None:
    with pytest.raises(ValueError, match="must deny group and other writes"):
        framework_directory_command("framework", mode=mode)


def test_framework_directory_rejects_unsafe_path() -> None:
    with pytest.raises(ValueError, match="contains unsafe characters"):
        framework_directory_command("framework; true")
