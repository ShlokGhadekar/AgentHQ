import asyncio
import subprocess
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import pytest
from fakeredis.aioredis import FakeRedis

from agenthq.agent import load_agent_configs
from agenthq.bus import RedisBus, read_events
from agenthq.config import Settings
from agenthq.events import Event
from agenthq.llm import FakeLLMClient, LLMError, LLMResponse
from agenthq.runtime import Run

SAMPLE_REPO = Path(__file__).resolve().parents[2] / "sample_repo"

# In-memory Redis with real stream semantics: tests stay fast and need no running services.


@pytest.fixture
async def redis() -> AsyncIterator[FakeRedis]:
    client = FakeRedis(decode_responses=True)
    yield client
    await client.aclose()


RunFactory = Callable[..., Run]


@pytest.fixture
def make_run(redis: FakeRedis) -> RunFactory:
    """Build a run of the real team (agents.yaml) where each agent replays the given script.

    Keyword arguments (repo, workspace, sandbox, repolens) are passed on to Run.
    """

    def build(scripts: dict[str, list[LLMResponse | LLMError]], repo: str | None = None, **resources: Any) -> Run:
        configs = load_agent_configs()
        settings = Settings(_env_file=None, inbox_block_ms=20, llm_retry_base_s=0, llm_max_retries=2)
        bus = RedisBus(redis, "run-test", [c.name for c in configs])
        return Run("run-test", "Add rate limiting to /login", repo, bus, configs,
                   lambda config: FakeLLMClient(scripts.get(config.name, [])), settings, **resources)

    return build


@pytest.fixture(scope="session")
def sample_git_repo(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A git repository holding a copy of sample_repo/, to clone like a GitHub repo.

    Built once per session: tests only ever change their own clones of it.
    """
    origin = tmp_path_factory.mktemp("sample") / "origin"
    subprocess.run(["cp", "-R", str(SAMPLE_REPO), str(origin)], check=True)
    for command in (["init", "-q"], ["add", "-A"], ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init"]):
        subprocess.run(["git", *command], cwd=origin, check=True)
    return origin


async def run_to_end(run: Run, redis: FakeRedis, approve: bool | None = True, comment: str = "") -> list[Event]:
    """Run until run_complete and return the global event stream.

    Plays the human: when the run asks for approval, answers with `approve` (None: never answer).
    """
    await run.start()
    async with asyncio.timeout(10):
        while not run.finished.is_set():
            if run.approval_pending and approve is not None:
                await run.decide_approval(approve, comment)
            await asyncio.sleep(0.005)
        await run.wait()
    return [event for _, event in await read_events(redis, "0-0", block_ms=10)]
