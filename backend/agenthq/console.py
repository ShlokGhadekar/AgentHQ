"""One readable terminal line per event, colored by sender."""

import json

from agenthq.events import Event, EventType

COLORS = {"manager": 35, "researcher": 36, "engineer": 33, "reviewer": 32, "human": 97, "system": 90}
DIM_TYPES = {EventType.LLM_USAGE, EventType.TOOL_RESULT}
MAX_WIDTH = 140


def format_event(event: Event, color: bool = True) -> str:
    """e.g. `12:00:01  engineer -> researcher  message  T2  Is there a central config...`"""
    route = event.sender or ""
    if event.recipient:
        route += f" -> {event.recipient}"
    line = f"{event.timestamp:%H:%M:%S}  {route:<24} {event.type:<18} {event.task_id or '':<4} {_summary(event)}"
    line = line.replace("\n", " ")
    if len(line) > MAX_WIDTH:
        line = line[: MAX_WIDTH - 1] + "…"
    if not color:
        return line
    if event.type == EventType.ERROR:
        return f"\033[1;31m{line}\033[0m"
    if event.type in DIM_TYPES or "agent_state" in event.payload:
        return f"\033[2m{line}\033[0m"
    return f"\033[{COLORS.get(event.sender or '', 37)}m{line}\033[0m"


def _summary(event: Event) -> str:
    """The most useful detail for each event type."""
    p = event.payload
    match event.type:
        case EventType.MESSAGE:
            return p.get("content", "")
        case EventType.TASK_CREATED:
            after = f" (after {', '.join(p['depends_on'])})" if p.get("depends_on") else ""
            return f"{p['title']} -> {p['assignee']}{after}"
        case EventType.TASK_ASSIGNED:
            return p.get("title", "")
        case EventType.STATUS_CHANGE if "agent_state" in p:
            return f"[{p['agent_state']}]"
        case EventType.STATUS_CHANGE:
            detail = p.get("summary") or p.get("reason") or p.get("comments") or p.get("comment") or p.get("note") or ""
            return f"[{p['task_status']}] {detail}"
        case EventType.TOOL_CALL:
            return f"{p['tool']}({json.dumps(p['arguments'])})"
        case EventType.TOOL_RESULT:
            return f"{'ok' if p['ok'] else 'FAILED'}: {p['result']}"
        case EventType.LLM_USAGE:
            return f"{p.get('model')} in={event.tokens_in} out={event.tokens_out} {p.get('text', '')}"
        case EventType.ERROR:
            return p.get("message", "")
        case EventType.RUN_COMPLETE:
            return p.get("report", "")
        case _:
            return json.dumps(p)
