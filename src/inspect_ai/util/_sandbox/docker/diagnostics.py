"""Post-mortem evidence for a sandbox container that stopped running.

`docker compose exec` against a dead container says nothing about *why* it
died, and a container dying mid-command produces no output at all — so a CI
failure of this kind used to carry no diagnostic evidence whatsoever (#264).
These helpers capture the container's state (`compose ps`), its exit detail
(`docker inspect .State`, including the OOM-killed flag) and its recent log
output at the moment a dead sandbox is detected.

All probes use short timeouts (not `compose_ps`'s 300s default) and no
timeout retries: they run on error paths, often when the docker daemon is
already struggling, and must not stall a sample for minutes.
"""

import json
from typing import Any, NamedTuple

from inspect_ai.util._subprocess import subprocess

from .compose import compose_command, compose_ps
from .util import ComposeProject

# fields of a `compose ps` record worth relaying (the full record is noisy)
_PS_FIELDS = ("Name", "ID", "State", "Status", "ExitCode")

# container states that positively confirm the container cannot exec.
# restarting/paused/created containers are left alone: they may recover,
# and misreading a recoverable container as dead aborts a healthy sample.
_DEAD_STATES = ("exited", "dead")

_LOG_TAIL_LINES = 100
_SECTION_LIMIT = 4096
_PROBE_TIMEOUT = 60


class ServicePsState(NamedTuple):
    """One observed `compose ps` result, reduced to what diagnostics need."""

    section: str
    """The rendered ps section of the diagnostics report."""

    container: str | None
    """Container id (or name) to pass to `docker inspect`, if one was found."""


async def dead_service_state(
    service: str, project: ComposeProject
) -> ServicePsState | None:
    """The observed ps state for `service` if every container is positively exited or dead.

    Conservative: returns None when the query fails or shows no containers.
    Callers use a non-None result to escalate an ambiguous exec failure to
    "sandbox dead", and anything short of an observed dead container is not
    evidence of that. The observation itself is returned so it can be handed
    to `sandbox_unavailable_diagnostics` as evidence: a dead container is
    often removed within seconds, and a second query can come back empty
    (#264).
    """
    try:
        entries = await _service_ps_entries(service, project)
    except Exception:
        return None
    if entries and all(_is_dead(entry) for entry in entries):
        return _ps_state_of(entries)
    return None


async def sandbox_unavailable_diagnostics(
    service: str,
    project: ComposeProject,
    ps: ServicePsState | None,
) -> str:
    """Capture ps/inspect/log evidence for a service whose exec failed fatally.

    Best-effort: each probe reports its own failure inline rather than
    raising, so collecting diagnostics can never mask the error it augments.

    Args:
        service: The compose service whose exec failed.
        project: The service's compose project.
        ps: ps state already observed for this service (from
            `dead_service_state`), reused as evidence rather than re-queried.
            None (e.g. when docker itself reported the container not running)
            queries live.
    """
    if ps is None:
        ps = await _ps_state(service, project)
    sections = [
        ps.section,
        await _inspect_state(ps.container),
        await _log_tail(service, project),
    ]
    body = "\n".join(section for section in sections if section)
    return f'Container diagnostics for service "{service}" (project {project.name}):\n{body}'


def _is_dead(entry: dict[str, Any]) -> bool:
    return str(entry.get("State", "")).lower() in _DEAD_STATES


async def _service_ps_entries(
    service: str, project: ComposeProject
) -> list[dict[str, Any]]:
    return [
        entry
        for entry in await compose_ps(
            project=project, all=True, timeout=_PROBE_TIMEOUT, timeout_retry=False
        )
        if entry.get("Service") == service
    ]


async def _ps_state(service: str, project: ComposeProject) -> ServicePsState:
    try:
        entries = await _service_ps_entries(service, project)
    except Exception as ex:
        return ServicePsState(f"docker compose ps --all failed: {ex}", None)
    return _ps_state_of(entries)


def _ps_state_of(entries: list[dict[str, Any]]) -> ServicePsState:
    if not entries:
        return ServicePsState(
            "docker compose ps --all: no container found for service "
            "(never created, or already removed)",
            None,
        )
    summaries = [_ps_summary(entry) for entry in entries]
    # inspect the dead container when there is more than one (e.g. a stale
    # exited container coexisting with a fresh one)
    target = next((entry for entry in entries if _is_dead(entry)), entries[0])
    container = target.get("ID") or target.get("Name")
    return ServicePsState(
        f"docker compose ps --all: {json.dumps(summaries)}", container
    )


def _ps_summary(entry: dict[str, Any]) -> dict[str, Any]:
    return {field: entry[field] for field in _PS_FIELDS if field in entry}


async def _inspect_state(container: str | None) -> str:
    if not container:
        return ""
    try:
        result = await subprocess(
            ["docker", "inspect", "--format", "{{json .State}}", container],
            timeout=_PROBE_TIMEOUT,
        )
    except Exception as ex:
        return f"docker inspect failed: {ex}"
    if not result.success:
        return f"docker inspect failed: {result.stderr.strip()}"
    return f"docker inspect .State: {_truncate(result.stdout.strip())}"


async def _log_tail(service: str, project: ComposeProject) -> str:
    try:
        result = await compose_command(
            ["logs", "--tail", str(_LOG_TAIL_LINES), service],
            project=project,
            timeout=_PROBE_TIMEOUT,
            timeout_retry=False,
            ansi="never",
        )
    except Exception as ex:
        return f"docker compose logs failed: {ex}"
    if not result.success:
        return f"docker compose logs failed: {result.stderr.strip()}"
    output = result.stdout.strip()
    if not output:
        return f"docker compose logs --tail {_LOG_TAIL_LINES}: (no log output)"
    return f"docker compose logs --tail {_LOG_TAIL_LINES}:\n{_truncate(output)}"


def _truncate(text: str, limit: int = _SECTION_LIMIT) -> str:
    if len(text) <= limit:
        return text
    return f"(truncated to last {limit} chars)\n{text[-limit:]}"
