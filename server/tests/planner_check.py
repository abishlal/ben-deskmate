"""Checks the planning features without Claude, Outlook or OneNote, on a throwaway database:
task triage, day and week summaries, meeting notes, focus time, snoozing, holding messages back during
meetings and focus time, meeting follow-ups, break and water reminders, the usage report and the "stop" phrases.

docker compose exec -e PET_DATA_DIR=/tmp/planner-check server python tests/planner_check.py
"""
import asyncio
import os

assert os.environ.get("PET_DATA_DIR", "").startswith("/tmp/"), "run this with PET_DATA_DIR=/tmp/... so real data is untouched"

from datetime import timedelta  # noqa: E402

from app import config, db, hub, planner, prefs, tools  # noqa: E402
from app.main import (_CAPTURE_STOP, _DISMISS, AUTO_TASK, capture_prompt, capture_request,  # noqa: E402
                      drop_wake_words)

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

    # Focus time sets Teams to Do not disturb through Graph, and stopping early clears it.
    import json as _json
    import httpx
    from app import msgraph
    sent: list[tuple[str, dict]] = []

    def graph(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer fake"
        sent.append((request.url.path.removeprefix("/v1.0"), _json.loads(request.content or b"{}")))
        return httpx.Response(200)

    real = (msgraph._http, msgraph._access_token, msgraph.signed_in)
    msgraph._http = httpx.AsyncClient(base_url="https://graph.microsoft.com/v1.0", transport=httpx.MockTransport(graph))
    msgraph._access_token = lambda *scopes: "fake"
    msgraph.signed_in = lambda: True
    out = await call(planner.start_focus, minutes=30, label="deck", block_calendar=False)
    assert "Do not disturb" in out, out
    assert sent == [("/me/presence/setUserPreferredPresence",
                     {"availability": "DoNotDisturb", "activity": "DoNotDisturb", "expirationDuration": "PT30M"})], sent
    out = await call(planner.stop_focus)
    assert "back to automatic" in out and sent[-1][0] == "/me/presence/clearUserPreferredPresence", (out, sent)
    out = await call(planner.stop_focus)
    assert len(sent) == 2, sent                                  # nothing to clear when focus is already off
    prefs.put("focus_teams_dnd_enabled", "off")
    await call(planner.start_focus, minutes=10, block_calendar=False)
    await call(planner.stop_focus)
    assert len(sent) == 2, sent                                  # the setting turns it off
    prefs.put("focus_teams_dnd_enabled", "on")
    msgraph._access_token = lambda *scopes: None if scopes else "fake"   # an older sign-in without the scope
    out = await call(planner.start_focus, minutes=10, block_calendar=False)
    assert "sign in again" in out and not planner.hold_reason() is None, out
    await call(planner.stop_focus)
    assert len(sent) == 2, sent                                  # failed to set, so nothing to clear
    msgraph.signed_in = lambda: False
    out = await call(planner.start_focus, minutes=10, block_calendar=False)
    assert "sign-in is not set up" in out, out
    await call(planner.stop_focus)
    msgraph._http, msgraph._access_token, msgraph.signed_in = real
    print("teams do not disturb ok")

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

    # Water reminders: on by default, only after the interval, only while at the PC, and a drink restarts the clock.
    db.run("DELETE FROM outbox WHERE kind='break'")
    prefs.in_quiet_hours = lambda at=None: False
    assert prefs.minutes("water_every_minutes") == 60
    planner.water_nudge()                                  # first run only starts the clock
    assert not db.one("SELECT 1 FROM outbox WHERE kind='water'")
    db.kv_set("water_last", str(now - 61 * 60))
    hub.active_since = None                                # away from the PC: nothing
    planner.water_nudge()
    assert not db.one("SELECT 1 FROM outbox WHERE kind='water'")
    hub.active_since = now - 60
    planner.water_nudge()
    assert len(db.rows("SELECT 1 FROM outbox WHERE kind='water'")) == 1
    planner.water_nudge()                                  # not again straight away
    assert len(db.rows("SELECT 1 FROM outbox WHERE kind='water'")) == 1
    print(await call(planner.log_water))
    print(await call(planner.log_water, glasses=2))
    assert planner.water_today() == 3 and int(db.kv_get("water_last")) >= now
    db.kv_set("water_last", str(now - 61 * 60))
    db.kv_set("focus_until", str(now + 600))               # held back during focus time
    planner.water_nudge()
    assert len(db.rows("SELECT 1 FROM outbox WHERE kind='water'")) == 1
    db.kv_set("focus_until", "0")
    planner.water_nudge()
    last = db.rows("SELECT body FROM outbox WHERE kind='water' ORDER BY id")
    assert len(last) == 2 and "glass number 4" in last[-1]["body"], last
    prefs.put("water_every_minutes", "0")
    db.kv_set("water_last", str(now - 999 * 60))
    planner.water_nudge()
    assert len(db.rows("SELECT 1 FROM outbox WHERE kind='water'")) == 2
    db.run("DELETE FROM outbox WHERE kind='water'")
    print("water reminder ok")

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

    # Repeated wake phrases are dropped from the start of what was said.
    assert drop_wake_words("Hey Jarvis Hey Jarvis Are you listening?") == "Are you listening?"
    assert drop_wake_words("Hey Jarvis") == ""
    assert drop_wake_words("I started around 6.30am") == "I started around 6.30am"
    # A request that ran long is handled normally; the clipboard only when they asked for a text.
    auto = capture_prompt(AUTO_TASK, "I'm already on the way to studying it.", auto=True)
    assert "Otherwise do not use deliver_text" in auto and "Pass the full result to deliver_text" not in auto
    asked = capture_prompt("Summarize this", "some long text", auto=False)
    assert "Pass the full result to deliver_text" in asked and '"Summarize this"' in asked
    assert "never for ordinary answers" in planner.deliver_text.description
    print("long requests and clipboard ok")
    print("\nall planner checks passed")


asyncio.run(main())
