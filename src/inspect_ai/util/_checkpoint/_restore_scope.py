"""Scope and structural checks for restoring a sandbox snapshot.

On resume a strategy materializes a committed snapshot into a fresh
sandbox as root. The snapshot came from the resume source — whatever
its last writer put there — so it is untrusted data: a crafted snapshot
can carry ``/etc/passwd``, a setuid shell at any path, or a replacement
for ``/bin/sh``, and restic and tar would write each node where the
snapshot says, with the mode and owner it says. Structural validation
before any byte enters the sandbox is the defense; the resume source
itself is not authenticated.

The trusted roots are this attempt's resolved capture set
(``SandboxBackupPaths.include``: the task's ``sandbox_paths`` entry, or
the default user's home dir resolved against the fresh sandbox) — never
anything read from the checkpoint dir. Each strategy lists its snapshot
on the host (``restic ls --json`` against the adopted repo; ``tarfile``
over the stored archive) and passes every node through
:meth:`RestoreRoots.check_node`:

- a node must sit at or under a root, or be a *directory* on the path
  above one (restic and tar both record a source path's ancestors);
- a node under a root must be a regular file, directory, symlink, or
  (tar) hard link whose target is also under a root — no device, fifo,
  or socket nodes;
- a node under a root carries no setuid, setgid, or sticky bit.

Symlink targets are not constrained: an agent legitimately keeps
symlinks in ``$HOME``, and both tools write a symlink as a symlink
without following it. Ancestors are exempt from the mode check (``/tmp``
is sticky) because the strategies restore each root individually —
``restic restore <id>:<parent> --include /<name>`` and ``tar -x <root>``
— so nothing above a root is written or has its metadata restored.

Ownership is the residual: restic and tar restore recorded uid/gid when
running as root. For the auto-home case the core snapshots the home
dir's owner before restore and re-owns every node under it that differs
afterwards (:func:`home_owner_uid` / :func:`enforce_home_owner`); for
configured ``sandbox_paths`` recorded ownership is accepted (root-owned
files under a configured ``/data`` may be legitimate).
"""

from __future__ import annotations

import posixpath
import shlex
import tarfile
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any, NamedTuple

from inspect_ai.util._sandbox.environment import SandboxEnvironment

from ._layout.schemas import SnapshotDetails

_SPECIAL_MODE_BITS = 0o7000
"""setuid | setgid | sticky, in POSIX ``st_mode`` layout."""

_GO_MODE_SETUID = 1 << 23
_GO_MODE_SETGID = 1 << 22
_GO_MODE_STICKY = 1 << 20
"""Go ``os.FileMode`` special bits, the layout ``restic ls --json`` uses
for ``mode`` (the permission bits are the low 9 bits as usual)."""

_RESTORABLE_KINDS = frozenset({"file", "dir", "symlink", "hardlink"})

_RESTIC_GLOB_CHARS = "\\*?["


class RestoreScopeError(RuntimeError):
    """A snapshot to be restored into a sandbox failed a scope or structure check."""


@dataclass(frozen=True)
class RestoreNode:
    """One snapshot node, in the vocabulary :meth:`RestoreRoots.check_node` checks."""

    path: str
    """Absolute path the node would be restored at."""

    kind: str
    """``file``, ``dir``, ``symlink``, ``hardlink``; any other value is rejected."""

    mode: int
    """POSIX permission bits including the special bits (``st_mode & 0o7777``)."""

    link_target: str | None = None
    """A hard link's target path (absolute); ``None`` for every other kind."""


def normalize_absolute(path: str, *, label: str, what: str) -> str:
    """``path`` if it is already canonical, else a :class:`RestoreScopeError`.

    Requires a leading ``/`` and rejects empty (``//``, trailing ``/``),
    ``.`` and ``..`` components rather than resolving them: a snapshot
    node or capture root with such a component is never what an honest
    capture wrote, and lexical resolution would decide containment for
    a path the restoring tool may interpret differently.
    """
    if not path.startswith("/"):
        raise RestoreScopeError(f"{label}: {what} is not an absolute path: {path!r}")
    parts = path[1:].split("/") if len(path) > 1 else []
    if any(part in ("", ".", "..") for part in parts):
        raise RestoreScopeError(
            f"{label}: {what} has an empty, '.' or '..' component: {path!r}"
        )
    return path


@dataclass(frozen=True)
class RestoreRoots:
    """The absolute paths a restore may write at or under."""

    roots: tuple[str, ...]
    """Canonical absolute roots, sorted, deduplicated. Never contains ``/``."""

    @classmethod
    def from_include(cls, include: Sequence[str], *, label: str) -> RestoreRoots:
        """Roots from a capture include set (``SandboxBackupPaths.include``).

        ``/`` is refused: a capture of the whole filesystem has no
        scope to enforce, and the per-root restore forms have no parent
        to anchor at.
        """
        roots: set[str] = set()
        for raw in include:
            root = normalize_absolute(
                posixpath.normpath(raw) if raw.startswith("/") else raw,
                label=label,
                what="capture root",
            )
            if root == "/":
                raise RestoreScopeError(
                    f"{label}: a capture root of '/' cannot be scoped for restore; "
                    f"configure sandbox_paths with specific directories"
                )
            roots.add(root)
        if not roots:
            raise RestoreScopeError(f"{label}: the capture include set is empty")
        return cls(tuple(sorted(roots)))

    def containing_root(self, path: str) -> str | None:
        """The root ``path`` sits at or under, else ``None``."""
        for root in self.roots:
            if path == root or path.startswith(root + "/"):
                return root
        return None

    def is_ancestor(self, path: str) -> bool:
        """Whether ``path`` lies strictly above some root."""
        return any(root.startswith(path + "/") for root in self.roots)

    def check_node(self, node: RestoreNode, *, label: str) -> str | None:
        """Check one node; return the root it falls under (``None`` for an ancestor).

        Raises :class:`RestoreScopeError` naming the offending path when
        the node lies outside every root, is a non-directory ancestor,
        has a kind that is not restorable, is a hard link whose target
        lies outside every root, or carries a special mode bit.
        """
        path = normalize_absolute(node.path, label=label, what="snapshot node path")
        root = self.containing_root(path)
        if root is None:
            if not self.is_ancestor(path):
                raise RestoreScopeError(
                    f"{label}: snapshot node {path} lies outside every capture root "
                    f"{list(self.roots)}"
                )
            if node.kind != "dir":
                raise RestoreScopeError(
                    f"{label}: snapshot node {path} is a {node.kind} on the path "
                    f"above a capture root; only directories may appear there"
                )
            return None
        if node.kind not in _RESTORABLE_KINDS:
            raise RestoreScopeError(
                f"{label}: snapshot node {path} is a {node.kind}; only regular "
                f"files, directories and symlinks are restored"
            )
        if node.kind == "hardlink":
            target = normalize_absolute(
                node.link_target or "", label=label, what=f"hard link target of {path}"
            )
            if self.containing_root(target) is None:
                raise RestoreScopeError(
                    f"{label}: snapshot node {path} is a hard link to {target}, "
                    f"outside every capture root"
                )
        if node.mode & _SPECIAL_MODE_BITS:
            raise RestoreScopeError(
                f"{label}: snapshot node {path} has mode {node.mode:04o} with a "
                f"setuid, setgid or sticky bit set"
            )
        return root

    def require_all_present(self, seen: Iterable[str], *, label: str) -> None:
        """Every root must have had at least one node checked under it.

        An honest capture always records each root itself (restic and
        tar both emit the source path as a node), so a root with no
        node is a snapshot that does not match this attempt's capture
        set — refused rather than restored partially.
        """
        missing = set(self.roots) - set(seen)
        if missing:
            raise RestoreScopeError(
                f"{label}: snapshot has no node at capture root(s) {sorted(missing)}"
            )


def restic_node(record: dict[str, Any], *, label: str) -> RestoreNode:
    """A :class:`RestoreNode` from one ``restic ls --json`` node record.

    Restic's ``mode`` is a Go ``os.FileMode``: permission bits low,
    special bits at Go's positions; mapped back to POSIX layout here.
    """
    path, kind, mode = record.get("path"), record.get("type"), record.get("mode", 0)
    if not isinstance(path, str) or not isinstance(kind, str):
        raise RestoreScopeError(f"{label}: malformed snapshot node record: {record}")
    if not isinstance(mode, int) or isinstance(mode, bool):
        raise RestoreScopeError(f"{label}: snapshot node {path} has a non-integer mode")
    posix_mode = mode & 0o777
    if mode & _GO_MODE_SETUID:
        posix_mode |= 0o4000
    if mode & _GO_MODE_SETGID:
        posix_mode |= 0o2000
    if mode & _GO_MODE_STICKY:
        posix_mode |= 0o1000
    return RestoreNode(path=path, kind=kind, mode=posix_mode)


def tar_member_node(member: tarfile.TarInfo, *, label: str) -> RestoreNode:
    """A :class:`RestoreNode` from one tar member.

    Member names must be relative and normalized (``home/user/x``), the
    form ``tar -c`` writes for an absolute source after stripping the
    leading ``/``; an absolute or ``.``/``..``-bearing name is refused
    outright rather than normalized, since the extracting tar decides
    its own interpretation of such a name.
    """
    path = _tar_member_path(member.name, label=label, what="archive member")
    link_target: str | None = None
    if member.isreg():
        kind = "file"
    elif member.isdir():
        kind = "dir"
    elif member.issym():
        kind = "symlink"
    elif member.islnk():
        kind = "hardlink"
        link_target = _tar_member_path(
            member.linkname, label=label, what=f"hard link target of {path}"
        )
    else:
        kind = f"tar type {member.type!r} entry"
    return RestoreNode(
        path=path, kind=kind, mode=member.mode & 0o7777, link_target=link_target
    )


def _tar_member_path(name: str, *, label: str, what: str) -> str:
    if not name or name.startswith("/"):
        raise RestoreScopeError(f"{label}: {what} is empty or absolute: {name!r}")
    return normalize_absolute("/" + name.rstrip("/"), label=label, what=what)


def tar_member_argument(root: str) -> str:
    """The member name ``tar -x`` scopes extraction to for ``root``.

    tar strips the leading ``/`` from member names at creation, so the
    root ``/home/user`` selects the members ``home/user`` and below.
    """
    return root.lstrip("/")


class ResticRestoreArgs(NamedTuple):
    """Restic arguments restoring exactly one root."""

    snapshot_spec: str
    """``<id>:<parent of root>`` — the restored tree is rooted at the parent."""

    target: str
    """``--target``: the root's parent, so the restored tree lands in place."""

    include: str
    """``--include``: the root's name, anchored and glob-escaped."""


def restic_restore_args(snapshot_id: str, root: str) -> ResticRestoreArgs:
    """Arguments for ``restic restore <id>:<parent> --target <parent> --include /<name>``.

    The root's parent is the restored tree root, so its own metadata —
    and that of every directory above it — is never written: restic
    restores the metadata of any directory on the way to a selected
    node when restoring with ``--target /``, which would let a snapshot
    reset ``/usr``'s owner or mode. Glob metacharacters in the name are
    escaped so the include matches the name literally.
    """
    parent, name = posixpath.split(root)
    escaped = "".join(f"\\{c}" if c in _RESTIC_GLOB_CHARS else c for c in name)
    return ResticRestoreArgs(
        snapshot_spec=f"{snapshot_id}:{parent}", target=parent, include=f"/{escaped}"
    )


def recorded_roots(details: SnapshotDetails, *, label: str) -> list[str] | None:
    """The capture roots a snapshot recorded (``roots`` extra), if any.

    ``None`` for records written before roots were recorded. A present
    value that is not a list of strings is a malformed record and an
    error, not a missing one.
    """
    extra = details.model_extra or {}
    value = extra.get("roots")
    if value is None:
        return None
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise RestoreScopeError(
            f"{label}: checkpoint record for {details.snapshot_id} has malformed "
            f"roots {value!r}"
        )
    return value


def check_recorded_roots(
    details: SnapshotDetails, roots: RestoreRoots, *, label: str
) -> None:
    """Fail if the snapshot recorded a capture set other than this attempt's.

    The recorded roots are diagnostics, never the source of trust (the
    checkpoint dir is what is being restored *from*); a mismatch means
    the configuration changed between attempts, and is reported rather
    than restored under either set.
    """
    recorded = recorded_roots(details, label=label)
    if recorded is None:
        return
    recorded_set = set(RestoreRoots.from_include(recorded, label=label).roots)
    if recorded_set != set(roots.roots):
        raise RestoreScopeError(
            f"{label}: snapshot {details.snapshot_id} was captured from "
            f"{sorted(recorded_set)} but this attempt captures {list(roots.roots)}; "
            f"restore the original sandbox_paths configuration and resume"
        )


async def home_owner_uid(env: SandboxEnvironment, home: str, *, label: str) -> int:
    """The uid owning ``home`` in the fresh sandbox, read before any restore.

    Read beforehand because a restore that includes the home dir node
    itself (tar does; restic's per-root form does not) would otherwise
    hand back whatever owner the snapshot recorded.
    """
    result = await env.exec(["stat", "-c", "%u", home], user="root")
    text = result.stdout.strip()
    if not result.success or not text.isdigit():
        raise RuntimeError(
            f"{label}: could not read the owner of home dir {home}: "
            f"{result.stderr.strip() or result.stdout.strip()}"
        )
    return int(text)


async def enforce_home_owner(
    env: SandboxEnvironment, home: str, uid: int, *, label: str
) -> None:
    """Re-own every node under ``home`` (and ``home`` itself) not owned by ``uid``.

    Symlinks are re-owned themselves, never followed. Files the capture
    excluded (the XDG cache dir) are traversed too; they are the fresh
    sandbox's own and already the user's.
    """
    script = f"find {shlex.quote(home)} ! -user {uid} -exec chown -h {uid} {{}} +"
    result = await env.exec(["sh", "-c", script], user="root")
    if not result.success:
        raise RuntimeError(
            f"{label}: re-owning restored files under {home} to uid {uid} failed: "
            f"{result.stderr.strip()}"
        )
