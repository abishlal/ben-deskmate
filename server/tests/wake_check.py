"""Wake word end to end, inside the server container: docker compose exec server python tests/wake_check.py

1. Text rules: "Hey <name>" is removed from the start of a request.
2. Live path: speech is streamed the way the pet streams its microphone; the detector must fire for the wake
   phrase and stay silent otherwise. Then the recording is sent and must be answered without the wake phrase.
   (Step 2 runs one real conversation turn.)
"""
import asyncio
import io
import json
import wave
from array import array

import websockets

from app import config, voice

NAME = config.WAKE_WORDS[0].title()   # the word the user says ("Jarvis"), which may differ from the pet's name
RULES = [
    (f"Hey {NAME}, what's on my list today?", "what's on my list today?"),
    (f"Okay {NAME}. Remind me at five.", "Remind me at five."),
    (f"Hey {NAME}.", ""),
    (NAME, ""),
    ("So I think we should move the release to next week.", None),
    (f"and then hey {NAME} was what I called it", None),
    ("Hey, can you remind me in a minute?", None),
]


def pcm16k(wav: bytes) -> array:
    """The recording as 16 kHz 16-bit samples with a second of silence either side."""
    with wave.open(io.BytesIO(wav)) as w:
        rate = w.getframerate()
        samples = array("h", w.readframes(w.getnframes()))
    step, out = rate / 16000, array("h", bytes(32000))
    for i in range(int(len(samples) / step) - 1):
        pos = i * step
        left = int(pos)
        out.append(int(samples[left] + (samples[left + 1] - samples[left]) * (pos - left)))
    out.extend(array("h", bytes(32000)))
    return out


async def stream(ws, pcm: array) -> bool:
    """Send live audio; True if the server reports the wake word."""
    for i in range(0, len(pcm), 683):
        await ws.send(b"WAKE" + pcm[i:i + 683].tobytes())
    try:
        while True:
            raw = await asyncio.wait_for(ws.recv(), timeout=2)
            if isinstance(raw, str) and json.loads(raw)["type"] == "wake_word":
                return True
    except asyncio.TimeoutError:
        return False


async def answer(ws, wav: bytes) -> str:
    await ws.send(json.dumps({"type": "audio", "wake": False}))
    await ws.send(wav)
    heard = ""
    while True:
        raw = await asyncio.wait_for(ws.recv(), timeout=180)
        if isinstance(raw, bytes):
            continue
        m = json.loads(raw)
        if m["type"] == "transcript":
            heard = m["text"]
        if m["type"] in ("reply_done", "error", "wake", "ignored"):
            return f"{m['type']}; request heard as {heard!r}; reply: {(m.get('text') or m.get('message') or '')[:90]}"


async def main() -> None:
    failed = 0
    for said, expected in RULES:
        got = voice.strip_wake_word(said)
        failed += got != expected
        print(("ok   " if got == expected else "FAIL ") + f"rule {said!r} -> {got!r}")

    async with websockets.connect("ws://127.0.0.1:8765/ws", max_size=None) as ws:
        hello = json.loads(await ws.recv())
        print("detector available:", hello.get("wake_engine"))
        for phrase, expected in [("So I think we should move the release to next week.", False),
                                 (f"Hey {NAME}.", True),
                                 (f"Hey {NAME}, how many meetings do I have today?", True)]:
            wav = await voice.speak(phrase)
            woke = await stream(ws, pcm16k(wav))
            failed += woke != expected
            print(("ok   " if woke == expected else "FAIL ") + f"live {phrase!r}: " + ("woke" if woke else "no wake"))
            if woke:
                print("     ", await answer(ws, wav))
    raise SystemExit(1 if failed else 0)


asyncio.run(main())
