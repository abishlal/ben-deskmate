"""Client for the local voice container (speech-to-text and text-to-speech)."""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections.abc import Awaitable, Callable
from contextlib import suppress

import httpx
import websockets

from . import config

log = logging.getLogger("pet.voice")
_http = httpx.AsyncClient(base_url=config.VOICE_URL, timeout=httpx.Timeout(60, connect=3))

_SENTENCE_END = re.compile(r"(?<=[.!?])[\"')\]]?\s+")


async def healthy() -> bool:
    try:
        return (await _http.get("/health", timeout=3)).status_code == 200
    except httpx.HTTPError:
        return False


async def wake_engine() -> bool:
    """True if the voice service has its wake-word detector."""
    try:
        r = await _http.get("/health", timeout=3)
        return r.status_code == 200 and bool(r.json().get("wake"))
    except (httpx.HTTPError, ValueError):
        return False


class WakeStream:
    """Forwards one client's microphone audio to the wake-word detector and reports detections.

    The connection to the voice service is opened on the first audio and reopened if it drops.
    """

    def __init__(self, on_wake: Callable[[float], Awaitable[None]]) -> None:
        self._on_wake = on_wake
        self._ws = None
        self._reader: asyncio.Task | None = None
        self._retry_at = 0.0

    async def feed(self, pcm: bytes) -> None:
        if self._ws is None:
            if time.monotonic() < self._retry_at:
                return
            try:
                self._ws = await websockets.connect(config.VOICE_URL.replace("http", "ws", 1) + "/wake", max_size=None)
                self._reader = asyncio.create_task(self._read(self._ws))
            except Exception as e:
                log.warning("wake-word detector unavailable: %s", e)
                self._retry_at = time.monotonic() + 5
                return
        try:
            await self._ws.send(pcm)
        except Exception:
            await self.close()

    async def _read(self, ws) -> None:
        try:
            async for message in ws:
                await self._on_wake(float(json.loads(message).get("wake", 0)))
        except Exception:
            pass

    async def close(self) -> None:
        ws, self._ws = self._ws, None
        if self._reader:
            self._reader.cancel()
            self._reader = None
        if ws is not None:
            with suppress(Exception):
                await ws.close()


async def transcribe(wav: bytes) -> str | None:
    """Text of a WAV recording, '' for silence, or None if the voice service is unavailable."""
    try:
        r = await _http.post("/stt", content=wav, headers={"content-type": "audio/wav"})
        r.raise_for_status()
        return r.json().get("text", "").strip()
    except httpx.HTTPError as e:
        log.warning("speech-to-text failed: %s", e)
        return None


async def speak(text: str) -> bytes | None:
    """WAV audio for the text, or None if the voice service is unavailable."""
    try:
        r = await _http.post("/tts", json={"text": text})
        r.raise_for_status()
        return r.content
    except httpx.HTTPError as e:
        log.warning("text-to-speech failed: %s", e)
        return None


_CLAUSE_END = re.compile(r"(?<=[,;:])\s+")


def split_sentences(buffer: str, first: bool = False) -> tuple[list[str], str]:
    """Complete sentences in the buffer, plus the unfinished remainder.
    first=True (nothing spoken yet): a long opening sentence is also cut after a clause, so speech starts sooner."""
    pieces = _SENTENCE_END.split(buffer)
    done = [p.strip() for p in pieces[:-1] if p.strip()]
    rest = pieces[-1]
    if first and not done and len(rest) > 60:
        cuts = [m.end() for m in _CLAUSE_END.finditer(rest) if m.start() >= 25]
        if cuts:
            done, rest = [rest[:cuts[0]].strip()], rest[cuts[0]:]
    return done, rest


_NAMES = "|".join(sorted({re.escape(w.split()[-1]) for w in config.WAKE_WORDS}, key=len, reverse=True))
# The wake phrase must open the utterance and include a greeting ("hey pip"). A name in the middle of a
# sentence, or a bare word like "pip" in "pip install", must not wake the pet during someone else's conversation.
_WAKE = re.compile(rf"^\W*(?:hey|hay|hi|hello|ok|okay|yo)\W+(?:{_NAMES})\b[\s,.!?:;-]*", re.IGNORECASE)
_NAME_ALONE = re.compile(rf"^\W*(?:{_NAMES})\W*$", re.IGNORECASE)


def strip_wake_word(text: str) -> str | None:
    """The request that follows the wake phrase, '' if only the name was said, or None if it was not said."""
    if _NAME_ALONE.match(text):
        return ""
    m = _WAKE.match(text)
    return text[m.end():].strip() if m else None
