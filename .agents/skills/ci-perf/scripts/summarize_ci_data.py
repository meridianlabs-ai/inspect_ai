#!/usr/bin/env python3
"""Reduce a CI snapshot to aggregate trends without retaining individual runs."""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from statistics import median
from typing import Any, NamedTuple


def parse_ts(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def window_hours(start: str, end: str) -> float:
    """Span between two ISO-8601 run start timestamps, in hours.

    A fixed run count covers a variable stretch of time, so two summaries
    cannot be weighed against each other without their spans.
    """
    return round((parse_ts(end) - parse_ts(start)).total_seconds() / 3600, 2)


def previous_window_end(summaries: list[dict[str, Any]], repo: str) -> str | None:
    """Latest `window.end` among retained summaries of `repo`, or None.

    The tracking issue stores summaries in posting order, but a re-run or a
    manual dispatch can post an older window after a newer one, so take the
    maximum rather than the last entry. A dispatch against another repository
    shares the tracking issue, so its windows are ignored.
    """
    ends = [s["window"]["end"] for s in summaries if s.get("repo") == repo]
    return max(ends, key=parse_ts) if ends else None


class WindowOverlap(NamedTuple):
    new_runs: int
    overlap_runs: int


def window_overlap(runs: list[dict[str, Any]], previous_end: str) -> WindowOverlap:
    """Split runs into those after the previous window's end and the rest.

    A run that started at or before the previous window's end was already
    available to the previous snapshot, so its timings re-measure the same
    sample. Only runs that started later add information. The fetch cap binds
    backwards from collection time, so a quiet upstream makes most of a fixed
    run count overlap the previous window while every other count in the
    summary looks normal.
    """
    cutoff = parse_ts(previous_end)
    new = sum(parse_ts(run["run_started_at"]) > cutoff for run in runs)
    return WindowOverlap(new, len(runs) - new)


def stats(values: list[float]) -> dict[str, float | int] | None:
    """Return sample count, median, and linearly interpolated p90."""
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * 0.9
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    return {
        "n": len(values),
        "median": round(median(values), 2),
        "p90": round(
            ordered[lower] + (position - lower) * (ordered[upper] - ordered[lower]), 2
        ),
    }


def summarize(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Summarize successful timings and observed pytest logs by job.

    Wait-from-run-start includes dependency time, so it is not labeled queue
    time. Workflow wall uses the collector's updated_at proxy, not push time.
    Duration samples include only tests printed by pytest's slow-tail filter.
    When the snapshot records `previous_window_end`, the window carries the
    split between runs the previous snapshot could already have analyzed and
    runs that are new; otherwise those counts are None, not zero.
    """
    runs = snapshot["runs"]
    if not runs:
        raise ValueError("Cannot summarize an empty CI window")
    workflows: dict[str, list[float]] = defaultdict(list)
    jobs: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    steps: dict[str, list[float]] = defaultdict(list)
    conclusions: dict[str, int] = defaultdict(int)
    runner_seconds = 0.0
    unavailable_jobs = 0
    unavailable_cancelled_jobs = 0
    cancelled_seconds = 0.0
    excluded = {"workflow_wall": 0, "job_wait": 0, "step": 0}
    for run in runs:
        conclusions[run["conclusion"]] += 1
        if run["conclusion"] == "success":
            if run["wall_seconds"] is None or run["wall_seconds"] < 0:
                excluded["workflow_wall"] += 1
            else:
                workflows[run["name"]].append(run["wall_seconds"])
        for job in run["jobs"]:
            if job["conclusion"] == "skipped":
                continue
            seconds = job["exec_seconds"]
            if seconds is None or seconds < 0:
                unavailable_jobs += 1
                if run["conclusion"] == "cancelled":
                    unavailable_cancelled_jobs += 1
                continue
            runner_seconds += seconds
            if run["conclusion"] == "cancelled":
                cancelled_seconds += seconds
            if job["conclusion"] != "success":
                continue
            key = f"{run['name']} / {job['name']}"
            jobs[key]["exec_seconds"].append(seconds)
            wait = job["wait_from_run_start_seconds"]
            if wait is None or wait < 0:
                excluded["job_wait"] += 1
            else:
                jobs[key]["wait_from_run_start_seconds"].append(wait)
            for step in job.get("steps", []):
                if step.get("conclusion") == "skipped":
                    continue
                if step["seconds"] is None or step["seconds"] < 0:
                    excluded["step"] += 1
                else:
                    steps[f"{key} / {step['name']}"].append(step["seconds"])

    suites: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    for key, summary in snapshot.get("pytest_summaries", {}).items():
        job_name = key.split("/", 1)[1]
        for field, value in summary.items():
            suites[job_name][field].append(value)

    tests: dict[str, list[float]] = defaultdict(list)
    for key, durations in snapshot.get("pytest_durations", {}).items():
        sample: dict[str, float] = defaultdict(float)
        for duration in durations:
            sample[duration["test"]] += duration["seconds"]
        job_name = key.split("/", 1)[1]
        for test, seconds in sample.items():
            tests[f"{job_name} / {test}"].append(seconds)

    step_stats = {
        key: value
        for key, samples in steps.items()
        if (value := stats(samples)) is not None
    }
    starts = sorted(run["run_started_at"] for run in runs)
    previous_end = snapshot.get("previous_window_end")
    overlap = window_overlap(runs, previous_end) if previous_end is not None else None
    return {
        "schema_version": 1,
        "generated_at": snapshot["generated_at"],
        "repo": snapshot["repo"],
        "window": {
            "start": starts[0],
            "end": starts[-1],
            "hours": window_hours(starts[0], starts[-1]),
            "runs": len(runs),
            # None means no previous window was recorded, not zero overlap.
            "previous_end": previous_end,
            "new_runs": overlap.new_runs if overlap is not None else None,
            "overlap_runs": overlap.overlap_runs if overlap is not None else None,
        },
        "conclusions": dict(conclusions),
        "runner_minutes": None if unavailable_jobs else round(runner_seconds / 60, 2),
        "unavailable_job_timings": unavailable_jobs,
        "excluded_timings": excluded,
        "cancelled_runner_minutes": None
        if unavailable_cancelled_jobs
        else round(cancelled_seconds / 60, 2),
        "workflow_wall_seconds": {
            key: stats(value) for key, value in sorted(workflows.items())
        },
        "jobs": {
            key: {field: stats(values) for field, values in fields.items()}
            for key, fields in sorted(jobs.items())
        },
        "suites": {
            key: {field: stats(values) for field, values in fields.items()}
            for key, fields in sorted(suites.items())
        },
        "slow_tests_seconds": {
            key: stats(tests[key])
            for key in sorted(tests, key=lambda key: median(tests[key]), reverse=True)[
                :15
            ]
        },
        "slow_steps_seconds": {
            key: step_stats[key]
            for key in sorted(
                step_stats, key=lambda key: step_stats[key]["p90"], reverse=True
            )[:15]
        },
    }


def overlap_line(window: dict[str, Any]) -> str:
    """State how much of the window the previous snapshot already covered.

    Summaries retained before these fields existed lack them entirely, so
    read them as unknown rather than failing.
    """
    if window.get("previous_end") is None:
        return "Previous window: none recorded, so overlap with earlier summaries is unknown."
    return (
        f"Previous window ended {window['previous_end']}: {window['new_runs']} runs "
        f"started after it and {window['overlap_runs']} were already available to "
        "the previous snapshot, so deltas against it re-measure those shared runs."
    )


def render(summary: dict[str, Any]) -> str:
    """Render the aggregate baseline so the report needs no raw snapshot."""
    lines = [
        "# CI performance measurements",
        "",
        f"Source: {summary['repo']}. Collected: {summary['generated_at']}.",
        f"Window: {summary['window']['start']} to {summary['window']['end']} ({summary['window'].get('hours')}h), {summary['window']['runs']} runs.",
        overlap_line(summary["window"]),
        f"Runner minutes: {summary['runner_minutes']}. Cancelled-run runner minutes: {summary['cancelled_runner_minutes']}. Missing or invalid job timings: {summary['unavailable_job_timings']}. None means unavailable, not zero.",
        "",
        f"Excluded missing or inverted observations: workflow wall {summary['excluded_timings']['workflow_wall']}, job wait {summary['excluded_timings']['job_wait']}, steps {summary['excluded_timings']['step']}.",
        "Successful runs and jobs only for timings. Workflow wall is run start to updated_at, not push-to-green. Job wait includes dependencies. Pytest counts are per outcome, not a count of unique tests across matrix jobs. Skipped steps are excluded when their status was collected. Slow-test totals include only printed phases.",
    ]
    for field, title in (
        ("workflow_wall_seconds", "Workflow wall seconds"),
        ("jobs", "Job seconds"),
        ("suites", "Pytest outcomes and seconds"),
        ("slow_tests_seconds", "Slow tests, observed seconds"),
        ("slow_steps_seconds", "Slow steps, seconds"),
    ):
        lines.extend(
            [
                "",
                f"## {title}",
                "",
                "| Metric | n | Median | p90 |",
                "| --- | ---: | ---: | ---: |",
            ]
        )
        for key, value in summary[field].items():
            metrics = (
                {key: value}
                if "n" in value
                else {f"{key} / {name}": item for name, item in value.items()}
            )
            for name, item in metrics.items():
                if item is not None:
                    name = name.replace("|", "\\|")
                    lines.append(
                        f"| {name} | {item['n']} | {item['median']} | {item['p90']} |"
                    )
        if not summary[field]:
            lines.append("| No observations | | | |")
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("snapshot", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--markdown", type=Path)
    args = parser.parse_args()
    summary = summarize(json.loads(args.snapshot.read_text()))
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(summary, separators=(",", ":")) + "\n")
    if args.markdown:
        args.markdown.write_text(render(summary))


if __name__ == "__main__":
    main()
