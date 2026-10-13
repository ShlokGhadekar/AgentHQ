"""Command line: start a run and watch it, or tail the global event stream.

    python -m agenthq.cli run "Add rate limiting to /login" --demo
    python -m agenthq.cli run "Add rate limiting to /login" --repo https://github.com/owner/repo
    python -m agenthq.cli tail
"""

import argparse
import asyncio
import logging
import sys

from redis.asyncio import Redis

from agenthq.bus import latest_event_id, read_events
from agenthq.config import get_settings
from agenthq.console import format_event
from agenthq.events import Event, EventType
from agenthq.registry import RunRegistry
from agenthq.runtime import Run


async def run_command(args: argparse.Namespace) -> None:
    settings = get_settings()
    redis = Redis.from_url(settings.redis_url, decode_responses=True)
    registry = RunRegistry(settings, redis)
    run = registry.create(args.goal, args.repo, demo=args.demo, demo_delay_s=args.delay)

    print(f"Run {run.run_id}: {args.goal}\n")
    # Read the printed log back from the global stream, not from the Run object: what you see
    # is exactly what the UI and replay will see.
    printer = asyncio.create_task(print_events(redis, await latest_event_id(redis), run))
    await run.start()
    try:
        await asyncio.wait_for(run.wait(), timeout=args.timeout)
    except TimeoutError:
        print(f"\nTimed out after {args.timeout}s, stopping.")
        await run.stop(f"Timed out after {args.timeout}s")
    except asyncio.CancelledError:  # Ctrl+C
        await run.stop("Interrupted from the terminal")
        raise
    finally:
        await asyncio.gather(printer, return_exceptions=True)
        _print_summary(run)
        await registry.shutdown()
        await redis.aclose()


async def print_events(redis: Redis, after_id: str, run: Run | None = None) -> None:
    """Print events from the global stream; with a run, only that run's, until it completes."""
    while True:
        for entry_id, event in await read_events(redis, after_id, block_ms=1000):
            after_id = entry_id
            if run and event.run_id != run.run_id:
                continue
            print(format_event(event) if run else f"{event.run_id}  {format_event(event)}", flush=True)
            if run and event.type == EventType.APPROVAL_REQUESTED:
                await _ask_for_approval(run, event)
            if run and event.type == EventType.RUN_COMPLETE:
                return


async def _ask_for_approval(run: Run, request: Event) -> None:
    """The approval gate in the terminal: show what changed, then let the human decide."""
    diff = request.payload.get("diff") or ""
    added = sum(1 for line in diff.splitlines() if line.startswith("+") and not line.startswith("+++"))
    removed = sum(1 for line in diff.splitlines() if line.startswith("-") and not line.startswith("---"))
    files = [line.split(" b/")[-1] for line in diff.splitlines() if line.startswith("diff --git")]
    tests = request.payload.get("tests")
    print("\n" + "=" * 70)
    print("APPROVAL REQUESTED\n")
    print(request.payload.get("summary", ""))
    print(f"\nChanged files: {', '.join(files) or 'none'} (+{added} / -{removed} lines)")
    if run.workspace:
        print(f"Full diff: git -C '{run.workspace.root}' diff")
    if tests:
        print(f"Tests (re-run by the system): {'PASSED' if tests['passed'] else 'FAILED'} - {tests['command']}")
    if not sys.stdin.isatty():
        print("\nNo terminal to ask in, so the run waits for a decision until it times out.")
        print("=" * 70 + "\n")
        return
    answer = (await asyncio.to_thread(input, "\nApprove and open a pull request? [y/N] ")).strip().lower()
    comment = "" if answer == "y" else (await asyncio.to_thread(input, "What should change? ")).strip()
    print("=" * 70 + "\n")
    pr_url = await run.decide_approval(answer == "y", comment)
    if pr_url:
        print(f"Pull request: {pr_url}\n")


def _print_summary(run: Run) -> None:
    print("\nTasks:")
    for task in run.board.tasks.values():
        print(f"  {task.id:<4} {task.status:<12} {task.assignee:<11} {task.title}")
    print(f"\nTokens: {run.guards.total_tokens:,} over {run.guards.total_steps} LLM calls")
    for agent, (tokens_in, tokens_out) in run.usage.items():
        print(f"  {agent:<11} {tokens_in:>8,} in / {tokens_out:,} out")
    if run.workspace:
        print(f"\nWorkspace with the changes: {run.workspace.root}")


async def tail_command(_args: argparse.Namespace) -> None:
    redis = Redis.from_url(get_settings().redis_url, decode_responses=True)
    print("Tailing agenthq:events (Ctrl+C to stop)")
    await print_events(redis, await latest_event_id(redis))


def main() -> None:
    parser = argparse.ArgumentParser(prog="agenthq")
    commands = parser.add_subparsers(dest="command", required=True)

    run = commands.add_parser("run", help="Start a run and print its events")
    run.add_argument("goal")
    run.add_argument("--repo", help="GitHub URL of the target repository")
    run.add_argument("--demo", action="store_true", help="Replay the scripted team (offline, no API key)")
    run.add_argument("--delay", type=float, default=0.4, help="Seconds per scripted LLM call in --demo")
    run.add_argument("--timeout", type=float, default=3600)
    run.set_defaults(handler=run_command)

    tail = commands.add_parser("tail", help="Print every new event from all runs")
    tail.set_defaults(handler=tail_command)

    args = parser.parse_args()
    # Warnings include retries (RepoLens backoff), which are worth seeing while a run is live.
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    try:
        asyncio.run(args.handler(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
