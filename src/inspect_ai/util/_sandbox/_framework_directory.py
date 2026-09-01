from pathlib import PurePosixPath


def framework_directory_command(
    path: str | PurePosixPath,
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
    if repair_mode:
        mode_check = (
            f'mode="$(stat -c %a -- {directory})" && '
            f'{{ test "$mode" = {mode_text} || {{ '
            'case "$mode" in *[2367][0-7]|*[0-7][2367]) false;; esac && '
            f"chmod {mode_text} -- {directory}; }}; }}"
        )
    else:
        mode_check = f'test "$(stat -c %a -- {directory})" = {mode_text}'
    script = (
        f"umask 077; if mkdir -m {mode_text} -- {directory} 2>/dev/null; "
        f"then {created}; else test -d {directory} && test ! -L {directory} && "
        f'test "$(stat -c %u -- {directory})" = "$(id -u)" && '
        f"{mode_check} && {verified}; fi"
    )
    return ["sh", "-c", script]
