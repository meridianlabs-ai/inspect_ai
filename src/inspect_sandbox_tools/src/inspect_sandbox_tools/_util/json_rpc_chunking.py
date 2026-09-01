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
from typing import Any, Iterator, NamedTuple

from inspect_sandbox_tools._util.framework_directory import ensure_framework_directory

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


def ensure_json_rpc_response_chunk_dir() -> None:
    """Ensure the hidden chunk-storage root is safe to use.

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
    current_uid = os.getuid()
    open_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    try:
        try:
            dir_fd = os.open(_CHUNK_DIR, open_flags)
        except PermissionError:
            # A root-owned 1733 parent intentionally cannot be listed by scoped
            # users. O_PATH permits verification and relative child creation.
            dir_fd = os.open(
                _CHUNK_DIR, os.O_PATH | os.O_DIRECTORY | os.O_NOFOLLOW
            )
    except OSError as ex:
        raise RuntimeError(
            f"JSON-RPC response chunk path is not a directory: {_CHUNK_DIR}"
        ) from ex

    try:
        chunk_dir_stat = os.fstat(dir_fd)
        if current_uid == 0 and chunk_dir_stat.st_uid != 0:
            raise RuntimeError(
                f"JSON-RPC response chunk directory has unexpected owner: {_CHUNK_DIR}"
            )
        if current_uid != 0 and chunk_dir_stat.st_uid not in (0, current_uid):
            raise RuntimeError(
                f"JSON-RPC response chunk directory has unexpected owner: {_CHUNK_DIR}"
            )

        # Private per-identity subdirectories hold response files. The sticky bit
        # and absent read bit prevent deletion and enumeration of another
        # identity's entries.
        required_mode = 0o1733
        if chunk_dir_stat.st_uid == current_uid:
            os.fchmod(dir_fd, required_mode)
        elif stat.S_IMODE(chunk_dir_stat.st_mode) != required_mode:
            raise RuntimeError(
                f"JSON-RPC response chunk directory has unsafe permissions: {_CHUNK_DIR}"
            )
    finally:
        os.close(dir_fd)


def _current_user_chunk_dir() -> Path:
    """Return a private chunk directory owned by the current identity."""
    ensure_json_rpc_response_chunk_dir()

    current_uid = os.getuid()
    chunk_dir = _CHUNK_DIR / str(current_uid)
    ensure_framework_directory(chunk_dir, owner_uid=current_uid)
    _remove_stale_chunks(chunk_dir)
    return chunk_dir


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

    handle, chunk_path = _write_response(response_bytes)
    try:
        return _read_chunk_response(request_id, handle, chunk_path, 0, response_limit)
    except Exception:
        chunk_path.unlink(missing_ok=True)
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
        try:
            with _open_chunk(handle) as chunk:
                os.unlink(chunk.filename, dir_fd=chunk.directory_fd)
        except FileNotFoundError:
            pass
        return _json_rpc_success(request_id, None)
    if "release" in params:
        return _json_rpc_error(request_id, -32602, "release must be true")

    offset = params.get("offset")
    if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
        return _json_rpc_error(request_id, -32602, "invalid chunk offset")

    try:
        with _open_chunk(handle) as chunk:
            return _read_chunk_response_fd(
                request_id,
                handle,
                chunk.file_fd,
                offset,
                _response_byte_limit(max_response_bytes),
            )
    except FileNotFoundError:
        return _json_rpc_error(request_id, -32000, "chunk handle not found")
    except ValueError as ex:
        return _json_rpc_error(request_id, -32602, str(ex))
    except OSError as ex:
        return _json_rpc_error(request_id, -32000, f"unable to read chunk: {ex}")


def _write_response(response_bytes: bytes) -> tuple[str, Path]:
    chunk_dir = _current_user_chunk_dir()
    while True:
        handle = uuid.uuid4().hex
        chunk_path = chunk_dir / f"{handle}.jsonrpc"
        try:
            descriptor = os.open(
                chunk_path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
        except FileExistsError:
            continue

        with os.fdopen(descriptor, "wb") as chunk_file:
            chunk_file.write(response_bytes)
        return handle, chunk_path


def _read_chunk_response(
    request_id: Any,
    handle: str,
    chunk_path: Path,
    offset: int,
    max_response_bytes: int,
) -> str:
    total_size = chunk_path.stat().st_size
    if offset >= total_size:
        raise ValueError("chunk offset is beyond the response")

    with chunk_path.open("rb") as chunk_file:
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
    os.utime(chunk_path, None)
    return response


def _read_chunk_response_fd(
    request_id: Any,
    handle: str,
    chunk_fd: int,
    offset: int,
    max_response_bytes: int,
) -> str:
    """Read a chunk from the same verified inode throughout the operation."""
    total_size = os.fstat(chunk_fd).st_size
    if offset >= total_size:
        raise ValueError("chunk offset is beyond the response")
    candidate = os.pread(
        chunk_fd, min(_MAX_CHUNK_BYTES, total_size - offset), offset
    )
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
    os.utime(chunk_fd, None)
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


class _OpenChunk(NamedTuple):
    directory_fd: int
    file_fd: int
    filename: str


@contextmanager
def _open_chunk(handle: str) -> Iterator[_OpenChunk]:
    """Open an owned chunk without following user-replaceable path entries."""
    if not _VALID_HANDLE.fullmatch(handle):
        raise ValueError("invalid chunk handle")
    filename = f"{handle}.jsonrpc"
    current_uid = os.getuid()
    root_flags = os.O_RDONLY if current_uid == 0 else os.O_PATH
    root_fd = os.open(
        _CHUNK_DIR, root_flags | os.O_DIRECTORY | os.O_NOFOLLOW
    )
    try:
        user_names = [str(current_uid)]
        if current_uid == 0:
            user_names = os.listdir(root_fd)
        for user_name in user_names:
            try:
                user_uid = int(user_name)
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
                    user_status.st_uid != user_uid
                    or stat.S_IMODE(user_status.st_mode) != 0o700
                ):
                    continue
                try:
                    chunk_fd = os.open(
                        filename, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=user_fd
                    )
                except OSError:
                    continue
                chunk_status = os.fstat(chunk_fd)
                if not stat.S_ISREG(chunk_status.st_mode) or (
                    chunk_status.st_uid != user_uid
                ):
                    os.close(chunk_fd)
                    continue
                try:
                    yield _OpenChunk(user_fd, chunk_fd, filename)
                finally:
                    os.close(chunk_fd)
                return
            finally:
                os.close(user_fd)
        raise FileNotFoundError(filename)
    finally:
        os.close(root_fd)


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


def _remove_stale_chunks(chunk_dir: Path) -> None:
    stale_before = time.time() - _CHUNK_TTL_SECONDS
    with suppress(OSError):
        for chunk_path in chunk_dir.glob("*.jsonrpc"):
            with suppress(OSError):
                if chunk_path.stat().st_mtime < stale_before:
                    chunk_path.unlink()


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
