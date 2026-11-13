"""Voice round trip, run inside the server container: text -> speech -> text, with timings.

docker compose exec server python tests/voice_check.py
"""
import asyncio
import time

from app import voice

PHRASES = ["Remind me to call the dentist at five thirty tomorrow.", "Hey Pip, what is on my todo list today?"]


async def main() -> None:
    for _ in range(60):
        if await voice.healthy():
            break
        await asyncio.sleep(2)
    else:
        raise SystemExit("voice service did not become healthy")
    for phrase in PHRASES:
        t0 = time.perf_counter()
        wav = await voice.speak(phrase)
        t1 = time.perf_counter()
        heard = await voice.transcribe(wav)
        t2 = time.perf_counter()
        seconds = (len(wav) - 44) / 2 / 22050
        print(f"said : {phrase}\nheard: {heard}\n  tts {t1 - t0:.2f}s for {seconds:.1f}s of audio, stt {t2 - t1:.2f}s")
        if heard is None:
            raise SystemExit("speech-to-text failed; see: docker compose logs voice")
        print("  after wake word:", repr(voice.strip_wake_word(heard)))


asyncio.run(main())
