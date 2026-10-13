"""The real Docker sandbox. Slow (installs packages), so excluded by default; run with `pytest -m docker`."""

from pathlib import Path

import pytest

from agenthq.sandbox import DockerSandbox
from agenthq.workspace import Workspace

pytestmark = pytest.mark.docker


async def test_sandbox_runs_tests_offline(sample_git_repo: Path, tmp_path: Path) -> None:
    workspace = Workspace(tmp_path / "ws" / "run-docker-test")
    await workspace.clone(str(sample_git_repo))
    sandbox = DockerSandbox("agenthq-sandbox:latest")
    await sandbox.start(workspace)
    try:
        tests = await sandbox.run("pytest -q", timeout_s=120)
        assert tests.passed, tests.output
        assert "13 passed" in tests.output

        lint = await sandbox.run("ruff check .", timeout_s=60)
        assert lint.passed, lint.output

        # Dependencies are installed, then the network is cut: test code can't phone home.
        network = await sandbox.run("python -c \"import urllib.request; urllib.request.urlopen('https://pypi.org', timeout=3)\"", 30)
        assert not network.passed

        timed_out = await sandbox.run("sleep 5", timeout_s=1)
        assert "TIMED OUT" in timed_out.summary()
    finally:
        await sandbox.stop()
