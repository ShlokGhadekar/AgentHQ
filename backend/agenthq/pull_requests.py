"""Open a pull request with a run's changes.

Only the runtime calls this, and only right after a human's approval_decision event (see
Run.decide_approval): agents have no tool that can push or open pull requests.
"""

import asyncio
import base64
import re
from typing import Protocol

from github import Auth, Github

from agenthq.workspace import Workspace

AUTHOR = "AgentHQ <agenthq@users.noreply.github.com>"


class PublishError(Exception):
    """Pushing the branch or opening the pull request failed."""


class PullRequestPublisher(Protocol):
    async def open(self, workspace: Workspace, repo_url: str, branch: str, title: str, body: str) -> str:
        """Push the workspace's changes to `branch` and open a pull request. Returns its URL."""
        ...


def parse_github_url(url: str) -> tuple[str, str]:
    """("owner", "repo") from https://github.com/owner/repo(.git) or git@github.com:owner/repo.git."""
    match = re.search(r"github\.com[/:]([^/]+)/([^/]+?)(?:\.git)?/?$", url.strip())
    if not match:
        raise PublishError(f"Not a GitHub repository URL: {url}")
    return match.group(1), match.group(2)


class GitHubPublisher:
    """Pushes with git and opens the pull request with PyGithub, using one token for both."""

    def __init__(self, token: str) -> None:
        self._token = token

    async def open(self, workspace: Workspace, repo_url: str, branch: str, title: str, body: str) -> str:
        owner, name = parse_github_url(repo_url)
        try:
            await workspace.commit_all(branch, title, AUTHOR)
            await workspace.push(f"https://github.com/{owner}/{name}.git", branch, env=self._git_auth_env())
            return await asyncio.to_thread(self._create_pull, owner, name, branch, title, body)
        except Exception as error:
            # Never let the token leak into an error message that ends up in the event log.
            raise PublishError(str(error).replace(self._token, "***")) from None

    def _git_auth_env(self) -> dict[str, str]:
        """Pass the token to git as an HTTP header through environment variables.

        Putting it in the remote URL would expose it in the process list and in git's error messages.
        """
        basic = base64.b64encode(f"x-access-token:{self._token}".encode()).decode()
        return {
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "http.https://github.com/.extraheader",
            "GIT_CONFIG_VALUE_0": f"AUTHORIZATION: basic {basic}",
            "GIT_TERMINAL_PROMPT": "0",
        }

    def _create_pull(self, owner: str, name: str, branch: str, title: str, body: str) -> str:
        repo = Github(auth=Auth.Token(self._token)).get_repo(f"{owner}/{name}")
        return repo.create_pull(title=title, body=body, head=branch, base=repo.default_branch).html_url
