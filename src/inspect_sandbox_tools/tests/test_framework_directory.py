import os
import stat
from pathlib import Path

import pytest
from inspect_sandbox_tools._util.framework_directory import (
    ensure_framework_directory,
    framework_directory,
)


def test_framework_directory_creates_private_directory(tmp_path: Path) -> None:
    directory = tmp_path / "framework"

    ensure_framework_directory(directory)

    status = directory.lstat()
    assert stat.S_ISDIR(status.st_mode)
    assert status.st_uid == os.getuid()
    assert stat.S_IMODE(status.st_mode) == 0o700


@pytest.mark.parametrize("entry_type", ("directory", "symlink", "file"))
def test_framework_directory_rejects_untrusted_entry(
    tmp_path: Path, entry_type: str
) -> None:
    directory = tmp_path / "framework"
    if entry_type == "directory":
        directory.mkdir(mode=0o755)
    elif entry_type == "symlink":
        target = tmp_path / "target"
        target.mkdir()
        directory.symlink_to(target, target_is_directory=True)
    else:
        directory.touch()

    if entry_type == "directory":
        ensure_framework_directory(directory)
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    else:
        with pytest.raises(RuntimeError, match="Unsafe framework directory"):
            ensure_framework_directory(directory)


def test_framework_directory_rejects_non_directory_replacement(tmp_path: Path) -> None:
    directory = tmp_path / "framework"
    directory.touch()

    with pytest.raises(RuntimeError, match="Unsafe framework directory"):
        ensure_framework_directory(directory, existing="replace")


def test_framework_directory_rejects_legacy_world_writable_directory(
    tmp_path: Path,
) -> None:
    directory = tmp_path / "framework"
    directory.mkdir(mode=0o777)
    directory.chmod(0o777)
    planted_file = directory / "server.pid"
    planted_file.write_text("123")

    with pytest.raises(RuntimeError, match="unexpected owner or mode"):
        ensure_framework_directory(directory)

    assert planted_file.read_text() == "123"


def test_framework_directory_descriptor_survives_path_replacement(
    tmp_path: Path,
) -> None:
    directory = tmp_path / "framework"
    directory.mkdir(mode=0o700)

    with framework_directory(directory, owner_uid=os.getuid()) as directory_fd:
        directory.rename(tmp_path / "moved")
        directory.mkdir()
        assert os.fstat(directory_fd).st_ino == (tmp_path / "moved").stat().st_ino
