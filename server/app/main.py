"""Gateway: one WebSocket for the pet client (text, audio and events) plus a small web chat page."""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections import deque
from contextlib import asynccontextmanager, suppress
from logging.handlers import RotatingFileHandler
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, WebSocket
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import config, db, hub, msgraph, planner, prefs, rephrase, scheduler, tools, voice, worklog
from .agent import DailyLimitReached, NotLoggedIn, assistant, logged_in

log = logging.getLogger("pet")
WEB = Path(__file__).resolve().parents[1] / "web"


def _setup_logging() -> None:
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    file = RotatingFileHandler(config.DATA_DIR / "pet.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    for handler in (logging.StreamHandler(), file):
        handler.setFormatter(fmt)
        root.addHandler(handler)


@asynccontextmanager
async def lifespan(_: FastAPI):
    _setup_logging()
    task = asyncio.create_task(scheduler.run())
    yield
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task
    await assistant.close()


app = FastAPI(title="Desktop Pet", lifespan=lifespan)


@app.get("/health")
async def health() -> dict:
    return {
        "ok": True,
        "name": config.PET_NAME,
        "voice": await voice.healthy(),
        "logged_in": logged_in(),
        "microsoft": {"configured": msgraph.configured(), "account": msgraph.account()},
        "desktop_bridge": {"connected": bool(hub.hosts), **hub.host_caps},
        "pets": [s for w, s in hub.client_status.items() if w in hub.clients],
        "todos_and_notes": tools.current_mode()._asdict(),
        "clients": len(hub.clients),
        "usage_today": db.usage_today(),
        "daily_turn_limit": config.DAILY_TURN_LIMIT,
    }


@app.get("/api/status")
async def status() -> dict:
    """Everything the status page shows."""
    now = db.ts()
    focus = planner.focus_state()
    meeting = planner.meeting_now()
    return {
        "name": config.PET_NAME,
        "time": config.now().strftime("%a %d %b %H:%M:%S"),
        "voice": {"up": await voice.healthy(), "wake_engine": await voice.wake_engine()},
        "claude": {"logged_in": logged_in(), "model": config.MODEL or "Claude CLI default", "effort": config.EFFORT},
        "bridge": {"connected": bool(hub.hosts & hub.clients), "can_reach": hub.host_caps, "last_calls": hub.host_health},
        "pets": [s for w, s in hub.client_status.items() if w in hub.clients],
        "user_idle_s": hub.user_idle_s(),
        "working_for_min": round((now - hub.active_since) / 60) if hub.active_since else None,
        "mode": tools.current_mode()._asdict(),
        "focus": {"until": planner._hm(focus["until"]), "label": focus["label"]} if focus else None,
        "meeting_now": meeting["title"] if meeting else None,
        "held_back": planner.hold_reason() or ("quiet hours" if prefs.in_quiet_hours() else None),
        "calendar_checked_s_ago": round(time.monotonic() - planner.calendar_checked) if planner.calendar_checked else None,
        "scheduler_tick_s_ago": round(time.time() - scheduler.last_tick) if scheduler.last_tick else None,
        "waiting_announcements": (db.one("SELECT COUNT(*) AS n FROM outbox WHERE delivered=0 AND created<=?", (now,)) or {}).get("n", 0),
        "pending_reminders": (db.one("SELECT COUNT(*) AS n FROM reminders WHERE status='pending'") or {}).get("n", 0),
        "worklog_waiting": worklog.pending_count(),
        "memory": {"facts": (db.one("SELECT COUNT(*) AS n FROM memory") or {}).get("n", 0),
                   "day_summaries": (db.one("SELECT COUNT(*) AS n FROM day_summaries WHERE summary != ''") or {}).get("n", 0),
                   "history_search": "full text" if db.HAS_FTS else "keywords"},
        "usage": {"today": db.usage_today(), "limit": config.DAILY_TURN_LIMIT, "days": db.usage_days(14)},
        "settings": {k: prefs.get(k) for k in prefs.DEFAULTS},
        "heard": list(_heard),
    }


@app.get("/status")
async def status_page() -> FileResponse:
    return FileResponse(WEB / "status.html")


@app.get("/api/overview")
async def overview() -> dict:
    reminders = db.rows("SELECT id, text, due_at, repeat FROM reminders WHERE status='pending' ORDER BY due_at")
    todos = db.rows("SELECT id, title, due_at, priority FROM todos WHERE done=0 ORDER BY due_at IS NULL, due_at, id")
    for item in (*reminders, *todos):
        item["due"] = db.fmt(item.pop("due_at"))
    return {"reminders": reminders, "todos": todos}


# The last few things heard while waiting for the wake phrase, kept in memory only (never written to disk),
# so a wake phrase that is being misheard can be diagnosed.
_heard: deque[dict] = deque(maxlen=12)


@app.get("/api/heard")
async def heard() -> dict:
    return {"wake_names": sorted({w.split()[-1] for w in config.WAKE_WORDS}), "recent": list(_heard)}


@app.get("/api/history")
async def history(limit: int = 60) -> dict:
    found = db.rows("SELECT ts, role, text FROM messages ORDER BY id DESC LIMIT ?", (max(1, min(limit, 500)),))
    return {"messages": [{"when": db.fmt(m["ts"]), "role": m["role"], "text": m["text"]} for m in found[::-1]]}


@app.get("/api/writing")
async def writing() -> dict:
    """The tone used for Ctrl+Alt+R rewrites, and the last few rewrites (kept in memory only)."""
    return {"style": prefs.get("writing_style"), "samples": rephrase.samples(), "recent": list(rephrase.recent)[::-1]}


@app.put("/api/writing")
async def save_writing(request: Request) -> dict:
    # Any web page can send requests to localhost: only our own pages may change settings.
    if request.headers.get("origin") not in config.ALLOWED_ORIGINS or "json" not in request.headers.get("content-type", ""):
        raise HTTPException(403, "Not allowed.")
    data = await request.json()
    try:
        style = prefs.put("writing_style", str(data.get("style", "")))
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    found = data.get("samples", [])
    return {"style": style, "samples": rephrase.set_samples(found if isinstance(found, list) else [])}


async def handle_rephrase(ws: WebSocket, rid: str, text: str) -> None:
    """Ctrl+Alt+R: rewrite the selected text in the user's tone. Silent; the shell pastes the result."""
    try:
        result = await rephrase.rephrase(text)
    except ValueError as e:
        await hub.send(ws, {"type": "rephrased", "id": rid, "error": str(e)})
    except Exception as e:
        log.warning("rephrase failed: %s", e)
        limit = "limit" in str(e).lower()
        await hub.send(ws, {"type": "rephrased", "id": rid, "error": "I've reached my limit for today." if limit
                            else "I couldn't rephrase that just now. Try again in a moment."})
    else:
        await hub.send(ws, {"type": "rephrased", "id": rid, "text": result})


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(WEB / "index.html")


# The pet's own page: loaded by the desktop shell, and usable in a browser tab for preview.
app.mount("/pet", StaticFiles(directory=WEB / "pet", html=True), name="pet")


def _speakable(text: str) -> str:
    return re.sub(r"[*_#`>|]+", "", text).strip()


async def _speech_worker(ws: WebSocket, queue: asyncio.Queue[str | None]) -> None:
    while (sentence := await queue.get()) is not None:
        wav = await voice.speak(sentence)
        if wav:
            await hub.send(ws, {"type": "audio", "text": sentence}, wav)


async def handle_text(ws: WebSocket, text: str, speak: bool, log_as: str | None = None) -> None:
    await hub.send(ws, {"type": "state", "state": "thinking"})
    queue: asyncio.Queue[str | None] = asyncio.Queue()
    worker = asyncio.create_task(_speech_worker(ws, queue)) if speak else None
    pending = ""

    async def on_text(piece: str) -> None:
        nonlocal pending
        await hub.send(ws, {"type": "reply_delta", "text": piece})
        if not worker:
            return
        sentences, pending = voice.split_sentences(pending + piece)
        for s in sentences:
            if clean := _speakable(s):
                queue.put_nowait(clean)

    try:
        try:
            reply = await assistant.ask(text, on_text, log_as=log_as)
        except DailyLimitReached:
            reply = "I've reached my limit for today, so I'll be back tomorrow."
            await on_text(reply)
        except asyncio.CancelledError:
            raise
        except NotLoggedIn as e:
            await hub.send(ws, {"type": "error", "message": str(e)})
            return
        except Exception as e:
            log.exception("turn failed")
            await hub.send(ws, {"type": "error", "message": f"The assistant could not answer: {e}"})
            return
        if worker:
            if clean := _speakable(pending):
                queue.put_nowait(clean)
            queue.put_nowait(None)
            await worker
        await hub.send(ws, {"type": "reply_done", "text": reply})
    finally:
        if worker and not worker.done():
            worker.cancel()


# Saying one of these instead of a request sends the pet away without asking Claude.
_DISMISS = re.compile(r"^\W*(?:ok(?:ay)?\W+)?(?:stop(?: talking)?|never ?mind|shut up|be quiet|quiet|that'?s (?:all|it)|that is all|"
                      r"nothing|no thanks|thanks|thank you(?: very much)?|go away|dismiss|cancel that)\W*$", re.IGNORECASE)


# --- "summarise this" / "rephrase this": listen until the user says stop, then hand the text to Claude ---
_CAPTURE_VERBS = (r"summari[sz]e|sum up|rephrase|reword|rewrite|re-write|paraphrase|tidy up|clean up|polish|shorten|"
                  r"proofread|make notes (?:of|from|on)|take notes (?:of|on)|turn|convert|draft")
_CAPTURE_START = re.compile(
    rf"^\W*(?:(?:can|could|will) you\W+|please\W+)?(?P<verb>{_CAPTURE_VERBS})\W*"
    r"(?:this|something|what i(?:'m going to| am going to| will)? say|the following|for me)?"
    r"(?P<how>\W+(?:as|into|in|for|to|so)\b.*?)?\W*$", re.IGNORECASE)
_CAPTURE_STOP = re.compile(r"^\W*(?:ok(?:ay)?\W+)?(?:stop|done|i'?m done|that'?s it|that'?s all|end|finished|"
                           r"stop listening|that is all|over)\W*$", re.IGNORECASE)
_CAPTURE_CANCEL = re.compile(r"^\W*(?:cancel|cancel that|scrap that|forget it|discard (?:it|that))\W*$", re.IGNORECASE)
CAPTURE_DEFAULT_TASK = "summarise it, or if it is short, rephrase it clearly"
# "Take the MoM", "note down the minutes of the meeting", "let's take meeting notes": dictating minutes.
# Speech recognition writes MoM as "MoM", "M.O.M." or "mom".
_NOTES_WORDS = r"(?:the\W+|some\W+|today'?s\W+|this\W+)?(?:mom|m\W?o\W?m|minutes|meeting\W+(?:notes|minutes)|notes)"
_NOTES_START = re.compile(
    r"^\W*(?:(?:can|could|will) you\W+|please\W+|let'?s\W+|i want to\W+|i'?d like to\W+)?"
    rf"(?:take|take down|note down|write down|record|capture|make|start|dictate)\W+{_NOTES_WORDS}"
    r"(?:\W+(?:of|for|from|on)\W+[\w' ]{1,60})?\W*$"
    rf"|^\W*{_NOTES_WORDS}\W+(?:of|for)\W+(?:the\W+|today'?s\W+|this\W+)?(?:meeting|call|discussion|session)[\w' ]{{0,40}}\W*$",
    re.IGNORECASE)
MEETING_TASK = ("write these up as meeting minutes: a one-line summary, the points discussed, decisions, and action items "
                "with owners and due dates where mentioned; keep everything they said, and offer to save it as a note")
# A request that ran on past ~20 seconds becomes a listening session; its first words say what to do with it.
AUTO_TASK = ("the text starts with their request; do what it asks with the rest (if it is about a meeting, " + MEETING_TASK + ")")


def capture_request(text: str) -> str | None:
    """The task if the request asks to summarise or rephrase something the user is about to say, or to take
    meeting minutes, else None. "Summarise this as bullet points" starts a session; "summarise my week" does not."""
    if _NOTES_START.match(text):
        return f"{text.strip().rstrip('.!?')}: {MEETING_TASK}"
    m = _CAPTURE_START.match(text)
    if not m or (m["verb"].lower() in ("turn", "convert", "draft") and not m["how"]):
        return None
    return text.strip().rstrip(".!?")


class Capture:
    def __init__(self, task: str) -> None:
        self.task = task
        self.parts: list[str] = []
        self.lock = asyncio.Lock()
        self.done = False


async def begin_capture(ws: WebSocket, conn: dict, task: str, auto: bool = False) -> None:
    conn["capture"] = Capture(task)
    log.info("listening session started%s", " (long request)" if auto else "")
    await hub.send(ws, {"type": "capture_start", "task": task, "auto": auto})


async def handle_capture(ws: WebSocket, conn: dict, wav: bytes) -> None:
    """One piece of what the user says during a listening session. Pieces are transcribed in order."""
    cap: Capture | None = conn.get("capture")
    if cap is None:
        return
    async with cap.lock:
        if cap.done:
            return
        text = await voice.transcribe(wav)
        if text is None:
            await hub.send(ws, {"type": "error", "message": "The voice service is not running, so I can't listen."})
            return
        text = text.strip()
        if not text or text.lower().strip(" .!") in ("you", "thanks for watching", "bye"):
            return
        command = voice.strip_wake_word(text)
        command = text if command is None else command
        if _CAPTURE_STOP.match(command) or (command == "" and text):   # "stop", or "Hey Jarvis" on its own
            await _finish_capture(ws, conn, cap)
        elif _CAPTURE_CANCEL.match(command):
            cap.done, conn["capture"] = True, None
            await hub.send(ws, {"type": "capture_end", "cancelled": True})
        else:
            cap.parts.append(text)
            words = sum(len(p.split()) for p in cap.parts)
            await hub.send(ws, {"type": "capture_progress", "words": words, "last": text[-120:]})


async def stop_capture(ws: WebSocket, conn: dict) -> None:
    """The hotkey or a long silence ends the session: wait for pieces still being transcribed, then finish."""
    cap: Capture | None = conn.get("capture")
    if cap is None:
        return
    async with cap.lock:
        if not cap.done:
            await _finish_capture(ws, conn, cap)


async def _finish_capture(ws: WebSocket, conn: dict, cap: Capture) -> None:
    cap.done, conn["capture"] = True, None
    transcript = " ".join(cap.parts).strip()
    words = len(transcript.split())
    log.info("listening session finished: %d words", words)
    await hub.send(ws, {"type": "capture_end", "words": words, "empty": not transcript})
    if not transcript:
        return
    prompt = (f'The user spoke the text below and asked: "{cap.task}". Do what they asked with it. '
              "It comes from speech recognition, so fix obvious recognition mistakes, and keep their meaning and facts. "
              "Pass the full result to deliver_text. Then reply in one or two short spoken sentences: what you did and, "
              "for a summary, the gist. Read the whole result aloud only if it is under forty words. Offer to paste it "
              f'or save it as a note.\n\nWhat they said:\n"""\n{transcript}\n"""')
    await conn["start"](handle_text(ws, prompt, speak=True, log_as=f"({cap.task}) {transcript}"))


async def handle_dictation(ws: WebSocket, wav: bytes, lock: asyncio.Lock) -> None:
    """Dictation: transcribe only, and send the text back to be typed into the active app. Pieces stay in order."""
    async with lock:
        text = await voice.transcribe(wav)
        if text is None:
            await hub.send(ws, {"type": "error", "message": "The voice service is not running, so I can't take dictation."})
            return
        text = text.strip()
        if text and text.lower().strip(" .!") not in ("you", "thanks for watching", "bye"):
            await hub.send(ws, {"type": "dictation", "text": text})


async def handle_audio(ws: WebSocket, wav: bytes, need_wake: bool, conn: dict) -> None:
    if not need_wake:  # background speech must not make the pet react until the wake word is confirmed
        await hub.send(ws, {"type": "state", "state": "thinking"})
    text = await voice.transcribe(wav)
    if text is None:
        await hub.send(ws, {"type": "error", "message": "The voice service is not running, so I could not hear that."})
        return
    if need_wake:
        request = voice.strip_wake_word(text)
        _heard.append({"at": config.now().strftime("%H:%M:%S"), "heard": text[:80], "woke": request is not None})
        if request is None:
            await hub.send(ws, {"type": "ignored", "text": text})
            return
        if len(request) < 2:  # just the name: wake up and wait for the request
            await hub.send(ws, {"type": "wake"})
            return
        text = request
    else:
        _heard.append({"at": config.now().strftime("%H:%M:%S"), "heard": "→ " + (text[:80] or "(nothing recognisable)"), "woke": bool(text)})
        # After the wake-word detector fired, the recording usually still begins with the wake phrase.
        request = voice.strip_wake_word(text)
        if request is not None:
            if len(request) < 2:  # only the wake phrase so far: keep listening for the request
                await hub.send(ws, {"type": "wake"})
                return
            text = request
    if len(text) < 2:
        await hub.send(ws, {"type": "ignored", "text": text})
        return
    if _DISMISS.match(text):
        await hub.send(ws, {"type": "dismissed", "text": text})
        return
    if (task := capture_request(text)) is not None:
        await hub.send(ws, {"type": "transcript", "text": text})
        await begin_capture(ws, conn, task)
        return
    await hub.send(ws, {"type": "transcript", "text": text})
    await handle_text(ws, text, speak=True)


@app.websocket("/ws")
async def websocket(ws: WebSocket) -> None:
    # Browsers let any web page open a WebSocket to localhost, so only our own pages may connect.
    if ws.headers.get("origin") not in config.ALLOWED_ORIGINS:
        await ws.close(code=1008)
        return
    await ws.accept()
    hub.clients.add(ws)
    await hub.send(ws, {"type": "hello", "name": config.PET_NAME, "voice": await voice.healthy(),
                        "wake_engine": await voice.wake_engine(), "meeting_quiet": scheduler.meeting_quiet_now()})
    flush = asyncio.create_task(hub.flush_outbox())
    turn: asyncio.Task | None = None
    audio_meta: dict = {}
    dictation_lock = asyncio.Lock()
    conn: dict = {"capture": None}   # this connection's listening session, and how to start a turn from it

    async def on_wake(score: float) -> None:
        _heard.append({"at": config.now().strftime("%H:%M:%S"), "heard": f"(wake word detected, score {score:.2f})", "woke": True})
        await hub.send(ws, {"type": "wake_word", "score": score})

    wake_stream = voice.WakeStream(on_wake)

    async def start(coro) -> None:
        nonlocal turn
        await stop()
        turn = asyncio.create_task(coro)

    conn["start"] = start

    async def stop() -> None:
        nonlocal turn
        if turn and not turn.done():
            turn.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await turn
        turn = None

    try:
        while True:
            msg = await ws.receive()
            if msg["type"] == "websocket.disconnect":
                break
            if msg.get("bytes") is not None:
                if msg["bytes"][:4] == b"WAKE":  # live microphone audio for the wake-word detector, not a recording
                    await wake_stream.feed(msg["bytes"][4:])
                    continue
                if audio_meta.get("dictate"):   # dictation runs beside a conversation and never cancels it
                    asyncio.create_task(handle_dictation(ws, msg["bytes"], dictation_lock))
                elif audio_meta.get("capture"):  # a piece of a listening session
                    asyncio.create_task(handle_capture(ws, conn, msg["bytes"]))
                else:
                    await start(handle_audio(ws, msg["bytes"], bool(audio_meta.get("wake")), conn))
                audio_meta = {}
                continue
            try:
                data = json.loads(msg.get("text") or "{}")
            except json.JSONDecodeError:
                continue
            kind = data.get("type")
            if kind == "text" and str(data.get("text", "")).strip():
                await start(handle_text(ws, str(data["text"]).strip(), bool(data.get("speak"))))
            elif kind == "audio":
                audio_meta = data
            elif kind == "interrupt":
                await stop()
            elif kind == "capture_start":   # the hotkey: listen first, then summarise or rephrase
                await stop()
                if data.get("auto"):   # the pet heard a request run past ~20 s: keep listening until "stop"
                    await begin_capture(ws, conn, AUTO_TASK, auto=True)
                else:
                    await begin_capture(ws, conn, str(data.get("task") or CAPTURE_DEFAULT_TASK)[:200])
            elif kind == "rephrase":   # runs beside a conversation and never cancels it
                asyncio.create_task(handle_rephrase(ws, str(data.get("id", ""))[:40], str(data.get("text", ""))))
            elif kind == "capture_stop":
                asyncio.create_task(stop_capture(ws, conn))
            elif kind == "capture_cancel" and conn.get("capture"):
                conn["capture"].done, conn["capture"] = True, None
                await hub.send(ws, {"type": "capture_end", "cancelled": True})
            elif kind == "client" and data.get("host"):
                hub.hosts.add(ws)  # the desktop shell: it can reach Outlook tasks and OneNote
                asyncio.create_task(hub.refresh_host_caps())
            elif kind == "heard":
                _heard.append({"at": config.now().strftime("%H:%M:%S"), "heard": str(data.get("text", ""))[:80], "woke": False})
            elif kind == "host_result":
                hub.host_result(data)
            elif kind == "status":  # the pet's own report: is its microphone alive, is it paused, and so on
                data.pop("type", None)
                hub.note_status(ws, {**data, "at": config.now().strftime("%H:%M:%S")})
    except Exception:
        log.debug("websocket closed", exc_info=True)
    finally:
        hub.drop(ws)
        flush.cancel()
        await wake_stream.close()
        await stop()
