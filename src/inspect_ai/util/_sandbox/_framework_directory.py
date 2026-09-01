from pathlib import PurePosixPath


def framework_directory_command(
    path: str | PurePosixPath,
    *,
    mode: int = 0o700,
) -> list[str]:
    """Build a command that securely creates or adopts a framework directory.

    Creation binds the leaf atomically. An existing entry is accepted only when
    it is a real directory owned by the effective user. A legacy directory with
    no group or other write bits is migrated to the requested mode.
    Callers must choose a parent that prevents other users from replacing an
    accepted leaf.

    Args:
        path: Absolute path, or a leaf name without shell metacharacters.
        mode: Exact directory mode. Group and other write access is prohibited.

    Returns:
        A command suitable for ``SandboxEnvironment.exec``.

    Raises:
        ValueError: If the path or mode cannot meet the security contract.
    """
    directory = PurePosixPath(path).as_posix()
    if directory in {"", ".", ".."}:
        raise ValueError(f"Framework directory must name a directory: {path}")
    if not directory.startswith("/") and "/" in directory:
        raise ValueError(f"Framework directory must be absolute or a leaf: {path}")
    if any(
        character
        not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_./-"
        for character in directory
    ):
        raise ValueError(f"Framework directory contains unsafe characters: {path}")
    if mode < 0 or mode > 0o777 or mode & 0o022:
        raise ValueError(
            f"Framework directory mode must deny group and other writes: {mode:o}"
        )

    mode_text = format(mode, "o")
    stat_values = (
        f"stat -c '%u %a' -- {directory} 2>/dev/null || "
        f"stat -f '%u %Lp' {directory} 2>/dev/null"
    )
    script = (
        f"umask 077; if mkdir -m {mode_text} -- {directory} 2>/dev/null; "
        f"then :; else test -d {directory} && test ! -L {directory} && "
        f'set -- $({stat_values}) && test "$1" = "$(id -u)" && '
        f"test $((0$2 & 022)) -eq 0 && chmod {mode_text} -- {directory} && "
        f'set -- $({stat_values}) && test "$1" = "$(id -u)" && '
        f'test "$2" = {mode_text}; fi'
    )
    return ["sh", "-c", script]
