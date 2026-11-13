"""Checks the work log and the memory upkeep against a fake OneNote, Outlook and Claude, on a throwaway database.

docker compose exec -e PET_DATA_DIR=/tmp/worklog-check server python tests/worklog_check.py
"""
import asyncio
import json
import os

assert os.environ.get("PET_DATA_DIR", "").startswith("/tmp/"), "run this with PET_DATA_DIR=/tmp/... so real data is untouched"

from datetime import timedelta  # noqa: E402

from app import config, db, hub, memory, tools, worklog  # noqa: E402

# The checks below use the default layout, whatever .env says.
worklog.SECTION, config.WORKLOG_PAGE, config.WORKLOG_YEAR_PAGE = "Work Log", "Work log - %B %Y", ""

# ---- a fake desktop bridge -------------------------------------------------------------------------
pages: dict[str, dict] = {}
sections = {"Quick Notes"}
calls: list[str] = []
today = config.now().date()
yesterday = today - timedelta(days=1)
offline = False


def iso(d, hm):
    return f"{d.isoformat()}T{hm}:00"


async def fake_host_call(op, args, timeout=60):
    calls.append(op)
    if offline:
        raise hub.HostError("OneNote is not responding")
    if op == "notes_search":
        q = args["query"].lower()
        return [{"id": i, "title": p["title"], "section": p["section"], "changed": ""} for i, p in pages.items() if q in p["title"].lower()]
    if op == "note_section_ensure":
        created = args["name"] not in sections
        sections.add(args["name"])
        return {"section": args["name"], "notebook": "Work", "created": created}
    if op == "note_create":
        if args.get("section") and args["section"] not in sections:
            raise hub.HostError("No section with that name.")
        pid = f"p{len(pages) + 1}"
        pages[pid] = {"title": args["title"], "section": args.get("section", "Quick Notes"), "lines": args["body"].splitlines(),
                      "parent": args.get("parent")}
        return {"id": pid, "title": args["title"], "section": pages[pid]["section"]}
    if op == "note_append":
        if args["id"] not in pages:
            raise hub.HostError("The page does not exist.")
        pages[args["id"]]["lines"] += args["body"].splitlines()
        return True
    if op == "note_read":
        p = pages[args["id"]]
        return "\n".join([p["title"], *p["lines"]])
    if op == "tasks_done":
        return [{"id": "A", "title": "Ship release 2.4", "list": "Tasks", "completed": iso(yesterday, "00:00")},
                {"id": "B", "title": "Old thing", "list": "Tasks", "completed": iso(yesterday - timedelta(days=3), "00:00")}]
    if op == "calendar_list":
        d = config.now().date() if args["start"].startswith(today.isoformat()) else yesterday
        return [{"id": "m1", "title": "Design review", "start": iso(d, "10:00"), "end": iso(d, "11:00"), "all_day": False, "is_meeting": True, "busy": "busy"},
                {"id": "m2", "title": "Standup", "start": iso(d, "09:30"), "end": iso(d, "09:45"), "all_day": False, "is_meeting": True, "busy": "busy"},
                {"id": "a1", "title": "Lunch", "start": iso(d, "13:00"), "end": iso(d, "14:00"), "all_day": False, "is_meeting": False, "busy": "busy"}]
    raise AssertionError(f"unexpected bridge call {op}")


hub.host_call = fake_host_call
hub.host_caps = {"tasks": True, "notes": True, "calendar": True}


async def call(t, **args) -> str:
    result = await t.handler(args)
    return ("ERROR " if result.get("is_error") else "") + result["content"][0]["text"]


async def main() -> None:
    assert tools.current_mode() == tools.Mode("desktop", "desktop", True)
    names = [t.name for t in tools.active_tools(tools.current_mode())]
    assert "log_work" in names and "read_work_log" in names and len(names) == len(set(names))

    # Logging by voice creates the month's page in a new Work Log section, then appends to it.
    title = worklog.page_title(today.strftime("%Y-%m"))
    print(await call(worklog.log_work, text="Fixed the payment timeout bug (errors down 40%)", kind="Delivery"))
    assert "Work Log" in sections and len(pages) == 1
    page = next(iter(pages.values()))
    assert page["title"] == title and page["section"] == "Work Log", page
    assert page["lines"][0].endswith("· Delivery · Fixed the payment timeout bug (errors down 40%)"), page["lines"]
    print(await call(worklog.log_work, text="Mentored Priya on the API design", kind="Collaboration"))
    print(await call(worklog.log_work, text="Mentored Priya on the API design", kind="Collaboration"))  # same item again
    assert len(pages) == 1 and len(page["lines"]) == 2, page["lines"]
    print("page:", page["title"], page["lines"])

    # OneNote unreachable: the entry waits, and is written once it can be.
    global offline
    offline = True
    text = await call(worklog.log_work, text="Presented the Q3 roadmap", kind="Achievement")
    assert "stays queued" in text and worklog.pending_count() == 1, text
    offline = False
    assert await worklog.flush(force=True) is None and worklog.pending_count() == 0
    assert page["lines"][-1].endswith("Presented the Q3 roadmap")

    # Automatic collection: yesterday's completed task and meetings, once only; appointments are not meetings.
    if yesterday.month == today.month:
        added = await worklog.collect_day(yesterday)
        again = await worklog.collect_day(yesterday)
        assert added == 2 and again == 0, (added, again)
        await worklog.flush(force=True)
        lines = page["lines"]
        assert any("Task done · Ship release 2.4" in l for l in lines), lines
        meet = [l for l in lines if "· Meetings ·" in l]
        assert len(meet) == 1 and "2 on the calendar: Design review (1 h); Standup (15 min)" in meet[0] and "Lunch" not in meet[0], meet
        assert not any("Old thing" in l for l in lines)
        print("collected:", meet[0])

    # Reading back respects the user's own edits in OneNote and the date range.
    page["lines"].append("Also: ran the hiring panel for two candidates")   # written by hand in OneNote
    page["lines"].append(worklog.make_line(today.replace(day=1) - timedelta(days=1) if today.day > 1 else today, "Note", "outside range")
                         if today.day > 1 else "x")
    text = await call(worklog.read_work_log, start=today.isoformat(), end=today.isoformat())
    assert "payment timeout" in text and "hiring panel" in text and "outside range" not in text, text
    print("read back ok")

    # The page was deleted in OneNote: the next entry starts a new one.
    pages.clear()
    db.run("DELETE FROM kv WHERE key LIKE 'worklog_page:%'")
    db.kv_set(f"worklog_page:{today.strftime('%Y-%m')}", "gone")
    print(await call(worklog.log_work, text="Closed 12 support tickets", kind="Delivery"))
    assert len(pages) == 1 and next(iter(pages.values()))["lines"][-1].endswith("Closed 12 support tickets")

    # Your own layout: "Nov, 2026" as a sub-page of "2026" in an existing "Daily Progress" section.
    pages.clear()
    db.run("DELETE FROM kv WHERE key LIKE 'worklog_page:%'")
    worklog.SECTION, config.WORKLOG_PAGE, config.WORKLOG_YEAR_PAGE = "Daily Progress", "%b, %Y", "%Y"
    print(await call(worklog.log_work, text="Reviewed the release plan", kind="Delivery"))
    made = next(iter(pages.values()))
    assert made["section"] == "Daily Progress" and made["parent"] == today.strftime("%Y"), made
    assert made["title"] == worklog.page_title(today.strftime("%Y-%m")) and "," in made["title"], made
    assert worklog.page_title("2026-09") == "Sept, 2026"
    worklog.SECTION, config.WORKLOG_PAGE, config.WORKLOG_YEAR_PAGE = "Work Log", "Work log - %B %Y", ""
    print("own layout ok:", made["section"], "/", made["parent"], "/", made["title"])

    # History search: full text, matching word beginnings, best first.
    for t in ("We should plan the deployment for Friday", "Lunch at noon", "The deploy script failed twice"):
        db.log_message("user", t)
    found = await call(tools.search_history, query="deploy friday")
    assert db.HAS_FTS and "deployment for Friday" in found and "deploy script" in found and "Lunch" not in found, found
    print("history search ok")

    # Daily summary: stored, and work the user mentioned goes to the log (except what was already logged).
    memory.logged_in = lambda: True
    start = int(config.now().replace(hour=10, minute=0, second=0, microsecond=0).timestamp()) - 86400
    for i, (role, text) in enumerate([("user", "I finished migrating the billing service to the new cluster today."),
                                      ("pet", "Nice work."), ("user", "Also got good feedback from the client on the demo.")]):
        db.run("INSERT INTO messages(ts, role, text) VALUES(?,?,?)", (start + i * 60, role, text))
    db.kv_set(f"wl_manual:{yesterday}", "Got good feedback from the client on the demo")
    seen_prompt = {}

    async def fake_complete(system, prompt):
        seen_prompt["p"] = prompt
        return json.dumps({"summary": "Finished the billing migration; client liked the demo.",
                           "work": [{"kind": "Delivery", "text": "Migrated the billing service to the new cluster"}]})
    memory.complete = fake_complete
    await memory.summarise_days()
    row = db.one("SELECT summary FROM day_summaries WHERE day=?", (yesterday.isoformat(),))
    assert row and "billing migration" in row["summary"], row
    assert "Already logged:\nGot good feedback" in seen_prompt["p"]
    assert any("Migrated the billing service" in l for p in pages.values() for l in p["lines"]) or worklog.pending_count()
    print("day summary ok")

    # Weekly fact tidy-up: merges and removals applied, a backup kept.
    for f in ("Likes tea", "Prefers tea in the morning", "Works at Soliton", "Lives in Chennai", "Moved to Bangalore", "Has a cat"):
        db.run("INSERT INTO memory(fact, created) VALUES(?,?)", (f, db.ts()))
    ids = {r["fact"]: r["id"] for r in db.rows("SELECT id, fact FROM memory")}

    async def fake_tidy(system, prompt):
        return json.dumps({"merge": [{"ids": [ids["Likes tea"], ids["Prefers tea in the morning"]], "fact": "Likes tea, especially in the morning"}],
                           "remove": [ids["Lives in Chennai"], 9999]})
    memory.complete = fake_tidy
    await memory.tidy_facts()
    facts = {r["fact"] for r in db.rows("SELECT fact FROM memory")}
    assert facts == {"Likes tea, especially in the morning", "Works at Soliton", "Moved to Bangalore", "Has a cat"}, facts
    assert db.one("SELECT 1 FROM kv WHERE key LIKE 'memory_backup:%'")
    print("fact tidy-up ok")
    print("\nall work log and memory checks passed")


asyncio.run(main())
