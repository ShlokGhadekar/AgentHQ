"""Adapter for RepoLens, the RAG service that answers questions about a codebase with citations.

RepoLens holds ONE global index, so this class treats it as a shared resource:
- every call runs under one asyncio lock, so two runs can never index or query at the same time;
- every question first checks (GET /status) that the index holds this run's repo, and re-indexes
  if another run replaced it in between.
This serialises all RepoLens use in the process. At scale you'd run one index per repo instead
(see DECISIONS.md).
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

log = logging.getLogger(__name__)

ProgressCallback = Callable[[dict[str, Any]], Awaitable[None]]


class RepoLensError(Exception):
    """Base class for RepoLens failures."""


class RepoLensConfigError(RepoLensError):
    """400: the repo is missing, private or has no indexable files. Retrying can't fix it."""


class RepoLensUnavailable(RepoLensError):
    """RepoLens or its LLM kept failing (502, timeouts, connection errors) after retries."""


class RepoLensRequestError(RepoLensError):
    """422: we sent a malformed request. A bug on our side, so it is logged and not retried."""


@dataclass(frozen=True)
class Citation:
    """A cited code range: lines are 1-indexed and inclusive, paths relative to the repo root."""

    path: str
    start_line: int
    end_line: int
    code: str


@dataclass(frozen=True)
class AskResult:
    answer: str
    found: bool
    best_similarity: float
    citations: list[Citation]


class CodebaseQA(Protocol):
    """What agents need from RepoLens. MockRepoLensTool implements the same two methods."""

    async def ensure_indexed(self, repo_url: str, on_progress: ProgressCallback | None = None) -> None: ...

    async def ask(self, repo_url: str, question: str, on_progress: ProgressCallback | None = None) -> AskResult: ...


def normalize_repo_url(url: str) -> str:
    """Compare repos by URL without a trailing slash or .git, the two usual spellings."""
    url = url.strip().rstrip("/")
    return url.removesuffix(".git")


class RepoLensTool:
    """HTTP client for one RepoLens server. The backend creates exactly one, so its lock is global."""

    def __init__(
        self,
        base_url: str,
        index_timeout_s: float = 300,
        ask_timeout_s: float = 60,
        retry_delays_s: tuple[float, ...] = (2, 5, 10),
        progress_interval_s: float = 1.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._client = httpx.AsyncClient(base_url=base_url, transport=transport)
        self._index_timeout_s = index_timeout_s
        self._ask_timeout_s = ask_timeout_s
        self._retry_delays_s = retry_delays_s
        self._progress_interval_s = progress_interval_s
        self._lock = asyncio.Lock()

    async def ensure_indexed(self, repo_url: str, on_progress: ProgressCallback | None = None) -> None:
        """Make sure RepoLens has indexed `repo_url`, indexing it now if needed."""
        async with self._lock:
            await self._ensure_indexed_locked(repo_url, on_progress)

    async def ask(self, repo_url: str, question: str, on_progress: ProgressCallback | None = None) -> AskResult:
        """Ask a question about `repo_url`. found=False is a normal answer, not an error."""
        async with self._lock:
            await self._ensure_indexed_locked(repo_url, on_progress)
            data = await self._with_retries("ask", lambda: self._client.post(
                "/ask", json={"question": question}, timeout=self._ask_timeout_s))
        return AskResult(
            answer=data["answer"],
            found=data["found"],
            best_similarity=data["best_similarity"],
            # Locations come from the structured citations, never from parsing the answer text.
            citations=[Citation(c["path"], c["start_line"], c["end_line"], c["code"]) for c in data["citations"]],
        )

    async def status(self) -> dict[str, Any]:
        return await self._with_retries("status", lambda: self._client.get("/status", timeout=10))

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _ensure_indexed_locked(self, repo_url: str, on_progress: ProgressCallback | None) -> None:
        status = await self.status()
        if status["indexed"] and normalize_repo_url(status["source"] or "") == normalize_repo_url(repo_url):
            return
        log.info("RepoLens has %s indexed; indexing %s", status["source"], repo_url)
        poller = asyncio.create_task(self._report_progress(on_progress)) if on_progress else None
        try:
            # POST /index blocks until indexing is done, so progress is polled from a second request.
            await self._with_retries("index", lambda: self._client.post(
                "/index", json={"source": repo_url}, timeout=self._index_timeout_s))
        finally:
            if poller:
                poller.cancel()
        if on_progress:
            await on_progress({"active": False, "stage": "done", "source": repo_url})

    async def _report_progress(self, on_progress: ProgressCallback) -> None:
        while True:
            try:
                response = await self._client.get("/index/progress", timeout=5)
                if response.status_code == 200:
                    await on_progress(response.json())
            except httpx.HTTPError:
                pass  # progress is cosmetic; the index call itself reports real failures
            await asyncio.sleep(self._progress_interval_s)

    async def _with_retries(self, name: str, send: Callable[[], Awaitable[httpx.Response]]) -> dict[str, Any]:
        """Send a request, retrying with backoff on the failures that can pass.

        409 (someone else is indexing) and 502 (RepoLens's LLM failed) are retried, as are
        timeouts and connection errors. 400 and 422 are raised at once.
        """
        for attempt, delay in enumerate([*self._retry_delays_s, None]):
            try:
                response = await send()
            except httpx.TransportError as error:  # timeouts, refused connections
                problem = f"{type(error).__name__}: {error}"
            else:
                if response.status_code == 200:
                    return response.json()
                detail = _detail(response)
                if response.status_code == 400:
                    raise RepoLensConfigError(detail)
                if response.status_code == 422:
                    log.error("RepoLens rejected our %s request: %s", name, detail)
                    raise RepoLensRequestError(detail)
                problem = f"HTTP {response.status_code}: {detail}"
            if delay is None:
                raise RepoLensUnavailable(f"RepoLens {name} failed after {attempt + 1} attempts: {problem}")
            log.warning("RepoLens %s failed (%s); retrying in %ss", name, problem, delay)
            await asyncio.sleep(delay)
        raise AssertionError("unreachable")


def _detail(response: httpx.Response) -> str:
    try:
        return str(response.json().get("detail", response.text))
    except ValueError:
        return response.text
