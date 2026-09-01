import os
from pathlib import PurePosixPath


def framework_directory_command(
    path: str | os.PathLike[str],
    *,
    mode: int = 0o700,
    report_creation: bool = False,
    repair_mode: bool = False,
) -> list[str]:
    """Build a bootstrap command that creates or verifies a sandbox directory.

    The command atomically creates the leaf or adopts it only when it is a real
    directory owned by the effective UID with the requested mode. Callers must
    choose a parent whose permissions prevent replacement after verification.
    """
    directory = PurePosixPath(path).as_posix()
    if not directory.startswith("/") and "/" in directory:
        raise ValueError(f"Framework directory must be absolute or a leaf: {path}")
    if any(
        character
        not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_./-"
        for character in directory
    ):
        raise ValueError(f"Framework directory contains unsafe characters: {path}")

    mode_text = format(mode, "o")
    created = "echo created" if report_creation else ":"
    verified = "echo existing" if report_creation else ":"
    stat_values = (
        f"stat -c '%u %a' -- {directory} 2>/dev/null || "
        f"stat -f '%u %Lp' {directory} 2>/dev/null"
    )
    mode_check = (
        (
            f"chmod {mode_text} -- {directory} && set -- $({stat_values}) && "
            if repair_mode
            else ""
        )
        + f'test "$2" = {mode_text}'
    )
    script = (
        f"umask 077; if mkdir -m {mode_text} -- {directory} 2>/dev/null; "
        f"then {created}; else test -d {directory} && test ! -L {directory} && "
        f"set -- $({stat_values}) && test \"$1\" = \"$(id -u)\" && "
        f"{mode_check} && {verified}; fi"
    )
    return ["sh", "-c", script]
