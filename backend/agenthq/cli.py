"""Command line: start a run and watch it, or tail the global event stream.

    python -m agenthq.cli run "Add rate limiting to /login" --fake
    python -m agenthq.cli tail
"""

import argparse
import asyncio
from collections.abc import Callable
from pathlib import Path

from redis.asyncio import Redis

from agenthq.agent import AgentConfig, load_agent_configs
from agenthq.bus import RedisBus, latest_event_id, read_events
from agenthq.config import get_settings
from agenthq.console import format_event
from agenthq.events import EventType
from agenthq.llm import FakeLLMClient, LLMClient, make_llm_client
from agenthq.llm.fake import load_scripts
from agenthq.runtime import Run, new_run_id

DEMO_SCRIPT = Path(__file__).with_name("demo_script.yaml")


async def run_command(args: argparse.Namespace) -> None:
    settings = get_settings()
    redis = Redis.from_url(settings.redis_url, decode_responses=True)
    configs = load_agent_configs()
    run_id = new_run_id()
    run = Run(run_id, args.goal, args.repo, RedisBus(redis, run_id, [c.name for c in configs]),
              configs, _llm_factory(args), settings)

    print(f"Run {run_id}: {args.goal}\n")
    # Read the printed log back from the global stream, not from the Run object: what you see
    # is exactly what the UI and replay will see.
    printer = asyncio.create_task(print_events(redis, await latest_event_id(redis), run_id))
    await run.start()
    try:
        await asyncio.wait_for(run.wait(), timeout=args.timeout)
    except TimeoutError:
        print(f"\nTimed out after {args.timeout}s, stopping.")
        await run.stop()
        printer.cancel()
    await asyncio.gather(printer, return_exceptions=True)
    _print_summary(run)
    await redis.aclose()


def _llm_factory(args: argparse.Namespace) -> Callable[[AgentConfig], LLMClient]:
    """Fake mode gives each agent its own scripted client; real mode shares one client."""
    if args.fake:
        scripts = load_scripts(args.script)
        return lambda config: FakeLLMClient(scripts.get(config.name, []), delay_s=args.delay)
    client: LLMClient = make_llm_client(get_settings())

    def shared(_config: AgentConfig) -> LLMClient:
        return client

    return shared


async def print_events(redis: Redis, after_id: str, run_id: str | None = None) -> None:
    """Print events from the global stream; with a run_id, only that run's, until it completes."""
    while True:
        for entry_id, event in await read_events(redis, after_id, block_ms=1000):
            after_id = entry_id
            if run_id and event.run_id != run_id:
                continue
            print(format_event(event) if run_id else f"{event.run_id}  {format_event(event)}")
            if run_id and event.type == EventType.RUN_COMPLETE:
                return


def _print_summary(run: Run) -> None:
    print("\nTasks:")
    for task in run.board.tasks.values():
        print(f"  {task.id:<4} {task.status:<12} {task.assignee:<11} {task.title}")
    print("\nTokens (in / out):")
    for agent, (tokens_in, tokens_out) in run.usage.items():
        print(f"  {agent:<11} {tokens_in:>7} / {tokens_out}")


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
    run.add_argument("--fake", action="store_true", help="Use the scripted FakeLLMClient (offline, no key)")
    run.add_argument("--script", type=Path, default=DEMO_SCRIPT, help="Script for --fake")
    run.add_argument("--delay", type=float, default=0.4, help="Seconds per fake LLM call, so the demo is watchable")
    run.add_argument("--timeout", type=float, default=600)
    run.set_defaults(handler=run_command)

    tail = commands.add_parser("tail", help="Print every new event from all runs")
    tail.set_defaults(handler=tail_command)

    args = parser.parse_args()
    try:
        asyncio.run(args.handler(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
