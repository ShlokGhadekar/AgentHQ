"""The Docker sandbox: the only place where the target repo's code is executed.

One container per run, with the run's workspace mounted. Agents edit files through Workspace
(plain Python); tests and the linter run here, because test code written by an LLM is untrusted.
The container gets the network only while it installs the repo's dependencies, then it is
disconnected, and it runs with memory, CPU and process limits.
"""

import asyncio
import logging
from dataclasses import dataclass
from typing import Protocol

import docker
from docker.errors import DockerException
from docker.models.containers import Container

from agenthq.workspace import Workspace

log = logging.getLogger(__name__)

MAX_OUTPUT_CHARS = 6000
TIMEOUT_EXIT_CODE = 124  # what coreutils `timeout` returns when it kills the command


class SandboxError(Exception):
    """The sandbox couldn't be created or used."""


@dataclass(frozen=True)
class CommandResult:
    command: str
    exit_code: int
    output: str

    @property
    def passed(self) -> bool:
        return self.exit_code == 0

    def summary(self) -> str:
        """What the agent sees: the verdict, then the tail of the output, where test failures are."""
        if self.exit_code == TIMEOUT_EXIT_CODE:
            verdict = "TIMED OUT"
        else:
            verdict = "PASSED" if self.passed else f"FAILED (exit code {self.exit_code})"
        output = self.output
        if len(output) > MAX_OUTPUT_CHARS:
            output = "[... earlier output cut ...]\n" + output[-MAX_OUTPUT_CHARS:]
        return f"$ {self.command}\n{verdict}\n\n{output}"


class Sandbox(Protocol):
    """What the runtime needs from a sandbox; tests use a fake with the same methods."""

    async def start(self, workspace: Workspace) -> None: ...

    async def run(self, command: str, timeout_s: int) -> CommandResult: ...

    async def stop(self) -> None: ...


class DockerSandbox:
    """A long-lived container per run. Docker's Python SDK is blocking, so calls go through a thread."""

    def __init__(self, image: str, workspaces_volume: str | None = None) -> None:
        self._image = image
        # When the backend itself runs in Docker, workspaces live in a named volume shared with
        # the sandbox containers; when it runs on the host, the workspace folder is bind-mounted.
        self._workspaces_volume = workspaces_volume
        self._container: Container | None = None

    async def start(self, workspace: Workspace) -> None:
        try:
            self._container = await asyncio.to_thread(self._create, workspace)
            await self._install_dependencies(workspace)
            await asyncio.to_thread(self._disconnect_network)
        except DockerException as error:
            raise SandboxError(f"Could not start the sandbox: {error}") from error

    async def run(self, command: str, timeout_s: int) -> CommandResult:
        if self._container is None:
            raise SandboxError("The sandbox is not running")
        container = self._container

        def execute() -> CommandResult:
            exit_code, output = container.exec_run(["timeout", str(timeout_s), "sh", "-c", command])
            return CommandResult(command, exit_code, output.decode(errors="replace"))

        return await asyncio.to_thread(execute)

    async def stop(self) -> None:
        if self._container is not None:
            container, self._container = self._container, None
            try:
                await asyncio.to_thread(container.remove, force=True)
            except Exception as error:  # teardown must never crash the caller (e.g. Docker already stopped)
                log.warning("Could not remove sandbox container: %s", error)

    def _create(self, workspace: Workspace) -> Container:
        if self._workspaces_volume:
            volumes = {self._workspaces_volume: {"bind": "/workspaces", "mode": "rw"}}
            working_dir = f"/workspaces/{workspace.root.name}"
        else:
            volumes = {str(workspace.root): {"bind": "/workspace", "mode": "rw"}}
            working_dir = "/workspace"
        return docker.from_env().containers.run(
            self._image, ["sleep", "infinity"], detach=True, working_dir=working_dir, volumes=volumes,
            mem_limit="1g", nano_cpus=2_000_000_000, pids_limit=256,
            labels={"agenthq.workspace": workspace.root.name},
        )

    async def _install_dependencies(self, workspace: Workspace) -> None:
        requirements = [name for name in ("requirements.txt", "requirements-dev.txt") if (workspace.root / name).is_file()]
        if not requirements:
            return
        command = "pip install --quiet --disable-pip-version-check " + " ".join(f"-r {name}" for name in requirements)
        result = await self.run(command, timeout_s=300)
        if not result.passed:
            raise SandboxError(f"Installing dependencies failed:\n{result.output[-2000:]}")

    def _disconnect_network(self) -> None:
        assert self._container is not None
        self._container.reload()
        for network in list(self._container.attrs["NetworkSettings"]["Networks"]):
            docker.from_env().networks.get(network).disconnect(self._container)
