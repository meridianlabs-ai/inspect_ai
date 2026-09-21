#!/usr/bin/env python3
"""Collect PR CI timing data into a JSON snapshot for the ci-perf skill.

Fetches recent completed pull_request workflow runs and their jobs via the
GitHub API (through the `gh` CLI, which must be authenticated), derives
per-job execution and wait times, and optionally parses pytest
`--durations` blocks out of test-job logs.

Analysis is deliberately NOT done here — the script records facts; the
skill's analyze phase interprets them (job dependency chains, trends,
proposals).
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, NamedTuple
from urllib.parse import urlencode

from summarize_ci_data import parse_ts as parse_required_ts
from summarize_ci_data import previous_window_end, summarize, window_overlap

# Upstream is public, so any fork's PR triggers CI there and its logs, step
# names and run titles carry text the PR author wrote. Only these two head
# repositories can be pushed to by people who already have upstream write
# access (upstream's own branches and Meridian's fork, from which colleagues
# open upstream PRs), so runs from anywhere else never reach the analysis
# agent. Merge state is not a substitute: cancelled and failing runs feed the
# runner-waste analysis.
TRUSTED_HEAD_REPOS = frozenset(
    {"UKGovernmentBEIS/inspect_ai", "meridianlabs-ai/inspect_ai"}
)


def gh_api(path: str) -> Any:
    result = subprocess.run(
        ["gh", "api", path], capture_output=True, text=True, check=True
    )
    return json.loads(result.stdout)


def gh_api_text(path: str) -> str:
    """Fetch a text API response (job logs).

    gh >= 2.97 refuses to print a response containing terminal escape
    sequences (pytest runs --color=yes, so logs always have them) unless
    --allow-escape-sequences is passed; older gh rejects that flag as
    unknown, hence the flagless retry.
    """
    result = subprocess.run(
        ["gh", "api", "--allow-escape-sequences", path],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        result = subprocess.run(
            ["gh", "api", path], capture_output=True, text=True, check=True
        )
    return result.stdout


def parse_ts(ts: str | None) -> datetime | None:
    """Optional wrapper over the summarizer's parser for absent API fields."""
    return parse_required_ts(ts) if ts else None


def seconds_between(start: str | None, end: str | None) -> float | None:
    s, e = parse_ts(start), parse_ts(end)
    return (e - s).total_seconds() if s and e else None


def since_arg(value: str) -> datetime:
    """Parse a `--since` value, rejecting empty and offset-less timestamps.

    The flag is meant to be fed from a shell variable, so an unset variable
    must not silently fall back to the full `--days` window.
    """
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise argparse.ArgumentTypeError(
            f"not an ISO-8601 timestamp: {value!r}"
        ) from error
    if parsed.tzinfo is None:
        raise argparse.ArgumentTypeError(
            f"{value!r} needs a UTC offset, e.g. 2026-09-19T13:01:25Z"
        )
    return parsed


def fetch_runs(
    repo: str, limit: int, days: int = 7, since: datetime | None = None
) -> list[dict[str, Any]]:
    """Collect a bounded recent window, retrying stale or repeated API pages.

    The unfiltered endpoint has served weeks-old cached pages during live runs.
    Fix the created-at range for all pages and validate every returned record
    against it. Do not publish a partial mix of current and stale pages.

    `since` tightens the created-at lower bound so a run pages back only that
    far; `limit` still caps the window. It never widens the range: the `days`
    bound still applies (with a warning when it wins), and every record is
    validated against the same created-at range the pages requested. Because
    the listing is filtered on creation time and completed status, a run that
    was created before `since` but still running at the previous collection
    belongs to neither window, so callers chaining windows should pass a value
    earlier than the previous window's end by at least the longest run time.
    """
    until = datetime.now(timezone.utc).replace(microsecond=0)
    oldest = until - timedelta(days=days)
    if since is not None:
        if since.tzinfo is None:
            raise ValueError("since must carry a UTC offset")
        if since >= until:
            raise ValueError(f"since {since.isoformat()} is not before collection time")
        if since < oldest:
            print(
                f"WARNING: since {since.isoformat()} is older than the {days}-day "
                f"bound; collecting from {oldest.isoformat()} instead",
                file=sys.stderr,
            )
    since = max(since, oldest) if since is not None else oldest
    for attempt in range(3):
        by_id: dict[int, dict[str, Any]] = {}
        page = 1
        stale = False
        while len(by_id) < limit:
            query = urlencode(
                {
                    "event": "pull_request",
                    "status": "completed",
                    "per_page": 100,
                    "page": page,
                    "created": f"{since.isoformat()}..{until.isoformat()}",
                }
            )
            batch = gh_api(f"repos/{repo}/actions/runs?{query}")["workflow_runs"]
            if not batch:
                break
            for run in batch:
                created = parse_ts(run["created_at"])
                if created is None or not since <= created <= until:
                    stale = True
                    break
            if stale:
                break
            previous_count = len(by_id)
            by_id.update({run["id"]: run for run in batch})
            if len(by_id) == previous_count:
                stale = True
                break
            page += 1
        if not stale:
            runs = sorted(
                by_id.values(), key=lambda r: r["run_started_at"], reverse=True
            )[:limit]
            warn_on_time_gap(runs)
            return runs
        print(
            f"WARNING: stale or repeated CI page; retry {attempt + 1}/3",
            file=sys.stderr,
        )
    raise RuntimeError(
        "GitHub returned stale or repeated CI pages on all three collection attempts"
    )


class TrustedRuns(NamedTuple):
    runs: list[dict[str, Any]]
    excluded: int


def trusted_runs(runs: list[dict[str, Any]]) -> TrustedRuns:
    """Keep runs whose head repository is in TRUSTED_HEAD_REPOS.

    Applied before any per-run fetch, so no job metadata or log of an excluded
    run is requested. A run with no head repository (deleted fork) is excluded.
    """
    kept = [
        run
        for run in runs
        if (run.get("head_repository") or {}).get("full_name") in TRUSTED_HEAD_REPOS
    ]
    return TrustedRuns(kept, len(runs) - len(kept))


def warn_on_time_gap(runs: list[dict[str, Any]]) -> None:
    """Warn when the snapshot's runs aren't one contiguous stretch of time.

    The runs endpoint occasionally serves a stale page, so pages that should
    be adjacent aren't, and the snapshot silently ends up with a multi-week
    hole in the middle — which skews every median and (if the older clump
    predates a CI change) can drop the pytest --durations data entirely.
    Re-running the collector gets a clean window.
    """
    starts = [t for r in runs if (t := parse_ts(r["run_started_at"]))]
    gaps = [(a - b).total_seconds() / 3600 for a, b in zip(starts, starts[1:])]
    if gaps and max(gaps) > 12:
        span = (starts[0] - starts[-1]).total_seconds() / 3600
        print(
            f"WARNING: {max(gaps):.0f}h gap inside the {span:.0f}h run window "
            f"({starts[-1].isoformat()} .. {starts[0].isoformat()}) — the window "
            "may have missing observations or a quiet period; compare windows carefully.",
            file=sys.stderr,
        )


def job_record(run: dict[str, Any], job: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": job["name"],
        "conclusion": job["conclusion"],
        "started_at": job["started_at"],
        "completed_at": job["completed_at"],
        "exec_seconds": seconds_between(job["started_at"], job["completed_at"]),
        # Wait from run start to job start. Only a true queue time for jobs
        # with no `needs`; for dependent jobs the analyze phase must subtract
        # predecessor completion (dependency map comes from the workflow files).
        "wait_from_run_start_seconds": seconds_between(
            run["run_started_at"], job["started_at"]
        ),
        # Per-step timings: job-level numbers hide which step costs what
        # (checkout vs install vs the actual work) and hide high-variance
        # steps whose median looks fine (e.g. full-pack git fetches that
        # take 30s or 4min depending on server pack-cache luck).
        "steps": [
            {
                "name": step["name"],
                "conclusion": step.get("conclusion"),
                "seconds": seconds_between(step["started_at"], step["completed_at"]),
            }
            for step in job.get("steps", [])
        ],
        "id": job["id"],
    }


class Duration(NamedTuple):
    seconds: float
    phase: str
    test: str


# pytest --durations lines, tolerating the Actions log timestamp prefix:
# "2026-08-04T19:40:01.123Z 12.34s call     tests/test_foo.py::test_bar"
DURATION_LINE = re.compile(
    r"(?:\S+ )?(\d+(?:\.\d+)?)s\s+(call|setup|teardown)\s+(\S+::\S+)\s*$"
)


def parse_durations(log_text: str) -> list[Duration]:
    return [
        Duration(float(m.group(1)), m.group(2), m.group(3))
        for line in log_text.splitlines()
        if (m := DURATION_LINE.match(line.strip()))
    ]


# pytest final summary line, e.g.
# "== 1234 passed, 56 skipped, 7 deselected, 2 xfailed in 412.34s =="
SUMMARY_SECONDS = re.compile(r"=+.* in (\d+(?:\.\d+)?)s(?: \([^)]*\))? =+\s*$")
SUMMARY_COUNT = re.compile(
    r"(\d+) (passed|failed|skipped|deselected|xfailed|xpassed|errors?|warnings?)"
)
# CI runs pytest with --color=yes, so the summary line (unlike --durations
# lines) carries ANSI escapes that break both regexes unless stripped.
ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*m")


def parse_summary(log_text: str) -> dict[str, Any] | None:
    """Outcome counts + total wall seconds from pytest's final summary line.

    The counts track suite size over time — distinct from --durations, which
    only sees the slow tail.
    """
    for raw in reversed(log_text.splitlines()):
        line = ANSI_ESCAPE.sub("", raw)
        if m := SUMMARY_SECONDS.search(line):
            counts = {
                # "1 error"/"2 errors" (and warning/warnings) would otherwise
                # produce different keys across runs, breaking aggregation.
                (kind.removesuffix("s") if kind in ("errors", "warnings") else kind): (
                    int(n)
                )
                for n, kind in SUMMARY_COUNT.findall(line)
            }
            if counts:
                return {**counts, "seconds": float(m.group(1))}
    return None


class TestLogData(NamedTuple):
    durations: dict[str, list[dict[str, Any]]]
    summaries: dict[str, dict[str, Any]]


def mine_test_logs(repo: str, runs: list[dict[str, Any]], max_runs: int) -> TestLogData:
    """Parse pytest --durations and summary lines from recent Build test-job logs.

    Durations come back empty silently when the flag isn't in CI yet — the
    skill's first proposed safe fix is adding it.
    """
    durations: dict[str, list[dict[str, Any]]] = {}
    summaries: dict[str, dict[str, Any]] = {}
    build_runs = [
        r for r in runs if r["name"] == "Build" and r["conclusion"] == "success"
    ][:max_runs]
    for run in build_runs:
        for job in run["jobs"]:
            if not job["name"].startswith("test") or job["conclusion"] != "success":
                continue
            try:
                log = gh_api_text(f"repos/{repo}/actions/jobs/{job['id']}/logs")
            except subprocess.CalledProcessError as error:
                print(
                    f"WARNING: unavailable test log {job['id']}: {error.stderr}",
                    file=sys.stderr,
                )
                continue  # logs expire after 90 days / may 404
            key = f"{run['id']}/{job['name']}"
            if parsed := parse_durations(log):
                durations[key] = [d._asdict() for d in parsed]
            if summary := parse_summary(log):
                summaries[key] = summary
    return TestLogData(durations, summaries)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default="UKGovernmentBEIS/inspect_ai")
    parser.add_argument("--limit", type=int, default=200, help="max runs to fetch")
    parser.add_argument(
        "--days", type=int, default=7, help="maximum run creation age in days"
    )
    parser.add_argument(
        "--since",
        type=since_arg,
        help="ISO-8601 UTC timestamp; fetch only runs created at or after it. "
        "--limit and --days still cap the window. Filters on creation time, so "
        "pass a value earlier than the previous window's end by at least the "
        "longest run time or runs still in flight at that collection are lost",
    )
    parser.add_argument(
        "--previous-summaries",
        type=Path,
        help="retained summaries JSON list (previous-summaries.json); records "
        "the latest window end so the summary can count new vs overlapping runs",
    )
    parser.add_argument(
        "--durations-runs",
        type=int,
        default=10,
        help="how many recent Build runs to mine for pytest --durations (0 to skip)",
    )
    parser.add_argument("--out", type=Path, required=True, help="snapshot JSON path")
    parser.add_argument("--summary-out", type=Path, help="compact aggregate JSON path")
    args = parser.parse_args()
    if any((parent / ".git").exists() for parent in args.out.resolve().parents):
        parser.error(
            "Raw snapshots must be written outside the repository, e.g. under /tmp"
        )
    if args.limit <= 0 or args.days <= 0 or args.durations_runs < 0:
        parser.error(
            "--limit and --days must be positive; --durations-runs must be nonnegative"
        )
    if args.since is not None and args.since >= datetime.now(timezone.utc):
        parser.error(f"--since {args.since.isoformat()} is not in the past")
    previous_end = None
    if args.previous_summaries is not None:
        if not args.previous_summaries.is_file():
            parser.error(
                f"--previous-summaries {args.previous_summaries} does not exist; "
                "run publish_ci_findings.py --read-history first"
            )
        previous_end = previous_window_end(
            json.loads(args.previous_summaries.read_text()), args.repo
        )

    fetched = fetch_runs(args.repo, args.limit, args.days, args.since)
    raw_runs, excluded_untrusted = trusted_runs(fetched)
    if not raw_runs:
        sys.exit(
            f"ERROR: no completed PR runs from trusted head repositories in the "
            f"requested window ({len(fetched)} fetched); nothing to snapshot"
        )
    print(
        f"fetched {len(fetched)} runs; excluded {excluded_untrusted} from "
        "untrusted head repositories; fetching jobs...",
        file=sys.stderr,
    )
    if previous_end is not None:
        overlap = window_overlap(raw_runs, previous_end)
        print(
            f"{overlap.new_runs} of {len(raw_runs)} runs started after the previous "
            f"window end {previous_end}; {overlap.overlap_runs} overlap it",
            file=sys.stderr,
        )

    runs = [
        {
            "id": run["id"],
            "name": run["name"],
            "head_branch": run["head_branch"],
            "conclusion": run["conclusion"],
            "run_attempt": run["run_attempt"],
            "run_started_at": run["run_started_at"],
            "updated_at": run["updated_at"],
            "wall_seconds": seconds_between(run["run_started_at"], run["updated_at"]),
            "jobs": [
                job_record(run, job)
                for job in gh_api(
                    f"repos/{args.repo}/actions/runs/{run['id']}/jobs?per_page=100"
                )["jobs"]
            ],
        }
        for run in raw_runs
    ]

    log_data = (
        mine_test_logs(args.repo, runs, args.durations_runs)
        if args.durations_runs
        else TestLogData({}, {})
    )
    snapshot = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "repo": args.repo,
        "run_count": len(runs),
        "excluded_untrusted_runs": excluded_untrusted,
        # None when no retained history was supplied; the summary then reports
        # new and overlapping run counts as unknown rather than zero.
        "previous_window_end": previous_end,
        "runs": runs,
        "pytest_durations": log_data.durations,
        "pytest_summaries": log_data.summaries,
    }

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(snapshot, indent=1))
    if args.summary_out:
        args.summary_out.parent.mkdir(parents=True, exist_ok=True)
        args.summary_out.write_text(
            json.dumps(summarize(snapshot), separators=(",", ":")) + "\n"
        )
    print(f"wrote {args.out}", file=sys.stderr)


if __name__ == "__main__":
    main()
