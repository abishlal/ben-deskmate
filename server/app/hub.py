"""Connected clients, and delivery of things the pet says on its own initiative."""
from __future__ import annotations

import asyncio
import logging
import time
import uuid

from fastapi import WebSocket

from . import db, prefs, voice

log = logging.getLogger("pet.hub")

clients: set[WebSocket] = set()
client_status: dict[WebSocket, dict] = {}  # each pet's latest self-report (microphone alive, paused, ...)
_flush_lock = asyncio.Lock()
active_since: int | None = None  # when the user last came back to the PC (from the shell's idle time)


def note_status(ws: WebSocket, status: dict) -> None:
    """Store a pet's self-report and follow whether the user is at the PC."""
    global active_since
    before = client_status.get(ws, {})
    changed = {k: status.get(k) for k in ("paused", "in_call", "in_meeting", "mic", "wake_word")
               if k in status and before.get(k) != status.get(k)}
    if changed and before:   # kept in the log, so "it stopped answering" can be traced afterwards
        log.info("pet status changed: %s", ", ".join(f"{k}={v}" for k, v in changed.items()))
    client_status[ws] = status
    idle = status.get("idle_s")
    if not isinstance(idle, (int, float)):
        return
    if idle >= 300:
        active_since = None          # away for five minutes: the work streak starts again
    elif active_since is None:
        active_since = db.ts() - int(idle)


def user_idle_s() -> float | None:
    """Seconds since the last keyboard or mouse input, as reported by the desktop shell; None if unknown."""
    values = [s["idle_s"] for w, s in client_status.items() if w in clients and isinstance(s.get("idle_s"), (int, float))]
    return min(values) if values else None


def user_active() -> bool:
    idle = user_idle_s()
    return idle is None or idle < 120


async def broadcast(message: dict) -> None:
    for ws in list(clients):
        await send(ws, message)


def connected() -> bool:
    return bool(clients)


async def send(ws: WebSocket, message: dict, audio: bytes | None = None) -> bool:
    try:
        await ws.send_json(message)
        if audio is not None:
            await ws.send_bytes(audio)
        return True
    except Exception:
        clients.discard(ws)
        return False


# --- desktop bridge -------------------------------------------------------------
# The pet shell on the host can run a fixed set of operations against Outlook tasks and OneNote.
# Requests go over the shell's existing WebSocket; nothing else is opened between Docker and the host.

HOST_OPS = {"status", "tasks_list", "tasks_done", "task_add", "task_update", "task_delete",
            "notes_search", "note_read", "note_create", "note_append", "note_delete", "note_sections", "note_section_ensure",
            "calendar_list", "calendar_add"}
hosts: set[WebSocket] = set()
host_caps: dict[str, bool] = {}
_pending: dict[str, asyncio.Future] = {}
host_health: dict[str, dict] = {}  # per app: the last call's outcome, for the status page


def _app_of(op: str) -> str:
    return "onenote" if op.startswith("note") else "calendar" if op.startswith("calendar") else         "outlook_tasks" if op.startswith("task") else "bridge"


def _record(op: str, ok: bool, error: str = "", started: float | None = None) -> None:
    from . import config
    ms = round((time.monotonic() - started) * 1000) if started else None
    host_health[_app_of(op)] = {"ok": ok, "op": op, "at": config.now().strftime("%d %b %H:%M:%S"), "ms": ms, "error": error[:200]}


class HostError(Exception):
    pass


async def host_call(op: str, args: dict, timeout: float = 60):
    if op not in HOST_OPS:
        raise HostError(f"Unknown desktop operation {op}.")
    ws = next(iter(hosts & clients), None)
    if ws is None:
        raise HostError("The desktop pet is not running on the PC, so Outlook and OneNote cannot be reached right now.")
    call_id = uuid.uuid4().hex
    started = time.monotonic()
    future = _pending[call_id] = asyncio.get_running_loop().create_future()
    try:
        if not await send(ws, {"type": "host_call", "id": call_id, "op": op, "args": args}):
            raise HostError("Lost the connection to the desktop pet.")
        result = await asyncio.wait_for(future, timeout)
    except asyncio.TimeoutError:
        _record(op, False, "no answer in time", started)
        raise HostError("The desktop app did not answer in time.") from None
    except HostError as e:
        _record(op, False, str(e), started)
        raise
    finally:
        _pending.pop(call_id, None)
    if not result.get("ok"):
        _record(op, False, str(result.get("error")), started)
        raise HostError(str(result.get("error") or "The desktop app reported an error."))
    _record(op, True, started=started)
    return result.get("data")


def host_result(message: dict) -> None:
    future = _pending.get(str(message.get("id")))
    if future and not future.done():
        future.set_result(message)


async def refresh_host_caps() -> None:
    """Ask the shell which apps the bridge can reach (a cheap check that starts neither app)."""
    global host_caps
    if not hosts & clients:
        host_caps = {}
        return
    try:
        caps = await host_call("status", {}, timeout=20)
        host_caps = {key: bool(caps.get(key)) for key in ("tasks", "notes", "calendar")}
    except HostError as e:
        log.warning("desktop bridge status failed: %s", e)
        host_caps = {}


_caps_checked = 0.0


async def refresh_host_caps_if_stale() -> None:
    """Re-check every five minutes, so setting up Outlook later is picked up without a restart."""
    global _caps_checked
    if hosts and time.monotonic() - _caps_checked > 300:
        _caps_checked = time.monotonic()
        await refresh_host_caps()


def drop(ws: WebSocket) -> None:
    global host_caps
    clients.discard(ws)
    hosts.discard(ws)
    client_status.pop(ws, None)
    if not hosts:
        host_caps = {}


_silent: tuple[bool, str] | None = None  # the silent-mode state the pets were last told


def silent_message() -> dict:
    on, reason = prefs.silent_state()
    return {"type": "silent", "on": on, "reason": reason}


async def sync_silent(announce: bool = False) -> bool:
    """Tell the pets when silent mode starts or ends (a setting changed, the hotkey, or office hours began).
    announce: the user just switched it, so the pet says so even if nothing changed. Returns True when it changed."""
    global _silent
    message = silent_message()
    state = (message["on"], message["reason"])
    changed = _silent is not None and state[0] != _silent[0]
    if announce or state != _silent:
        _silent = state
        log.info("silent mode %s (%s)", "on" if state[0] else "off", state[1])
        await broadcast({**message, "changed": changed, "announce": announce})
    return changed


def announce(kind: str, title: str, body: str) -> None:
    """Queue something for the pet to say. It is delivered now, or when a client next connects."""
    db.run("INSERT INTO outbox(kind, title, body, created) VALUES(?,?,?,?)", (kind, title, body, db.ts()))


def announce_at(when: int, kind: str, title: str, body: str) -> None:
    """Queue something to be said at a later time (the 'created' column doubles as the not-before time)."""
    db.run("INSERT INTO outbox(kind, title, body, created) VALUES(?,?,?,?)", (kind, title, body, when))


# Kinds of announcement that are useless once they are late, and how late (seconds) they may still be said.
STALE_AFTER = {"meeting": 1800, "followup": 1800, "break": 900, "water": 900, "focus": 3600}
# While held back (in a meeting, in focus time), only these kinds get through.
LET_THROUGH = {"meeting": {"reminder"}, "focus": {"reminder", "meeting", "focus"}}


async def flush_outbox() -> None:
    """Deliver queued announcements. Only reminders get through during quiet hours or a meeting;
    during focus time, reminders, meeting alerts and the end of focus."""
    if not clients:
        return
    from . import planner
    async with _flush_lock:
        quiet = prefs.in_quiet_hours()
        held = planner.hold_reason()
        for item in db.rows("SELECT * FROM outbox WHERE delivered=0 AND created<=? ORDER BY created, id", (db.ts(),)):
            limit = STALE_AFTER.get(item["kind"])
            if limit and item["created"] < db.ts() - limit:
                db.run("UPDATE outbox SET delivered=1 WHERE id=?", (item["id"],))  # too late to be useful
                continue
            if quiet and item["kind"] != "reminder":
                continue
            if held and item["kind"] not in LET_THROUGH[held]:
                continue
            wav = None if prefs.silent_now() else await voice.speak(item["body"])
            message = {
                "type": "notify", "id": item["id"], "kind": item["kind"],
                "title": item["title"], "body": item["body"], "has_audio": wav is not None,
            }
            delivered = [await send(ws, message, wav) for ws in list(clients)]
            if any(delivered):
                db.run("UPDATE outbox SET delivered=1, delivered_at=? WHERE id=?", (db.ts(), item["id"]))
                db.log_message("announce", item["body"])
                log.info("announced %s: %s", item["kind"], item["body"])
