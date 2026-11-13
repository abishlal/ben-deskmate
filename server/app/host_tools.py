"""Todo and note tools that work through the desktop bridge: Microsoft To Do via classic Outlook's
task folders, and the OneNote desktop app. They replace the local tools while the pet shell reports
that the bridge can reach those apps.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from claude_agent_sdk import tool

from . import config, db, hub
from .ms_tools import WHEN, _err, _ok, _ref, _resolve


def _guard(fn):
    async def wrapped(args: dict[str, Any]) -> dict[str, Any]:
        try:
            return await fn(args)
        except hub.HostError as e:
            return _err(str(e))
    return wrapped


def _local_iso(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, config.TZ).strftime("%Y-%m-%dT%H:%M:%S")


def task_time(task: dict) -> int | None:
    """Epoch of a task's reminder time, if it has one."""
    return db.parse_when(task["reminder"]) if task.get("reminder") else None


def _fmt_due(task: dict) -> str:
    if task.get("reminder"):
        return db.fmt(db.parse_when(task["reminder"]))
    return datetime.fromisoformat(task["due"]).strftime("%a %d %b %Y") if task.get("due") else "no date"


def _due_arg(args: dict[str, Any], out: dict[str, Any]) -> str | None:
    """Copy a 'due' argument into the bridge request; returns an error message if it cannot be read."""
    if "due" not in args:
        return None
    if not args["due"]:
        out["due"] = ""
        return None
    try:
        out["due"] = _local_iso(db.parse_when(args["due"]))
    except ValueError:
        return f"Could not read the due time {args['due']!r}. Use {WHEN}."
    return None


# --- Microsoft To Do, through Outlook's task folders ----------------------------

@tool(
    "add_todo",
    "Add a task to the user's Microsoft To Do. Goes to the default Tasks list unless a list name is given.",
    {
        "type": "object",
        "properties": {
            "title": {"type": "string"},
            "details": {"type": "string"},
            "due": {"type": "string", "description": "Optional. " + WHEN + ". The user is also reminded at this time."},
            "priority": {"type": "string", "enum": ["low", "normal", "high"]},
            "list": {"type": "string", "description": "Optional name of an existing list"},
        },
        "required": ["title"],
    },
)
@_guard
async def add_todo(args: dict[str, Any]) -> dict[str, Any]:
    req = {k: args[k] for k in ("title", "details", "priority", "list") if args.get(k)}
    if problem := _due_arg(args, req):
        return _err(problem)
    task = await hub.host_call("task_add", req)
    return _ok(f"Added to the {task['list']} list as #{_ref('t', task['id'])}"
               + (f", due {_fmt_due(task)}." if task.get("due") else "."))


@tool("list_todos", "List the open tasks in the user's Microsoft To Do, across all lists.", {"type": "object", "properties": {}})
@_guard
async def list_todos(args: dict[str, Any]) -> dict[str, Any]:
    tasks = await hub.host_call("tasks_list", {})
    if not tasks:
        return _ok("Microsoft To Do has no open tasks.")
    return _ok("\n".join(
        f"#{_ref('t', t['id'])} {t['title']} (list {t['list']}, priority {t['priority']}, due {_fmt_due(t)})"
        + (f" - {t['details']}" if t.get("details") else "") for t in tasks
    ))


@tool(
    "update_todo",
    "Change a Microsoft To Do task: mark it done or not done, rename it, or change its due time, priority or details. "
    "The id is a reference such as t3 from list_todos.",
    {
        "type": "object",
        "properties": {
            "id": {"type": "string"},
            "done": {"type": "boolean"},
            "title": {"type": "string"},
            "details": {"type": "string"},
            "due": {"type": "string", "description": WHEN + ', or "" to clear'},
            "priority": {"type": "string", "enum": ["low", "normal", "high"]},
        },
        "required": ["id"],
    },
)
@_guard
async def update_todo(args: dict[str, Any]) -> dict[str, Any]:
    ids = _resolve(args["id"], "t")
    if not ids:
        return _err("Unknown task reference. Call list_todos first.")
    req = {k: args[k] for k in ("done", "title", "details", "priority") if k in args}
    if problem := _due_arg(args, req):
        return _err(problem)
    if not req:
        return _err("Nothing to change.")
    await hub.host_call("task_update", {"id": ids[-1], **req})
    return _ok("Updated.")


@tool("delete_todo", "Delete a Microsoft To Do task. Ask the user to confirm first.", {"id": str})
@_guard
async def delete_todo(args: dict[str, Any]) -> dict[str, Any]:
    ids = _resolve(args["id"], "t")
    if not ids:
        return _err("Unknown task reference. Call list_todos first.")
    await hub.host_call("task_delete", {"id": ids[-1]})
    return _ok("Deleted.")


# --- OneNote desktop app ------------------------------------------------------------

NOTE_BODY = ("Plain text, one paragraph per line. Start a line with '- ' for a bullet point, "
             "'[ ] ' for a checkbox item, or '# ' for a heading.")


@tool(
    "save_note",
    "Create a new page in the user's OneNote. It goes to the Quick Notes section unless a section name is given "
    "(see list_note_sections).",
    {
        "type": "object",
        "properties": {
            "title": {"type": "string"},
            "body": {"type": "string", "description": NOTE_BODY},
            "section": {"type": "string", "description": "Optional name of an existing section, e.g. 'Meetings'"},
        },
        "required": ["title", "body"],
    },
)
@_guard
async def save_note(args: dict[str, Any]) -> dict[str, Any]:
    req = {"title": args["title"], "body": args["body"]}
    if args.get("section"):
        req["section"] = args["section"]
    page = await hub.host_call("note_create", req)
    return _ok(f"OneNote page created in section {page.get('section', 'Quick Notes')} as #{_ref('n', page['id'])}.")


@tool("list_note_sections", "List the user's OneNote notebooks and their sections.", {"type": "object", "properties": {}})
@_guard
async def list_note_sections(args: dict[str, Any]) -> dict[str, Any]:
    sections = await hub.host_call("note_sections", {})
    if not sections:
        return _ok("OneNote has no open notebooks.")
    return _ok("\n".join(f"{s['notebook']} / {s['section']} ({s['pages']} pages)" for s in sections))


@tool(
    "search_notes",
    "Find OneNote pages by words in the title or the page text. An empty query lists the most recently changed pages.",
    {"type": "object", "properties": {"query": {"type": "string"}}},
)
@_guard
async def search_notes(args: dict[str, Any]) -> dict[str, Any]:
    pages = await hub.host_call("notes_search", {"query": args.get("query", "")})
    if not pages:
        return _ok("No matching OneNote pages.")
    return _ok("\n".join(
        f"#{_ref('n', p['id'])} {p.get('title') or 'Untitled'} (section {p.get('section', '?')}, changed {p.get('changed', '')[:10]})"
        for p in pages
    ))


@tool("read_note", "Read a OneNote page as plain text. The id is a reference such as n2 from search_notes.", {"id": str})
@_guard
async def read_note(args: dict[str, Any]) -> dict[str, Any]:
    ids = _resolve(args["id"], "n")
    if not ids:
        return _err("Unknown note reference. Call search_notes first.")
    content = await hub.host_call("note_read", {"id": ids[-1]})
    return _ok((content[:6000] + ("\n[truncated]" if len(content) > 6000 else "")) or "The page is empty.")


@tool(
    "update_note",
    "Add text to the end of a OneNote page. Existing page content cannot be replaced, only appended to.",
    {"type": "object", "properties": {"id": {"type": "string"}, "body": {"type": "string", "description": NOTE_BODY}},
     "required": ["id", "body"]},
)
@_guard
async def update_note(args: dict[str, Any]) -> dict[str, Any]:
    ids = _resolve(args["id"], "n")
    if not ids:
        return _err("Unknown note reference. Call search_notes first.")
    await hub.host_call("note_append", {"id": ids[-1], "body": args["body"]})
    return _ok("Added to the page.")


@tool("delete_note", "Delete a OneNote page (it goes to the notebook's recycle bin). Ask the user to confirm first.", {"id": str})
@_guard
async def delete_note(args: dict[str, Any]) -> dict[str, Any]:
    ids = _resolve(args["id"], "n")
    if not ids:
        return _err("Unknown note reference. Call search_notes first.")
    await hub.host_call("note_delete", {"id": ids[-1]})
    return _ok("Deleted.")


# --- Outlook calendar ------------------------------------------------------------------

def _clock(iso: str) -> str:
    return datetime.fromisoformat(iso).strftime("%a %d %b %H:%M")


def describe_event(e: dict) -> str:
    when = f"{datetime.fromisoformat(e['start']).strftime('%a %d %b')} all day" if e.get("all_day") \
        else f"{_clock(e['start'])} to {datetime.fromisoformat(e['end']).strftime('%H:%M')}"
    extra = "".join(f", {label} {e[key]}" for key, label in (("location", "at"), ("organizer", "organised by")) if e.get(key))
    kind = "meeting" if e.get("is_meeting") else "appointment"
    return f"{when}: {e['title']} ({kind}{extra}, shown as {e.get('busy', 'busy')})"


@tool(
    "list_events",
    "List events on the user's Outlook calendar between two local date-times, earliest first (recurring events included).",
    {
        "type": "object",
        "properties": {
            "start": {"type": "string", "description": WHEN},
            "end": {"type": "string", "description": WHEN},
        },
        "required": ["start", "end"],
    },
)
@_guard
async def list_events(args: dict[str, Any]) -> dict[str, Any]:
    try:
        start, end = db.parse_when(args["start"]), db.parse_when(args["end"])
    except ValueError:
        return _err(f"Could not read those times. Use {WHEN}.")
    if end <= start:
        return _err("The end must be after the start.")
    events = await hub.host_call("calendar_list", {"start": _local_iso(start), "end": _local_iso(end)})
    if not events:
        return _ok("Nothing on the calendar in that period.")
    return _ok("\n".join(describe_event(e) for e in events))


@tool(
    "add_event",
    "Put a personal appointment on the user's Outlook calendar, for example a focus block. It has no attendees and "
    "sends no invitations; you cannot invite other people. Say back the title and time when done.",
    {
        "type": "object",
        "properties": {
            "title": {"type": "string"},
            "start": {"type": "string", "description": WHEN},
            "end": {"type": "string", "description": "Optional. " + WHEN + ". Defaults to 30 minutes after the start."},
            "location": {"type": "string"},
            "details": {"type": "string"},
        },
        "required": ["title", "start"],
    },
)
@_guard
async def add_event(args: dict[str, Any]) -> dict[str, Any]:
    try:
        start = db.parse_when(args["start"])
        end = db.parse_when(args["end"]) if args.get("end") else start + 1800
    except ValueError:
        return _err(f"Could not read those times. Use {WHEN}.")
    if end <= start:
        return _err("The end must be after the start.")
    req = {"title": args["title"], "start": _local_iso(start), "end": _local_iso(end)}
    req |= {k: args[k] for k in ("location", "details") if args.get(k)}
    event = await hub.host_call("calendar_add", req)
    return _ok("Added to the calendar: " + describe_event(event))


TASK_TOOLS = [add_todo, list_todos, update_todo, delete_todo]
NOTE_TOOLS = [save_note, search_notes, read_note, update_note, delete_note]
NOTE_EXTRA_TOOLS = [list_note_sections]
CALENDAR_TOOLS = [list_events, add_event]
