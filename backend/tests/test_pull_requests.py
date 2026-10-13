import subprocess
from pathlib import Path

import pytest

from agenthq.pull_requests import AUTHOR, GitHubPublisher, PublishError, parse_github_url
from agenthq.workspace import Workspace, WorkspaceError


@pytest.mark.parametrize("url", [
    "https://github.com/ShlokGhadekar/agenthq-sample-notes",
    "https://github.com/ShlokGhadekar/agenthq-sample-notes.git",
    "https://github.com/ShlokGhadekar/agenthq-sample-notes/",
    "git@github.com:ShlokGhadekar/agenthq-sample-notes.git",
])
def test_parse_github_url(url: str) -> None:
    assert parse_github_url(url) == ("ShlokGhadekar", "agenthq-sample-notes")


def test_non_github_url_is_rejected() -> None:
    with pytest.raises(PublishError):
        parse_github_url("https://gitlab.com/a/b")


async def test_commit_and_push_a_branch(sample_git_repo: Path, tmp_path: Path) -> None:
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "clone", "--quiet", "--bare", str(sample_git_repo), str(remote)], check=True)
    workspace = Workspace(tmp_path / "ws" / "run-1")
    await workspace.clone(str(remote))
    workspace.write("app/rate_limit.py", "LIMIT = 5\n")
    (workspace.root / ".pytest_cache").mkdir()  # created by test runs; must not be committed
    (workspace.root / ".pytest_cache" / "x").write_text("cache")

    await workspace.commit_all("agenthq/run-1", "Add rate limiting", AUTHOR)
    await workspace.push(str(remote), "agenthq/run-1")

    files = subprocess.run(["git", "ls-tree", "-r", "--name-only", "agenthq/run-1"], cwd=remote,
                           capture_output=True, text=True, check=True).stdout.split()
    assert "app/rate_limit.py" in files
    assert not any(f.startswith(".pytest_cache") for f in files)
    author = subprocess.run(["git", "log", "-1", "--format=%an <%ae>", "agenthq/run-1"], cwd=remote,
                            capture_output=True, text=True, check=True).stdout.strip()
    assert author == AUTHOR


async def test_token_never_appears_in_errors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    token = "ghp_secret123"

    async def failing_push(*args: object, **kwargs: object) -> None:
        raise WorkspaceError(f"fatal: could not read from https://x-access-token:{token}@github.com/a/b")

    async def no_op(*args: object, **kwargs: object) -> None:
        return None

    workspace = Workspace(tmp_path)
    monkeypatch.setattr(workspace, "commit_all", no_op)
    monkeypatch.setattr(workspace, "push", failing_push)
    with pytest.raises(PublishError) as raised:
        await GitHubPublisher(token).open(workspace, "https://github.com/a/b", "branch", "title", "body")
    assert token not in str(raised.value)


def test_token_goes_to_git_through_the_environment_only() -> None:
    env = GitHubPublisher("ghp_secret123")._git_auth_env()
    assert env["GIT_CONFIG_KEY_0"] == "http.https://github.com/.extraheader"
    assert "ghp_secret123" not in env["GIT_CONFIG_VALUE_0"]  # base64-encoded inside a header
