"""Rephrase selected text in the user's own tone: the Ctrl+Alt+R hotkey.

The desktop shell copies the selection, the pet page sends it here, and the rewrite goes back to be pasted over
the selection. It is one Claude request without tools, outside the main conversation, so it is quick and does not
take on the pet's spoken style. Neither text is written to the chat history; the last few are kept in memory only,
so the chat page can show them until the server restarts.
"""
from __future__ import annotations

import json
import logging
import re
from collections import deque

from . import config, db, memory, prefs
from .agent import NotLoggedIn, logged_in

log = logging.getLogger("pet.rephrase")

MAX_CHARS = 5000
MAX_SAMPLES, MAX_SAMPLE_CHARS = 5, 2000
SAMPLES_KEY = "writing_samples"

# Words and habits that make writing read as machine-made. Checked case-insensitively on word boundaries.
BANNED = [
    "delve", "leverage", "leveraging", "seamless", "seamlessly", "utilize", "utilise", "robust", "streamline",
    "elevate", "empower", "foster", "synergy", "tapestry", "realm", "paramount", "pivotal", "meticulous",
    "furthermore", "moreover", "additionally", "in today's fast-paced", "i hope this email finds you well",
    "i hope this message finds you well",
    "i hope you are doing well", "certainly!", "great question", "rest assured", "kindly note", "please do not hesitate",
    "don't hesitate to reach out", "feel free to reach out", "navigate the", "a testament to", "it's worth noting",
    "it is worth noting", "game-changer", "cutting-edge", "unlock", "dive into", "deep dive",
]
_BANNED_RE = re.compile(r"(?<![\w-])(?:" + "|".join(re.escape(b) for b in BANNED) + r")(?![\w-])",
                        re.IGNORECASE)
_PREAMBLE = re.compile(r"^\s*(?:sure[,!.]?\s*|okay[,!.]?\s*|certainly[,!.]?\s*)?(?:here(?:'s| is| are)\b[^\n:]*:|"
                       r"(?:rephrased|rewritten|revised|polished) (?:version|text)[^\n:]*:)\s*", re.IGNORECASE)
_QUOTES = ('"""', '"', "'", "“", "”", "‘", "’", "```")

# The last few rewrites, in memory only: {"at", "original", "result"}.
recent: deque[dict] = deque(maxlen=5)


def samples() -> list[str]:
    try:
        found = json.loads(db.kv_get(SAMPLES_KEY) or "[]")
    except ValueError:
        return []
    return [str(s) for s in found if str(s).strip()][:MAX_SAMPLES]


def set_samples(items: list[str]) -> list[str]:
    clean = [str(s).strip()[:MAX_SAMPLE_CHARS] for s in items if str(s).strip()][:MAX_SAMPLES]
    db.kv_set(SAMPLES_KEY, json.dumps(clean))
    return clean


def banned_in(text: str, original: str = "") -> list[str]:
    """AI-sounding phrases in the result that the user did not write themselves."""
    found = {m.group(0).lower() for m in _BANNED_RE.finditer(text)}
    mine = {m.group(0).lower() for m in _BANNED_RE.finditer(original)}
    hits = sorted(found - mine)
    if "—" in text and "—" not in original:
        hits.append("em dashes (—)")
    return hits


def clean(text: str, original: str = "") -> str:
    """Strip what the model sometimes wraps the answer in: an opener, a code fence, surrounding quotes."""
    text = _PREAMBLE.sub("", text.strip(), count=1).strip()
    if text.startswith("```"):
        text = re.sub(r"^```\w*\n?|\n?```$", "", text).strip()
    for _ in range(2):
        for q in _QUOTES:
            closing = {"“": "”", "‘": "’"}.get(q, q)
            if len(text) > 2 * len(q) and text.startswith(q) and text.endswith(closing) and not original.strip().startswith(q):
                text = text[len(q):len(text) - len(closing)].strip()
    return text


def build_prompt(text: str, previous: list[str] | None = None, avoid: list[str] | None = None) -> tuple[str, str]:
    style = prefs.get("writing_style").strip()
    name = prefs.get("user_name").strip()
    examples = samples()
    system = f"""You rewrite short pieces of workplace writing for {name or 'the user'} so they read better but still sound like {'them' if not name else name}, not like an AI.

The text you get is their own draft: an email, a chat message, a comment or a document paragraph. Rewrite it so it is clear, correct and professional, in their voice.

Rules:
- Keep their meaning, their facts and their intent. Never add ideas, promises, apologies, compliments or filler.
- Keep every name, number, date, time, link, code snippet and technical term exactly as written.
- Keep their greeting and sign-off if there is one, and do not add one if there is not.
- Stay close to the original length (within about 20 percent). Shorter is fine when the draft rambles.
- Keep the structure: the same line breaks, paragraphs and bullet points. Do not add headings or markdown.
- Write simply and directly. Plain everyday words, short sentences, contractions are fine. Polite but not stiff or over-formal.
- Fix grammar, spelling and awkward phrasing. Prefer small changes over rewriting sentences that already work.
- Sound like a person typing at work: no em dashes, no lists of three for rhythm, no upbeat closing line, no "I hope this finds you well".
- Never use these words or phrases: {", ".join(BANNED)}.
- Write in the same language as the draft.
- Reply with the rewritten text only: no introduction, no explanation, no quote marks around it."""
    if style:
        system += f"\n\nHow they describe their own writing style (follow it):\n{style}"
    if examples:
        system += ("\n\nSamples of their real writing. Match this voice: word choice, sentence length, how formal they are. "
                   "Do not copy their content.\n" + "\n".join(f'<sample>\n{s}\n</sample>' for s in examples))
    prompt = f"<draft>\n{text}\n</draft>"
    if previous:
        prompt += ("\n\nThey already saw the version(s) below and want a different one. Keep the same rules but word it "
                   "differently:\n" + "\n".join(f"<seen>\n{p}\n</seen>" for p in previous[-3:]))
    if avoid:
        prompt += "\n\nYour last attempt used phrasing that sounds machine-written. Do not use: " + ", ".join(avoid) + "."
    return system, prompt


def find_previous(text: str) -> tuple[str, list[str]] | None:
    """If the selection is one of the recent rewrites, the user wants another version: return the original
    draft and the versions already seen."""
    key = _norm(text)
    for item in reversed(recent):
        seen = [item["result"], *item.get("seen", [])]
        if key in {_norm(s) for s in seen}:
            return item["original"], seen
    return None


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()


async def rephrase(text: str) -> str:
    text = text.replace("\r\n", "\n").strip()
    if not logged_in():
        raise ValueError(str(NotLoggedIn()))
    if not text:
        raise ValueError("Select some text first.")
    if len(text) > MAX_CHARS:
        raise ValueError(f"That's too long for me to rephrase in one go; select under {MAX_CHARS} characters.")
    again = find_previous(text)
    original, previous = again if again else (text, [])
    system, prompt = build_prompt(original, previous)
    result = clean(await memory.complete(system, prompt), original)
    if hits := banned_in(result, original):
        log.info("rephrase retry: %s", ", ".join(hits))
        system, prompt = build_prompt(original, previous, avoid=hits)
        retry = clean(await memory.complete(system, prompt), original)
        if retry:
            result = retry
    if "—" in result and "—" not in original:
        result = re.sub(r"\s*—\s*", " - ", result)
    if not result:
        raise RuntimeError("I got an empty answer back.")
    if again:
        for item in recent:
            if item["original"] == original:
                item["seen"] = [*item.get("seen", []), item["result"]][-5:]
                item["result"], item["at"] = result, config.now().strftime("%H:%M")
                break
    else:
        recent.append({"at": config.now().strftime("%H:%M"), "original": original, "result": result, "seen": []})
    log.info("rephrased %d characters%s", len(original), " (another version)" if again else "")
    return result
