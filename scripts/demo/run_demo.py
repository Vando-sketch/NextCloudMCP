"""Drive the real MCP server against a throwaway Nextcloud and record a transcript.

Each scene is a plain-language request plus the tool calls a client such as
Claude would make for it. Tool calls go through the real FastMCP server and
CalDAV/Notes code; only the "which tool to call" step is scripted.

    NEXTCLOUD_BASE_URL=http://localhost:8088 NEXTCLOUD_USERNAME=demo \
    NEXTCLOUD_APP_PASSWORD=... PUBLIC_BASE_URL=http://localhost:8000 \
    uv run python scripts/demo/run_demo.py assets/demo/transcript.json
"""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import date, timedelta
from typing import Any

from fastmcp import Client

from nextcloud_organizer_mcp.config import Settings
from nextcloud_organizer_mcp.server import build_server

TODAY = date.today()
THURSDAY = TODAY + timedelta(days=(3 - TODAY.weekday()) % 7)
NEXT_MONDAY = TODAY + timedelta(days=7 - TODAY.weekday())


def scenes() -> list[dict[str, Any]]:
    thu = THURSDAY.isoformat()
    return [
        {
            "prompt": "Set up a Work list and calendar, then add a high-priority task: "
            "finish the Q4 report by Thursday 5pm, remind me 2 hours before.",
            "calls": [
                ("create_task_list", {"display_name": "Work"}),
                ("create_calendar", {"display_name": "Work", "color": "#0082c9"}),
                (
                    "create_task",
                    {
                        "list_name": "Work",
                        "title": "Finish Q4 report",
                        "due_date": f"{thu}T17:00:00",
                        "priority": "high",
                        "tags": ["report"],
                        "reminders": ["-PT2H"],
                    },
                ),
            ],
        },
        {
            "prompt": "Add a weekly team sync, Mondays 10:00 for 30 minutes.",
            "calls": [
                (
                    "create_event",
                    {
                        "calendar_name": "Work",
                        "title": "Team sync",
                        "start": f"{NEXT_MONDAY.isoformat()}T10:00:00",
                        "end": f"{NEXT_MONDAY.isoformat()}T10:30:00",
                        "recurrence": "FREQ=WEEKLY;BYDAY=MO",
                        "location": "Room 4",
                    },
                ),
            ],
        },
        {
            "prompt": "Block Thursday 9-11 to work on the report.",
            "calls": [
                (
                    "list_tasks",
                    {"list_names": ["Work"], "search_text": "Q4", "compact": True},
                ),
                ("__timebox__", {"start": f"{thu}T09:00:00", "duration_minutes": 120}),
            ],
        },
        {
            "prompt": "What does my Thursday look like?",
            "calls": [("get_agenda", {"date": thu})],
        },
        {
            "prompt": "Start a note for the Q4 report with an outline.",
            "calls": [
                (
                    "create_note",
                    {
                        "title": "Q4 report",
                        "category": "Work",
                        "content": "# Q4 report\n\n## Status\nDraft not started.\n\n"
                        "## Open questions\n- Which revenue figures are final?\n",
                    },
                ),
                (
                    "__note_status__",
                    {
                        "section": "## Status",
                        "content": "## Status\nOutline done, writing Thursday.",
                    },
                ),
            ],
        },
    ]


def _short(value: Any) -> Any:
    """Trim noisy keys so the recorded output stays readable."""
    if isinstance(value, dict):
        return {
            k: _short_uid(k, _short(v))
            for k, v in value.items()
            if k not in {"list_url", "url", "calendar_url", "source_url", "etag"}
            and v not in (None, [], "")
        }
    if isinstance(value, list):
        return [_short(v) for v in value]
    return value


def _short_uid(key: str, value: Any) -> Any:
    if key.endswith("uid") and isinstance(value, str) and len(value) > 12:
        return value[:8] + "…"
    return value


async def main(out_path: str) -> None:
    mcp = build_server(Settings.from_env())
    transcript: list[dict[str, Any]] = []
    state: dict[str, Any] = {}
    async with Client(mcp) as client:
        for scene in scenes():
            steps = []
            for name, args in scene["calls"]:
                if name == "__timebox__":
                    name, args = (
                        "create_event_from_task",
                        {
                            "list_name": "Work",
                            "task_uid": state["task_uid"],
                            "calendar_name": "Work",
                            **args,
                        },
                    )
                elif name == "__note_status__":
                    name, args = "update_note_section", {"note_id": state["note_id"], **args}
                result = await client.call_tool(name, args)
                data = result.structured_content
                if isinstance(data, dict) and set(data) == {"result"}:
                    data = data["result"]
                if name == "create_task":
                    state["task_uid"] = data["uid"]
                if name == "create_note":
                    state["note_id"] = data["id"]
                shown = {k: v for k, v in args.items() if k not in {"task_uid", "note_id"}} | (
                    {"task_uid": "…"} if "task_uid" in args else {}
                )
                steps.append({"tool": name, "args": shown, "result": _short(data)})
            transcript.append({"prompt": scene["prompt"], "steps": steps})
    with open(out_path, "w") as fh:
        json.dump(transcript, fh, indent=2, ensure_ascii=False, default=str)
    print(f"wrote {out_path}")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else "transcript.json"))
