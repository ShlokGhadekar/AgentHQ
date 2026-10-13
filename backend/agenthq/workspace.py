"""A run's private clone of the target repo, and the file operations agents perform on it.

Agents read and edit files through these methods (plain Python, no shell), and every path is
checked to stay inside the clone. Running the repo's code (tests, linter) happens separately,
in the Docker sandbox, because that is the part that executes untrusted code.
"""

import asyncio
import re
from pathlib import Path

# Never shown to agents: VCS internals, caches and installed packages are noise or huge.
SKIPPED_DIRS = {".git", "__pycache__", ".pytest_cache", ".ruff_cache", ".venv", "venv", "node_modules", ".mypy_cache"}
MAX_READ_LINES = 400
MAX_LIST_ENTRIES = 300
MAX_SEARCH_MATCHES = 50


class WorkspaceError(Exception):
    """A file operation was refused or failed; the message is meant for the agent."""


class Workspace:
    """A directory holding one run's clone of the target repository."""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()

    async def clone(self, repo_url: str) -> None:
        """Clone the repo into the (new, empty) workspace directory."""
        self.root.parent.mkdir(parents=True, exist_ok=True)
        await _git(self.root.parent, "clone", "--quiet", repo_url, str(self.root))

    def read(self, path: str, start_line: int = 1, end_line: int | None = None) -> str:
        """File contents with line numbers, so agents can cite and edit precise ranges."""
        lines = self._file(path).read_text(errors="replace").splitlines()
        start = max(start_line, 1)
        end = min(end_line or len(lines), len(lines), start + MAX_READ_LINES - 1)
        numbered = [f"{n:>5}  {lines[n - 1]}" for n in range(start, end + 1)]
        note = f"\n[showing lines {start}-{end} of {len(lines)}]" if (start, end) != (1, len(lines)) else ""
        return "\n".join(numbered) + note if numbered else f"[{path} has {len(lines)} lines]"

    def write(self, path: str, content: str) -> str:
        """Create or overwrite a file."""
        target = self._resolve(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        existed = target.exists()
        target.write_text(content)
        return f"{'Overwrote' if existed else 'Created'} {path} ({len(content.splitlines())} lines)"

    def edit(self, path: str, old: str, new: str) -> str:
        """Replace one exact, unique snippet. Safer than rewriting a whole file from memory."""
        target = self._file(path)
        text = target.read_text()
        count = text.count(old)
        if count != 1:
            raise WorkspaceError(
                f"The text to replace appears {count} times in {path}; it must appear exactly once. "
                "Read the file again and include more surrounding lines."
            )
        target.write_text(text.replace(old, new, 1))
        return f"Edited {path}"

    def list_files(self, path: str = ".") -> list[str]:
        """Files under `path`, relative to the repo root."""
        base = self._resolve(path)
        if not base.is_dir():
            raise WorkspaceError(f"{path} is not a directory")
        files = sorted(
            p.relative_to(self.root).as_posix() for p in base.rglob("*")
            if p.is_file() and not SKIPPED_DIRS.intersection(p.relative_to(self.root).parts)
        )
        return files[:MAX_LIST_ENTRIES]

    def search(self, pattern: str, path: str = ".") -> list[str]:
        """Lines matching a regular expression, as `path:line: text`."""
        try:
            regex = re.compile(pattern)
        except re.error as error:
            raise WorkspaceError(f"Invalid regular expression: {error}") from error
        matches = []
        for rel in self.list_files(path):
            try:
                lines = (self.root / rel).read_text().splitlines()
            except (UnicodeDecodeError, OSError):
                continue  # binary or unreadable file
            for number, line in enumerate(lines, start=1):
                if regex.search(line):
                    matches.append(f"{rel}:{number}: {line.strip()}")
                    if len(matches) >= MAX_SEARCH_MATCHES:
                        return matches
        return matches

    async def diff(self) -> str:
        """All changes since the clone, including new files."""
        # "intent to add" makes new files show up in git diff without staging their content.
        await _git(self.root, "add", "--intent-to-add", "--all")
        return await _git(self.root, "diff")

    def _resolve(self, path: str) -> Path:
        """Turn an agent-supplied path into an absolute one, refusing anything outside the clone."""
        target = (self.root / path).resolve()
        if not target.is_relative_to(self.root):
            raise WorkspaceError(f"{path} is outside the repository")
        if ".git" in target.relative_to(self.root).parts:
            raise WorkspaceError("The .git directory is off limits")
        return target

    def _file(self, path: str) -> Path:
        target = self._resolve(path)
        if not target.is_file():
            raise WorkspaceError(f"{path} does not exist (paths are relative to the repo root)")
        return target


async def _git(cwd: Path, *args: str) -> str:
    process = await asyncio.create_subprocess_exec(
        "git", *args, cwd=cwd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await process.communicate()
    if process.returncode != 0:
        raise WorkspaceError(f"git {args[0]} failed: {stderr.decode().strip()}")
    return stdout.decode()
