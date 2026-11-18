import re
import textwrap
import time

import pytest
from test_helpers.utils import simple_task_state

from inspect_ai.scorer import CORRECT, INCORRECT, AnswerPattern, Target, answer


@pytest.mark.anyio
async def test_letter_success():
    scorer = answer("letter")
    state = simple_task_state(model_output="ANSWER: B")
    result = await scorer(state, Target(["B"]))

    assert result.text == CORRECT


@pytest.mark.anyio
async def test_letter_failure():
    scorer = answer("letter")
    state = simple_task_state(model_output="ANSWER: B")
    result = await scorer(state, Target(["C"]))

    assert result.text == INCORRECT


@pytest.mark.anyio
async def test_word_success():
    scorer = answer("word")
    state = simple_task_state(model_output="ANSWER: Yes")
    result = await scorer(state, Target(["Yes"]))

    assert result.text == CORRECT


@pytest.mark.anyio
async def test_word_failure():
    scorer = answer("letter")
    state = simple_task_state(model_output="ANSWER: Yes")
    result = await scorer(state, Target(["No"]))

    assert result.text == INCORRECT
    assert result.reason == "invalid_response_format"


@pytest.mark.anyio
@pytest.mark.parametrize(
    "model_output,target",
    [
        ("ANSWER: ☆", "☆"),
        ("ANSWER: ○", "○"),
        ("ANSWER: ◎", "◎"),
        ("ANSWER: Yes.", "Yes"),
        ("ANSWER: 42,\n", "42"),
        ("ANSWER: correct!", "correct"),
        ("ANSWER: 3.14", "3.14"),
    ],
)
async def test_word_matching(model_output: str, target: str):
    scorer = answer("word")
    state = simple_task_state(model_output=model_output)
    result = await scorer(state, Target([target]))

    assert result is not None
    assert result.text == CORRECT


@pytest.mark.anyio
@pytest.mark.parametrize(
    "model_output,target",
    [
        ("ANSWER: Yes then more text", "Yes"),
        ("ANSWER: No, because reasons", "No"),
    ],
)
async def test_word_trailing_prose_is_format_violation(model_output: str, target: str):
    scorer = answer("word")
    state = simple_task_state(model_output=model_output)
    result = await scorer(state, Target([target]))

    assert result is not None and result.text == INCORRECT
    assert result.reason == "invalid_response_format"


@pytest.mark.anyio
async def test_line_success():
    scorer = answer("line")
    state = simple_task_state(model_output="ANSWER:\nThis is a whole new line")
    result = await scorer(state, Target(["This is a whole new line"]))

    assert result.text == CORRECT


@pytest.mark.anyio
async def test_line_failure():
    scorer = answer("line")
    state = simple_task_state(model_output="ANSWER:\nThis is a whole new line")
    result = await scorer(state, Target(["This doesn't match does it?"]))

    assert result.text == INCORRECT


@pytest.mark.anyio
async def test_letter_last_occurrence_wins():
    # Models sometimes self-correct ("ANSWER: A ... wait, ANSWER: B"). The
    # final ANSWER: line should be the one that is scored, consistent with
    # the behaviour of answer("line").
    scorer = answer("letter")
    state = simple_task_state(
        model_output="Let me think. ANSWER: A\nWait, that is wrong.\nANSWER: B"
    )
    result = await scorer(state, Target(["B"]))

    assert result.answer == "B"
    assert result.text == CORRECT


@pytest.mark.anyio
async def test_word_last_occurrence_wins():
    scorer = answer("word")
    state = simple_task_state(
        model_output="ANSWER: No\nActually, on reflection...\nANSWER: Yes"
    )
    result = await scorer(state, Target(["Yes"]))

    assert result.answer == "Yes"
    assert result.text == CORRECT


@pytest.mark.anyio
async def test_line_multiple_matches():
    scorer = answer("line")
    state = simple_task_state(
        model_output=textwrap.dedent("""\
            What if I submitted this as the answer: some stuff we don't want to be the answer

            No, that seems wrong.

            ANSWER: 0.1
        """)
    )
    result = await scorer(state, Target(["0.1"]))

    assert result.text == CORRECT


@pytest.mark.parametrize(
    "model_output,expected",
    [
        ("ANSWER:\n\n  42\n", "42"),
        ("ANSWER: 42  \n\n", "42  "),
        ("ANSWER: a ANSWER: b", "a ANSWER: b"),
        ("ANSWER: a\nANSWER: b\n", "b"),
        ("ANSWER: \t\n\n", "\t"),
        ("ANSWER:\n\n", None),
        ("ANSWER: a\nmore", None),
    ],
)
def test_line_pattern_captures(model_output: str, expected: str | None) -> None:
    match = re.search(AnswerPattern.LINE, model_output)
    assert (match.group(1) if match else None) == expected


@pytest.mark.parametrize(
    "model_output",
    [
        pytest.param("ANSWER:" + " " * 100_000 + "\nx\ny", id="spaces"),
        pytest.param("ANSWER:" + " \t" * 50_000 + "\nx\ny", id="tabs"),
        pytest.param("ANSWER: x" + " " * 100_000 + "\nx\ny", id="after-answer"),
        pytest.param(("ANSWER:" + " " * 1_000 + "\nx\n") * 100 + "y", id="repeated"),
        pytest.param("ANSWER:" * 15_000 + "\nx\ny", id="markers-on-one-line"),
    ],
)
def test_line_pattern_is_linear(model_output: str) -> None:
    # The previous pattern took ~0.6 s for 800 spaces (cubic) and ~16 s for
    # 15,000 markers on one line (quadratic).
    started = time.perf_counter()
    assert re.search(AnswerPattern.LINE, model_output) is None
    assert time.perf_counter() - started < 2.0
