"""Settings, read from environment variables (and the project's .env when run natively)."""
from __future__ import annotations

import os
from datetime import datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]


def _load_dotenv() -> None:
    env = ROOT / ".env"
    if not env.exists():
        return
    for line in env.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv()


def _hhmm(value: str) -> time:
    h, _, m = value.partition(":")
    return time(int(h), int(m or 0))


PET_NAME = os.getenv("PET_NAME", "Jarvis")
USER_NAME = os.getenv("PET_USER_NAME", "")
# Work log in OneNote: the section, the monthly page's title (strftime) and, optionally, a yearly page the month
# pages sit under as sub-pages (e.g. "Daily Progress", "%b, %Y", "%Y" gives "Nov, 2026" under "2026").
WORKLOG_SECTION = os.getenv("PET_WORKLOG_SECTION", "Work Log").strip() or "Work Log"
WORKLOG_PAGE = os.getenv("PET_WORKLOG_PAGE", "Work log - %B %Y").strip() or "Work log - %B %Y"
WORKLOG_YEAR_PAGE = os.getenv("PET_WORKLOG_YEAR_PAGE", "").strip()
WAKE_WORDS = [w.strip().lower() for w in os.getenv("PET_WAKE_WORDS", "jarvis,javis,jervis,jarvus,jarves,jarvice,jarbis").split(",") if w.strip()]
# What the user says to wake it, which can differ from its name (the wake-word model decides): "Hey Jarvis".
WAKE_PHRASE = os.getenv("PET_WAKE_PHRASE", "").strip() or f"Hey {WAKE_WORDS[0].title() if WAKE_WORDS else PET_NAME}"
TZ = ZoneInfo(os.getenv("TZ", "Asia/Kolkata"))
DATA_DIR = Path(os.getenv("PET_DATA_DIR", str(ROOT / "data")))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "pet.db"

VOICE_URL = os.getenv("PET_VOICE_URL", "http://127.0.0.1:8766")
MODEL = os.getenv("PET_MODEL") or None  # None = the Claude CLI's default model
EFFORT = os.getenv("PET_EFFORT", "low")
DAILY_TURN_LIMIT = int(os.getenv("PET_DAILY_TURN_LIMIT", "200"))
SESSION_IDLE_RESET_S = int(os.getenv("PET_SESSION_IDLE_MINUTES", "30")) * 60

BRIEFING_TIME = _hhmm(os.getenv("PET_BRIEFING_TIME", "09:00"))
BRIEFING_ENABLED = os.getenv("PET_BRIEFING", "1") not in ("0", "false", "no")
QUIET_START = _hhmm(os.getenv("PET_QUIET_START", "22:00"))
QUIET_END = _hhmm(os.getenv("PET_QUIET_END", "08:00"))
NUDGE_LEAD_MIN = int(os.getenv("PET_NUDGE_LEAD_MINUTES", "30"))

# Microsoft To Do and OneNote (optional): an Entra app registration's client id, and the tenant to sign in to.
MS_CLIENT_ID = os.getenv("MS_CLIENT_ID", "").strip()
MS_TENANT = os.getenv("MS_TENANT_ID", "").strip() or "organizations"

PUBLIC_PORT =os.getenv("PET_PORT", "47821")
ALLOWED_ORIGINS = {None, "null", "file://", f"http://127.0.0.1:{PUBLIC_PORT}", f"http://localhost:{PUBLIC_PORT}"}


def now() -> datetime:
    return datetime.now(TZ)


def in_quiet_hours(at: datetime | None = None) -> bool:
    t = (at or now()).time()
    if QUIET_START <= QUIET_END:
        return QUIET_START <= t < QUIET_END
    return t >= QUIET_START or t < QUIET_END
