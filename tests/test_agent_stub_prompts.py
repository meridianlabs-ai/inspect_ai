"""Tests for the fork guidance the agent stubs pass to the reusable workflows.

Upstream squash-merges with the branch's commit messages, and promotion
refuses a branch whose commit messages carry a bare `#N`, so every stub whose
agent writes commits must tell it to qualify issue and PR references there.
The prompts in claude-auto.yml are separate copies of the rule, so these tests
keep all of them saying it.
"""

import pathlib
from typing import Any

import pytest
import yaml

WORKFLOWS = pathlib.Path(__file__).parents[1] / ".github" / "workflows"

AGENT_JOBS = [
    ("claude.yml", "claude"),
    ("claude.yml", "claude-auto"),
    ("claude-auto.yml", "ci-fix"),
    ("claude-auto.yml", "review-fix"),
]


def fork_prompt(workflow: str, job: str) -> str:
    data: dict[Any, Any] = yaml.safe_load((WORKFLOWS / workflow).read_text())
    prompt = data["jobs"][job]["with"]["append_system_prompt"]
    assert isinstance(prompt, str)
    return prompt


@pytest.mark.parametrize("workflow,job", AGENT_JOBS)
def test_prompt_qualifies_references_in_commit_messages(
    workflow: str, job: str
) -> None:
    prompt = fork_prompt(workflow, job)
    assert "every commit subject and body you write" in prompt
    assert "`meridianlabs-ai/inspect_ai#N`, never a bare `#N`" in prompt
    assert '"from issue meridianlabs-ai/inspect_ai#N"' in prompt
    assert "promotion refuses a branch" in prompt
