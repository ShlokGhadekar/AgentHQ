from collections.abc import AsyncIterator, Callable

import pytest
from fakeredis.aioredis import FakeRedis

from agenthq.agent import load_agent_configs
from agenthq.bus import RedisBus
from agenthq.config import Settings
from agenthq.llm import FakeLLMClient, LLMError, LLMResponse
from agenthq.runtime import Run

# In-memory Redis with real stream semantics: tests stay fast and need no running services.


@pytest.fixture
async def redis() -> AsyncIterator[FakeRedis]:
    client = FakeRedis(decode_responses=True)
    yield client
    await client.aclose()


RunFactory = Callable[[dict[str, list[LLMResponse | LLMError]]], Run]


@pytest.fixture
def make_run(redis: FakeRedis) -> RunFactory:
    """Build a run of the real team (agents.yaml) where each agent replays the given script."""

    def build(scripts: dict[str, list[LLMResponse | LLMError]]) -> Run:
        configs = load_agent_configs()
        settings = Settings(_env_file=None, inbox_block_ms=20)
        bus = RedisBus(redis, "run-test", [c.name for c in configs])
        return Run("run-test", "Add rate limiting to /login", None, bus, configs,
                   lambda config: FakeLLMClient(scripts.get(config.name, [])), settings)

    return build
