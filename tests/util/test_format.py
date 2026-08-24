"""Tests for format_progress_time() and format_duration_compact()."""

import pytest

from inspect_ai._util.format import format_duration_compact, format_progress_time


def test_progress_time_whole_seconds() -> None:
    assert format_progress_time(0.0) == " 0:00:00"
    assert format_progress_time(5.0) == " 0:00:05"
    assert format_progress_time(60.0) == " 0:01:00"
    assert format_progress_time(3600.0) == " 1:00:00"
    assert format_progress_time(3661.0) == " 1:01:01"


def test_progress_time_truncates_fractional_seconds() -> None:
    # A fractional component must be truncated, not rounded: the ".0f" spec used
    # to round 59.9 up to ":60", which a clock can never legitimately show.
    assert format_progress_time(59.9) == " 0:00:59"
    assert format_progress_time(3599.9) == " 0:59:59"
    assert format_progress_time(3659.9) == " 1:00:59"


def test_progress_time_no_pad_hours() -> None:
    assert format_progress_time(0.0, pad_hours=False) == "0:00:00"
    assert format_progress_time(59.9, pad_hours=False) == "0:00:59"
    assert format_progress_time(3661.0, pad_hours=False) == "1:01:01"


def test_progress_time_hours_not_truncated_to_two_digits() -> None:
    assert format_progress_time(36000.0) == "10:00:00"


@pytest.mark.parametrize(
    "seconds,expected",
    [
        # seconds, up to the minute boundary
        (0, "0s"),
        (1, "1s"),
        (59, "59s"),
        # minutes, suppressing a zero seconds component
        (60, "1m"),
        (90, "1m30s"),
        (3599, "59m59s"),
        # hours, suppressing a zero minutes component
        (3600, "1h"),
        (3660, "1h1m"),
        (86399, "23h59m"),
        # days, suppressing a zero hours component
        (86400, "1d"),
        (90000, "1d1h"),
        (172800, "2d"),
        # fractional input truncates rather than rounds
        (59.9, "59s"),
        # negatives (an already-passed deadline) clamp
        (-1, "0s"),
    ],
)
def test_duration_compact(seconds: float, expected: str) -> None:
    assert format_duration_compact(seconds) == expected
