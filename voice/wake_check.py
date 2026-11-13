"""Wake-word check, run inside the voice container: python wake_check.py

Speaks test phrases with the local text-to-speech voice, streams them into the /wake detector the way
the pet does, and reports which ones triggered it.
"""
import asyncio
import io
import json
import urllib.request
import wave

import numpy as np
import websockets

BASE = "127.0.0.1:8766"
CASES = [
    ("Hey Jarvis.", True),
    ("Hey Jarvis, what's on my calendar today?", True),
    ("So I think we should move the release to next week.", False),
    ("Hey buddy, can you send me that file?", False),
    ("The service is in Jakarta, and it runs every morning.", False),
    ("You need to pip install the package first.", False),
]


def speak_16k(text: str) -> np.ndarray:
    req = urllib.request.Request(f"http://{BASE}/tts", data=json.dumps({"text": text}).encode(),
                                 headers={"content-type": "application/json"})
    with wave.open(io.BytesIO(urllib.request.urlopen(req).read())) as w:
        rate = w.getframerate()
        samples = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").astype(np.float32)
    samples = np.interp(np.arange(0, samples.size, rate / 16000), np.arange(samples.size), samples)
    pad = np.zeros(16000, dtype=np.float32)                     # a second of silence either side
    return np.concatenate([pad, samples, pad]).astype("<i2")


async def detect(pcm: np.ndarray) -> float | None:
    async with websockets.connect(f"ws://{BASE}/wake") as ws:
        for i in range(0, pcm.size, 683):                       # the size of piece the pet sends
            await ws.send(pcm[i:i + 683].tobytes())
        try:
            return json.loads(await asyncio.wait_for(ws.recv(), timeout=1.5))["wake"]
        except asyncio.TimeoutError:
            return None


async def main() -> None:
    failed = 0
    for text, expected in CASES:
        score = await detect(speak_16k(text))
        ok = (score is not None) == expected
        failed += not ok
        print(("ok   " if ok else "FAIL ") + f"{text!r}: " + (f"woke (score {score})" if score is not None else "no wake"))
    raise SystemExit(1 if failed else 0)


asyncio.run(main())
