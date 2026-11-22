"""Local voice service: speech-to-text (faster-whisper) and text-to-speech (Piper), both on CPU.

POST /stt   body: WAV audio          -> {"text": "..."}
POST /tts   body: {"text": "..."}    -> audio/wav
GET  /health
"""
from __future__ import annotations

import io
import logging
import os
import threading
import wave
from pathlib import Path

import time

import numpy as np
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from openwakeword.model import Model as WakeModel
from fastapi.responses import Response
from faster_whisper import WhisperModel
from piper import PiperVoice
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s voice: %(message)s")
log = logging.getLogger("voice")

MODELS = Path(os.getenv("VOICE_MODEL_DIR", "/models"))
STT_MODEL = os.getenv("STT_MODEL", "small.en")
TTS_VOICE = os.getenv("TTS_VOICE", "en_US-amy-medium")
THREADS = int(os.getenv("VOICE_THREADS", "6"))
STT_HINT = os.getenv("STT_HINT", "")
WAKE_MODEL = os.getenv("WAKE_MODEL", "hey_jarvis")
WAKE_THRESHOLD = float(os.getenv("WAKE_THRESHOLD", "0.5"))
WAKE_FRAME = 1280  # 80 ms at 16 kHz, the step openWakeWord works in
WAKE_GAP_S = 1.0   # audio resuming after a longer pause starts the detector afresh
VAD_FRAME = 512    # 32 ms at 16 kHz, the step the Silero speech detector works in
VAD_ON, VAD_OFF = 0.5, 0.3   # speech starts above the first probability and ends below the second

app = FastAPI(title="Pet voice")
# What Whisper tends to "hear" in noise without speech.
_HALLUCINATIONS = {"", "you", "thank you", "thanks for watching", "thank you for watching", "bye", "okay", "so", "uh", "um"}
_stt: WhisperModel | None = None
_tts: PiperVoice | None = None
_stt_lock = threading.Lock()
_tts_lock = threading.Lock()


def _load() -> None:
    global _stt, _tts
    # Both models are baked into the image at build time; nothing is downloaded here.
    log.info("loading speech-to-text model %s", STT_MODEL)
    _stt = WhisperModel(STT_MODEL, device="cpu", compute_type="int8", cpu_threads=THREADS,
                        download_root=str(MODELS / "whisper"), local_files_only=True)
    log.info("loading voice %s", TTS_VOICE)
    _tts = PiperVoice.load(str(MODELS / "piper" / f"{TTS_VOICE}.onnx"))
    log.info("ready")


def _decode_wav(data: bytes) -> np.ndarray:
    """16-bit PCM WAV bytes -> mono float32 samples at 16 kHz, which is what Whisper expects."""
    with wave.open(io.BytesIO(data), "rb") as wav:
        if wav.getsampwidth() != 2:
            raise ValueError("sample width must be 16 bits")
        rate, channels = wav.getframerate(), wav.getnchannels()
        samples = np.frombuffer(wav.readframes(wav.getnframes()), dtype="<i2").astype(np.float32) / 32768.0
    if channels > 1:
        samples = samples.reshape(-1, channels).mean(axis=1)
    if rate != 16000 and samples.size:
        target = np.arange(0, samples.size, rate / 16000)
        samples = np.interp(target, np.arange(samples.size), samples).astype(np.float32)
    return samples


@app.on_event("startup")
def startup() -> None:
    threading.Thread(target=_load, daemon=True).start()


@app.get("/health")
def health() -> dict:
    if _stt is None or _tts is None:
        raise HTTPException(503, "models are still loading")
    return {"ok": True, "stt": STT_MODEL, "tts": TTS_VOICE, "wake": WAKE_MODEL, "wake_threshold": WAKE_THRESHOLD}


@app.post("/stt")
async def stt(request: Request) -> dict:
    if _stt is None:
        raise HTTPException(503, "models are still loading")
    try:
        audio = _decode_wav(await request.body())
    except (wave.Error, EOFError, ValueError) as e:
        raise HTTPException(400, f"expected 16-bit PCM WAV audio: {e}")
    if audio.size < 1600:  # under a tenth of a second
        return {"text": ""}
    # Quiet speech (a soft voice, the microphone far away, noise suppression) is raised to a normal level:
    # the voice filter below discards quiet speech as silence.
    peak = float(np.abs(audio).max())
    if 0.002 < peak < 0.5:
        audio = audio * min(12.0, 0.7 / peak)

    def transcribe(vad: bool) -> str:
        # Optional vocabulary hint. Never put the wake phrase in it: Whisper then invents it in unclear audio.
        segments, _ = _stt.transcribe(audio, language="en", beam_size=1, vad_filter=vad,
                                      vad_parameters={"threshold": 0.35, "min_speech_duration_ms": 150} if vad else None,
                                      condition_on_previous_text=False, initial_prompt=STT_HINT or None,
                                      without_timestamps=True)   # only the text is used; saves ~0.3 s per request
        return " ".join(s.text.strip() for s in segments if s.no_speech_prob < 0.6).strip()

    def run() -> str:
        with _stt_lock:
            text = transcribe(vad=True)
            # The pet only sends audio its own voice detector judged to be speech, so if the filter here removed
            # everything, try once more without it rather than drop what the user said.
            if not text and float(np.sqrt(np.mean(audio ** 2))) > 0.01:
                text = transcribe(vad=False)
                if text.lower().strip(" .!?") in _HALLUCINATIONS:
                    text = ""
                log.info("second pass without the voice filter: %s", "recognised speech" if text else "nothing")
            return text

    import anyio
    return {"text": await anyio.to_thread.run_sync(run)}


@app.websocket("/wake")
async def wake(ws: WebSocket) -> None:
    """Wake-word detection on a live audio stream.

    The caller sends raw 16 kHz mono 16-bit PCM in pieces of any size; whenever the wake phrase is
    detected this sends {"wake": score}. Nothing is transcribed or stored.
    """
    await ws.accept()
    model = WakeModel(wakeword_models=[WAKE_MODEL], inference_framework="onnx")
    pending = np.zeros(0, dtype=np.int16)
    # The same audio also goes through a speech detector, which says when speech starts and stops
    # ({"speech": true/false}): the pet uses it to tell when you have finished a request in a noisy room.
    try:
        speech = SpeechDetector()
    except Exception as e:   # never let it cost the wake word
        log.warning("speech detector unavailable: %s", e)
        speech = None
    vad_pending = np.zeros(0, dtype=np.int16)
    silent_until = last_audio = 0.0
    try:
        while True:
            data = await ws.receive_bytes()
            # The pet stops sending audio while it records a request. The model still holds the last seconds it
            # was given (often the wake phrase itself), so after a pause it would fire again on old audio.
            if last_audio and time.monotonic() - last_audio > WAKE_GAP_S:
                model.reset()
                pending = np.zeros(0, dtype=np.int16)
                if speech:
                    speech.reset()
                    vad_pending = np.zeros(0, dtype=np.int16)
            last_audio = time.monotonic()
            samples = np.frombuffer(data, dtype="<i2")
            pending = np.concatenate([pending, samples])
            if speech:
                vad_pending = np.concatenate([vad_pending, samples])
                was = speech.speech
                while vad_pending.size >= VAD_FRAME:
                    frame, vad_pending = vad_pending[:VAD_FRAME], vad_pending[VAD_FRAME:]
                    p = speech.frame(frame)
                    if speech.speech is None:
                        speech.speech = p >= VAD_ON
                    elif speech.speech and p < VAD_OFF:
                        speech.speech = False
                    elif not speech.speech and p >= VAD_ON:
                        speech.speech = True
                if speech.speech is not None and speech.speech != was:
                    await ws.send_json({"speech": speech.speech})
            while pending.size >= WAKE_FRAME:
                frame, pending = pending[:WAKE_FRAME], pending[WAKE_FRAME:]
                score = max(model.predict(frame).values())
                if score >= WAKE_THRESHOLD and time.monotonic() >= silent_until:
                    silent_until = time.monotonic() + 2.0       # one detection per utterance of the phrase
                    log.info("wake phrase detected (score %.2f)", score)
                    model.reset()                                 # forget the phrase so it is never reported twice
                    pending = np.zeros(0, dtype=np.int16)
                    await ws.send_json({"wake": round(float(score), 3)})
    except WebSocketDisconnect:
        pass


class SpeechDetector:
    """Silero voice activity detection on a live stream, one 32 ms frame at a time, keeping the model's state
    between frames. Unlike a loudness check, it tells speech from clatter, music, a fan or traffic."""

    def __init__(self) -> None:
        from faster_whisper.vad import get_vad_model
        self._session = get_vad_model().session   # the model file faster-whisper ships, shared and loaded once
        self.reset()

    def reset(self) -> None:
        self._h = np.zeros((1, 1, 128), dtype="float32")
        self._c = np.zeros((1, 1, 128), dtype="float32")
        self._context = np.zeros(64, dtype="float32")
        self.speech: bool | None = None   # None until the first frame after a reset

    def frame(self, pcm: np.ndarray) -> float:
        audio = pcm.astype("float32") / 32768.0
        x = np.concatenate([self._context, audio])[None, :]
        self._context = audio[-64:]
        out, self._h, self._c = self._session.run(None, {"input": x, "h": self._h, "c": self._c})
        return float(np.ravel(out)[0])


class Speak(BaseModel):
    text: str


@app.post("/tts")
async def tts(body: Speak) -> Response:
    if _tts is None:
        raise HTTPException(503, "models are still loading")
    if not body.text.strip():
        raise HTTPException(400, "empty text")

    def run() -> bytes:
        buf = io.BytesIO()
        with _tts_lock, wave.open(buf, "wb") as wav:
            _tts.synthesize_wav(body.text, wav)
        return buf.getvalue()

    import anyio
    return Response(await anyio.to_thread.run_sync(run), media_type="audio/wav")
