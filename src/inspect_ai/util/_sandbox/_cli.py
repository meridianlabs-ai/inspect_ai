"""Path to the sandbox tools injected into sandbox environments.

The tools ship as a PyInstaller --onedir bundle (a launcher executable plus an
``_internal`` directory), so what is injected is a directory tree rather than a single
file. ``SANDBOX_TOOLS_DIR`` is where the tree is extracted, and ``SANDBOX_CLI`` is the
launcher inside it.

We choose /var/tmp as the injection location since:
  1) it is accessible in all major linux distributions
  2) all users can create entries there, which supports rootless sandboxes
  3) it is unlikely to be cleared during an evaluation
     (https://en.wikipedia.org/wiki/Filesystem_Hierarchy_Standard)
  4) it is unlikely to be accidentally stumbled upon by an LLM solving a
     task that requires interacting with temp files

We additionally choose a dot-prefixed random hash sub-directory to reduce
accidental discovery. When Inspect can run commands in the sandbox as root, it
installs the tree as root and restricts it to 0700. A root-owned 0700 tree
prevents access by other, non-root users, but not by a process running in the
sandbox as root. Local sandboxes instead choose a private runtime or home
directory, with an unpredictable private temporary directory as a fallback.
"""

import os
import subprocess
import tempfile
from pathlib import Path

# Also defined in inspect_ai.tool._sandbox_tools_utils._build_config — keep in sync.
SANDBOX_TOOLS_BASE_NAME = "inspect-sandbox-tools"

SANDBOX_TOOLS_DIR = "/var/tmp/.da7be258e003d428"

SANDBOX_CLI = f"{SANDBOX_TOOLS_DIR}/{SANDBOX_TOOLS_BASE_NAME}"


def _supports_executables(parent: Path) -> bool:
    """Return whether executables can run from a writable directory."""
    probe_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile("w", dir=parent, delete=False) as probe:
            probe.write("#!/bin/sh\nexit 0\n")
            probe_path = probe.name
        os.chmod(probe_path, 0o700)
        return subprocess.run(
            [probe_path],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=1,
            check=False,
        ).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False
    finally:
        if probe_path is not None:
            Path(probe_path).unlink(missing_ok=True)


def _local_sandbox_tools_dir() -> str:
    """Choose a private, executable host-local installation directory."""
    runtime_dir = os.environ.get("XDG_RUNTIME_DIR")
    candidates = [Path(runtime_dir)] if runtime_dir else []
    candidates.append(Path.home())
    for parent in candidates:
        try:
            stat = parent.stat()
        except OSError:
            continue
        getuid = getattr(os, "getuid", None)
        owned = getuid is None or stat.st_uid == getuid()
        if (
            owned
            and stat.st_mode & 0o022 == 0
            and os.access(parent, os.W_OK | os.X_OK)
            and _supports_executables(parent)
        ):
            return str(parent / f".{Path(SANDBOX_TOOLS_DIR).name}")

    # The fallback is unpredictable and created with mode 0700, so another user
    # cannot reserve or replace it in the shared temporary directory.
    private_parent = tempfile.mkdtemp(prefix="inspect-ai-tools-")
    if not _supports_executables(Path(private_parent)):
        raise RuntimeError(
            "Local sandbox tools require an executable runtime, home, or temporary "
            "directory"
        )
    return str(Path(private_parent) / f".{Path(SANDBOX_TOOLS_DIR).name}")


LOCAL_SANDBOX_TOOLS_DIR = _local_sandbox_tools_dir()
