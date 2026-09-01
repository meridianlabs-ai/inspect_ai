import fcntl
import os
import stat
import uuid
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

    parent_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    directory_fd: int | None = None
    try:
        # The parent is stable for the lifetime of this descriptor. Serializing
        # migration on it prevents a concurrent verifier from quarantining the
        # replacement installed by another process.
        fcntl.flock(parent_fd, fcntl.LOCK_EX)
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
                if existing != "replace":
                    raise RuntimeError(f"Unsafe framework directory: {path}") from ex
                _quarantine_entry(parent_fd, path)

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
            # A same-owner legacy directory that was writable by other users may
            # contain planted sockets or metadata. Never adopt its contents.
            if existing != "replace" and not (
                stat.S_ISDIR(status.st_mode)
                and status.st_uid == owner_uid
                and unsafe_permissions
            ):
                raise RuntimeError(
                    f"Framework directory has unexpected owner or mode: {path}"
                )
            _quarantine_entry(parent_fd, path)

        fcntl.flock(parent_fd, fcntl.LOCK_UN)
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


def _quarantine_entry(parent_fd: int, path: Path) -> None:
    quarantine_name = f".{path.name}.untrusted-{uuid.uuid4().hex}"
    try:
        os.rename(
            path.name, quarantine_name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd
        )
    except FileNotFoundError:
        return
    except OSError as ex:
        raise RuntimeError(
            f"Unable to replace unsafe framework directory: {path}"
        ) from ex
