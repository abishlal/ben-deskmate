"""The assistant's tools, exposed to Claude as an in-process MCP server.

To add an integration later (Microsoft To Do, OneNote, Teams), write more
@tool functions and add them to ALL_TOOLS; nothing else needs to change.
"""
from __future__ import annotations

import re
from typing import Any, NamedTuple

from claude_agent_sdk import create_sdk_mcp_server, tool

from . import config, db, prefs

SERVER_NAME = "pet"


def _ok(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}]}


def _err(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}], "is_error": True}


WHEN = "ISO 8601 local date-time, e.g. 2026-11-10T17:00"


# --- reminders --------------------------------------------------------------

@tool(
    "add_reminder",
    "Set a reminder that will be spoken to the user at the given time.",
    {
        "type": "object",
        "properties": {
            "text": {"type": "string", "description": "What to remind the user about"},
            "when": {"type": "string", "description": WHEN},
            "repeat": {"type": "string", "enum": ["none", "daily", "weekdays", "weekly"]},
        },
        "required": ["text", "when"],
    },
)
async def add_reminder(args: dict[str, Any]) -> dict[str, Any]:
    try:
        due = db.parse_when(args["when"])
    except ValueError:
        return _err(f"Could not read the time {args['when']!r}. Use {WHEN}.")
    if due <= db.ts() and args.get("repeat", "none") == "none":
        return _err("That time is in the past.")
    rid = db.run(
        "INSERT INTO reminders(text, due_at, repeat, created) VALUES(?,?,?,?)",
        (args["text"], due, args.get("repeat", "none"), db.ts()),
    )
    return _ok(f"Reminder {rid} set for {db.fmt(due)}.")


@tool("list_reminders", "List pending reminders, soonest first.", {"type": "object", "properties": {}})
async def list_reminders(args: dict[str, Any]) -> dict[str, Any]:
    found = db.rows("SELECT * FROM reminders WHERE status='pending' ORDER BY due_at")
    if not found:
        return _ok("No pending reminders.")
    return _ok("\n".join(
        f"#{r['id']} {db.fmt(r['due_at'])}{'' if r['repeat'] == 'none' else ' (' + r['repeat'] + ')'}: {r['text']}"
        for r in found
    ))


@tool("cancel_reminder", "Cancel a reminder by id.", {"id": int})
async def cancel_reminder(args: dict[str, Any]) -> dict[str, Any]:
    n = db.run("UPDATE reminders SET status='cancelled' WHERE id=? AND status='pending'", (args["id"],))
    return _ok("Cancelled.") if n else _err("No pending reminder with that id.")


# --- todos ------------------------------------------------------------------

@tool(
    "add_todo",
    "Add a task to the user's todo list.",
    {
        "type": "object",
        "properties": {
            "title": {"type": "string"},
            "details": {"type": "string"},
            "due": {"type": "string", "description": "Optional. " + WHEN},
            "priority": {"type": "string", "enum": ["low", "normal", "high"]},
        },
        "required": ["title"],
    },
)
async def add_todo(args: dict[str, Any]) -> dict[str, Any]:
    due = None
    if args.get("due"):
        try:
            due = db.parse_when(args["due"])
        except ValueError:
            return _err(f"Could not read the due time {args['due']!r}. Use {WHEN}.")
    tid = db.run(
        "INSERT INTO todos(title, details, due_at, priority, created) VALUES(?,?,?,?,?)",
        (args["title"], args.get("details", ""), due, args.get("priority", "normal"), db.ts()),
    )
    return _ok(f"Todo {tid} added" + (f", due {db.fmt(due)}." if due else "."))


@tool(
    "list_todos",
    "List todos. By default only open ones.",
    {"type": "object", "properties": {"include_done": {"type": "boolean"}}},
)
async def list_todos(args: dict[str, Any]) -> dict[str, Any]:
    where = "" if args.get("include_done") else "WHERE done=0"
    found = db.rows(f"SELECT * FROM todos {where} ORDER BY done, due_at IS NULL, due_at, id")
    if not found:
        return _ok("The todo list is empty.")
    return _ok("\n".join(
        f"#{t['id']} [{'x' if t['done'] else ' '}] {t['title']} "
        f"(priority {t['priority']}, due {db.fmt(t['due_at'])})" + (f" - {t['details']}" if t["details"] else "")
        for t in found
    ))


@tool(
    "update_todo",
    "Change a todo: mark it done or not done, rename it, or change its due time, priority or details.",
    {
        "type": "object",
        "properties": {
            "id": {"type": "integer"},
            "done": {"type": "boolean"},
            "title": {"type": "string"},
            "details": {"type": "string"},
            "due": {"type": "string", "description": WHEN + ', or "" to clear'},
            "priority": {"type": "string", "enum": ["low", "normal", "high"]},
        },
        "required": ["id"],
    },
)
async def update_todo(args: dict[str, Any]) -> dict[str, Any]:
    sets, vals = [], []
    if "done" in args:
        sets += ["done=?", "completed_at=?"]
        vals += [1 if args["done"] else 0, db.ts() if args["done"] else None]
    for key in ("title", "details", "priority"):
        if key in args:
            sets.append(f"{key}=?")
            vals.append(args[key])
    if "due" in args:
        try:
            due = db.parse_when(args["due"]) if args["due"] else None
        except ValueError:
            return _err(f"Could not read the due time {args['due']!r}.")
        sets += ["due_at=?", "nudged=0"]
        vals.append(due)
    if not sets:
        return _err("Nothing to change.")
    n = db.run(f"UPDATE todos SET {', '.join(sets)} WHERE id=?", (*vals, args["id"]))
    if n and args.get("done"):
        from . import hub  # (imported here, as in the other tools that reach the pet)
        await hub.broadcast({"type": "cheer"})  # the pet gives a thumbs up
    return _ok("Updated.") if n else _err("No todo with that id.")


@tool("delete_todo", "Permanently delete a todo. Ask the user to confirm first.", {"id": int})
async def delete_todo(args: dict[str, Any]) -> dict[str, Any]:
    n = db.run("DELETE FROM todos WHERE id=?", (args["id"],))
    return _ok("Deleted.") if n else _err("No todo with that id.")


# --- notes ------------------------------------------------------------------

@tool("save_note", "Save a new note.", {"title": str, "body": str})
async def save_note(args: dict[str, Any]) -> dict[str, Any]:
    nid = db.run(
        "INSERT INTO notes(title, body, created, updated) VALUES(?,?,?,?)",
        (args["title"], args["body"], db.ts(), db.ts()),
    )
    return _ok(f"Note {nid} saved.")


@tool(
    "search_notes",
    "Search notes by words in the title or body. An empty query lists the most recent notes.",
    {"type": "object", "properties": {"query": {"type": "string"}}},
)
async def search_notes(args: dict[str, Any]) -> dict[str, Any]:
    q = f"%{args.get('query', '')}%"
    found = db.rows(
        "SELECT id, title, substr(body, 1, 120) AS preview, updated FROM notes "
        "WHERE title LIKE ? OR body LIKE ? ORDER BY updated DESC LIMIT 15",
        (q, q),
    )
    if not found:
        return _ok("No matching notes.")
    return _ok("\n".join(f"#{n['id']} {n['title']} ({db.fmt(n['updated'])}): {n['preview']}" for n in found))


@tool("read_note", "Read a note in full by id.", {"id": int})
async def read_note(args: dict[str, Any]) -> dict[str, Any]:
    n = db.one("SELECT * FROM notes WHERE id=?", (args["id"],))
    return _ok(f"{n['title']}\n\n{n['body']}") if n else _err("No note with that id.")


@tool(
    "update_note",
    "Replace a note's body, or append to it.",
    {
        "type": "object",
        "properties": {"id": {"type": "integer"}, "body": {"type": "string"}, "append": {"type": "boolean"}},
        "required": ["id", "body"],
    },
)
async def update_note(args: dict[str, Any]) -> dict[str, Any]:
    sql = "UPDATE notes SET body = body || char(10) || ?, updated=? WHERE id=?" if args.get("append") \
        else "UPDATE notes SET body=?, updated=? WHERE id=?"
    n = db.run(sql, (args["body"], db.ts(), args["id"]))
    return _ok("Note updated.") if n else _err("No note with that id.")


@tool("delete_note", "Permanently delete a note. Ask the user to confirm first.", {"id": int})
async def delete_note(args: dict[str, Any]) -> dict[str, Any]:
    n = db.run("DELETE FROM notes WHERE id=?", (args["id"],))
    return _ok("Deleted.") if n else _err("No note with that id.")


# --- long-term memory -------------------------------------------------------

@tool(
    "remember",
    "Store a lasting fact about the user (a preference, a person, a routine) so it is known in future conversations.",
    {"fact": str},
)
async def remember(args: dict[str, Any]) -> dict[str, Any]:
    mid = db.run("INSERT INTO memory(fact, created) VALUES(?,?)", (args["fact"], db.ts()))
    return _ok(f"Remembered as #{mid}.")


@tool("forget", "Remove a stored fact by id.", {"id": int})
async def forget(args: dict[str, Any]) -> dict[str, Any]:
    n = db.run("DELETE FROM memory WHERE id=?", (args["id"],))
    return _ok("Forgotten.") if n else _err("No stored fact with that id.")


# --- chat history and settings ---------------------------------------------------

@tool(
    "search_history",
    "Search earlier conversations with the user (saved across days). Use it when they refer to something said before "
    "that you do not remember. An empty query returns the most recent messages.",
    {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Words to look for"},
            "days": {"type": "integer", "description": "How many days back to look; default 30"},
        },
    },
)
async def search_history(args: dict[str, Any]) -> dict[str, Any]:
    since = db.ts() - int(args.get("days") or 30) * 86400
    words = re.findall(r"\w+", str(args.get("query") or "").lower())
    found: list[dict] = []
    if words and db.HAS_FTS:
        # Any of the words, best matches first; a word also matches longer words it starts ("deploy" -> "deployment").
        match = " OR ".join(f'"{w}"*' for w in words[:12])
        try:
            found = db.rows(
                "SELECT m.ts, m.role, m.text FROM messages_fts f JOIN messages m ON m.id = f.rowid "
                "WHERE messages_fts MATCH ? AND m.ts>=? ORDER BY f.rank LIMIT 20", (match, since))
            found.sort(key=lambda m: m["ts"], reverse=True)
        except Exception:
            found = []
    if not found:
        found = db.rows("SELECT ts, role, text FROM messages WHERE ts>=? AND text LIKE ? ORDER BY id DESC LIMIT 20",
                        (since, f"%{args.get('query', '')}%"))
    # Day summaries reach further back than the raw search window.
    days = [s for s in db.rows("SELECT day, summary FROM day_summaries WHERE summary != '' ORDER BY day DESC LIMIT 120")
            if not words or any(w in s["summary"].lower() for w in words)][:5]
    if days:
        found_text = "Day summaries:\n" + "\n".join(f"{s['day']}: {s['summary']}" for s in days[::-1])
    else:
        found_text = ""
    if not found and not found_text:
        return _ok("Nothing matching in the saved conversations.")
    who = {"user": "User", "pet": "You", "announce": "You (unprompted)", "result": "You (full text delivered)"}
    lines = "\n".join(f"{db.fmt(m['ts'])} {who.get(m['role'], m['role'])}: {m['text'][:400]}" for m in found[::-1])
    return _ok("\n\n".join(p for p in (found_text, lines) if p))


@tool(
    "set_preference",
    "Change one of the pet's settings when the user asks, for example quiet hours, the briefing time, or how early to warn about meetings.",
    {
        "type": "object",
        "properties": {
            "key": {"type": "string", "enum": list(prefs.DEFAULTS)},
            "value": {"type": "string", "description": "HH:MM for times, a number for minutes, on/off for switches"},
        },
        "required": ["key", "value"],
    },
)
async def set_preference(args: dict[str, Any]) -> dict[str, Any]:
    try:
        stored = prefs.put(args["key"], args["value"])
    except ValueError as e:
        return _err(str(e))
    if args["key"] == "stay_on_screen":
        from . import hub
        await hub.sync_stay()
    return _ok(f"{args['key']} is now {stored}.")


ALL_TOOLS = [
    add_reminder, list_reminders, cancel_reminder,
    add_todo, list_todos, update_todo, delete_todo,
    save_note, search_notes, read_note, update_note, delete_note,
    remember, forget, search_history, set_preference,
]


class Mode(NamedTuple):
    """Where todos and notes live for the current conversation."""
    todos: str = "local"   # local | graph | desktop
    notes: str = "local"   # local | graph | desktop
    calendar: bool = False  # Outlook calendar reachable through the desktop bridge


def current_mode() -> Mode:
    """Microsoft Graph if signed in; otherwise the desktop bridge for whatever it can reach; otherwise local."""
    from . import hub, msgraph
    calendar = bool(hub.host_caps.get("calendar"))
    if msgraph.signed_in():
        return Mode("graph", "graph", calendar)
    return Mode("desktop" if hub.host_caps.get("tasks") else "local",
                "desktop" if hub.host_caps.get("notes") else "local", calendar)


def active_tools(mode: Mode) -> list:
    """Local tools, with the todo and note tools swapped for their Microsoft-backed versions as the mode says,
    plus the calendar tools when Outlook's calendar is reachable."""
    swap: list = []
    if mode.calendar:
        from . import host_tools
        swap += host_tools.CALENDAR_TOOLS
    if "graph" in mode:
        from . import ms_tools
        swap += [t for t, kind in ((t, "todos" if "todo" in t.name else "notes") for t in ms_tools.TOOLS)
                 if getattr(mode, kind) == "graph"]
    if "desktop" in mode:
        from . import host_tools
        swap += host_tools.TASK_TOOLS if mode.todos == "desktop" else []
        swap += (host_tools.NOTE_TOOLS + host_tools.NOTE_EXTRA_TOOLS) if mode.notes == "desktop" else []
    from . import planner, worklog
    replaced = {t.name for t in swap}
    return [t for t in ALL_TOOLS if t.name not in replaced] + swap + planner.TOOLS + worklog.TOOLS


def allowed_names(mode: Mode) -> list[str]:
    return [f"mcp__{SERVER_NAME}__{t.name}" for t in active_tools(mode)]


def build_server(mode: Mode):
    return create_sdk_mcp_server(name=SERVER_NAME, version="1.0.0", tools=active_tools(mode))
