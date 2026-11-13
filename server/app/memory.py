"""Long-term memory upkeep, run in the background by the scheduler:

- a short summary of each day's conversation, given to new conversations (search_history reaches the rest);
  the same pass picks out work the user said they did and adds it to the work log;
- once a week, duplicate or outdated facts about the user are merged or removed (a backup is kept).

Each uses one small Claude request without tools, outside the main conversation.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from datetime import date, timedelta

from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, ResultMessage, TextBlock, query

from . import config, db, worklog
from .agent import logged_in

log = logging.getLogger("pet.memory")


async def complete(system: str, prompt: str) -> str:
    """One Claude request with no tools and no conversation; counts towards the daily limit."""
    if db.usage_today()["turns"] >= config.DAILY_TURN_LIMIT:
        raise RuntimeError("daily limit reached")
    options = ClaudeAgentOptions(system_prompt=system, tools=[], allowed_tools=[], setting_sources=[], model=config.MODEL,
                                 effort="low", max_turns=1, cwd=str(config.DATA_DIR))
    parts: list[str] = []
    async for msg in query(prompt=prompt, options=options):
        if isinstance(msg, AssistantMessage):
            parts += [b.text for b in msg.content if isinstance(b, TextBlock)]
        elif isinstance(msg, ResultMessage):
            db.add_usage(msg.total_cost_usd or 0.0)
            if msg.is_error:
                raise RuntimeError(msg.result or msg.subtype)
    return "".join(parts).strip()


def _json(text: str) -> dict:
    m = re.search(r"\{.*\}", text, re.S)
    return json.loads(m.group(0)) if m else {}


# --- daily summaries ------------------------------------------------------------------------------

SUMMARY_SYSTEM = f"""You read one day's conversation between a user and their desktop assistant {config.PET_NAME} and reply with JSON only:
{{"summary": "...", "work": [{{"kind": "...", "text": "..."}}]}}

summary: two to four plain sentences about what mattered that day: decisions, plans, commitments, open questions and anything to follow up. Leave out greetings, small talk and the assistant's routine reminders. If nothing mattered, use "".

work: things the USER said they did, finished, delivered, learned or received feedback on, that would be worth recalling in a performance appraisal. Not plans or intentions, not things the assistant did on its own, and not items listed under "Already logged". Each text is short, specific and past tense, keeping any numbers. kind is one of: {", ".join(k for k in worklog.KINDS if k not in ("Task done", "Meetings", "Meeting notes", "Focus"))}. Use [] when there is nothing."""


def recent_summaries(days: int = 7) -> list[dict]:
    since = (config.now().date() - timedelta(days=days)).isoformat()
    return db.rows("SELECT day, summary FROM day_summaries WHERE day>=? AND summary != '' ORDER BY day", (since,))


_last_summary_try = 0.0


async def summarise_days() -> None:
    """Summarise the most recent past day (up to a week back) that has conversation and no summary yet.
    One day per call, at most every ten minutes, so a backlog never causes a burst of requests."""
    global _last_summary_try
    if time.monotonic() - _last_summary_try < 600 or not logged_in():
        return
    _last_summary_try = time.monotonic()
    today = config.now().date()
    for back in range(7, 0, -1):
        day = today - timedelta(days=back)
        if db.one("SELECT 1 FROM day_summaries WHERE day=?", (day.isoformat(),)):
            continue
        start = int(config.now().replace(year=day.year, month=day.month, day=day.day, hour=0, minute=0, second=0, microsecond=0).timestamp())
        msgs = db.rows("SELECT ts, role, text FROM messages WHERE ts>=? AND ts<? ORDER BY id", (start, start + 86400))
        if sum(1 for m in msgs if m["role"] == "user") < 2:
            db.run("INSERT OR REPLACE INTO day_summaries(day, summary, created) VALUES(?,?,?)", (day.isoformat(), "", db.ts()))
            continue
        who = {"user": "User", "pet": config.PET_NAME, "announce": f"{config.PET_NAME} (unprompted)", "result": f"{config.PET_NAME} (text delivered)"}
        transcript = "\n".join(f"{db.fmt(m['ts'])[-5:]} {who.get(m['role'], m['role'])}: {m['text'][:600]}" for m in msgs)[-24000:]
        logged = db.kv_get(f"wl_manual:{day}") or "nothing"
        try:
            data = _json(await complete(SUMMARY_SYSTEM, f"Day: {day.strftime('%A %d %B %Y')}\n\nAlready logged:\n{logged}\n\nConversation:\n{transcript}"))
        except Exception as e:
            log.warning("could not summarise %s: %s", day, e)
            return
        db.run("INSERT OR REPLACE INTO day_summaries(day, summary, created) VALUES(?,?,?)",
               (day.isoformat(), str(data.get("summary") or "").strip(), db.ts()))
        added = 0
        for item in data.get("work") or []:
            kind = item.get("kind") if item.get("kind") in worklog.KINDS else "Note"
            text = str(item.get("text") or "").strip()
            if text:
                added += worklog.queue(day, kind, text, "wl:conv:" + hashlib.sha1(f"{day}:{text.lower()}".encode()).hexdigest()[:16])
        log.info("summarised %s; %d work item(s) found", day, added)
        await worklog.flush()
        return


# --- weekly fact tidy-up ------------------------------------------------------------------------------

TIDY_SYSTEM = """You maintain a short list of facts an assistant remembers about its user. Reply with JSON only:
{"merge": [{"ids": [1, 4], "fact": "the combined fact"}], "remove": [7]}

merge: facts that say the same thing, or where a newer fact (higher id) updates an older one; give the single fact that replaces them, keeping every detail that is still true.
remove: facts that a newer fact clearly contradicts and that are not part of a merge.
When unsure, leave a fact alone. Never invent facts. Use empty lists when nothing needs to change."""


async def tidy_facts() -> None:
    week = config.now().strftime("%G-W%V")
    if db.kv_get("memory_tidy_week") == week or not logged_in():
        return
    facts = db.rows("SELECT id, fact, created FROM memory ORDER BY id")
    if len(facts) < 6:
        db.kv_set("memory_tidy_week", week)
        return
    db.kv_set("memory_tidy_week", week)   # one attempt a week
    try:
        data = _json(await complete(TIDY_SYSTEM, "\n".join(f"#{f['id']} ({db.fmt(f['created'])[:16]}) {f['fact']}" for f in facts)))
    except Exception as e:
        log.warning("fact tidy-up failed: %s", e)
        return
    ids = {f["id"] for f in facts}
    db.kv_set(f"memory_backup:{config.now().date()}", json.dumps(facts))
    used: set[int] = set()
    merged = removed = 0
    for m in data.get("merge") or []:
        group = [i for i in m.get("ids", []) if isinstance(i, int) and i in ids and i not in used]
        if len(group) < 2 or not str(m.get("fact") or "").strip():
            continue
        used |= set(group)
        db.run(f"DELETE FROM memory WHERE id IN ({','.join('?' * len(group))})", tuple(group))
        db.run("INSERT INTO memory(fact, created) VALUES(?,?)", (str(m["fact"]).strip(), db.ts()))
        merged += 1
    for i in data.get("remove") or []:
        if isinstance(i, int) and i in ids and i not in used:
            db.run("DELETE FROM memory WHERE id=?", (i,))
            removed += 1
    log.info("facts tidied: %d merged group(s), %d removed (backup kept as memory_backup:%s)", merged, removed, config.now().date())
