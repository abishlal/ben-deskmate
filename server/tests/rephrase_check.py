"""Checks "rephrase the selected text" (Ctrl+Alt+R) without Claude, on a throwaway database: the prompt carries
the user's style and samples, wrappers are stripped, AI-sounding phrasing triggers one retry, the user's own words
are never counted against them, and selecting a rewrite again asks for a different version of the original.

docker compose exec -e PET_DATA_DIR=/tmp/rephrase-check server python tests/rephrase_check.py
"""
import asyncio
import os

assert os.environ.get("PET_DATA_DIR", "").startswith("/tmp/"), "run this with PET_DATA_DIR=/tmp/... so real data is untouched"

from app import memory, prefs, rephrase  # noqa: E402

calls: list[tuple[str, str]] = []
answers: list[str] = []


async def fake_complete(system: str, prompt: str) -> str:
    calls.append((system, prompt))
    return answers.pop(0)


async def main() -> None:
    memory.complete = fake_complete
    rephrase.logged_in = lambda: True

    # Style and samples are stored and reach the prompt.
    prefs.put("writing_style", "Direct, friendly, no fluff.")
    assert rephrase.set_samples(["Hi Ravi, quick one: can we move the review to 4?", "  ", "x" * 3000]) == \
        ["Hi Ravi, quick one: can we move the review to 4?", "x" * 2000]
    system, prompt = rephrase.build_prompt("hey can u send the report")
    assert "Direct, friendly, no fluff." in system and "move the review to 4" in system
    assert "<draft>\nhey can u send the report\n</draft>" in prompt

    # Wrappers the model sometimes adds are stripped; the user's own quotes are kept.
    assert rephrase.clean('Here\'s the rephrased version:\n"Can you send the report?"') == "Can you send the report?"
    assert rephrase.clean("```\nCan you send it?\n```") == "Can you send it?"
    assert rephrase.clean('"Ship it" was his answer.', '"ship it" was his answer') == '"Ship it" was his answer.'
    assert rephrase.clean("Sure! Here is the rewrite: Thanks, will do.") == "Thanks, will do."

    # AI-sounding phrasing is caught, except words the user wrote themselves.
    hits = rephrase.banned_in("We will leverage this to delve deeper — fast.")
    assert set(hits) == {"leverage", "delve", "em dashes (—)"}, hits
    assert rephrase.banned_in("We leverage the cache.", "we leverage the cache") == []
    assert rephrase.banned_in("Unlocked doors and robustness are fine.") == []

    # One retry when the first answer sounds machine-written, naming what to avoid.
    calls.clear()
    answers[:] = ["I hope this email finds you well. Could you send the report?", "Could you send the report?"]
    out = await rephrase.rephrase("hey can u send the report")
    assert out == "Could you send the report?", out
    assert len(calls) == 2 and "i hope this email finds you well" in calls[1][1]

    # An em dash that survives the retry becomes a plain hyphen.
    answers[:] = ["Done — sending now.", "Done — sending now."]
    assert await rephrase.rephrase("done, sending now") == "Done - sending now."

    # Selecting the rewrite and pressing again gives another version of the original draft.
    calls.clear()
    answers[:] = ["Can you share the report?"]
    out = await rephrase.rephrase("could you send the report?\r\n")
    assert out == "Can you share the report?"
    assert "<draft>\nhey can u send the report\n</draft>" in calls[0][1] and "<seen>" in calls[0][1], calls[0][1]

    # Empty, too long, and not logged in are refused before any request.
    calls.clear()
    for text in ("   ", "x" * 6000):
        try:
            await rephrase.rephrase(text)
            raise AssertionError("should have been refused")
        except ValueError:
            pass
    rephrase.logged_in = lambda: False
    try:
        await rephrase.rephrase("hello")
        raise AssertionError("should need a login")
    except ValueError:
        pass
    assert not calls

    # The style setting has a length limit.
    try:
        prefs.put("writing_style", "x" * 1200)
        raise AssertionError("should be too long")
    except ValueError:
        pass
    print("rephrase check passed")


asyncio.run(main())
