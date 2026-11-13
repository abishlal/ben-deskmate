"""Todo and note tools backed by Microsoft To Do and OneNote.

They have the same names as the local tools in tools.py and replace them while the user is signed in to Microsoft.
"""
from __future__ import annotations

import html
import re
from datetime import datetime, timezone
from typing import Any

from claude_agent_sdk import tool

from . import config, db
from .msgraph import GraphError, call

WHEN = "ISO 8601 local date-time, e.g. 2026-11-10T17:00"

# Graph ids are long opaque strings, so the model is given short references instead.
_refs: dict[str, tuple[str, ...]] = {}
_ref_of: dict[tuple[str, ...], str] = {}


def _ref(prefix: str, *ids: str) -> str:
    if ids not in _ref_of:
        ref = f"{prefix}{sum(1 for r in _refs if r.startswith(prefix)) + 1}"
        _refs[ref], _ref_of[ids] = ids, ref
    return _ref_of[ids]


def _resolve(ref: str, prefix: str) -> tuple[str, ...] | None:
    return _refs.get(str(ref).strip().lstrip("#").lower()) if str(ref).strip().lstrip("#").lower().startswith(prefix) else None


def _ok(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}]}


def _err(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}], "is_error": True}


def _graph_time(epoch: int) -> dict[str, str]:
    return {"dateTime": datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S"), "timeZone": "UTC"}


def graph_epoch(value: dict | None) -> int | None:
    """Epoch seconds from a Graph dateTimeTimeZone value (these tools always ask Graph for UTC)."""
    if not value or not value.get("dateTime"):
        return None
    return int(datetime.fromisoformat(value["dateTime"][:19]).replace(tzinfo=timezone.utc).timestamp())


def _fmt_due(task: dict) -> str:
    when = graph_epoch(task.get("reminderDateTime")) if task.get("isReminderOn") else None
    if when:
        return db.fmt(when)
    due = graph_epoch(task.get("dueDateTime"))
    return datetime.fromtimestamp(due, config.TZ).strftime("%a %d %b %Y") if due else "no date"


UTC = {"Prefer": 'outlook.timezone="UTC"'}


async def task_lists() -> list[dict]:
    return (await call("GET", "/me/todo/lists", params={"$top": "50"})).get("value", [])


async def open_tasks() -> list[tuple[dict, dict]]:
    """(list, task) for every task that is not completed, across all To Do lists."""
    found = []
    for lst in await task_lists():
        data = await call("GET", f"/me/todo/lists/{lst['id']}/tasks", headers=UTC,
                          params={"$filter": "status ne 'completed'", "$top": "100"})
        found += [(lst, t) for t in data.get("value", [])]
    return found


def _guard(fn):
    async def wrapped(args: dict[str, Any]) -> dict[str, Any]:
        try:
            return await fn(args)
        except GraphError as e:
            return _err(str(e))
    return wrapped


# --- Microsoft To Do ----------------------------------------------------------

@tool(
    "add_todo",
    "Add a task to the user's Microsoft To Do. Goes to the default list unless a list name is given.",
    {
        "type": "object",
        "properties": {
            "title": {"type": "string"},
            "details": {"type": "string"},
            "due": {"type": "string", "description": "Optional. " + WHEN + ". To Do also reminds the user at this time."},
            "priority": {"type": "string", "enum": ["low", "normal", "high"]},
            "list": {"type": "string", "description": "Optional name of an existing To Do list"},
        },
        "required": ["title"],
    },
)
@_guard
async def add_todo(args: dict[str, Any]) -> dict[str, Any]:
    lists = await task_lists()
    if not lists:
        return _err("The user has no To Do lists.")
    wanted = (args.get("list") or "").strip().lower()
    if wanted:
        target = next((l for l in lists if l["displayName"].lower() == wanted), None)
        if not target:
            return _err("No list with that name. Lists: " + ", ".join(l["displayName"] for l in lists))
    else:
        target = next((l for l in lists if l.get("wellknownListName") == "defaultList"), lists[0])
    body: dict[str, Any] = {"title": args["title"], "importance": args.get("priority", "normal")}
    if args.get("details"):
        body["body"] = {"content": args["details"], "contentType": "text"}
    due = None
    if args.get("due"):
        try:
            due = db.parse_when(args["due"])
        except ValueError:
            return _err(f"Could not read the due time {args['due']!r}. Use {WHEN}.")
        body |= {"dueDateTime": _graph_time(due), "reminderDateTime": _graph_time(due), "isReminderOn": True}
    task = await call("POST", f"/me/todo/lists/{target['id']}/tasks", json=body)
    ref = _ref("t", target["id"], task["id"])
    return _ok(f"Added to the {target['displayName']} list as #{ref}" + (f", due {db.fmt(due)}." if due else "."))


@tool("list_todos", "List the open tasks in the user's Microsoft To Do, across all lists.", {"type": "object", "properties": {}})
@_guard
async def list_todos(args: dict[str, Any]) -> dict[str, Any]:
    found = await open_tasks()
    if not found:
        return _ok("Microsoft To Do has no open tasks.")
    return _ok("\n".join(
        f"#{_ref('t', lst['id'], t['id'])} {t['title']} (list {lst['displayName']}, priority {t.get('importance', 'normal')}, "
        f"due {_fmt_due(t)})" for lst, t in found
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
    body: dict[str, Any] = {}
    if "done" in args:
        body["status"] = "completed" if args["done"] else "notStarted"
    if "title" in args:
        body["title"] = args["title"]
    if "priority" in args:
        body["importance"] = args["priority"]
    if "details" in args:
        body["body"] = {"content": args["details"], "contentType": "text"}
    if "due" in args:
        if args["due"]:
            try:
                due = db.parse_when(args["due"])
            except ValueError:
                return _err(f"Could not read the due time {args['due']!r}.")
            body |= {"dueDateTime": _graph_time(due), "reminderDateTime": _graph_time(due), "isReminderOn": True}
        else:
            body |= {"dueDateTime": None, "reminderDateTime": None, "isReminderOn": False}
    if not body:
        return _err("Nothing to change.")
    await call("PATCH", f"/me/todo/lists/{ids[0]}/tasks/{ids[1]}", json=body)
    return _ok("Updated.")


@tool("delete_todo", "Permanently delete a Microsoft To Do task. Ask the user to confirm first.", {"id": str})
@_guard
async def delete_todo(args: dict[str, Any]) -> dict[str, Any]:
    ids = _resolve(args["id"], "t")
    if not ids:
        return _err("Unknown task reference. Call list_todos first.")
    await call("DELETE", f"/me/todo/lists/{ids[0]}/tasks/{ids[1]}")
    return _ok("Deleted.")


# --- OneNote ------------------------------------------------------------------

def _to_html(text: str) -> str:
    return "".join(f"<p>{html.escape(line)}</p>" for line in text.splitlines() if line.strip()) or "<p></p>"


def _to_text(page_html: str) -> str:
    body = re.sub(r"(?is)<(head|script|style).*?</\1>", "", page_html)
    body = re.sub(r"(?i)<\s*(br|/p|/div|/li|/h[1-6]|/tr)[^>]*>", "\n", body)
    lines = (html.unescape(re.sub(r"<[^>]+>", "", body)).replace("\xa0", " ")).splitlines()
    return "\n".join(l.strip() for l in lines if l.strip())


@tool("save_note", "Create a new page in the user's OneNote (default notebook).", {"title": str, "body": str})
@_guard
async def save_note(args: dict[str, Any]) -> dict[str, Any]:
    page = (f"<!DOCTYPE html><html><head><title>{html.escape(args['title'])}</title></head>"
            f"<body>{_to_html(args['body'])}</body></html>")
    created = await call("POST", "/me/onenote/pages", content=page, headers={"Content-Type": "text/html"})
    return _ok(f"OneNote page created as #{_ref('n', created['id'])}.")


@tool(
    "search_notes",
    "Find OneNote pages by words in the page title (page text is not searched). An empty query lists the most recently changed pages.",
    {"type": "object", "properties": {"query": {"type": "string"}}},
)
@_guard
async def search_notes(args: dict[str, Any]) -> dict[str, Any]:
    params = {"$top": "15", "$orderby": "lastModifiedDateTime desc",
              "$select": "id,title,lastModifiedDateTime", "$expand": "parentSection($select=displayName)"}
    query = (args.get("query") or "").strip().lower().replace("'", "''")
    if query:
        params["$filter"] = f"contains(tolower(title),'{query}')"
    pages = (await call("GET", "/me/onenote/pages", params=params)).get("value", [])
    if not pages:
        return _ok("No OneNote pages with that in the title.")
    return _ok("\n".join(
        f"#{_ref('n', p['id'])} {p.get('title') or 'Untitled'} "
        f"(section {(p.get('parentSection') or {}).get('displayName', '?')}, changed {p['lastModifiedDateTime'][:10]})"
        for p in pages
    ))


@tool("read_note", "Read a OneNote page as plain text. The id is a reference such as n2 from search_notes.", {"id": str})
@_guard
async def read_note(args: dict[str, Any]) -> dict[str, Any]:
    ids = _resolve(args["id"], "n")
    if not ids:
        return _err("Unknown note reference. Call search_notes first.")
    content = _to_text(await call("GET", f"/me/onenote/pages/{ids[0]}/content", text=True))
    return _ok(content[:6000] + ("\n[truncated]" if len(content) > 6000 else "") or "The page is empty.")


@tool(
    "update_note",
    "Add text to the end of a OneNote page. Existing page content cannot be replaced, only appended to.",
    {"type": "object", "properties": {"id": {"type": "string"}, "body": {"type": "string"}}, "required": ["id", "body"]},
)
@_guard
async def update_note(args: dict[str, Any]) -> dict[str, Any]:
    ids = _resolve(args["id"], "n")
    if not ids:
        return _err("Unknown note reference. Call search_notes first.")
    await call("PATCH", f"/me/onenote/pages/{ids[0]}/content",
               json=[{"target": "body", "action": "append", "content": _to_html(args["body"])}])
    return _ok("Added to the page.")


@tool("delete_note", "Permanently delete a OneNote page. Ask the user to confirm first.", {"id": str})
@_guard
async def delete_note(args: dict[str, Any]) -> dict[str, Any]:
    ids = _resolve(args["id"], "n")
    if not ids:
        return _err("Unknown note reference. Call search_notes first.")
    await call("DELETE", f"/me/onenote/pages/{ids[0]}")
    return _ok("Deleted.")


TOOLS = [add_todo, list_todos, update_todo, delete_todo, save_note, search_notes, read_note, update_note, delete_note]
