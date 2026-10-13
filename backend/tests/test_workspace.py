from pathlib import Path

import pytest

from agenthq.workspace import Workspace, WorkspaceError


@pytest.fixture
async def workspace(sample_git_repo: Path, tmp_path: Path) -> Workspace:
    ws = Workspace(tmp_path / "runs" / "run-1")
    await ws.clone(str(sample_git_repo))
    return ws


async def test_read_numbers_lines_and_supports_ranges(workspace: Workspace) -> None:
    text = workspace.read("app/main.py", 1, 3)
    assert text.splitlines()[0] == '    1  """Application entry point."""'
    assert "[showing lines 1-3 of" in text


@pytest.mark.parametrize("path", ["../outside.txt", "/etc/passwd", "app/../../x", ".git/config"])
async def test_paths_outside_the_repo_are_refused(workspace: Workspace, path: str) -> None:
    with pytest.raises(WorkspaceError):
        workspace.write(path, "x")


async def test_edit_replaces_one_exact_snippet(workspace: Workspace) -> None:
    workspace.edit("app/main.py", 'FastAPI(title="Notes API")', 'FastAPI(title="Notes API", version="1.1")')
    assert 'version="1.1"' in workspace.read("app/main.py")


async def test_edit_refuses_ambiguous_or_missing_snippets(workspace: Workspace) -> None:
    with pytest.raises(WorkspaceError, match="appears 2 times"):
        workspace.edit("app/main.py", "include_router", "add_router")  # two routers are included
    with pytest.raises(WorkspaceError, match="appears 0 times"):
        workspace.edit("app/main.py", "no such text", "x")


async def test_list_and_search(workspace: Workspace) -> None:
    files = workspace.list_files()
    assert "app/auth.py" in files
    assert not any(f.startswith(".git/") for f in files)
    matches = workspace.search(r'@router\.post\("/login"')
    assert matches and matches[0].startswith("app/auth.py:")


async def test_diff_includes_new_and_changed_files(workspace: Workspace) -> None:
    assert await workspace.diff() == ""
    workspace.write("app/rate_limit.py", "LIMIT = 5\n")
    workspace.edit("app/main.py", 'title="Notes API"', 'title="Notes"')
    diff = await workspace.diff()
    assert "app/rate_limit.py" in diff and "+LIMIT = 5" in diff
    assert '-    app = FastAPI(title="Notes API")' in diff
