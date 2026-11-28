"""Tests for the fork sync workflow (.github/workflows/sync-upstream.yml).

The workflow pushes to the fork's protected branches as a GitHub App, so
these tests pin the shape that keeps that credential contained (read-only
job token, environment-bound job, no persisted checkout credential, a
step-scoped credential helper keyed to the server URL) and run the sync
step's script against temporary local repositories to check the branch
updates it performs and how it fails on a conflict.
"""

import os
import pathlib
import re
import subprocess
from typing import Any

import pytest
import yaml

WORKFLOW = (
    pathlib.Path(__file__).parents[1] / ".github" / "workflows" / "sync-upstream.yml"
)
SYNC_STEP_NAME = "Fast-forward main and merge into meridian"
MINT_STEP_ID = "mint"
UPSTREAM_URL = "https://github.com/UKGovernmentBEIS/inspect_ai.git"
SERVER_URL = "https://github.com"
DUMMY_TOKEN = "ghs_dummy_installation_token_for_tests"

# The step-scoped credential helper block shared with the agent workflows in
# meridianlabs-ai/agents: one variable name, and a helper keyed to the server
# URL rather than the generic `credential.helper` (which is reset to nothing).
HELPER_ENV = {
    "GIT_CONFIG_COUNT": "2",
    "GIT_CONFIG_KEY_0": "credential.helper",
    "GIT_CONFIG_VALUE_0": "",
    "GIT_CONFIG_KEY_1": "credential.${{ github.server_url }}.helper",
    "GIT_CONFIG_VALUE_1": '!f() { echo username=x-access-token; echo "password=$GIT_TOKEN"; }; f',
}

EXPRESSIONS = {
    "${{ github.server_url }}": SERVER_URL,
    "${{ steps.mint.outputs.token }}": DUMMY_TOKEN,
}


def workflow() -> dict[Any, Any]:
    # `dict[Any, Any]`: PyYAML reads the bare `on` key as the boolean True.
    return yaml.safe_load(WORKFLOW.read_text())


def sync_job() -> dict[str, Any]:
    jobs = workflow()["jobs"]
    assert list(jobs) == ["sync"]
    return jobs["sync"]


def step_named(name: str) -> dict[str, Any]:
    matching = [s for s in sync_job()["steps"] if s.get("name") == name]
    assert len(matching) == 1, f"expected exactly one step named {name!r}"
    return matching[0]


def step_with_id(step_id: str) -> dict[str, Any]:
    matching = [s for s in sync_job()["steps"] if s.get("id") == step_id]
    assert len(matching) == 1, f"expected exactly one step with id {step_id!r}"
    return matching[0]


def render(value: str) -> str:
    """Substitute the expressions the runner would, and refuse any other."""
    for expression, replacement in EXPRESSIONS.items():
        value = value.replace(expression, replacement)
    assert "${{" not in value, f"unexpected expression in {value!r}"
    return value


# --- workflow shape ---


def test_job_token_is_read_only() -> None:
    assert workflow()["permissions"] == {"contents": "read"}
    assert "permissions" not in sync_job()


def test_job_is_bound_to_the_sync_environment_and_trusted_ref() -> None:
    job = sync_job()
    assert job["environment"] == "upstream-sync"
    condition = job["if"]
    assert "github.repository == 'meridianlabs-ai/inspect_ai'" in condition
    assert "github.ref == 'refs/heads/meridian'" in condition
    assert " && " in condition and "||" not in condition


def test_triggers_are_schedule_and_manual_dispatch() -> None:
    on = workflow()[True]
    assert set(on) == {"schedule", "workflow_dispatch"}
    assert on["schedule"] == [{"cron": "17 * * * *"}]


def test_checkout_persists_no_credential() -> None:
    steps = sync_job()["steps"]
    checkout = steps[0]
    assert checkout["uses"].startswith("actions/checkout@")
    assert checkout["with"] == {
        "ref": "meridian",
        "fetch-depth": 0,
        "persist-credentials": False,
    }


def test_mint_step_scopes_the_app_token() -> None:
    mint = step_with_id(MINT_STEP_ID)
    assert mint["uses"] == "actions/create-github-app-token@v2"
    assert mint["with"] == {
        "app-id": "${{ vars.SYNC_APP_ID }}",
        "private-key": "${{ secrets.SYNC_APP_PRIVATE_KEY }}",
        "owner": "meridianlabs-ai",
        "repositories": "inspect_ai",
        "permission-contents": "write",
        "permission-workflows": "write",
    }
    # A failed mint fails the run: no `continue-on-error`, no `if`, and the
    # token is revoked when the job ends (`skip-token-revoke` unset).
    assert set(mint) == {"name", "id", "uses", "with"}


def test_no_other_privileged_identity() -> None:
    text = WORKFLOW.read_text()
    assert "SYNC_TOKEN" not in text
    assert set(re.findall(r"secrets\.([A-Za-z0-9_]+)", text)) == {
        "SYNC_APP_PRIVATE_KEY",
        "SLACK_WEBHOOK_URL",
    }
    steps = sync_job()["steps"]
    with_token = [s for s in steps if "GIT_TOKEN" in s.get("env", {})]
    assert [s["name"] for s in with_token] == [SYNC_STEP_NAME]


def test_sync_step_authenticates_through_step_scoped_helper() -> None:
    steps = sync_job()["steps"]
    names = [s.get("id") or s.get("name") for s in steps]
    assert names.index(MINT_STEP_ID) < names.index(SYNC_STEP_NAME)
    step = step_named(SYNC_STEP_NAME)
    assert step["env"] == {
        "GIT_TOKEN": "${{ steps.mint.outputs.token }}",
        **HELPER_ENV,
    }
    script = step["run"]
    assert "${{" not in script, "the script must not interpolate expressions"
    assert UPSTREAM_URL in script
    assert "x-access-token" not in script and "extraheader" not in script
    pushes = re.findall(r"^\s*git push.*$", script, flags=re.MULTILINE)
    assert pushes == [
        "git push origin refs/remotes/upstream/main:refs/heads/main",
        "git push origin meridian",
    ], "both pushes stay plain non-force ref updates"


# --- credential helper ---


def helper_env(tmp_path: pathlib.Path) -> dict[str, str]:
    """Environment of the sync step as the runner would set it.

    Adds an isolated HOME and a global config whose own helper must be
    ignored.
    """
    global_config = tmp_path / "gitconfig"
    global_config.write_text(
        "[credential]\n\thelper = !echo username=wrong; echo password=wrong\n"
    )
    env = {
        "PATH": os.environ["PATH"],
        "HOME": str(tmp_path),
        "GIT_CONFIG_GLOBAL": str(global_config),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_ASKPASS": "",
    }
    for key, value in step_named(SYNC_STEP_NAME)["env"].items():
        env[key] = render(value)
    return env


def credential_fill(
    host: str, tmp_path: pathlib.Path
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "credential", "fill"],
        input=f"protocol=https\nhost={host}\n\n",
        env=helper_env(tmp_path),
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_helper_answers_the_server_url_with_the_app_token(
    tmp_path: pathlib.Path,
) -> None:
    result = credential_fill("github.com", tmp_path)
    assert result.returncode == 0, result.stderr
    assert "username=x-access-token\n" in result.stdout
    assert f"password={DUMMY_TOKEN}\n" in result.stdout
    assert "wrong" not in result.stdout, "the global helper must be reset"


def test_helper_answers_no_other_host(tmp_path: pathlib.Path) -> None:
    result = credential_fill("example.com", tmp_path)
    assert result.returncode != 0
    assert DUMMY_TOKEN not in result.stdout + result.stderr
    assert "wrong" not in result.stdout


# --- the sync script against local repositories ---


def git(*args: str, cwd: pathlib.Path, env: dict[str, str]) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def commit(
    repo: pathlib.Path, env: dict[str, str], filename: str, content: str, message: str
) -> str:
    (repo / filename).write_text(content)
    git("add", filename, cwd=repo, env=env)
    git("commit", "-q", "-m", message, cwd=repo, env=env)
    return git("rev-parse", "HEAD", cwd=repo, env=env)


class Fixture:
    """Bare `origin` (the fork) and `upstream` repositories plus a work clone.

    Laid out as the workflow finds them: the clone is on `meridian`, `main`
    is the mirror of upstream, `meridian` carries one fork-only commit.
    """

    def __init__(self, tmp_path: pathlib.Path) -> None:
        self.env = {
            "PATH": os.environ["PATH"],
            "HOME": str(tmp_path),
            "GIT_CONFIG_GLOBAL": str(tmp_path / "gitconfig"),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@example.com",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@example.com",
        }
        (tmp_path / "gitconfig").write_text("")
        self.origin = tmp_path / "origin.git"
        self.upstream = tmp_path / "upstream.git"
        self.work = tmp_path / "work"
        seed = tmp_path / "seed"
        git(
            "init",
            "-q",
            "--bare",
            "-b",
            "main",
            str(self.origin),
            cwd=tmp_path,
            env=self.env,
        )
        git(
            "init",
            "-q",
            "--bare",
            "-b",
            "main",
            str(self.upstream),
            cwd=tmp_path,
            env=self.env,
        )
        git("init", "-q", "-b", "main", str(seed), cwd=tmp_path, env=self.env)
        self.base = commit(seed, self.env, "shared.txt", "v1\n", "upstream base")
        for remote in (self.origin, self.upstream):
            git("push", "-q", str(remote), "main:main", cwd=seed, env=self.env)
        git("checkout", "-q", "-b", "meridian", cwd=seed, env=self.env)
        self.meridian_only = commit(
            seed, self.env, "meridian.txt", "fork only\n", "meridian-only change"
        )
        git("push", "-q", str(self.origin), "meridian:meridian", cwd=seed, env=self.env)
        git(
            "clone",
            "-q",
            "-b",
            "meridian",
            str(self.origin),
            str(self.work),
            cwd=tmp_path,
            env=self.env,
        )

    def advance_upstream(self, filename: str, content: str) -> str:
        """Add a commit on upstream main and return its SHA."""
        scratch = self.upstream.parent / "upstream-work"
        git(
            "clone",
            "-q",
            "-b",
            "main",
            str(self.upstream),
            str(scratch),
            cwd=self.upstream.parent,
            env=self.env,
        )
        sha = commit(scratch, self.env, filename, content, f"upstream {filename}")
        git("push", "-q", str(self.upstream), "main:main", cwd=scratch, env=self.env)
        return sha

    def advance_origin_main(self) -> str:
        """Put a commit on fork main that upstream does not have."""
        scratch = self.origin.parent / "origin-main-work"
        git(
            "clone",
            "-q",
            "-b",
            "main",
            str(self.origin),
            str(scratch),
            cwd=self.origin.parent,
            env=self.env,
        )
        sha = commit(
            scratch, self.env, "stray.txt", "not a mirror\n", "stray fork commit"
        )
        git("push", "-q", str(self.origin), "main:main", cwd=scratch, env=self.env)
        return sha

    def ref(self, remote: pathlib.Path, branch: str) -> str:
        return git("rev-parse", f"refs/heads/{branch}", cwd=remote, env=self.env)

    def run_sync_step(self) -> subprocess.CompletedProcess[str]:
        """Run the sync step's script as the runner does (`bash -e`).

        The step's env is rendered and the hard-coded upstream URL redirected
        to the local bare repository.
        """
        step = step_named(SYNC_STEP_NAME)
        script = self.work.parent / "step.sh"
        script.write_text(step["run"])
        env = dict(self.env)
        for key, value in step["env"].items():
            env[key] = render(value)
        count = int(env["GIT_CONFIG_COUNT"])
        env[f"GIT_CONFIG_KEY_{count}"] = f"url.{self.upstream}.insteadOf"
        env[f"GIT_CONFIG_VALUE_{count}"] = UPSTREAM_URL
        env["GIT_CONFIG_COUNT"] = str(count + 1)
        return subprocess.run(
            ["bash", "--noprofile", "--norc", "-e", str(script)],
            env=env,
            cwd=self.work,
            capture_output=True,
            text=True,
            timeout=120,
        )


@pytest.fixture
def fixture(tmp_path: pathlib.Path) -> Fixture:
    return Fixture(tmp_path)


def test_sync_fast_forwards_main_and_merges_meridian(fixture: Fixture) -> None:
    upstream_tip = fixture.advance_upstream("upstream.txt", "new upstream work\n")

    result = fixture.run_sync_step()

    assert result.returncode == 0, result.stderr
    assert fixture.ref(fixture.origin, "main") == upstream_tip
    merged = fixture.ref(fixture.origin, "meridian")
    parents = git(
        "rev-list", "--parents", "-n", "1", merged, cwd=fixture.origin, env=fixture.env
    ).split()[1:]
    assert set(parents) == {fixture.meridian_only, upstream_tip}
    assert fixture.ref(fixture.upstream, "main") == upstream_tip, (
        "upstream is never pushed"
    )
    # The token is used only through the helper: nothing on disk or in the
    # output carries it.
    assert DUMMY_TOKEN not in (fixture.work / ".git" / "config").read_text()
    assert DUMMY_TOKEN not in result.stdout + result.stderr


def test_sync_conflict_fails_after_mirroring_main(fixture: Fixture) -> None:
    upstream_tip = fixture.advance_upstream("meridian.txt", "upstream wrote this too\n")

    result = fixture.run_sync_step()

    assert result.returncode != 0
    assert "CONFLICT" in result.stdout + result.stderr
    assert fixture.ref(fixture.origin, "main") == upstream_tip, (
        "main mirrors upstream first"
    )
    assert fixture.ref(fixture.origin, "meridian") == fixture.meridian_only, (
        "a conflicting merge is never pushed"
    )


def test_sync_refuses_a_non_fast_forward_main(fixture: Fixture) -> None:
    stray = fixture.advance_origin_main()
    fixture.advance_upstream("upstream.txt", "new upstream work\n")

    result = fixture.run_sync_step()

    assert result.returncode != 0
    assert "non-fast-forward" in result.stderr or "rejected" in result.stderr
    assert fixture.ref(fixture.origin, "main") == stray, "main is never force-pushed"
    assert fixture.ref(fixture.origin, "meridian") == fixture.meridian_only
