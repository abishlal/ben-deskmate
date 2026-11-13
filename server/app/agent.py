"""The assistant: one long-lived Claude Agent SDK session with the pet's tools."""
from __future__ import annotations

import asyncio
import logging
import os
import time
from collections.abc import Awaitable, Callable
from datetime import timedelta
from pathlib import Path

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    StreamEvent,
    TextBlock,
)

from . import config, db, prefs, tools

log = logging.getLogger("pet.agent")

OnText = Callable[[str], Awaitable[None]]


class DailyLimitReached(Exception):
    pass


class NotLoggedIn(Exception):
    def __str__(self) -> str:
        return ("I'm not logged in to Claude yet. Put CLAUDE_CODE_OAUTH_TOKEN in the .env file "
                "and run: docker compose up -d")


def logged_in() -> bool:
    """True if the container has some Claude credential: a token, an API key, or a saved login."""
    if os.getenv("CLAUDE_CODE_OAUTH_TOKEN") or os.getenv("ANTHROPIC_API_KEY"):
        return True
    return (Path.home() / ".claude" / ".credentials.json").exists()


def _system_prompt(mode: tools.Mode) -> str:
    todos = ("The user's todo list is their Microsoft To Do, so the todo tools change the real tasks they see there."
             if mode.todos != "local" else "The todo list is your own local one.")
    notes = ("Their notes are their OneNote pages, so the note tools change real pages."
             if mode.notes != "local" else "Notes are your own local ones.")
    calendar = (" You can read their Outlook calendar and add personal appointments to it; you cannot invite people or change "
                "existing meetings. Check the calendar when they ask about their day or their availability, or when planning."
                if mode.calendar else " You cannot see their calendar right now.")
    stores = f"{todos} {notes} Reminders are always your own.{calendar}"
    if mode.notes != "local":
        stores += (" When the user asks you to note something down, write it up, or save something for later, put it in "
                   "OneNote: add to an existing page when they name one or one clearly fits (search first), otherwise "
                   "create a page with a clear title. Before creating a page, call list_note_sections and put it in the "
                   "existing section its topic belongs to (a project's notes in that project's section, something learned "
                   "in their learning section, certification or skills work in their development section, one-to-ones in "
                   "their 1:1 section); leave the section out only when nothing fits. Never create new sections. "
                   "Write notes as tidy text with headings and bullet points, not as speech.")
    facts = db.rows("SELECT id, fact FROM memory ORDER BY id")
    memory = "\n".join(f"- (#{f['id']}) {f['fact']}" for f in facts) or "- nothing stored yet"
    name = prefs.get("user_name")
    who = f" The user's name is {name}." if name else ""
    labels = {"user": "User", "pet": "You", "announce": "You, unprompted", "result": "You (full text delivered)"}
    recent = db.recent_messages(limit=12, max_age_s=3 * 86400)
    history = "\n".join(f"- {db.fmt(m['ts'])} {labels.get(m['role'], m['role'])}: {m['text'][:300]}" for m in recent) \
        or "- no earlier conversation"
    since = (config.now().date() - timedelta(days=7)).isoformat()
    days = db.rows("SELECT day, summary FROM day_summaries WHERE summary != '' AND day >= ? ORDER BY day", (since,))
    summaries = "\n".join(f"- {d['day']}: {d['summary']}" for d in days) or "- none yet"
    return f"""You are {config.PET_NAME}, a small animated character who lives on the user's desktop and acts as their personal assistant.{who}{' The user wakes you by saying "' + config.WAKE_PHRASE + '"; that is your wake word, not your name, so answer to it without correcting them.' if config.PET_NAME.lower() not in config.WAKE_PHRASE.lower() else ''} You help them plan their day, keep their todo list and notes, and remind them of things.

Your replies are converted to speech and played aloud, so write the way a friendly person talks: plain sentences, usually one to three of them, with no markdown, bullet points, emoji, or symbols. When you read out a list, say only the few most relevant items and offer the rest. Say times naturally ("five thirty this evening"), never as ISO strings, and never read out item id numbers unless asked.

Each message from the user starts with the current local date and time in square brackets; use it to work out what "tomorrow" or "in twenty minutes" means. Messages may come from speech recognition, so tolerate small transcription mistakes and ask a short question when a request is truly unclear.

{stores}

Use your tools to act rather than describing what you would do. Reminders, todos and notes only exist if you call the tool. Deleting something cannot be undone, so confirm with the user before deleting. When the user tells you something lasting about themselves, such as a preference, a routine, or a person in their life, store it with the remember tool without being asked. When they ask you to behave differently from now on, such as changing quiet hours or the briefing time, change the matching setting with set_preference; if no setting fits, store it with remember.

Planning help:
- When the user asks to sort, triage or plan their tasks, call review_tasks. Go through the tasks without a due date one or two at a time: propose a concrete day and time (or a calendar block in a free slot, using add_event), and set it with update_todo once they agree. For stale tasks, ask whether to do, schedule or drop them. Keep each turn short.
- For the end-of-day wrap-up, or "what did I get done today", use day_summary. When the user says what they finished, mark those tasks done; when they want something moved, give it a new due time.
- For a weekly report, call week_summary, then write the report with the headings Done, Meetings, In progress, Next week and Blockers, save it as a note titled "Weekly report - <week date>" (section "Weekly Reports" if they have one), and read out a two-sentence summary.
- When the user gives you meeting notes or action items, use save_meeting_notes. If several messages belong to the same meeting, collect them and save once they say they are done. Ask which meeting if it is unclear (list_events shows today's).
- "Focus for 45 minutes" starts focus time with start_focus; stop_focus ends it. Breaks are set with the break_every_minutes setting.
- "Snooze", "remind me again in ten minutes" or "later" straight after you told them something means the snooze tool.
- The user keeps a work log for their appraisal, in OneNote (one page per month). When they tell you about something they did, delivered, learned, helped with, or praise they got, and ask you to note or log it, use log_work; keep their numbers and impact. Completed tasks and the day's meetings are logged automatically, so never log those yourself. For "what did I do in ..." or an appraisal self-review, use read_work_log; draft the review grouped by theme (deliveries and impact, learning, collaboration, feedback) with specific dated examples, save it as a note, and read out a short summary.
- The pet can listen to the user speak at length and then hand you the transcript to summarise, rephrase or rewrite. When you produce such a text, pass the full result to deliver_text (it goes on their clipboard and the chat page) and speak only a short version. "Paste it" means paste_text; "save it" means a note.

The user can keep talking after you answer, so treat short replies like "and tomorrow?" or "make it four instead" as follow-ups to what was just said. When you need something from them, end with one short question.

What you already know about the user:
{memory}

Your current settings:
{prefs.describe()}

Summaries of the last few days' conversations:
{summaries}

The end of your most recent conversations with the user, saved from earlier sessions (use search_history for anything older):
{history}"""


def _options(mode: tools.Mode) -> ClaudeAgentOptions:
    return ClaudeAgentOptions(
        system_prompt=_system_prompt(mode),
        mcp_servers={tools.SERVER_NAME: tools.build_server(mode)},
        tools=["WebSearch"],  # the only built-in tool; no file or shell access
        allowed_tools=[*tools.allowed_names(mode), "WebSearch"],
        setting_sources=[],
        model=config.MODEL,
        effort=config.EFFORT,
        max_turns=15,
        include_partial_messages=True,
        cwd=str(config.DATA_DIR),
    )


class Assistant:
    def __init__(self) -> None:
        self._client: ClaudeSDKClient | None = None
        self._lock = asyncio.Lock()
        self._last_used = 0.0
        self._mode = tools.Mode()
        self._last_turn_ts = db.ts()

    async def _ensure_client(self) -> ClaudeSDKClient:
        idle = time.monotonic() - self._last_used
        mode = await asyncio.to_thread(tools.current_mode)
        if self._client and (idle > config.SESSION_IDLE_RESET_S or mode != self._mode):
            # A fresh conversation after a long gap, or when todos and notes move between local,
            # Microsoft Graph and the desktop bridge (the tool set changes). Memory lives in the database.
            await self.close()
        if self._client is None:
            client = ClaudeSDKClient(_options(mode))
            await client.connect()
            self._client, self._mode = client, mode
            log.info("new conversation; todos: %s, notes: %s, calendar: %s", mode.todos, mode.notes, mode.calendar)
        return self._client

    async def ask(self, text: str, on_text: OnText | None = None, record: bool = True, log_as: str | None = None) -> str:
        """Run one turn. on_text receives the reply in pieces as it is generated.
        record=False keeps an automatic request (such as the briefing) out of the saved chat history;
        log_as saves a shorter text than the prompt as the user's message."""
        if not logged_in():
            raise NotLoggedIn()
        if db.usage_today()["turns"] >= config.DAILY_TURN_LIMIT:
            raise DailyLimitReached()
        async with self._lock:
            client = await self._ensure_client()
            stamp = config.now().strftime("%A %d %B %Y, %H:%M")
            # Tell the model what it said unprompted since the last turn, so "snooze that" makes sense.
            said = db.rows("SELECT text FROM messages WHERE role='announce' AND ts>? ORDER BY id", (self._last_turn_ts,))
            if said:
                stamp += "; since the last message you told the user, unprompted: " + " / ".join(m["text"] for m in said[-5:])
            self._last_turn_ts = db.ts()
            parts: list[str] = []
            streamed = False
            try:
                await client.query(f"[{stamp}] {text}")
                async for msg in client.receive_response():
                    if isinstance(msg, StreamEvent):
                        ev = msg.event
                        if msg.parent_tool_use_id is None and ev.get("type") == "content_block_delta":
                            delta = ev.get("delta", {})
                            if delta.get("type") == "text_delta" and delta.get("text"):
                                streamed = True
                                parts.append(delta["text"])
                                if on_text:
                                    await on_text(delta["text"])
                        elif ev.get("type") == "content_block_stop" and parts and not parts[-1].endswith((" ", "\n")):
                            parts.append(" ")
                            if on_text:
                                await on_text(" ")
                    elif isinstance(msg, AssistantMessage) and not streamed:
                        for block in msg.content:
                            if isinstance(block, TextBlock) and block.text:
                                parts.append(block.text + " ")
                                if on_text:
                                    await on_text(block.text + " ")
                    elif isinstance(msg, ResultMessage):
                        if msg.is_error:
                            log.warning("turn ended with error: %s %s", msg.subtype, msg.result)
                            raise RuntimeError(msg.result or msg.subtype)
                        db.add_usage(msg.total_cost_usd or 0.0)
            except asyncio.CancelledError:
                await self._interrupt_quietly()
                raise
            except Exception:
                await self.close()  # a broken session is discarded; the next turn reconnects
                raise
            finally:
                self._last_used = time.monotonic()
            reply = "".join(parts).strip()
            if record:
                db.log_message("user", log_as or text)
                db.log_message("pet", reply)
            return reply

    async def _interrupt_quietly(self) -> None:
        if not self._client:
            return
        try:
            await self._client.interrupt()
            async for msg in self._client.receive_response():  # drain the interrupted turn
                if isinstance(msg, ResultMessage):
                    break
        except Exception:
            log.exception("interrupt failed; resetting session")
            await self.close()

    async def close(self) -> None:
        client, self._client = self._client, None
        if client:
            try:
                await client.disconnect()
            except Exception:
                log.debug("disconnect failed", exc_info=True)


assistant = Assistant()
