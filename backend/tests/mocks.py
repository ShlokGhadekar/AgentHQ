"""Test doubles for the external services a run uses."""

from agenthq.repolens import AskResult, Citation, ProgressCallback, RepoLensUnavailable
from agenthq.sandbox import CommandResult
from agenthq.workspace import Workspace

FOUND = AskResult(
    answer="POST /login is defined in app/auth.py.",
    found=True,
    best_similarity=0.71,
    citations=[Citation("app/auth.py", 29, 37, '@router.post("/login", response_model=TokenOut)\ndef login(body: LoginRequest) -> TokenOut:')],
)
NOT_FOUND = AskResult(answer="I could not find this in the repository", found=False, best_similarity=0.12, citations=[])
UNAVAILABLE = RepoLensUnavailable("RepoLens ask failed after 4 attempts: HTTP 502: Groq rate limit")


class MockRepoLensTool:
    """Replays scripted answers (or raises scripted errors) and records every call."""

    def __init__(self, script: list[AskResult | Exception] | None = None) -> None:
        self._script = list(script or [])
        self.indexed: list[str] = []
        self.questions: list[str] = []

    async def ensure_indexed(self, repo_url: str, on_progress: ProgressCallback | None = None) -> None:
        self.indexed.append(repo_url)
        if on_progress:
            await on_progress({"active": True, "source": repo_url, "stage": "embed", "done": 3, "total": 6})

    async def ask(self, repo_url: str, question: str, on_progress: ProgressCallback | None = None) -> AskResult:
        self.questions.append(question)
        entry = self._script.pop(0)
        if isinstance(entry, Exception):
            raise entry
        return entry


class FakeSandbox:
    """Answers every command with the next scripted result (default: passing)."""

    def __init__(self, results: list[CommandResult] | None = None) -> None:
        self._results = list(results or [])
        self.commands: list[str] = []
        self.started = False
        self.stopped = False

    async def start(self, workspace: Workspace) -> None:
        self.started = True

    async def run(self, command: str, timeout_s: int) -> CommandResult:
        self.commands.append(command)
        return self._results.pop(0) if self._results else CommandResult(command, 0, "13 passed")

    async def stop(self) -> None:
        self.stopped = True
