"""RepoLensTool against a fake RepoLens server (httpx.MockTransport): re-index checks, retries, error mapping, the lock."""

import asyncio
import json
from typing import Any

import httpx
import pytest

from agenthq.repolens import RepoLensConfigError, RepoLensRequestError, RepoLensTool, RepoLensUnavailable

REPO = "https://github.com/ShlokGhadekar/agenthq-sample-notes"
ANSWER = {
    "answer": "See 【app/auth.py:29-37】.", "model": "m", "found": True, "best_similarity": 0.7, "min_similarity": 0.25,
    "refusal_reason": None, "retrieval": [],
    "citations": [{"path": "app/auth.py", "start_line": 29, "end_line": 37, "language": "python", "code": "def login(): ..."}],
}


class FakeRepoLens:
    """Plays RepoLens: a single global index, plus scripted failures per endpoint."""

    def __init__(self, source: str | None = None, failures: dict[str, list[int]] | None = None) -> None:
        self.source = source
        self.failures = failures or {}
        self.requests: list[str] = []
        self.active_asks = 0
        self.max_parallel_asks = 0

    async def handle(self, request: httpx.Request) -> httpx.Response:
        route = f"{request.method} {request.url.path}"
        self.requests.append(route)
        if self.failures.get(route):
            status = self.failures[route].pop(0)
            return httpx.Response(status, json={"detail": [{"msg": "bad"}] if status == 422 else f"error {status}"})
        if route == "GET /status":
            return httpx.Response(200, json={"indexed": self.source is not None, "source": self.source})
        if route == "POST /index":
            await asyncio.sleep(0.05)  # long enough for the progress poller to run
            self.source = _json(request)["source"]
            return httpx.Response(200, json={"source": self.source, "files_indexed": 14, "chunks_indexed": 40})
        if route == "GET /index/progress":
            return httpx.Response(200, json={"active": True, "source": REPO, "stage": "embed", "done": 1, "total": 2})
        if route == "POST /ask":
            self.active_asks += 1
            self.max_parallel_asks = max(self.max_parallel_asks, self.active_asks)
            await asyncio.sleep(0.01)
            self.active_asks -= 1
            return httpx.Response(200, json=ANSWER)
        return httpx.Response(404)


def _json(request: httpx.Request) -> dict[str, Any]:
    return json.loads(request.content)


def tool_for(server: FakeRepoLens) -> RepoLensTool:
    return RepoLensTool("http://repolens", retry_delays_s=(0, 0), progress_interval_s=0.01,
                        transport=httpx.MockTransport(server.handle))


async def test_skips_indexing_when_repo_is_already_loaded() -> None:
    server = FakeRepoLens(source=REPO + ".git")  # same repo, other spelling
    await tool_for(server).ensure_indexed(REPO + "/")
    assert server.requests == ["GET /status"]


async def test_indexes_other_repo_and_reports_progress() -> None:
    server = FakeRepoLens(source="https://github.com/someone/else")
    progress: list[dict] = []

    async def record(update: dict) -> None:
        progress.append(update)

    await tool_for(server).ensure_indexed(REPO, record)
    assert server.source == REPO
    assert any(p.get("stage") == "embed" for p in progress)
    assert progress[-1]["stage"] == "done"


async def test_ask_reindexes_if_another_run_replaced_the_index() -> None:
    server = FakeRepoLens(source=REPO)
    tool = tool_for(server)
    server.source = "https://github.com/someone/else"  # another run indexed its own repo meanwhile
    await tool.ask(REPO, "Where is login?")
    assert server.requests[:2] == ["GET /status", "POST /index"]
    assert server.requests[-1] == "POST /ask"  # asked only after re-indexing finished
    assert server.source == REPO


async def test_ask_reads_locations_from_citations() -> None:
    result = await tool_for(FakeRepoLens(source=REPO)).ask(REPO, "Where is login?")
    assert result.found
    assert [(c.path, c.start_line, c.end_line) for c in result.citations] == [("app/auth.py", 29, 37)]


async def test_409_while_indexing_is_retried() -> None:
    server = FakeRepoLens(failures={"POST /index": [409]})
    await tool_for(server).ensure_indexed(REPO)
    assert server.requests.count("POST /index") == 2


async def test_502_is_retried_then_succeeds() -> None:
    server = FakeRepoLens(source=REPO, failures={"POST /ask": [502]})
    assert (await tool_for(server).ask(REPO, "q")).found
    assert server.requests.count("POST /ask") == 2


async def test_persistent_502_raises_unavailable() -> None:
    server = FakeRepoLens(source=REPO, failures={"POST /ask": [502, 502, 502]})
    with pytest.raises(RepoLensUnavailable):
        await tool_for(server).ask(REPO, "q")
    assert server.requests.count("POST /ask") == 3  # first try + 2 retries


async def test_400_is_a_config_error_and_not_retried() -> None:
    server = FakeRepoLens(failures={"POST /index": [400]})
    with pytest.raises(RepoLensConfigError):
        await tool_for(server).ensure_indexed("https://github.com/private/repo")
    assert server.requests.count("POST /index") == 1


async def test_422_is_our_bug_and_not_retried() -> None:
    server = FakeRepoLens(source=REPO, failures={"POST /ask": [422]})
    with pytest.raises(RepoLensRequestError):
        await tool_for(server).ask(REPO, "")
    assert server.requests.count("POST /ask") == 1


async def test_connection_errors_become_unavailable() -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    tool = RepoLensTool("http://repolens", retry_delays_s=(0,), transport=httpx.MockTransport(refuse))
    with pytest.raises(RepoLensUnavailable):
        await tool.ask(REPO, "q")


async def test_lock_serialises_calls_from_concurrent_runs() -> None:
    server = FakeRepoLens(source=REPO)
    tool = tool_for(server)
    await asyncio.gather(*(tool.ask(REPO, f"question {i}") for i in range(5)))
    assert server.max_parallel_asks == 1
