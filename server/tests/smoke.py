"""End-to-end check against a running server: python tests/smoke.py [ws-url]

Sends two text turns over the WebSocket and prints what comes back.
"""
import asyncio
import json
import sys

import websockets

URL = sys.argv[1] if len(sys.argv) > 1 else "ws://127.0.0.1:8765/ws"
TURNS = [
    "Add a todo to send the weekly report, due today at 6 pm, high priority. Also remind me in one minute to stretch.",
    "What's on my todo list?",
]


async def main() -> None:
    async with websockets.connect(URL, max_size=None) as ws:
        print("hello:", json.loads(await ws.recv()))
        for text in TURNS:
            print(f"\n> {text}")
            await ws.send(json.dumps({"type": "text", "text": text, "speak": True}))
            audio = 0
            while True:
                raw = await asyncio.wait_for(ws.recv(), timeout=120)
                if isinstance(raw, bytes):
                    audio += 1
                    continue
                msg = json.loads(raw)
                if msg["type"] == "reply_done":
                    print(f"< {msg['text']}   [{audio} audio clips]")
                    break
                if msg["type"] == "error":
                    print("ERROR:", msg["message"])
                    return
        print("\nwaiting up to 90s for the reminder...")
        try:
            while True:
                raw = await asyncio.wait_for(ws.recv(), timeout=90)
                if isinstance(raw, str) and json.loads(raw)["type"] == "notify":
                    print("notify:", json.loads(raw))
                    break
        except asyncio.TimeoutError:
            print("no reminder arrived")


asyncio.run(main())
