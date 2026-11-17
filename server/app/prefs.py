"""Settings the user can change by talking to the pet. Stored in the database; defaults come from .env."""
from __future__ import annotations

import os
import re
from datetime import datetime, time

from . import config, db

DEFAULTS = {
    "briefing_enabled": "on" if config.BRIEFING_ENABLED else "off",
    "briefing_time": os.getenv("PET_BRIEFING_TIME", "09:00"),
    "quiet_start": os.getenv("PET_QUIET_START", "22:00"),
    "quiet_end": os.getenv("PET_QUIET_END", "08:00"),
    "nudge_lead_minutes": str(config.NUDGE_LEAD_MIN),
    "meeting_lead_minutes": os.getenv("PET_MEETING_LEAD_MINUTES", "10"),
    "user_name": config.USER_NAME,
    "work_start": os.getenv("PET_WORK_START", "09:30"),
    "work_end": os.getenv("PET_WORK_END", "18:30"),
    "wrapup_enabled": os.getenv("PET_WRAPUP", "on"),
    "wrapup_time": os.getenv("PET_WRAPUP_TIME", "18:00"),
    "meeting_followup_enabled": os.getenv("PET_MEETING_FOLLOWUP", "on"),
    "meeting_quiet_enabled": os.getenv("PET_MEETING_QUIET", "on"),
    "break_every_minutes": os.getenv("PET_BREAK_EVERY_MINUTES", "0"),
    "stale_task_days": os.getenv("PET_STALE_TASK_DAYS", "14"),
    "writing_style": "",
}
DESCRIPTIONS = {
    "briefing_enabled": "morning briefing on or off",
    "briefing_time": "time of the morning briefing, HH:MM",
    "quiet_start": "start of quiet hours, HH:MM (only reminders the user set get through)",
    "quiet_end": "end of quiet hours, HH:MM",
    "nudge_lead_minutes": "how many minutes before a task is due to speak up",
    "meeting_lead_minutes": "how many minutes before a calendar event to speak up; 0 turns meeting alerts off",
    "user_name": "what to call the user",
    "work_start": "start of the working day, HH:MM (Monday to Friday)",
    "work_end": "end of the working day, HH:MM",
    "wrapup_enabled": "end-of-day wrap-up on or off (working days only)",
    "wrapup_time": "time of the end-of-day wrap-up, HH:MM",
    "meeting_followup_enabled": "after a meeting ends, offer to capture notes and action items, on or off",
    "meeting_quiet_enabled": "stay silent and stop listening for the wake word during meetings on the calendar, on or off",
    "break_every_minutes": "suggest a stretch break after this many minutes of continuous work; 0 turns it off",
    "stale_task_days": "a task open longer than this many days counts as stale in task reviews",
    "writing_style": "how the user describes their own writing tone, used when rephrasing selected text (Ctrl+Alt+R); "
                     "free text, for example 'direct and polite, a bit informal with my team'",
}


def get(key: str) -> str:
    return db.kv_get(f"pref:{key}") or DEFAULTS[key]


def put(key: str, value: str) -> str:
    """Validate and store a setting; returns the stored value. Raises ValueError with a message for the model."""
    if key not in DEFAULTS:
        raise ValueError("Unknown setting. Settings: " + ", ".join(DEFAULTS))
    value = str(value).strip()
    if key.endswith(("_time", "_start", "_end")):
        m = re.fullmatch(r"(\d{1,2}):(\d{2})", value)
        if not m or int(m[1]) > 23 or int(m[2]) > 59:
            raise ValueError("Use 24-hour HH:MM, for example 21:30.")
        value = f"{int(m[1]):02d}:{m[2]}"
    elif key.endswith("_minutes"):
        if not value.isdigit() or int(value) > 240:
            raise ValueError("Use a whole number of minutes from 0 to 240.")
    elif key.endswith("_days"):
        if not value.isdigit() or not 1 <= int(value) <= 365:
            raise ValueError("Use a whole number of days from 1 to 365.")
    elif key == "writing_style":
        if len(value) > 1000:
            raise ValueError("Keep the style description under 1000 characters.")
    elif key.endswith("_enabled"):
        if value.lower() not in ("on", "off"):
            raise ValueError("Use on or off.")
        value = value.lower()
    db.kv_set(f"pref:{key}", value)
    return value


def clock(key: str) -> time:
    h, m = get(key).split(":")
    return time(int(h), int(m))


def minutes(key: str) -> int:
    return int(get(key))


def enabled(key: str) -> bool:
    return get(key) == "on"


def in_quiet_hours(at: datetime | None = None) -> bool:
    t, start, end = (at or config.now()).time(), clock("quiet_start"), clock("quiet_end")
    if start <= end:
        return start <= t < end
    return t >= start or t < end


def in_work_hours(at: datetime | None = None) -> bool:
    at = at or config.now()
    return at.weekday() < 5 and clock("work_start") <= at.time() < clock("work_end")


def describe() -> str:
    return "\n".join(f"- {key} = {get(key) or '(not set)'}  ({DESCRIPTIONS[key]})" for key in DEFAULTS)
