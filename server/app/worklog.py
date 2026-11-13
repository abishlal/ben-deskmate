"""Work log for appraisals.

OneNote holds the log, and nothing else does: one page per month ("Work log - November 2026") in a
"Work Log" section, one line per entry, for example

    Tue 11 Nov · Delivery · Fixed the payment timeout bug (errors down 40%)

so anything the user edits there is what the pet reads back. Locally there are only two bookkeeping pieces:
a queue of lines not yet written (OneNote unreachable, laptop asleep) and a record of what was logged
automatically, so a completed task or a day's meetings is never logged twice.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import time
from datetime import date, datetime, timedelta
from typing import Any

from claude_agent_sdk import tool

from . import config, db, hub, prefs, tools
from .ms_tools import _err, _ok

log = logging.getLogger("pet.worklog")
SECTION = config.WORKLOG_SECTION
KINDS = ["Delivery", "Achievement", "Learning", "Collaboration", "Feedback", "Task done", "Meetings",
         "Meeting notes", "Focus", "Note"]
_LINE_DATE = re.compile(r"^(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)\w*\s+(\d{1,2})\s+([A-Z][a-z]{2})\b")
_MONTHS = {m: i for i, m in enumerate(["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], 1)}


def _title(fmt: str, month: str) -> str:
    d = datetime.strptime(month + "-01", "%Y-%m-%d")
    text = d.strftime(fmt)
    if "%b" in fmt and d.month == 9:
        text = text.replace("Sep", "Sept", 1)   # "Sept, 2024", as people write it
    return text


def page_title(month: str) -> str:
    """'2026-11' -> 'Work log - November 2026' (or as PET_WORKLOG_PAGE says, e.g. 'Nov, 2026')."""
    return _title(config.WORKLOG_PAGE, month)


def year_title(month: str) -> str | None:
    """The yearly page the month's page sits under, if PET_WORKLOG_YEAR_PAGE is set ('2026')."""
    return _title(config.WORKLOG_YEAR_PAGE, month) if config.WORKLOG_YEAR_PAGE else None


def make_line(day: date, kind: str, text: str) -> str:
    text = " ".join(str(text).split())
    return f"{day.strftime('%a')} {day.day} {day.strftime('%b')} · {kind} · {text}"


def seen(key: str) -> bool:
    return db.one("SELECT 1 FROM ms_nudged WHERE key=?", (key,)) is not None


def queue(day: date, kind: str, text: str, key: str | None = None) -> bool:
    """Add an entry to the queue for OneNote. With a key, an entry already logged under it is skipped."""
    if not str(text).strip():
        return False
    if key:
        if seen(key):
            return False
        db.run("INSERT INTO ms_nudged(key, created) VALUES(?, ?)", (key, db.ts()))
    db.run("INSERT INTO worklog_queue(month, line, created) VALUES(?,?,?)",
           (day.strftime("%Y-%m"), make_line(day, kind, text), db.ts()))
    return True


def pending_count() -> int:
    return (db.one("SELECT COUNT(*) AS n FROM worklog_queue") or {}).get("n", 0)


# --- OneNote --------------------------------------------------------------------------------------

async def _find_page(month: str) -> str | None:
    cached = db.kv_get(f"worklog_page:{month}")
    if cached:
        return cached
    title = page_title(month)
    pages = await hub.host_call("notes_search", {"query": title})
    exact = [p for p in pages if (p.get("title") or "").strip().lower() == title.lower()]
    exact.sort(key=lambda p: p.get("section") != SECTION)   # the one in the Work Log section first
    if not exact:
        return None
    db.kv_set(f"worklog_page:{month}", exact[0]["id"])
    return exact[0]["id"]


async def _create_page(month: str, body: str) -> str:
    req = {"title": page_title(month), "body": body}
    if year_title(month):
        req["parent"] = year_title(month)
    try:
        await hub.host_call("note_section_ensure", {"name": SECTION})
        req["section"] = SECTION
    except hub.HostError as e:
        log.warning("could not make the %s section, using the default one: %s", SECTION, e)
    page = await hub.host_call("note_create", req)
    db.kv_set(f"worklog_page:{month}", page["id"])
    return page["id"]


async def _write(month: str, lines: list[str]) -> None:
    body = "\n".join(lines)
    page = await _find_page(month)
    if page is None:
        await _create_page(month, body)
        return
    try:
        await hub.host_call("note_append", {"id": page, "body": body})
    except hub.HostError:
        # The page may have been deleted or moved: look for it again, or start a new one.
        db.run("DELETE FROM kv WHERE key=?", (f"worklog_page:{month}",))
        page = await _find_page(month)
        if page is None:
            await _create_page(month, body)
        else:
            await hub.host_call("note_append", {"id": page, "body": body})


_flush_lock = asyncio.Lock()
_retry_at = 0.0


async def flush(force: bool = False) -> str | None:
    """Write queued lines to OneNote. Returns None when done (or nothing to do), else why it could not."""
    global _retry_at
    if not pending_count():
        return None
    if not hub.host_caps.get("notes"):
        return "OneNote cannot be reached right now; the entry will be added as soon as it can."
    if not force and time.monotonic() < _retry_at:
        return "Waiting to retry OneNote."
    async with _flush_lock:
        rows = db.rows("SELECT * FROM worklog_queue ORDER BY id")
        for month in sorted({r["month"] for r in rows}):
            batch = [r for r in rows if r["month"] == month]
            try:
                await _write(month, [r["line"] for r in batch])
            except hub.HostError as e:
                _retry_at = time.monotonic() + 300
                log.warning("work log not written yet: %s", e)
                return f"Could not write to OneNote yet ({e}); it stays queued."
            db.run(f"DELETE FROM worklog_queue WHERE id IN ({','.join('?' * len(batch))})", tuple(r["id"] for r in batch))
            log.info("work log: %d line(s) written for %s", len(batch), month)
    return None


async def read(start: date, end: date) -> list[tuple[str, list[str]]]:
    """(page title, lines) for each month from start to end; dated lines outside the range are left out.
    Lines the user wrote in their own format (no date in front) are kept, since their month is known."""
    out = []
    month = date(start.year, start.month, 1)
    while month <= end:
        key = month.strftime("%Y-%m")
        lines: list[str] = []
        page = await _find_page(key)
        if page:
            title = page_title(key)
            text = await hub.host_call("note_read", {"id": page})
            for line in (text or "").splitlines():
                line = line.strip()
                if not line or line.lower() == title.lower() or line.lower().startswith("# work log"):
                    continue
                m = _LINE_DATE.match(line)
                if m and _MONTHS.get(m[2]):
                    try:
                        d = date(month.year, _MONTHS[m[2]], int(m[1]))
                    except ValueError:
                        d = None
                    if d and not start <= d <= end:
                        continue
                lines.append(line)
        lines += [r["line"] + "  (not in OneNote yet)" for r in db.rows("SELECT line FROM worklog_queue WHERE month=? ORDER BY id", (key,))]
        if lines:
            out.append((page_title(key), lines))
        month = (month + timedelta(days=32)).replace(day=1)
    return out


# --- automatic entries ----------------------------------------------------------------------------

async def collect_day(day: date) -> int:
    """Log one day's completed tasks and meetings. Returns how many entries were added."""
    from . import planner
    mode = tools.current_mode()
    start = int(datetime(day.year, day.month, day.day, tzinfo=config.TZ).timestamp())
    end = start + 86400
    added = 0
    for t in await planner.done_tasks(mode, start):
        if t["completed"] < end:
            where = f" ({t['list']})" if t.get("list") and t["list"].lower() not in ("tasks", "to do") else ""
            key = "wl:task:" + hashlib.sha1(f"{t['title']}@{day}".encode()).hexdigest()[:16]
            added += queue(day, "Task done", t["title"] + where, key)
    evts = await planner.events(mode, start, end)
    if evts:
        meetings = [e for e in evts if e.get("is_meeting") and not e.get("all_day") and e.get("busy") != "free"]
        if meetings:
            def length(e: dict) -> str:
                minutes = round((db.parse_when(e["end"]) - db.parse_when(e["start"])) / 60)
                return f"{minutes} min" if minutes < 60 else f"{minutes / 60:.1f} h".replace(".0 h", " h")
            text = "; ".join(f"{e['title']} ({length(e)})" for e in meetings)
            added += queue(day, "Meetings", f"{len(meetings)} on the calendar: {text}", f"wl:meetings@{day}")
    return added


_last_collect = 0.0


async def daily_collect() -> None:
    """Catch up on past working days (up to a week back) and, after the wrap-up time, today."""
    global _last_collect
    if not (hub.host_caps.get("tasks") and hub.host_caps.get("notes")) or time.monotonic() - _last_collect < 600:
        return
    _last_collect = time.monotonic()
    today = config.now().date()
    last = db.kv_get("worklog_collected_through")
    first = max(date.fromisoformat(last) + timedelta(days=1), today - timedelta(days=7)) if last else today - timedelta(days=1)
    try:
        day = first
        while day < today:
            if day.weekday() < 5:
                await collect_day(day)
            db.kv_set("worklog_collected_through", day.isoformat())
            day += timedelta(days=1)
        if config.now().time() >= prefs.clock("wrapup_time") and db.kv_get("worklog_today") != today.isoformat():
            await collect_day(today)
            db.kv_set("worklog_today", today.isoformat())
    except hub.HostError as e:
        log.warning("work log collection postponed: %s", e)
    await flush()


# --- tools -------------------------------------------------------------------------------------

@tool(
    "log_work",
    "Add an entry to the user's work log (kept in OneNote, used for their appraisal). Use it when the user tells you "
    "about something they did, delivered, learned, helped with, or feedback they got, and asks you to log or note it, "
    "or answers the wrap-up question about their day. One entry per item, with any numbers or impact they mention.",
    {
        "type": "object",
        "properties": {
            "text": {"type": "string", "description": "Short, specific, past tense, e.g. 'Fixed the payment timeout bug (errors down 40%)'"},
            "kind": {"type": "string", "enum": KINDS},
            "date": {"type": "string", "description": "The day it happened, e.g. 2026-11-10; default today"},
        },
        "required": ["text", "kind"],
    },
)
async def log_work(args: dict[str, Any]) -> dict[str, Any]:
    try:
        day = date.fromisoformat(str(args["date"])[:10]) if args.get("date") else config.now().date()
    except ValueError:
        return _err("Give the date as YYYY-MM-DD.")
    text = str(args["text"]).strip()
    if not queue(day, args["kind"], text, "wl:manual:" + hashlib.sha1(f"{day}:{text.lower()}".encode()).hexdigest()[:16]):
        return _ok("That is already in the work log for that day.")
    db.kv_set(f"wl_manual:{day}", ((db.kv_get(f"wl_manual:{day}") or "") + "\n" + text).strip()[-4000:])
    problem = await flush(force=True)
    return _ok(f"Logged for {day.strftime('%a %d %b')}." + (f" {problem}" if problem else " It is in OneNote."))


@tool(
    "read_work_log",
    "Read the user's work log between two dates, for questions like 'what did I do in October' or to draft an "
    "appraisal self-review. Covers at most twelve months per call.",
    {
        "type": "object",
        "properties": {"start": {"type": "string", "description": "e.g. 2026-04-01"}, "end": {"type": "string", "description": "e.g. 2026-09-30; default today"}},
        "required": ["start"],
    },
)
async def read_work_log(args: dict[str, Any]) -> dict[str, Any]:
    try:
        start = date.fromisoformat(str(args["start"])[:10])
        end = date.fromisoformat(str(args["end"])[:10]) if args.get("end") else config.now().date()
    except ValueError:
        return _err("Give the dates as YYYY-MM-DD.")
    if end < start:
        return _err("The end must be after the start.")
    if (end - start).days > 366:
        return _err("Ask for at most twelve months at a time.")
    try:
        months = await read(start, end)
    except hub.HostError as e:
        return _err(f"Could not read the work log from OneNote: {e}")
    if not months:
        return _ok("Nothing in the work log for that period.")
    text = "\n\n".join(f"{title}\n" + "\n".join(lines) for title, lines in months)
    if len(text) > 40000:
        text = text[:40000] + "\n[truncated: ask for a shorter period for the rest]"
    return _ok(text)


TOOLS = [log_work, read_work_log]
