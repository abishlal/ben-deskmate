"""Checks everything that does not need Claude, inside the server container:
tools write to the database, a due reminder is announced over the WebSocket with speech,
a repeating reminder is rescheduled, a due todo produces a nudge, and foreign web origins are refused.

docker compose exec server python tests/offline_check.py
"""
import asyncio
import json
import time

import websockets

from app import config, db, tools

URL = "ws://127.0.0.1:8765/ws"


async def call(tool, **args) -> str:
    result = await tool.handler(args)
    return ("ERROR " if result.get("is_error") else "") + result["content"][0]["text"]


async def main() -> None:
    soon = config.now().replace(microsecond=0)
    iso = lambda dt: dt.strftime("%Y-%m-%dT%H:%M:%S")
    from datetime import timedelta

    print(await call(tools.add_reminder, text="offline-check stretch", when=iso(soon + timedelta(seconds=3))))
    print(await call(tools.add_reminder, text="offline-check standup", when=iso(soon + timedelta(seconds=3)), repeat="weekdays"))
    print(await call(tools.add_reminder, text="bad", when="next tuesday"))
    print(await call(tools.add_todo, title="offline-check report", due=iso(soon + timedelta(minutes=10)), priority="high"))
    print(await call(tools.save_note, title="offline-check note", body="Milk, eggs"))
    print(await call(tools.search_notes, query="eggs"))
    print(await call(tools.list_todos))

    try:
        async with websockets.connect(URL, origin="https://evil.example"):
            print("FOREIGN ORIGIN ACCEPTED (bad)")
    except Exception as e:
        print("foreign origin refused:", type(e).__name__)

    async with websockets.connect(URL, max_size=None) as ws:
        seen, audio, deadline = [], 0, time.time() + 45
        while time.time() < deadline and len(seen) < 3:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=deadline - time.time())
            except asyncio.TimeoutError:
                break
            if isinstance(raw, bytes):
                audio += 1
                continue
            msg = json.loads(raw)
            if msg["type"] == "notify":
                seen.append(f"{msg['kind']}: {msg['body']}")
        print(f"announcements ({audio} with audio):")
        for line in seen:
            print("  ", line)

    print(await call(tools.list_reminders))

    # clean up everything this check created
    db.run("DELETE FROM reminders WHERE text LIKE 'offline-check%'")
    db.run("DELETE FROM todos WHERE title LIKE 'offline-check%'")
    db.run("DELETE FROM notes WHERE title LIKE 'offline-check%'")
    db.run("DELETE FROM outbox WHERE body LIKE '%offline-check%'")
    print("cleaned up")


asyncio.run(main())
