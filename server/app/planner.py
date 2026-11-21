"""Planning features: task triage, the daily wrap-up, the weekly report, meeting notes, focus time,
snoozing and the usage report. The tools work with wherever the todo list, notes and calendar live
(the desktop bridge, Microsoft Graph, or the pet's own database), so they are offered in every mode.
"""
from __future__ import annotations

import logging
import random
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from claude_agent_sdk import tool

from . import config, db, hub, prefs, tools
from .ms_tools import WHEN, _err, _ok, _ref

log = logging.getLogger("pet.planner")
DAY = 86400


# --- reading the user's data, wherever it lives ------------------------------------------

def _local_iso(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, config.TZ).strftime("%Y-%m-%dT%H:%M:%S")


def _local_epoch(value: str | None) -> int | None:
    """Epoch of a local date-time string from the bridge (no offset), or None."""
    try:
        return db.parse_when(value) if value else None
    except ValueError:
        return None


def _utc_epoch(value: str | None) -> int | None:
    """Epoch of a UTC date-time string from Graph, such as 2026-11-10T05:00:00.0000000Z."""
    if not value:
        return None
    try:
        return int(datetime.fromisoformat(value[:19]).replace(tzinfo=timezone.utc).timestamp())
    except ValueError:
        return None


def _day_start(epoch: int | None = None) -> int:
    at = datetime.fromtimestamp(epoch or db.ts(), config.TZ)
    return int(at.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())


async def open_tasks(mode: tools.Mode) -> list[dict]:
    """Open tasks as {ref, title, list, priority, due, created}; due and created are epochs or None.
    A task with only a due date (no time) counts as due at the end of that day."""
    found: list[dict] = []
    if mode.todos == "desktop":
        for t in await hub.host_call("tasks_list", {}):
            due = _local_epoch(t.get("reminder"))
            if due is None and t.get("due"):
                due = _local_epoch(t["due"]) + DAY - 1
            found.append({"ref": _ref("t", t["id"]), "title": t["title"], "list": t.get("list", ""),
                          "priority": t.get("priority", "normal"), "due": due, "created": _local_epoch(t.get("created"))})
    elif mode.todos == "graph":
        from . import ms_tools
        for lst, t in await ms_tools.open_tasks():
            due = ms_tools.graph_epoch(t.get("reminderDateTime")) if t.get("isReminderOn") else None
            if due is None and t.get("dueDateTime"):
                due = ms_tools.graph_epoch(t["dueDateTime"]) + DAY - 1
            found.append({"ref": _ref("t", lst["id"], t["id"]), "title": t["title"], "list": lst["displayName"],
                          "priority": t.get("importance", "normal"), "due": due, "created": _utc_epoch(t.get("createdDateTime"))})
    else:
        for t in db.rows("SELECT * FROM todos WHERE done=0 ORDER BY id"):
            found.append({"ref": str(t["id"]), "title": t["title"], "list": "", "priority": t["priority"],
                          "due": t["due_at"], "created": t["created"]})
    return found


async def done_tasks(mode: tools.Mode, since: int) -> list[dict]:
    """Tasks completed since the given time, as {title, list, completed}."""
    if mode.todos == "desktop":
        tasks = await hub.host_call("tasks_done", {"since": _local_iso(_day_start(since))})
        return [{"title": t["title"], "list": t.get("list", ""), "completed": _local_epoch(t.get("completed")) or since}
                for t in tasks if (_local_epoch(t.get("completed")) or since) >= _day_start(since)]
    if mode.todos == "graph":
        from . import ms_tools
        from .msgraph import call
        found = []
        for lst in await ms_tools.task_lists():
            data = await call("GET", f"/me/todo/lists/{lst['id']}/tasks", headers=ms_tools.UTC,
                              params={"$filter": "status eq 'completed'", "$top": "100"})
            for t in data.get("value", []):
                when = ms_tools.graph_epoch(t.get("completedDateTime"))
                if when and when >= since:
                    found.append({"title": t["title"], "list": lst["displayName"], "completed": when})
        return found
    return [{"title": t["title"], "list": "", "completed": t["completed_at"]}
            for t in db.rows("SELECT title, completed_at FROM todos WHERE done=1 AND completed_at>=? ORDER BY completed_at", (since,))]


async def events(mode: tools.Mode, start: int, end: int) -> list[dict] | None:
    """Calendar events between two times, or None if the calendar cannot be seen."""
    if not mode.calendar:
        return None
    return await hub.host_call("calendar_list", {"start": _local_iso(start), "end": _local_iso(end)})


async def pages_changed(mode: tools.Mode, since: int) -> list[str]:
    """Titles of note pages changed since the given time (best effort)."""
    if mode.notes == "desktop":
        pages = await hub.host_call("notes_search", {"query": ""})
        return [p.get("title") or "Untitled" for p in pages
                if (_utc_epoch(p.get("changed")) or 0) >= since]
    if mode.notes == "local":
        return [n["title"] for n in db.rows("SELECT title FROM notes WHERE updated>=? ORDER BY updated", (since,))]
    return []


def busy_blocks(evts: list[dict]) -> list[tuple[int, int]]:
    """Merged (start, end) periods in which the user is not free."""
    spans = sorted((db.parse_when(e["start"]), db.parse_when(e["end"])) for e in evts
                   if not e.get("all_day") and e.get("busy") != "free")
    merged: list[tuple[int, int]] = []
    for s, e in spans:
        if merged and s <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], e))
        else:
            merged.append((s, e))
    return merged


def free_slots(evts: list[dict], start: int, end: int, min_minutes: int) -> list[tuple[int, int]]:
    """Gaps of at least min_minutes between start and end that have nothing on the calendar."""
    slots, cursor = [], start
    for s, e in busy_blocks(evts):
        if e <= cursor:
            continue
        if s - cursor >= min_minutes * 60:
            slots.append((cursor, min(s, end)))
        cursor = max(cursor, e)
        if cursor >= end:
            break
    if end - cursor >= min_minutes * 60:
        slots.append((cursor, end))
    return [(s, e) for s, e in slots if e - s >= min_minutes * 60]


def _work_window(day: datetime) -> tuple[int, int]:
    ws, we = prefs.clock("work_start"), prefs.clock("work_end")
    return (int(day.replace(hour=ws.hour, minute=ws.minute, second=0, microsecond=0).timestamp()),
            int(day.replace(hour=we.hour, minute=we.minute, second=0, microsecond=0).timestamp()))


def _next_workday(after: datetime) -> datetime:
    day = after + timedelta(days=1)
    while day.weekday() >= 5:
        day += timedelta(days=1)
    return day


def _hm(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, config.TZ).strftime("%H:%M")


def _slots_text(slots: list[tuple[int, int]]) -> str:
    return ", ".join(f"{_hm(s)}-{_hm(e)}" for s, e in slots[:6]) or "none"


def _event_line(e: dict) -> str:
    if e.get("all_day"):
        return f"all day: {e['title']}"
    return f"{_hm(db.parse_when(e['start']))}-{_hm(db.parse_when(e['end']))} {e['title']}" + \
        (" (meeting)" if e.get("is_meeting") else "")


async def _call(name: str, args: dict[str, Any]) -> tuple[bool, str]:
    """Run another of the pet's tools (in its current form: local, Graph or desktop) and return (ok, text)."""
    mode = tools.current_mode()
    target = next((t for t in tools.active_tools(mode) if t.name == name), None)
    if target is None:
        return False, f"{name} is not available."
    result = await target.handler(args)
    return not result.get("is_error"), result["content"][0]["text"]


def _guard(fn):
    async def wrapped(args: dict[str, Any]) -> dict[str, Any]:
        try:
            return await fn(args)
        except hub.HostError as e:
            return _err(str(e))
        except Exception as e:  # Graph errors and the like: report them to the model rather than failing the turn
            log.exception("%s failed", fn.__name__)
            return _err(f"That did not work: {e}")
    return wrapped


# --- task triage --------------------------------------------------------------------------

@tool(
    "review_tasks",
    "Sort the user's open tasks for triage: overdue, due soon, without a due date, and stale (open for a long time), "
    "plus free time on the calendar today and on the next working day. Use it when the user asks to sort, review, "
    "triage or plan their tasks.",
    {"type": "object", "properties": {}},
)
@_guard
async def review_tasks(args: dict[str, Any]) -> dict[str, Any]:
    mode = tools.current_mode()
    tasks = await open_tasks(mode)
    if not tasks:
        return _ok("There are no open tasks.")
    now = db.ts()
    stale_before = now - prefs.minutes("stale_task_days") * DAY

    def line(t: dict) -> str:
        due = f", due {db.fmt(t['due'])}" if t["due"] else ""
        age = f", open {round((now - t['created']) / DAY)} days" if t["created"] else ""
        where = f", list {t['list']}" if t["list"] else ""
        return f"#{t['ref']} {t['title']} (priority {t['priority']}{due}{age}{where})"

    overdue = [t for t in tasks if t["due"] and t["due"] < now]
    soon = [t for t in tasks if t["due"] and now <= t["due"] < now + 2 * DAY]
    undated = [t for t in tasks if not t["due"]]
    stale = [t for t in tasks if t["created"] and t["created"] < stale_before]
    parts = [f"{len(tasks)} open tasks."]
    for label, group in (("Overdue", overdue), ("Due in the next two days", soon),
                         ("No due date", undated), (f"Stale (open more than {prefs.get('stale_task_days')} days)", stale)):
        if group:
            parts.append(f"{label} ({len(group)}):\n" + "\n".join(line(t) for t in group))
    today = config.now()
    if mode.calendar:
        for label, day in (("today", today), ("next working day " + _next_workday(today).strftime("%a %d %b"), _next_workday(today))):
            start, end = _work_window(day)
            start = max(start, now)
            if end <= start:
                parts.append(f"Free time {label}: the working day is over.")
                continue
            slots = free_slots(await events(mode, start, end) or [], start, end, 30)
            parts.append(f"Free time {label} (30 minutes or more, working hours): {_slots_text(slots)}")
    else:
        parts.append("The calendar cannot be seen, so free time is unknown.")
    parts.append("To set a date use update_todo with the reference; to block time use add_event.")
    return _ok("\n\n".join(parts))


@tool(
    "find_free_time",
    "Find free periods on the user's calendar on one day, within working hours unless whole_day is true.",
    {
        "type": "object",
        "properties": {
            "date": {"type": "string", "description": "The day, e.g. 2026-11-12"},
            "min_minutes": {"type": "integer", "description": "Shortest useful gap; default 30"},
            "whole_day": {"type": "boolean", "description": "Look from 07:00 to 22:00 instead of working hours"},
        },
        "required": ["date"],
    },
)
@_guard
async def find_free_time(args: dict[str, Any]) -> dict[str, Any]:
    mode = tools.current_mode()
    if not mode.calendar:
        return _err("The calendar cannot be seen right now.")
    try:
        day = datetime.fromisoformat(str(args["date"])[:10]).replace(tzinfo=config.TZ)
    except ValueError:
        return _err("Give the date as YYYY-MM-DD.")
    if args.get("whole_day"):
        start, end = int(day.replace(hour=7).timestamp()), int(day.replace(hour=22).timestamp())
    else:
        start, end = _work_window(day)
    start = max(start, db.ts())
    if end <= start:
        return _ok("That time has already passed.")
    slots = free_slots(await events(mode, start, end) or [], start, end, int(args.get("min_minutes") or 30))
    return _ok(f"Free on {day.strftime('%a %d %b')}: {_slots_text(slots)}")


# --- daily wrap-up and weekly report ----------------------------------------------------------

@tool(
    "day_summary",
    "Summarise one day: tasks completed that day, tasks still open that were due by then, the day's calendar, and "
    "the first commitments of the next working day. Use it for the end-of-day wrap-up.",
    {"type": "object", "properties": {"date": {"type": "string", "description": "The day, e.g. 2026-11-10; default today"}}},
)
@_guard
async def day_summary(args: dict[str, Any]) -> dict[str, Any]:
    mode = tools.current_mode()
    try:
        day = datetime.fromisoformat(str(args["date"])[:10]).replace(tzinfo=config.TZ) if args.get("date") else config.now()
    except ValueError:
        return _err("Give the date as YYYY-MM-DD.")
    start = _day_start(int(day.timestamp()))
    end = start + DAY
    done = [t for t in await done_tasks(mode, start) if t["completed"] < end]
    open_ = await open_tasks(mode)
    due = [t for t in open_ if t["due"] and t["due"] < end]
    parts = [
        f"Completed on {day.strftime('%a %d %b')} ({len(done)}): " + ("; ".join(t["title"] for t in done) or "nothing marked done"),
        f"Still open and due by the end of that day ({len(due)}):\n" + ("\n".join(
            f"#{t['ref']} {t['title']} (due {db.fmt(t['due'])})" for t in due) or "none"),
        f"Open tasks without a due date: {sum(1 for t in open_ if not t['due'])}",
    ]
    today = await events(mode, start, end)
    if today is not None:
        parts.append("Calendar that day:\n" + ("\n".join(_event_line(e) for e in today) or "nothing"))
        nxt = _next_workday(day.replace(hour=0, minute=0, second=0, microsecond=0))
        ns = int(nxt.timestamp())
        tomorrow = await events(mode, ns, ns + DAY) or []
        parts.append(f"Next working day {nxt.strftime('%a %d %b')}:\n" + ("\n".join(_event_line(e) for e in tomorrow[:5]) or "nothing on the calendar"))
    return _ok("\n\n".join(parts))


@tool(
    "week_summary",
    "Collect material for a weekly report: tasks completed in a week (Monday to Sunday), meetings attended, note "
    "pages changed, and tasks still overdue. Draft the report from it.",
    {"type": "object", "properties": {"week_of": {"type": "string", "description": "Any date in the week, e.g. 2026-11-10; default this week"}}},
)
@_guard
async def week_summary(args: dict[str, Any]) -> dict[str, Any]:
    mode = tools.current_mode()
    try:
        day = datetime.fromisoformat(str(args["week_of"])[:10]).replace(tzinfo=config.TZ) if args.get("week_of") else config.now()
    except ValueError:
        return _err("Give the date as YYYY-MM-DD.")
    monday = day.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=day.weekday())
    start, end = int(monday.timestamp()), int((monday + timedelta(days=7)).timestamp())
    done = [t for t in await done_tasks(mode, start) if t["completed"] < end]
    parts = [f"Week of {monday.strftime('%d %b %Y')}.",
             f"Tasks completed ({len(done)}):\n" + ("\n".join(
                 f"- {datetime.fromtimestamp(t['completed'], config.TZ).strftime('%a')}: {t['title']}" for t in done) or "none recorded")]
    evts = await events(mode, start, min(end, db.ts() + DAY))
    if evts is not None:
        meetings = [e for e in evts if e.get("is_meeting") and not e.get("all_day")]
        hours = sum(db.parse_when(e["end"]) - db.parse_when(e["start"]) for e in meetings) / 3600
        titles = sorted({e["title"] for e in meetings})
        parts.append(f"Meetings: {len(meetings)}, about {hours:.1f} hours. Titles: " + ("; ".join(titles) or "none"))
    pages = await pages_changed(mode, start)
    if pages:
        parts.append("Note pages changed: " + "; ".join(pages))
    overdue = [t for t in await open_tasks(mode) if t["due"] and t["due"] < db.ts()]
    parts.append(f"Still overdue ({len(overdue)}): " + ("; ".join(t["title"] for t in overdue) or "none"))
    return _ok("\n\n".join(parts))


# --- meeting notes ----------------------------------------------------------------------------

@tool(
    "save_meeting_notes",
    "Save notes from a meeting: creates one note page (in a 'Meetings' section if there is one) with the notes and "
    "the action items, and adds each action item that is the user's own as a task.",
    {
        "type": "object",
        "properties": {
            "meeting": {"type": "string", "description": "Meeting name, e.g. 'Design review'"},
            "notes": {"type": "string", "description": "The notes as tidy lines; '- ' starts a bullet, '# ' a heading"},
            "action_items": {
                "type": "array",
                "description": "Follow-ups agreed in the meeting",
                "items": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string"},
                        "owner": {"type": "string", "description": "Who does it; 'me' for the user"},
                        "due": {"type": "string", "description": "Optional. " + WHEN},
                    },
                    "required": ["title"],
                },
            },
            "section": {"type": "string", "description": "Optional note section; default 'Meetings'"},
        },
        "required": ["meeting", "notes"],
    },
)
@_guard
async def save_meeting_notes(args: dict[str, Any]) -> dict[str, Any]:
    items = args.get("action_items") or []
    mine = [a for a in items if (a.get("owner") or "me").strip().lower() in ("me", "i", "myself", "user", "", prefs.get("user_name").lower())]
    lines = [f"# Notes", args["notes"].strip()]
    if items:
        lines.append("# Action items")
        for a in items:
            owner = (a.get("owner") or "me").strip()
            due = ""
            if a.get("due"):
                try:
                    due = f", due {db.fmt(db.parse_when(a['due']))}"
                except ValueError:
                    pass
            lines.append(f"[ ] {a['title']} ({owner}{due})")
    title = f"{args['meeting'].strip()} - {config.now().strftime('%d %b %Y')}"
    note = {"title": title, "body": "\n".join(lines), "section": (args.get("section") or "Meetings").strip()}
    ok, text = await _call("save_note", note)
    if not ok and "section" in text.lower():
        note.pop("section")
        ok, text = await _call("save_note", note)
    if not ok:
        return _err("Could not save the notes: " + text)
    added, failed = [], []
    for a in mine:
        task = {"title": a["title"], "details": f"From the meeting: {args['meeting']}"}
        if a.get("due"):
            task["due"] = a["due"]
        task_ok, task_text = await _call("add_todo", task)
        (added if task_ok else failed).append(a["title"] if task_ok else f"{a['title']} ({task_text})")
    from . import worklog
    worklog.queue(config.now().date(), "Meeting notes",
                  f"{args['meeting'].strip()}: notes saved" + (f", {len(items)} action item(s)" if items else ""))
    result = f"Notes saved: {text}"
    if added:
        result += f" Added {len(added)} task(s): " + "; ".join(added) + "."
    if failed:
        result += " Could not add: " + "; ".join(failed) + "."
    if len(mine) < len(items):
        result += f" {len(items) - len(mine)} action item(s) belong to other people and are only on the page."
    return _ok(result)


# --- focus time -----------------------------------------------------------------------------

def focus_state() -> dict | None:
    until = int(db.kv_get("focus_until") or 0)
    if until <= db.ts():
        return None
    return {"until": until, "label": db.kv_get("focus_label") or ""}


@tool(
    "start_focus",
    "Start focus time: until it ends the pet stays silent except for reminders and meeting alerts, it can block "
    "the time on the calendar, and it sets the user's Teams status to Do not disturb (cleared automatically at the "
    "end). At the end it checks in with the user.",
    {
        "type": "object",
        "properties": {
            "minutes": {"type": "integer", "description": "5 to 240"},
            "label": {"type": "string", "description": "What the user is focusing on"},
            "block_calendar": {"type": "boolean", "description": "Put a 'Focus' appointment on the calendar; default true"},
            "teams_dnd": {"type": "boolean", "description": "Set the user's Teams status to Do not disturb until focus "
                                                           "ends; default from the focus_teams_dnd_enabled setting"},
        },
        "required": ["minutes"],
    },
)
@_guard
async def start_focus(args: dict[str, Any]) -> dict[str, Any]:
    minutes = int(args["minutes"])
    if not 5 <= minutes <= 240:
        return _err("Focus time must be between 5 and 240 minutes.")
    now = db.ts()
    until = now + minutes * 60
    label = (args.get("label") or "").strip()
    db.kv_set("focus_until", str(until))
    db.kv_set("focus_label", label)
    if label:
        from . import worklog
        worklog.queue(config.now().date(), "Focus", f"{label} ({minutes} min)")
    extra = ""
    mode = tools.current_mode()
    if args.get("block_calendar", True) and mode.calendar:
        try:
            await hub.host_call("calendar_add", {"title": f"Focus: {label}" if label else "Focus time", "start": _local_iso(now),
                                                 "end": _local_iso(until), "details": f"Blocked by {config.PET_NAME}."})
            extra = " The time is blocked on the calendar."
        except hub.HostError as e:
            extra = f" Could not block the calendar: {e}"
    extra += await _teams_dnd(args.get("teams_dnd"), minutes)
    return _ok(f"Focus time until {_hm(until)}.{extra}")


async def _teams_dnd(wanted: bool | None, minutes: int) -> str:
    """Set Teams to Do not disturb for the focus time; returns a sentence for the tool result."""
    from . import msgraph
    if wanted is None:
        wanted = prefs.get("focus_teams_dnd_enabled") == "on"
    if not wanted:
        return ""
    if not msgraph.signed_in():
        return " Teams status not changed: Microsoft sign-in is not set up."
    try:
        await msgraph.set_dnd(minutes)
    except msgraph.GraphError as e:
        return f" Could not set Teams to Do not disturb: {e}"
    db.kv_set("focus_teams_dnd", "1")
    return " Teams is set to Do not disturb until then."


@tool("stop_focus", "End focus time early.", {"type": "object", "properties": {}})
async def stop_focus(args: dict[str, Any]) -> dict[str, Any]:
    if not focus_state():
        return _ok("Focus time was not on.")
    db.kv_set("focus_until", "0")
    extra = ""
    if db.kv_get("focus_teams_dnd") == "1":
        from . import msgraph
        db.kv_set("focus_teams_dnd", "0")
        try:
            await msgraph.clear_status()
            extra = " Teams status is back to automatic."
        except msgraph.GraphError as e:
            extra = f" Could not reset the Teams status, so it stays Do not disturb until the planned end: {e}"
    return _ok(f"Focus time ended. Any calendar block stays on the calendar.{extra}")


# --- snooze ------------------------------------------------------------------------------------

SNOOZABLE = ("reminder", "nudge", "meeting", "break", "water", "followup", "focus", "wrapup")


@tool(
    "snooze",
    "Repeat the most recent reminder, task nudge, meeting alert or other unprompted message after some minutes. "
    "Use it when the user says 'snooze', 'remind me again in ten minutes' or similar right after you told them something.",
    {"type": "object", "properties": {"minutes": {"type": "integer", "description": "1 to 240; default 10"}}},
)
async def snooze(args: dict[str, Any]) -> dict[str, Any]:
    minutes = int(args.get("minutes") or 10)
    if not 1 <= minutes <= 240:
        return _err("Snooze for 1 to 240 minutes.")
    last = db.one(
        f"SELECT * FROM outbox WHERE delivered=1 AND delivered_at>=? AND kind IN ({','.join('?' * len(SNOOZABLE))}) "
        "ORDER BY delivered_at DESC, id DESC", (db.ts() - 3 * 3600, *SNOOZABLE))
    if not last:
        return _err("Nothing was announced in the last three hours, so there is nothing to snooze. Use add_reminder instead.")
    when = db.ts() + minutes * 60
    hub.announce_at(when, "reminder" if last["kind"] in ("meeting", "followup") else last["kind"], last["title"], last["body"])
    return _ok(f"Snoozed until {_hm(when)}: {last['body']}")


# --- water ----------------------------------------------------------------------------------------

def water_today() -> int:
    """Glasses of water the user has told the pet about today."""
    day, _, count = (db.kv_get("water_day") or "").partition(":")
    return int(count) if day == config.now().date().isoformat() and count.isdigit() else 0


@tool(
    "log_water",
    "Record that the user just drank water. Use it whenever they say they drank, had or finished a glass, cup or bottle "
    "of water (including in reply to a water reminder). It restarts the water reminder clock and returns today's count.",
    {"type": "object", "properties": {"glasses": {"type": "integer", "description": "How many glasses; default 1"}}},
)
async def log_water(args: dict[str, Any]) -> dict[str, Any]:
    glasses = int(args.get("glasses") or 1)
    if not 1 <= glasses <= 10:
        return _err("Log 1 to 10 glasses at a time.")
    total = water_today() + glasses
    db.kv_set("water_day", f"{config.now().date().isoformat()}:{total}")
    db.kv_set("water_last", str(db.ts()))
    every = prefs.minutes("water_every_minutes")
    nxt = f" Next reminder in about {every} minutes." if every else " Water reminders are off."
    return _ok(f"Logged. {total} glass{'es' if total != 1 else ''} of water today.{nxt}")


# --- usage ---------------------------------------------------------------------------------------

@tool(
    "usage_report",
    "How much the assistant has been used: conversation turns and estimated cost per day.",
    {"type": "object", "properties": {"days": {"type": "integer", "description": "How many days back; default 7"}}},
)
async def usage_report(args: dict[str, Any]) -> dict[str, Any]:
    days = max(1, min(int(args.get("days") or 7), 90))
    found = db.usage_days(days)
    if not found:
        return _ok("No usage recorded in that period.")
    total_turns, total_cost = sum(d["turns"] for d in found), sum(d["cost"] for d in found)
    return _ok(f"Last {days} days: {total_turns} turns, about ${total_cost:.2f} estimated (API-equivalent; a "
               f"subscription is not billed per turn). Daily limit {config.DAILY_TURN_LIMIT} turns.\n"
               + "\n".join(f"{d['day']}: {d['turns']} turns, ${d['cost']:.2f}" for d in found))


# --- delivering long texts (summaries, rephrasings) -----------------------------------------------

@tool(
    "deliver_text",
    "Give the user a text you wrote for them to use elsewhere, such as a summary, a rephrasing, a draft or meeting "
    "minutes: it is copied to their clipboard and shown on the chat page. Speak only a short version yourself. Only "
    "for texts they asked you to write; never for ordinary answers, replies or conversation, however long.",
    {"text": str},
)
async def deliver_text(args: dict[str, Any]) -> dict[str, Any]:
    text = str(args["text"]).strip()
    if not text:
        return _err("The text is empty.")
    db.kv_set("last_result", text)
    db.log_message("result", text)
    await hub.broadcast({"type": "result_text", "text": text})
    return _ok(f"Copied to the clipboard ({len(text.split())} words) and shown on the chat page.")


@tool(
    "paste_text",
    "Type a text where the user's cursor is, in whatever app they are using. Without a text, pastes the last text "
    "given with deliver_text.",
    {"type": "object", "properties": {"text": {"type": "string"}}},
)
async def paste_text(args: dict[str, Any]) -> dict[str, Any]:
    text = (args.get("text") or db.kv_get("last_result") or "").strip()
    if not text:
        return _err("There is nothing to paste yet.")
    ws = next(iter(hub.hosts & hub.clients), None)
    if ws is None or not await hub.send(ws, {"type": "type_text", "text": text}):
        return _err("The desktop pet is not running, so nothing can be pasted.")
    return _ok("Pasted at the cursor.")


TOOLS = [review_tasks, find_free_time, day_summary, week_summary, save_meeting_notes,
         start_focus, stop_focus, snooze, log_water, usage_report, deliver_text, paste_text]


# --- background jobs, run by the scheduler ------------------------------------------------------------

_calendar: list[dict] = []
calendar_checked = 0.0
_calendar_read_ok = 0.0   # when the calendar was last read successfully


async def refresh_calendar() -> list[dict]:
    """Events from three hours ago to at least two hours ahead, re-read every five minutes."""
    global _calendar, calendar_checked, _calendar_read_ok
    if not hub.host_caps.get("calendar"):
        if time.monotonic() - _calendar_read_ok > 1800:   # gone for half an hour: forget it
            _calendar = []
        return _calendar
    if time.monotonic() - calendar_checked < 300:
        return _calendar
    calendar_checked = time.monotonic()
    now = db.ts()
    ahead = max(2 * 3600, prefs.minutes("meeting_lead_minutes") * 60 + 900)
    try:
        _calendar = await hub.host_call("calendar_list", {"start": _local_iso(now - 3 * 3600), "end": _local_iso(now + ahead)})
        _calendar_read_ok = time.monotonic()
    except hub.HostError as e:
        log.warning("could not read the calendar: %s", e)
    return _calendar


def cached_events() -> list[dict]:
    return _calendar


def meeting_now() -> dict | None:
    """The meeting on the calendar happening right now, if any (a real meeting shown as busy, not an appointment)."""
    now = db.ts()
    for e in _calendar:
        if e.get("is_meeting") and not e.get("all_day") and e.get("busy") in ("busy", "out of office") \
                and db.parse_when(e["start"]) <= now < db.parse_when(e["end"]):
            return e
    return None


def hold_reason() -> str | None:
    """Why unprompted messages other than reminders are being held back right now."""
    if prefs.enabled("meeting_quiet_enabled") and meeting_now():
        return "meeting"
    if focus_state():
        return "focus"
    return None


def meeting_followups() -> None:
    """After a meeting ends, offer to capture notes and action items."""
    if not prefs.enabled("meeting_followup_enabled"):
        return
    now = db.ts()
    for e in _calendar:
        if not e.get("is_meeting") or e.get("all_day") or e.get("busy") == "free":
            continue
        end = db.parse_when(e["end"])
        if not now - 900 <= end <= now:
            continue
        key = f"end:{e['id']}@{end}"
        if db.one("SELECT 1 FROM ms_nudged WHERE key=?", (key,)):
            continue
        db.run("INSERT INTO ms_nudged(key, created) VALUES(?, ?)", (key, now))
        hub.announce("followup", "Meeting finished",
                     f"{e['title']} has finished. If there are notes or action items, tell me and I'll put them in OneNote and your tasks.")


def focus_end() -> None:
    until = int(db.kv_get("focus_until") or 0)
    if until and until <= db.ts():
        db.kv_set("focus_until", "0")
        db.kv_set("focus_teams_dnd", "0")  # Teams lets the status expire on its own at this time
        label = db.kv_get("focus_label") or ""
        hub.announce("focus", "Focus time over",
                     f"Your focus time{' on ' + label if label else ''} is over. Nice work. How did it go?")


_BREAKS = [
    "You've been at it for a while. Stand up, stretch, and look at something far away for a minute.",
    "Time for a short break. Roll your shoulders, have some water, and come back fresh.",
    "Quick break? A minute away from the screen will help your eyes and your back.",
]


def break_nudge() -> None:
    every = prefs.minutes("break_every_minutes")
    if not every or hub.active_since is None or not prefs.in_work_hours() or hold_reason():
        return
    if db.ts() - hub.active_since >= every * 60:
        hub.active_since = db.ts()
        hub.announce("break", "Break", random.choice(_BREAKS))


_WATER = [
    "Time for some water. Grab a glass and take a few sips.",
    "Quick reminder to drink some water. Your brain works better hydrated.",
    "Water break! Have a glass, then carry on.",
    "It's been a while since your last drink. How about some water?",
]


def water_nudge() -> None:
    """Remind the user to drink water every water_every_minutes, only while they are at the PC and nothing is
    holding messages back (quiet hours, a meeting, focus time). Saying they drank restarts the clock (log_water)."""
    every = prefs.minutes("water_every_minutes")
    if not every:
        return
    now = db.ts()
    last = int(db.kv_get("water_last") or 0)
    if not last:
        db.kv_set("water_last", str(now))      # first run: start counting from now
        return
    if now - last < every * 60:
        return
    if hub.active_since is None or not hub.user_active() or prefs.in_quiet_hours() or hold_reason():
        return
    db.kv_set("water_last", str(now))
    if db.one("SELECT 1 FROM outbox WHERE kind='break' AND created>=?", (now - 600,)):
        return                                  # a break reminder just went out, and it already mentions water
    glasses = water_today()
    so_far = f" That would be glass number {glasses + 1} today." if glasses else ""
    hub.announce("water", "Drink water", random.choice(_WATER) + so_far)
