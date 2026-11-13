"""Background loop: fires reminders, nudges about due todos, and gives the morning briefing."""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta

from . import config, db, hub, memory, msgraph, planner, prefs, tools, worklog
from .agent import DailyLimitReached, assistant, logged_in

log = logging.getLogger("pet.scheduler")
TICK_SECONDS = 15
last_tick = 0.0


def fire_due_reminders() -> None:
    for r in db.rows("SELECT * FROM reminders WHERE status='pending' AND due_at<=?", (db.ts(),)):
        hub.announce("reminder", "Reminder", f"Reminder: {r['text']}")
        nxt = db.next_occurrence(r["due_at"], r["repeat"])
        if nxt:
            db.run("UPDATE reminders SET due_at=? WHERE id=?", (nxt, r["id"]))
        else:
            db.run("UPDATE reminders SET status='done' WHERE id=?", (r["id"],))


def nudge_due_todos() -> None:
    horizon = db.ts() + prefs.minutes("nudge_lead_minutes") * 60
    for t in db.rows("SELECT * FROM todos WHERE done=0 AND nudged=0 AND due_at IS NOT NULL AND due_at<=?", (horizon,)):
        minutes = round((t["due_at"] - db.ts()) / 60)
        when = "is overdue" if minutes < 0 else "is due now" if minutes < 2 else f"is due in {minutes} minutes"
        hub.announce("nudge", "Task due", f"Heads up, your task {t['title']} {when}.")
        db.run("UPDATE todos SET nudged=1 WHERE id=?", (t["id"],))


_last_ms_check = 0.0


async def _microsoft_task_times(source: str) -> list[tuple[str, str, int]]:
    """(id, title, reminder time) for open Microsoft To Do tasks that have a reminder."""
    if source == "graph":
        from . import ms_tools
        return [(t["id"], t["title"], when) for _lst, t in await ms_tools.open_tasks()
                if t.get("isReminderOn") and (when := ms_tools.graph_epoch(t.get("reminderDateTime")))]
    from . import host_tools
    return [(t["id"], t["title"], when) for t in await hub.host_call("tasks_list", {})
            if (when := host_tools.task_time(t))]


async def nudge_microsoft_todos() -> None:
    """Speak up about Microsoft To Do tasks whose reminder time is near.
    Checked every two minutes through Graph, every five through the desktop bridge (it starts PowerShell)."""
    global _last_ms_check
    await hub.refresh_host_caps_if_stale()
    source = (await asyncio.to_thread(tools.current_mode)).todos
    if source == "local" or time.monotonic() - _last_ms_check < (120 if source == "graph" else 300):
        return
    _last_ms_check = time.monotonic()
    try:
        tasks = await _microsoft_task_times(source)
    except (msgraph.GraphError, hub.HostError) as e:
        log.warning("could not check Microsoft To Do: %s", e)
        return
    horizon = db.ts() + prefs.minutes("nudge_lead_minutes") * 60
    for task_id, title, when in tasks:
        if when > horizon or when < db.ts() - 6 * 3600:
            continue
        key = f"{task_id}@{when}"
        if db.one("SELECT 1 FROM ms_nudged WHERE key=?", (key,)):
            continue
        minutes = round((when - db.ts()) / 60)
        phrase = "is overdue" if minutes < 0 else "is due now" if minutes < 2 else f"is due in {minutes} minutes"
        hub.announce("nudge", "Task due", f"Heads up, your task {title} {phrase}.")
        db.run("INSERT INTO ms_nudged(key, created) VALUES(?, ?)", (key, db.ts()))


async def nudge_meetings() -> None:
    """Speak up shortly before an event on the calendar starts, mentioning a related note page if there is one."""
    lead = prefs.minutes("meeting_lead_minutes")
    if not lead:
        return
    from . import host_tools
    now = db.ts()
    for e in planner.cached_events():
        start = db.parse_when(e["start"])
        if e.get("all_day") or e.get("busy") == "free" or start < now or start > now + lead * 60 + 360:
            continue
        key = f"cal:{e['id']}@{start}"
        if db.one("SELECT 1 FROM ms_nudged WHERE key=?", (key,)):
            continue
        db.run("INSERT INTO ms_nudged(key, created) VALUES(?, ?)", (key, now))
        where = f" at {e['location']}" if e.get("location") else ""
        text = f"{e['title']} starts at {datetime.fromtimestamp(start, config.TZ).strftime('%H:%M')}{where}."
        if e.get("is_meeting") and hub.host_caps.get("notes"):
            text += await _related_page(e["title"])
        hub.announce_at(max(now, start - lead * 60), "meeting", "Coming up", text)


async def _related_page(title: str) -> str:
    """' You have a page called X ...' when a OneNote page seems to be about this meeting, else ''."""
    words = " ".join(w for w in title.replace("-", " ").replace(":", " ").split() if len(w) > 2)
    if not words:
        return ""
    try:
        pages = await hub.host_call("notes_search", {"query": words}, timeout=30)
    except hub.HostError:
        return ""
    if not pages:
        return ""
    return f" You have a OneNote page called {pages[0].get('title') or 'Untitled'} that may be about it; ask me to read it."


_meeting_quiet: str | None = None


async def meeting_quiet() -> None:
    """Tell the pets when a calendar meeting starts or ends, so they stop listening for the wake word during it."""
    global _meeting_quiet
    meeting = planner.meeting_now() if prefs.enabled("meeting_quiet_enabled") else None
    title = meeting["title"] if meeting else None
    if title != _meeting_quiet:
        _meeting_quiet = title
        log.info("meeting quiet %s", f"on ({title})" if title else "off")
        await hub.broadcast({"type": "meeting_quiet", "on": bool(title), "title": title or ""})


def meeting_quiet_now() -> bool:
    return bool(_meeting_quiet)


def _due_once_a_day(day_key: str, pref_on: str, pref_time: str, window_hours: int, workdays_only: bool) -> bool:
    """True once a day, after the set time, while the user is at the PC (not too long after the set time)."""
    if not prefs.enabled(pref_on) or not hub.connected() or prefs.in_quiet_hours() or not logged_in():
        return False
    now = config.now()
    today = now.strftime("%Y-%m-%d")
    if (workdays_only and now.weekday() >= 5) or db.kv_get(day_key) == today:
        return False
    at = now.replace(hour=prefs.clock(pref_time).hour, minute=prefs.clock(pref_time).minute, second=0, microsecond=0)
    if now < at:
        return False
    if now > at + timedelta(hours=window_hours):
        db.kv_set(day_key, today)  # the user was not around in time; skip today
        return False
    if not hub.user_active() or planner.hold_reason():
        return False  # wait until they are back at the PC and not in a meeting or focus time
    if hub.hosts and not hub.host_caps:
        return False  # the desktop pet has just connected: wait until Outlook and OneNote can be reached
    db.kv_set(day_key, today)  # one attempt a day, even if it fails
    return True


async def _automatic(kind: str, title: str, prompt: str) -> None:
    try:
        text = await assistant.ask("This is an automatic request, not the user speaking. " + prompt, record=False)
    except DailyLimitReached:
        return
    except Exception:
        log.exception("%s failed", kind)
        return
    if text:
        hub.announce(kind, title, text)


async def morning_briefing() -> None:
    if not _due_once_a_day("briefing_day", "briefing_enabled", "briefing_time", 4, workdays_only=False):
        return
    await _automatic("briefing", "Today",
                     "Give the user their briefing for today: look at today's calendar if you can see it, call review_tasks, "
                     "and check the pending reminders. Then say in at most five sentences what is on today, what is due or "
                     "overdue, and what you would suggest doing first. If some tasks have no due date or have been open for "
                     "weeks, say how many and offer to sort them with the user. If there is nothing at all, say so in one "
                     "cheerful sentence.")


async def daily_wrapup() -> None:
    if not _due_once_a_day("wrapup_day", "wrapup_enabled", "wrapup_time", 3, workdays_only=True):
        return
    friday = " It is Friday, so also offer to draft the weekly report." if config.now().weekday() == 4 else ""
    await _automatic("wrapup", "End of day",
                     "It is the end of the user's working day. Call day_summary for today. Then in at most four sentences say "
                     "what got done, name what is still open that was due today or is overdue and offer to move it to "
                     f"tomorrow, and mention the first thing on the next working day.{friday} End with one short question: "
                     "if nothing needs moving, ask whether anything from today is worth noting in their work log for "
                     "their appraisal (completed tasks and meetings are already logged automatically).")


async def run() -> None:
    global last_tick
    log.info("scheduler started")
    while True:
        try:
            fire_due_reminders()
            nudge_due_todos()
            await nudge_microsoft_todos()
            await planner.refresh_calendar()
            await meeting_quiet()
            await nudge_meetings()
            planner.meeting_followups()
            planner.focus_end()
            planner.break_nudge()
            await morning_briefing()
            await daily_wrapup()
            await worklog.daily_collect()
            if worklog.pending_count():
                await worklog.flush()
            await memory.summarise_days()
            await memory.tidy_facts()
            await hub.flush_outbox()
            last_tick = time.time()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("scheduler tick failed")
        await asyncio.sleep(TICK_SECONDS)
