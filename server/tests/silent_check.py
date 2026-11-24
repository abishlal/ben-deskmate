"""Checks silent mode without Claude or the voice service, on a throwaway database: the settings and their checks,
office days during work hours, the hotkey/voice switch (only for today on an office day), the spoken phrases,
and that nothing is turned into speech while silent, for replies and for announcements.

docker compose exec -e PET_DATA_DIR=/tmp/silent-check server python tests/silent_check.py
"""
import asyncio
import os
from datetime import datetime

assert os.environ.get("PET_DATA_DIR", "").startswith("/tmp/"), "run this with PET_DATA_DIR=/tmp/... so real data is untouched"

from app import config, db, hub, main, prefs, voice  # noqa: E402

failures = 0


def check(what: str, ok: bool) -> None:
    global failures
    failures += not ok
    print(("ok   " if ok else "FAIL ") + what)


TZ = config.now().tzinfo


def at(day: str, hm: str) -> datetime:
    return datetime.fromisoformat(f"{day}T{hm}:00").replace(tzinfo=TZ)


MON, SAT = "2026-11-23", "2026-11-28"   # a Monday and a Saturday


class FakeWs:
    def __init__(self) -> None:
        self.sent: list[tuple[dict, bool]] = []

    async def send_json(self, message: dict) -> None:
        self.sent.append((message, False))

    async def send_text(self, text: str) -> None:
        import json
        self.sent.append((json.loads(text), False))

    async def send_bytes(self, data: bytes) -> None:
        self.sent.append(({"type": "<audio bytes>"}, True))


async def main_check() -> None:


    # --- settings
    check("silent_mode defaults to off", prefs.get("silent_mode") == "off")
    check("silent_mode accepts 'office days'", prefs.put("silent_mode", "Office days") == "office_days")
    for bad in ("sometimes", ""):
        try:
            prefs.put("silent_mode", bad)
            check(f"silent_mode refuses {bad!r}", False)
        except ValueError:
            check(f"silent_mode refuses {bad!r}", True)
    check("office_days is normalised", prefs.put("office_days", "Thursday, mon wed") == "mon,wed,thu")
    try:
        prefs.put("office_days", "mon,funday")
        check("office_days refuses unknown days", False)
    except ValueError:
        check("office_days refuses unknown days", True)
    check("stale_task_days still checked as a number", prefs.put("stale_task_days", "20") == "20")

    # --- office days: silent only on those days, during work hours
    prefs.put("work_start", "09:30"); prefs.put("work_end", "18:30")
    real_now = config.now
    config.now = lambda: at(MON, "10:00")
    check("Monday 10:00 on an office day: silent", prefs.silent_now())
    config.now = lambda: at(MON, "19:00")
    check("Monday 19:00, after work: speaks", not prefs.silent_now())
    config.now = lambda: at(SAT, "10:00")
    check("Saturday 10:00, not an office day: speaks", not prefs.silent_now())

    # --- the hotkey / voice switch on an office day lasts only for today
    config.now = lambda: at(MON, "10:00")
    on, reason = prefs.set_silent(False)
    check("switched off on an office day", not on and "today" in reason)
    config.now = lambda: at(MON, "15:00")
    check("still off later that day", not prefs.silent_now())
    config.now = lambda: at("2026-11-25", "10:00")   # Wednesday, the next office day
    check("back to silent on the next office day", prefs.silent_now())
    config.now = lambda: at(SAT, "11:00")
    on, reason = prefs.set_silent(True)
    check("switched on for a Saturday", on and "today" in reason)
    prefs.put("office_days", "mon,tue")
    check("changing the office days drops today's switch", not prefs.silent_now())
    config.now = real_now

    # --- off/on modes: the switch changes the mode itself
    prefs.put("silent_mode", "off")
    check("toggle from off turns the mode on", prefs.set_silent(True)[0] and prefs.get("silent_mode") == "on")
    check("toggle from on turns the mode off", not prefs.set_silent(False)[0] and prefs.get("silent_mode") == "off")

    # --- spoken phrases
    phrases = {
        True: ["Silent mode on.", "turn on silent mode", "Switch to silent mode", "silent mode", "Go silent please",
               "Okay, be silent", "Activate silent mode now", "stay silent"],
        False: ["Silent mode off.", "Turn silent mode off", "turn off the silent mode", "you can talk again",
                "Start talking again", "unmute", "disable silent mode", "Exit silent mode."],
        None: ["be quiet", "stop talking", "what is silent mode", "remind me about silent mode at 5",
               "set a reminder", "is silent mode on"],
    }
    for want, texts in phrases.items():
        for text in texts:
            check(f"{text!r} -> {want}", main.silent_command(text) is want)

    # --- "stay on screen" phrases (the pet's own setting)
    stay = {
        True: ["Stay on screen.", "turn on stay on screen", "Stay on the screen", "enable stay on screen",
               "keep yourself on screen", "stay visible please", "Hey Jarvis, stay on screen"],
        False: ["Turn off stay on screen.", "turn off stay on screen", "stay on screen off", "Switch stay on screen off",
                "disable stay on screen", "don't stay on screen", "stop staying on screen", "only come when I call you",
                "hide when you're done"],
        None: ["what is stay on screen", "stay silent", "turn off silent mode", "remind me to stay focused",
               "is stay on screen on"],
    }
    for want, texts in stay.items():
        for text in texts:
            check(f"stay: {text!r} -> {want}", main.stay_command(text) is want)

    # --- "stay on screen" is stored on the server, and every pet is told when it changes
    check("stay_on_screen is not stored at first", not prefs.is_set("stay_on_screen"))
    check("stay_on_screen defaults to off", prefs.get("stay_on_screen") == "off")
    check("stay_on_screen accepts ON", prefs.put("stay_on_screen", "ON") == "on")
    try:
        prefs.put("stay_on_screen", "maybe")
        check("stay_on_screen rejects 'maybe'", False)
    except ValueError:
        check("stay_on_screen rejects 'maybe'", True)
    real_speak = voice.speak

    async def no_speak(text: str) -> bytes:
        return b""
    voice.speak = no_speak
    try:
        for on in (False, True, False):
            ws, other = FakeWs(), FakeWs()
            hub.clients.clear(); hub.clients.update({ws, other})
            await main.handle_stay_command(ws, on)
            told = [m for m, _ in other.sent if m.get("type") == "stay"]
            check(f"voice stay {on}: stored", prefs.enabled("stay_on_screen") is on)
            check(f"voice stay {on}: other pets told", told == [{"type": "stay", "on": on}])
            check(f"voice stay {on}: answered", any(m.get("type") == "reply_done" for m, _ in ws.sent))
    finally:
        voice.speak = real_speak
        hub.clients.clear()
    check("stay_on_screen is stored now", prefs.is_set("stay_on_screen"))

    # --- nothing is spoken while silent
    spoken: list[str] = []

    async def fake_speak(text: str) -> bytes:
        spoken.append(text)
        return b"RIFF"
    voice.speak = fake_speak
    main.voice.speak = fake_speak
    hub.voice.speak = fake_speak

    async def fake_ask(text, on_text, log_as=None):
        reply = "Here is a reply. It has two sentences."
        await on_text(reply)
        return reply
    main.assistant.ask = fake_ask

    for silent in (True, False):
        prefs.put("silent_mode", "on" if silent else "off")
        spoken.clear()
        ws = FakeWs()
        await main.handle_text(ws, "hello", speak=True)
        audio = sum(1 for _, b in ws.sent if b)
        done = [m for m, _ in ws.sent if m.get("type") == "reply_done"]
        label = "silent" if silent else "speaking"
        check(f"reply when {label}: text reaches the bubble", bool(done) and "two sentences" in done[0]["text"])
        check(f"reply when {label}: {'no' if silent else 'some'} speech", (audio == 0 and not spoken) if silent else audio > 0)

        spoken.clear()
        ws = FakeWs()
        hub.clients.clear(); hub.clients.add(ws)
        quiet_start, quiet_end = prefs.get("quiet_start"), prefs.get("quiet_end")
        prefs.put("quiet_start", "00:00"); prefs.put("quiet_end", "00:00")   # never quiet hours
        hub.announce("reminder", "Reminder", f"silent-check reminder {label}")
        await hub.flush_outbox()
        prefs.put("quiet_start", quiet_start); prefs.put("quiet_end", quiet_end)
        notes = [m for m, _ in ws.sent if m.get("type") == "notify"]
        check(f"announcement when {label}: shown", bool(notes) and label in notes[0]["body"])
        check(f"announcement when {label}: has_audio={not silent}", bool(notes) and notes[0]["has_audio"] is (not silent))

    # --- the voice command answers in the bubble, aloud only when turning silent mode off
    prefs.put("silent_mode", "off")
    for on in (True, False):
        spoken.clear()
        ws = FakeWs()
        hub.clients.clear(); hub.clients.add(ws)
        await main.handle_silent_command(ws, on)
        types = [m["type"] for m, _ in ws.sent]
        check(f"voice command {'on' if on else 'off'}: pets told", "silent" in types)
        check(f"voice command {'on' if on else 'off'}: reply shown", "reply_done" in types)
        check(f"voice command {'on' if on else 'off'}: {'silent' if on else 'spoken'} confirmation", bool(spoken) is (not on))
    hub.clients.clear()

    print("\nall passed" if not failures else f"\n{failures} FAILED")
    raise SystemExit(1 if failures else 0)


asyncio.run(main_check())
