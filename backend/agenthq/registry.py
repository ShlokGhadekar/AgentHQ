"""Builds runs with everything they need and keeps them by id. Used by both the API and the CLI."""

import asyncio
from collections.abc import Callable
from pathlib import Path

from redis.asyncio import Redis

from agenthq.agent import AgentConfig, load_agent_configs
from agenthq.bus import RedisBus
from agenthq.config import Settings
from agenthq.llm import FakeLLMClient, LLMClient, make_llm_client
from agenthq.llm.fake import load_scripts
from agenthq.pull_requests import GitHubPublisher
from agenthq.repolens import RepoLensTool
from agenthq.runtime import Run, new_run_id
from agenthq.sandbox import DockerSandbox
from agenthq.workspace import Workspace

DEMO_SCRIPT = Path(__file__).with_name("demo_script.yaml")


class RunRegistry:
    """Creates runs and tracks them while the process lives.

    There is exactly one RunRegistry, and so one RepoLensTool, per process: that single
    instance's lock is what keeps concurrent runs from using RepoLens at the same time.
    """

    def __init__(self, settings: Settings, redis: Redis) -> None:
        self.settings = settings
        self.redis = redis
        self.repolens = RepoLensTool(settings.repolens_url, settings.repolens_index_timeout_s, settings.repolens_ask_timeout_s)
        self.runs: dict[str, Run] = {}
        self._llm: LLMClient | None = None
        self._background: set[asyncio.Task[None]] = set()

    def create(self, goal: str, repo: str | None = None, demo: bool = False, demo_delay_s: float | None = None) -> Run:
        """Build a run. `demo` replays the scripted team (offline, no repo, no API key)."""
        configs = load_agent_configs()
        run_id = new_run_id()
        resources = {}
        if repo and not demo:
            resources = {
                "workspace": Workspace(self.settings.workspaces_dir / run_id),
                "sandbox": DockerSandbox(self.settings.sandbox_image, self.settings.workspaces_volume),
                "repolens": self.repolens,
                "publisher": GitHubPublisher(self.settings.github_token) if self.settings.github_token else None,
            }
        run = Run(run_id, goal, None if demo else repo, RedisBus(self.redis, run_id, [c.name for c in configs]),
                  configs, self._llm_factory(demo, self.settings.demo_llm_delay_s if demo_delay_s is None else demo_delay_s),
                  self.settings, **resources)
        self.runs[run_id] = run
        return run

    def start_in_background(self, run: Run) -> None:
        """Start the run and let it play out in the background, tidying up its sandbox at the end.

        The registry keeps a reference to the task: asyncio only holds weak references, so an
        unreferenced task can be garbage-collected halfway through a run.
        """
        task = asyncio.create_task(self._start_and_wait(run), name=run.run_id)
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    @staticmethod
    async def _start_and_wait(run: Run) -> None:
        await run.start()
        await run.wait()

    def get(self, run_id: str) -> Run:
        return self.runs[run_id]

    async def shutdown(self) -> None:
        for run in self.runs.values():
            if not run.finished.is_set():
                await run.stop("The server is shutting down")
        await self.repolens.aclose()

    def _llm_factory(self, demo: bool, demo_delay_s: float) -> Callable[[AgentConfig], LLMClient]:
        if demo:
            scripts = load_scripts(DEMO_SCRIPT)
            return lambda config: FakeLLMClient(scripts.get(config.name, []), model="scripted demo", delay_s=demo_delay_s)
        if self._llm is None:
            self._llm = make_llm_client(self.settings)  # raises if the provider's API key is missing
        llm = self._llm
        return lambda _config: llm
