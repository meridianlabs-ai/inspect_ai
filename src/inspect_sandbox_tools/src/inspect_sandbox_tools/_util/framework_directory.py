import os
import stat
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Literal

ExistingDirectoryPolicy = Literal["verify", "replace"]


@contextmanager
def framework_directory(
    path: Path,
    *,
    owner_uid: int,
    mode: int = 0o700,
    existing: ExistingDirectoryPolicy = "verify",
) -> Iterator[int]:
    """Open a verified framework-owned directory without following its entry.

    The parent must already exist. The returned descriptor remains bound to the
    object that was verified, allowing callers to perform sensitive operations
    descriptor-relatively where needed.
    """
    if path.name in ("", ".", ".."):
        raise ValueError(f"Framework directory must name a child: {path}")

    parent_fd = os.open(path.parent, os.O_PATH | os.O_DIRECTORY | os.O_NOFOLLOW)
    directory_fd: int | None = None
    try:
        while directory_fd is None:
            try:
                directory_fd = os.open(
                    path.name,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=parent_fd,
                )
            except FileNotFoundError:
                try:
                    os.mkdir(path.name, mode, dir_fd=parent_fd)
                except FileExistsError:
                    continue
                directory_fd = os.open(
                    path.name,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=parent_fd,
                )
            except OSError as ex:
                raise RuntimeError(f"Unsafe framework directory: {path}") from ex

            if directory_fd is None:
                continue
            status = os.fstat(directory_fd)
            unsafe_permissions = stat.S_IMODE(status.st_mode) & 0o022
            if (
                stat.S_ISDIR(status.st_mode)
                and status.st_uid == owner_uid
                and not unsafe_permissions
            ):
                os.fchmod(directory_fd, mode)
                status = os.fstat(directory_fd)
                if stat.S_IMODE(status.st_mode) == mode:
                    break

            os.close(directory_fd)
            directory_fd = None
            # Replacing an entry in an attacker-writable parent cannot be made
            # conditional on the inode inspected above with portable Python APIs.
            # Fail closed rather than racing a rename of a concurrent replacement.
            raise RuntimeError(
                f"Framework directory has unexpected owner or mode: {path}"
            )

        yield directory_fd
    finally:
        if directory_fd is not None:
            os.close(directory_fd)
        os.close(parent_fd)


def ensure_framework_directory(
    path: Path,
    *,
    owner_uid: int | None = None,
    mode: int = 0o700,
    existing: ExistingDirectoryPolicy = "verify",
) -> None:
    """Create or verify a framework-owned directory."""
    with framework_directory(
        path,
        owner_uid=os.getuid() if owner_uid is None else owner_uid,
        mode=mode,
        existing=existing,
    ):
        pass
