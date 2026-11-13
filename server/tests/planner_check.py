"""Checks the planning features without Claude, Outlook or OneNote, on a throwaway database:
task triage, day and week summaries, meeting notes, focus time, snoozing, holding messages back during
meetings and focus time, meeting follow-ups, break reminders, the usage report and the "stop" phrases.

docker compose exec -e PET_DATA_DIR=/tmp/planner-check server python tests/planner_check.py
"""
import asyncio
import os

assert os.environ.get("PET_DATA_DIR", "").startswith("/tmp/"), "run this with PET_DATA_DIR=/tmp/... so real data is untouched"

from datetime import timedelta  # noqa: E402

from app import config, db, hub, planner, prefs, tools  # noqa: E402
from app.main import _CAPTURE_STOP, _DISMISS, capture_request  # noqa: E402

DAY = 86400


async def call(t, **args) -> str:
    result = await t.handler(args)
    return ("ERROR " if result.get("is_error") else "") + result["content"][0]["text"]


def iso(epoch: int) -> str:
    return planner._local_iso(epoch)


def ev(title, start, end, meeting=True, busy="busy"):
    return {"id": title, "title": title, "start": iso(start), "end": iso(end), "all_day": False,
            "is_meeting": meeting, "busy": busy, "location": "", "organizer": ""}


async def main() -> None:
    now = db.ts()
    assert tools.current_mode() == tools.Mode(), tools.current_mode()

    # The planning tools are offered in every mode, without clashing names.
    for mode in (tools.Mode(), tools.Mode("desktop", "desktop", True), tools.Mode("graph", "graph", True)):
        names = [t.name for t in tools.active_tools(mode)]
        assert len(names) == len(set(names)), names
        assert {"review_tasks", "day_summary", "week_summary", "save_meeting_notes", "start_focus", "snooze"} <= set(names)

    # Free time between calendar events.
    base = int(config.now().replace(hour=9, minute=0, second=0, microsecond=0).timestamp())
    evts = [ev("a", base + 3600, base + 7200), ev("b", base + 6000, base + 9000), ev("free", base, base + 99999, busy="free")]
    slots = planner.free_slots(evts, base, base + 6 * 3600, 30)
    assert slots == [(base, base + 3600), (base + 9000, base + 6 * 3600)], slots
    print("free slots:", planner._slots_text(slots))

    # Task triage.
    for title, due in (("check-undated", None), ("check-overdue", now - 3600), ("check-soon", now + 3600)):
        db.run("INSERT INTO todos(title, due_at, created) VALUES(?,?,?)", (title, due, now))
    db.run("INSERT INTO todos(title, created) VALUES(?,?)", ("check-stale", now - 40 * DAY))
    text = await call(planner.review_tasks)
    print(text, "\n")
    for part in ("Overdue (1)", "No due date (2)", "Stale", "check-stale", "calendar cannot be seen"):
        assert part in text, part

    # Day and week summaries see a task done today.
    db.run("UPDATE todos SET done=1, completed_at=? WHERE title='check-soon'", (now,))
    text = await call(planner.day_summary)
    assert "Completed on" in text and "check-soon" in text and "check-overdue" in text, text
    text = await call(planner.week_summary)
    assert "check-soon" in text and "Still overdue (1)" in text, text
    print("day and week summaries ok")

    # Meeting notes: one page, tasks only for the user's own action items.
    text = await call(planner.save_meeting_notes, meeting="Design review", notes="- Agreed on the new layout",
                      action_items=[{"title": "check-send mockups", "owner": "me", "due": iso(now + DAY)},
                                    {"title": "check-review API", "owner": "Dan"}])
    print(text)
    page = db.one("SELECT * FROM notes WHERE title LIKE 'Design review - %'")
    assert page and "[ ] check-send mockups" in page["body"] and "[ ] check-review API (Dan)" in page["body"], page
    assert db.one("SELECT 1 FROM todos WHERE title='check-send mockups' AND due_at IS NOT NULL")
    assert not db.one("SELECT 1 FROM todos WHERE title='check-review API'")

    # Focus time holds back everything except reminders, meeting alerts and its own end.
    assert planner.hold_reason() is None
    print(await call(planner.start_focus, minutes=25, label="report"))
    assert planner.hold_reason() == "focus" and planner.focus_state()["label"] == "report"
    assert hub.LET_THROUGH["focus"] == {"reminder", "meeting", "focus"}
    db.kv_set("focus_until", str(now - 1))
    planner.focus_end()
    assert db.one("SELECT 1 FROM outbox WHERE kind='focus' AND body LIKE '%report is over%'")
    assert planner.hold_reason() is None

    # A meeting on the calendar: quiet now, a follow-up after it ends.
    planner._calendar = [ev("Standup", now - 600, now + 600), ev("Retro", now - 3000, now - 120),
                         ev("Lunch", now - 600, now + 600, meeting=False)]
    assert planner.meeting_now()["title"] == "Standup"
    assert planner.hold_reason() == "meeting"
    prefs.put("meeting_quiet_enabled", "off")
    assert planner.hold_reason() is None
    prefs.put("meeting_quiet_enabled", "on")
    planner.meeting_followups()
    planner.meeting_followups()   # only once per meeting
    follow = db.rows("SELECT body FROM outbox WHERE kind='followup'")
    assert len(follow) == 1 and follow[0]["body"].startswith("Retro has finished"), follow
    planner._calendar = []
    print("focus, meeting quiet and follow-ups ok")

    # Breaks after continuous work, only when switched on.
    prefs.in_work_hours = lambda at=None: True
    hub.active_since = now - 50 * 60
    planner.break_nudge()
    assert not db.one("SELECT 1 FROM outbox WHERE kind='break'")
    prefs.put("break_every_minutes", "45")
    planner.break_nudge()
    assert db.one("SELECT 1 FROM outbox WHERE kind='break'") and hub.active_since >= now
    print("break reminder ok")

    # Snooze repeats the last thing announced.
    print(await call(planner.snooze, minutes=5))  # nothing delivered yet
    db.run("UPDATE outbox SET delivered=1, delivered_at=? WHERE kind='followup'", (now - 60,))
    text = await call(planner.snooze, minutes=10)
    print(text)
    again = db.one("SELECT * FROM outbox WHERE delivered=0 AND body LIKE 'Retro has finished%'")
    assert again and abs(again["created"] - (now + 600)) < 5 and again["kind"] == "reminder", again

    # Usage.
    db.add_usage(0.25)
    db.add_usage(0.05)
    text = await call(planner.usage_report, days=3)
    assert "2 turns" in text and "$0.30" in text, text

    # Settings validation.
    for key, bad in (("wrapup_time", "6pm"), ("stale_task_days", "0"), ("meeting_followup_enabled", "maybe")):
        try:
            prefs.put(key, bad)
            raise AssertionError(f"{key}={bad} accepted")
        except ValueError:
            pass
    assert prefs.put("work_start", "9:00") == "09:00"

    # Phrases that send the pet away without asking Claude, and requests that must not.
    for phrase in ("Stop.", "stop talking", "Never mind.", "thanks", "Thank you very much!", "that's all", "Okay, stop."):
        assert _DISMISS.match(phrase), phrase
    for phrase in ("stop the timer", "cancel my 5 pm reminder", "thanks, and add milk", "no", "nothing on Friday?"):
        assert not _DISMISS.match(phrase), phrase
    # Requests that start a listening session, and ones that must not.
    for phrase in ("Summarize this.", "Can you rephrase this as a formal email?", "summarise what I say",
                   "Turn this into bullet points", "rewrite this for my manager", "Please summarize."):
        assert capture_request(phrase), phrase
    for phrase in ("Summarize my week", "rephrase that", "what's on my calendar", "turn off the briefing",
                   "draft my weekly report", "summarize the meeting notes page"):
        assert capture_request(phrase) is None, phrase
    for phrase in ("Stop.", "That's it.", "I'm done", "okay, stop"):
        assert _CAPTURE_STOP.match(phrase), phrase
    for phrase in ("we need to stop the release", "done with the migration"):
        assert not _CAPTURE_STOP.match(phrase), phrase
    print("listening-session phrases ok")
    print("\nall planner checks passed")


asyncio.run(main())
