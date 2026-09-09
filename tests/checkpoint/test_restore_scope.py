"""Unit tests for the restore scope/structure checks (``_restore_scope``).

Pure checks over node descriptions — no restic, tar, or sandbox: which
paths a restore may write, which node kinds and modes are refused, how
restic's Go-layout modes and tar members map onto nodes, and the
per-root restic restore arguments.
"""

from __future__ import annotations

import io
import tarfile

import pytest

from inspect_ai.util._checkpoint._layout.schemas import SnapshotDetails
from inspect_ai.util._checkpoint._restore_scope import (
    RestoreNode,
    RestoreRoots,
    RestoreScopeError,
    check_recorded_roots,
    recorded_roots,
    restic_node,
    restic_restore_args,
    tar_member_argument,
    tar_member_node,
)

LABEL = "test restore"
HOME = RestoreRoots.from_include(["/home/user"], label=LABEL)


def _node(path: str, kind: str = "file", mode: int = 0o644) -> RestoreNode:
    return RestoreNode(path=path, kind=kind, mode=mode)


# --- roots -----------------------------------------------------------------


def test_roots_normalize_and_dedupe() -> None:
    roots = RestoreRoots.from_include(
        ["/data/", "/home//user", "/data", "/opt/./x"], label=LABEL
    )
    assert roots.roots == ("/data", "/home/user", "/opt/x")


@pytest.mark.parametrize(
    "include,match",
    [
        (["/"], "cannot be scoped"),
        (["/data", "/"], "cannot be scoped"),
        (["relative/path"], "not an absolute path"),
        ([], "include set is empty"),
    ],
)
def test_roots_refuse_unscopable_include(include: list[str], match: str) -> None:
    with pytest.raises(RestoreScopeError, match=match):
        RestoreRoots.from_include(include, label=LABEL)


# --- node checks -----------------------------------------------------------


def test_nodes_under_a_root_pass_and_report_their_root() -> None:
    roots = RestoreRoots.from_include(["/home/user", "/data"], label=LABEL)
    assert roots.check_node(_node("/home/user"), label=LABEL) == "/home/user"
    assert roots.check_node(_node("/home/user/a/b.txt"), label=LABEL) == "/home/user"
    assert roots.check_node(_node("/data/db", "dir", 0o755), label=LABEL) == "/data"
    assert (
        roots.check_node(_node("/home/user/link", "symlink", 0o777), label=LABEL)
        == "/home/user"
    )


def test_ancestor_directories_pass_without_mode_check() -> None:
    """``/`` and ``/tmp``-style ancestors are listed by every tool; sticky is fine there."""
    roots = RestoreRoots.from_include(["/tmp/work"], label=LABEL)
    assert roots.check_node(_node("/tmp", "dir", 0o1777), label=LABEL) is None
    assert HOME.check_node(_node("/home", "dir", 0o755), label=LABEL) is None


def test_ancestor_that_is_not_a_directory_is_refused() -> None:
    with pytest.raises(RestoreScopeError, match="/home is a file on the path above"):
        HOME.check_node(_node("/home", "file"), label=LABEL)
    with pytest.raises(RestoreScopeError, match="/home is a symlink on the path"):
        HOME.check_node(_node("/home", "symlink", 0o777), label=LABEL)


@pytest.mark.parametrize(
    "path",
    ["/etc/passwd", "/home/user2/x", "/home/userx", "/root", "/bin/sh"],
)
def test_node_outside_every_root_is_refused_naming_it(path: str) -> None:
    with pytest.raises(RestoreScopeError, match=f"{path} lies outside every"):
        HOME.check_node(_node(path), label=LABEL)


@pytest.mark.parametrize("kind", ["fifo", "chardev", "dev", "socket", "irregular"])
def test_special_node_kinds_are_refused(kind: str) -> None:
    with pytest.raises(RestoreScopeError, match=f"/home/user/x is a {kind}"):
        HOME.check_node(_node("/home/user/x", kind), label=LABEL)


@pytest.mark.parametrize("mode", [0o4755, 0o2755, 0o1777, 0o6777, 0o7777])
def test_special_mode_bits_are_refused(mode: int) -> None:
    with pytest.raises(RestoreScopeError, match="setuid, setgid or sticky"):
        HOME.check_node(_node("/home/user/bin/sh", mode=mode), label=LABEL)
    with pytest.raises(RestoreScopeError, match="setuid, setgid or sticky"):
        HOME.check_node(_node("/home/user/d", "dir", mode), label=LABEL)


def test_hard_link_target_must_be_under_a_root() -> None:
    inside = RestoreNode("/home/user/b", "hardlink", 0o644, link_target="/home/user/a")
    assert HOME.check_node(inside, label=LABEL) == "/home/user"
    outside = RestoreNode("/home/user/pw", "hardlink", 0o644, link_target="/etc/passwd")
    with pytest.raises(RestoreScopeError, match="hard link to /etc/passwd"):
        HOME.check_node(outside, label=LABEL)
    with pytest.raises(RestoreScopeError, match="hard link target"):
        HOME.check_node(
            RestoreNode("/home/user/pw", "hardlink", 0o644, link_target=None),
            label=LABEL,
        )


@pytest.mark.parametrize(
    "path", ["home/user/x", "/home/user/../../etc/passwd", "/home/user/./x", "//x"]
)
def test_unnormalized_node_paths_are_refused_not_resolved(path: str) -> None:
    with pytest.raises(RestoreScopeError, match="snapshot node path"):
        HOME.check_node(_node(path), label=LABEL)


def test_every_root_must_be_present() -> None:
    roots = RestoreRoots.from_include(["/home/user", "/data"], label=LABEL)
    roots.require_all_present({"/home/user", "/data"}, label=LABEL)
    with pytest.raises(
        RestoreScopeError, match=r"no node at capture root\(s\) \['/data'\]"
    ):
        roots.require_all_present({"/home/user"}, label=LABEL)


# --- restic node records ---------------------------------------------------


def test_restic_node_maps_go_mode_bits_to_posix() -> None:
    """``restic ls --json`` encodes special bits at Go's ``os.FileMode`` positions."""
    node = restic_node(
        {"type": "file", "path": "/home/user/suid", "mode": (1 << 23) | 0o755},
        label=LABEL,
    )
    assert node.mode == 0o4755
    assert (
        restic_node(
            {"type": "file", "path": "/x", "mode": (1 << 22) | 0o755}, label=LABEL
        ).mode
        == 0o2755
    )
    assert (
        restic_node(
            {"type": "dir", "path": "/tmp", "mode": (1 << 31) | (1 << 20) | 0o777},
            label=LABEL,
        ).mode
        == 0o1777
    )
    plain = restic_node({"type": "file", "path": "/x", "mode": 0o644}, label=LABEL)
    assert (plain.kind, plain.mode, plain.link_target) == ("file", 0o644, None)


@pytest.mark.parametrize(
    "record",
    [
        {"type": "file"},
        {"path": "/x"},
        {"type": "file", "path": "/x", "mode": "0644"},
        {"type": "file", "path": "/x", "mode": True},
    ],
)
def test_restic_node_rejects_malformed_record(record: dict[str, object]) -> None:
    with pytest.raises(RestoreScopeError):
        restic_node(record, label=LABEL)


# --- tar members -----------------------------------------------------------


def _member(
    name: str, type: bytes = tarfile.REGTYPE, **attrs: object
) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name)
    info.type = type
    for key, value in attrs.items():
        setattr(info, key, value)
    return info


def test_tar_member_node_kinds_and_modes() -> None:
    assert tar_member_node(
        _member("home/user/f", mode=0o4755), label=LABEL
    ) == RestoreNode("/home/user/f", "file", 0o4755)
    assert tar_member_node(
        _member("home/user/d/", tarfile.DIRTYPE, mode=0o755), label=LABEL
    ) == (RestoreNode("/home/user/d", "dir", 0o755))
    assert tar_member_node(
        _member("home/user/l", tarfile.SYMTYPE, linkname="/etc/passwd", mode=0o777),
        label=LABEL,
    ) == RestoreNode("/home/user/l", "symlink", 0o777)
    assert tar_member_node(
        _member("home/user/h", tarfile.LNKTYPE, linkname="home/user/f", mode=0o644),
        label=LABEL,
    ) == RestoreNode("/home/user/h", "hardlink", 0o644, link_target="/home/user/f")
    fifo = tar_member_node(_member("home/user/p", tarfile.FIFOTYPE), label=LABEL)
    with pytest.raises(RestoreScopeError, match="/home/user/p is a tar type"):
        HOME.check_node(fifo, label=LABEL)
    for type in (tarfile.CHRTYPE, tarfile.BLKTYPE):
        with pytest.raises(RestoreScopeError, match="tar type"):
            HOME.check_node(
                tar_member_node(_member("home/user/dev", type), label=LABEL),
                label=LABEL,
            )


@pytest.mark.parametrize(
    "name", ["/home/user/x", "./home/user/x", "home/user/../x", "", "home//user/x"]
)
def test_tar_member_names_must_be_relative_and_normalized(name: str) -> None:
    with pytest.raises(RestoreScopeError, match="archive member"):
        tar_member_node(_member(name), label=LABEL)


def test_tar_member_argument_strips_leading_slash() -> None:
    assert tar_member_argument("/home/user") == "home/user"


def test_tar_listing_round_trip_through_tarfile() -> None:
    """A real in-memory tar: members as ``tar -c`` writes them list and check."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        tar.addfile(_member("home/", tarfile.DIRTYPE, mode=0o755))
        tar.addfile(_member("home/user/", tarfile.DIRTYPE, mode=0o700))
        info = _member("home/user/notes.txt", mode=0o644, size=5)
        tar.addfile(info, io.BytesIO(b"hello"))
    buf.seek(0)
    with tarfile.open(fileobj=buf, mode="r|") as tar:
        seen = {
            HOME.check_node(tar_member_node(m, label=LABEL), label=LABEL) for m in tar
        }
    assert seen == {None, "/home/user"}


# --- restic restore arguments ----------------------------------------------


def test_restic_restore_args_anchor_at_the_parent() -> None:
    args = restic_restore_args("abc123", "/home/user")
    assert args == ("abc123:/home", "/home", "/user")
    assert restic_restore_args("abc123", "/data") == ("abc123:/", "/", "/data")


def test_restic_restore_args_escape_glob_metacharacters() -> None:
    """Backslash, ``*``, ``?`` and ``[`` are escaped; a lone ``]`` is already literal."""
    args = restic_restore_args("abc123", "/srv/da[t]a*?\\x")
    assert args.include == "/da\\[t]a\\*\\?\\\\x"
    assert args.target == "/srv"


# --- recorded roots ---------------------------------------------------------


def _details(**extra: object) -> SnapshotDetails:
    return SnapshotDetails.model_validate(
        dict(snapshot_id="abc", size_bytes=1, duration_ms=1, **extra)
    )


def test_recorded_roots_absent_is_none_and_accepted() -> None:
    assert recorded_roots(_details(), label=LABEL) is None
    check_recorded_roots(_details(), HOME, label=LABEL)


def test_recorded_roots_matching_current_set_pass() -> None:
    check_recorded_roots(_details(roots=["/home/user/"]), HOME, label=LABEL)


def test_recorded_roots_mismatch_names_both_sets() -> None:
    with pytest.raises(RestoreScopeError, match=r"\['/data'\].*\['/home/user'\]"):
        check_recorded_roots(_details(roots=["/data"]), HOME, label=LABEL)


@pytest.mark.parametrize("roots", ["/home/user", [1, 2], {"a": 1}])
def test_recorded_roots_malformed_is_an_error(roots: object) -> None:
    with pytest.raises(RestoreScopeError, match="malformed roots"):
        recorded_roots(_details(roots=roots), label=LABEL)
