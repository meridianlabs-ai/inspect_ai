import base64
import json
import os
import re
import stat
import sys
import tempfile
import time
import uuid
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import Any, Iterator

JSON_RPC_RESPONSE_CHUNK_METHOD = "__inspect_json_rpc_response_chunk__"
JSON_RPC_RESPONSE_CHUNK_FIELD = "__inspect_json_rpc_response_chunk__"
JSON_RPC_RESPONSE_CHUNK_VERSION = 1
JSON_RPC_RESPONSE_MAX_BYTES_ENV = "INSPECT_SANDBOX_JSON_RPC_RESPONSE_MAX_BYTES"

_DEFAULT_MAX_RESPONSE_BYTES = 64 * 1024
_MAX_CHUNK_BYTES = 512 * 1024
_CHUNK_TTL_SECONDS = 60 * 60
_VALID_HANDLE = re.compile(r"^[0-9a-f]{32}$")


def _default_chunk_dir() -> Path:
    """Return a hidden per-injection chunk-storage root.

    Frozen sandbox tools run from a dot-prefixed random directory under
    ``/var/tmp``. Keeping chunk storage beside that directory avoids a stable,
    self-describing path that an agent could pre-occupy or discover casually.
    """
    executable = Path(sys.executable).resolve()
    if getattr(sys, "frozen", False):
        return executable.parent.parent / f"{executable.parent.name}-json-rpc-chunks"
    return Path(tempfile.gettempdir()) / ".inspect-sandbox-tools-json-rpc-chunks"


_CHUNK_DIR = _default_chunk_dir()


@contextmanager
def _json_rpc_response_chunk_dir() -> Iterator[int]:
    """Yield a descriptor for the verified hidden chunk-storage root.

    Individual responses live in private UID subdirectories created lazily only
    when a response exceeds the transport limit.
    """
    try:
        _CHUNK_DIR.mkdir(mode=0o1733)
    except FileExistsError:
        pass

    # A `user=`-scoped exec may have created this directory, so the entry can be
    # owned - and replaced - by a sandbox user. Inspect and modify it through a
    # descriptor: a path-based check leaves a window to swap in a symlink, and
    # chmod on a path follows it. O_DIRECTORY|O_NOFOLLOW also subsumes the
    # is-it-really-a-directory check.
    try:
        dir_fd = os.open(_CHUNK_DIR, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError as ex:
        raise RuntimeError(
            f"JSON-RPC response chunk path is not a directory: {_CHUNK_DIR}"
        ) from ex

    try:
        chunk_dir_stat = os.fstat(dir_fd)
        current_uid = os.getuid()
        if chunk_dir_stat.st_uid not in (0, current_uid) and current_uid != 0:
            raise RuntimeError(
                f"JSON-RPC response chunk directory has unexpected owner: {_CHUNK_DIR}"
            )

        # Private per-identity subdirectories hold response files. The sticky bit
        # and absent read bit prevent deletion and enumeration of another
        # identity's entries.
        required_mode = 0o1733
        if chunk_dir_stat.st_uid == current_uid or current_uid == 0:
            os.fchmod(dir_fd, required_mode)
        elif stat.S_IMODE(chunk_dir_stat.st_mode) != required_mode:
            raise RuntimeError(
                f"JSON-RPC response chunk directory has unsafe permissions: {_CHUNK_DIR}"
            )
        yield dir_fd
    finally:
        os.close(dir_fd)


def ensure_json_rpc_response_chunk_dir() -> None:
    """Ensure the hidden chunk-storage root is safe to use."""
    with _json_rpc_response_chunk_dir():
        pass


@contextmanager
def _current_user_chunk_dir() -> Iterator[int]:
    """Yield a descriptor for the current identity's private chunk directory."""
    current_uid = os.getuid()
    with _json_rpc_response_chunk_dir() as root_fd:
        with _user_chunk_dir(root_fd, current_uid) as directory_fd:
            _remove_stale_chunks(directory_fd)
            yield directory_fd


@contextmanager
def _user_chunk_dir(root_fd: int, owner_uid: int) -> Iterator[int]:
    name = str(owner_uid)
    try:
        os.mkdir(name, 0o700, dir_fd=root_fd)
    except FileExistsError:
        pass
    try:
        directory_fd = os.open(
            name,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
            dir_fd=root_fd,
        )
    except OSError as ex:
        raise RuntimeError("Unsafe JSON-RPC response user chunk directory") from ex
    try:
        status = os.fstat(directory_fd)
        if status.st_uid != owner_uid:
            raise RuntimeError("Unexpected JSON-RPC response chunk directory owner")
        os.fchmod(directory_fd, 0o700)
        yield directory_fd
    finally:
        os.close(directory_fd)


def chunk_json_rpc_response_if_needed(
    request_data: dict[str, Any],
    response: str,
    max_response_bytes: int | None = None,
) -> str:
    """Return a bounded response envelope, spilling large frames to a file."""
    request_id = request_data.get("id")
    if request_id is None:
        return response

    response_bytes = response.encode("utf-8")
    response_limit = _response_byte_limit(max_response_bytes)
    if len(response_bytes) + 1 <= response_limit:
        return response

    handle = _write_response(response_bytes)
    try:
        return _read_chunk_response(request_id, handle, 0, response_limit)
    except Exception:
        _release_chunk(handle)
        raise


def handle_json_rpc_response_chunk_request(
    request_data: dict[str, Any], max_response_bytes: int | None = None
) -> str:
    """Fetch or release a previously spilled JSON-RPC response."""
    request_id = request_data.get("id")
    params = request_data.get("params")
    if not isinstance(params, dict):
        return _json_rpc_error(request_id, -32602, "chunk params must be an object")

    handle = params.get("handle")
    if not isinstance(handle, str) or not _VALID_HANDLE.fullmatch(handle):
        return _json_rpc_error(request_id, -32602, "invalid chunk handle")
    if params.get("release") is True:
        _release_chunk(handle)
        return _json_rpc_success(request_id, None)
    if "release" in params:
        return _json_rpc_error(request_id, -32602, "release must be true")

    offset = params.get("offset")
    if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
        return _json_rpc_error(request_id, -32602, "invalid chunk offset")

    try:
        return _read_chunk_response(
            request_id,
            handle,
            offset,
            _response_byte_limit(max_response_bytes),
        )
    except FileNotFoundError:
        return _json_rpc_error(request_id, -32000, "chunk handle not found")
    except ValueError as ex:
        return _json_rpc_error(request_id, -32602, str(ex))
    except OSError as ex:
        return _json_rpc_error(request_id, -32000, f"unable to read chunk: {ex}")


def _write_response(response_bytes: bytes) -> str:
    with _current_user_chunk_dir() as directory_fd:
        while True:
            handle = uuid.uuid4().hex
            filename = f"{handle}.jsonrpc"
            try:
                descriptor = os.open(
                    filename,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                    0o600,
                    dir_fd=directory_fd,
                )
            except FileExistsError:
                continue

            with os.fdopen(descriptor, "wb") as chunk_file:
                chunk_file.write(response_bytes)
            return handle


def _read_chunk_response(
    request_id: Any,
    handle: str,
    offset: int,
    max_response_bytes: int,
) -> str:
    with _open_chunk(handle) as (descriptor, _, _):
        total_size = os.fstat(descriptor).st_size
        if offset >= total_size:
            raise ValueError("chunk offset is beyond the response")
        chunk_file = os.fdopen(os.dup(descriptor), "rb")
        with chunk_file:
            chunk_file.seek(offset)
            candidate = chunk_file.read(min(_MAX_CHUNK_BYTES, total_size - offset))
        if not candidate:
            raise OSError("chunk file ended before its declared size")

        response = _largest_fitting_chunk_response(
            request_id,
            handle,
            offset,
            total_size,
            candidate,
            max_response_bytes,
        )
        os.utime(descriptor, None)
        return response


def _largest_fitting_chunk_response(
    request_id: Any,
    handle: str,
    offset: int,
    total_size: int,
    candidate: bytes,
    max_response_bytes: int,
) -> str:
    smallest = _chunk_response(request_id, handle, offset, total_size, candidate[:1])
    if len(smallest.encode("utf-8")) + 1 > max_response_bytes:
        raise ValueError(
            "sandbox exec output limit is too small for a JSON-RPC chunk envelope"
        )

    low = 1
    high = len(candidate)
    best = smallest
    while low <= high:
        size = (low + high) // 2
        response = _chunk_response(
            request_id, handle, offset, total_size, candidate[:size]
        )
        if len(response.encode("utf-8")) + 1 <= max_response_bytes:
            best = response
            low = size + 1
        else:
            high = size - 1
    return best


def _chunk_response(
    request_id: Any,
    handle: str,
    offset: int,
    total_size: int,
    chunk: bytes,
) -> str:
    next_offset = offset + len(chunk)
    return json.dumps(
        {
            "jsonrpc": "2.0",
            "id": request_id,
            JSON_RPC_RESPONSE_CHUNK_FIELD: {
                "version": JSON_RPC_RESPONSE_CHUNK_VERSION,
                "handle": handle,
                "offset": offset,
                "next_offset": next_offset,
                "total_size": total_size,
                "done": next_offset == total_size,
                "chunk": base64.b64encode(chunk).decode("ascii"),
            },
        },
        separators=(",", ":"),
    )


@contextmanager
def _open_chunk(handle: str) -> Iterator[tuple[int, int, str]]:
    if not _VALID_HANDLE.fullmatch(handle):
        raise ValueError("invalid chunk handle")
    filename = f"{handle}.jsonrpc"
    descriptor: int | None = None
    directory_fd: int | None = None
    with _json_rpc_response_chunk_dir() as root_fd:
        try:
            current_uid = os.getuid()
            user_names = [str(current_uid)]
            if current_uid == 0:
                user_names.extend(name for name in os.listdir(root_fd) if name != "0")
            for user_name in user_names:
                try:
                    expected_uid = int(user_name)
                    user_fd = os.open(
                        user_name,
                        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                        dir_fd=root_fd,
                    )
                except (OSError, ValueError):
                    continue
                try:
                    user_status = os.fstat(user_fd)
                    if (
                        user_status.st_uid != expected_uid
                        or stat.S_IMODE(user_status.st_mode) != 0o700
                    ):
                        continue
                    try:
                        candidate = os.open(
                            filename, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=user_fd
                        )
                    except OSError:
                        continue
                    candidate_status = os.fstat(candidate)
                    if (
                        stat.S_ISREG(candidate_status.st_mode)
                        and candidate_status.st_uid == expected_uid
                    ):
                        descriptor = candidate
                        directory_fd = os.dup(user_fd)
                        break
                    os.close(candidate)
                finally:
                    os.close(user_fd)
            if descriptor is None:
                raise FileNotFoundError(filename)
            assert directory_fd is not None
            yield descriptor, directory_fd, filename
        finally:
            if descriptor is not None:
                os.close(descriptor)
            if directory_fd is not None:
                os.close(directory_fd)


def _release_chunk(handle: str) -> None:
    try:
        with _open_chunk(handle) as (descriptor, directory_fd, filename):
            status = os.fstat(descriptor)
            candidate = os.stat(
                filename, dir_fd=directory_fd, follow_symlinks=False
            )
            if (
                candidate.st_ino == status.st_ino
                and candidate.st_dev == status.st_dev
            ):
                os.unlink(filename, dir_fd=directory_fd)
    except (FileNotFoundError, OSError):
        pass


def _response_byte_limit(explicit_limit: int | None) -> int:
    value: int | str | None = explicit_limit
    if value is None:
        value = os.environ.get(JSON_RPC_RESPONSE_MAX_BYTES_ENV)
    if value is None:
        return _DEFAULT_MAX_RESPONSE_BYTES
    try:
        limit = int(value)
    except (TypeError, ValueError):
        return _DEFAULT_MAX_RESPONSE_BYTES
    return limit if limit > 0 else _DEFAULT_MAX_RESPONSE_BYTES


def _remove_stale_chunks(directory_fd: int) -> None:
    stale_before = time.time() - _CHUNK_TTL_SECONDS
    with suppress(OSError):
        for filename in os.listdir(directory_fd):
            if not filename.endswith(".jsonrpc"):
                continue
            with suppress(OSError):
                status = os.stat(filename, dir_fd=directory_fd, follow_symlinks=False)
                if stat.S_ISREG(status.st_mode) and status.st_mtime < stale_before:
                    os.unlink(filename, dir_fd=directory_fd)


def _json_rpc_success(request_id: Any, result: object) -> str:
    return json.dumps(
        {"jsonrpc": "2.0", "id": request_id, "result": result},
        separators=(",", ":"),
    )


def _json_rpc_error(request_id: Any, code: int, message: str) -> str:
    return json.dumps(
        {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": code, "message": message},
        },
        separators=(",", ":"),
    )
