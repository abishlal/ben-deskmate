# Desktop Pet

A voice-driven personal assistant that lives on your desktop as a small animated character.
It keeps your todo list and notes, sets reminders, speaks up when something is due, and gives a morning briefing.

```
Desktop shell (Electron, optional)        Docker: compose project "desktop-pet"
 transparent strip above the taskbar       ┌────────────────────────────────────────────┐
 tray menu, Ctrl+Alt+B, toasts         │ server  (FastAPI + Claude Agent SDK)       │
        │ loads                            │   /pet/  the pet page (animation, mic, audio)
        ▼                                  │   /      chat page with reminders and todos │
 http://127.0.0.1:47821/pet/  ◄──────────► │   /ws    text, audio and events            │
 (also works in a browser tab)             │   scheduler, SQLite in volume "pet-data"   │
                                           │        │ internal network only             │
                                           │ voice   (faster-whisper + Piper, CPU)      │
                                           │   no internet, no published port           │
                                           └────────────────────────────────────────────┘
```

## Isolation

The entire backend runs in Docker and shares nothing with other compose projects.

- No host folders are mounted. Data and Claude state live in the named volume `desktop-pet_pet-data`.
- The voice container is on an internal-only network: it cannot reach the internet or the host. Its models are baked into the image at build time.
- The server is published on `127.0.0.1:47821` only, and refuses WebSocket connections from other web origins.
- Both containers run as a non-root user, with a read-only filesystem, all capabilities dropped and `no-new-privileges`.
- The agent has no file or shell tools. It can use only the pet's own tools (reminders, todos, notes, memory) and web search.

The one piece that cannot run in a container is the on-screen character, because a container has no access to your screen or microphone. The pet's page is still served from Docker; the Electron shell in `client/` is a transparent window around it, plus the narrow Outlook-tasks and OneNote bridge described below.

## Setup

1. Create a Claude token on the host and put it in `.env`:

   ```
   claude setup-token
   copy .env.example .env      # then paste the token after CLAUDE_CODE_OAUTH_TOKEN=
   ```

2. Build and start the backend:

   ```
   docker compose up -d --build
   ```

3. Open the pet:
   - in a browser: http://127.0.0.1:47821/pet/ (chat and lists at http://127.0.0.1:47821/)
   - on the desktop: `cd client`, `npm install`, `npm start`

   To run the desktop pet in the background, without a terminal attached, launch Electron's executable directly from `client`. The command returns immediately, and the pet keeps running after the terminal closes:

   ```
   # PowerShell
   Start-Process "node_modules\electron\dist\electron.exe" -ArgumentList "."
   # cmd
   start "" node_modules\electron\dist\electron.exe .
   ```

   Ctrl+Alt+X only stops and hides the pet. To quit it completely, run `Stop-Process -Name electron` (PowerShell) or `taskkill /im electron.exe /f` (cmd). Either one closes every `electron.exe` process.

## Using it

The pet stays out of sight. It comes up when you call it or when it has a reminder for you, and leaves a few seconds after the conversation ends.

| Action | How |
|---|---|
| Call it | Say "Hey Jarvis", either on its own (it comes up and waits) or followed by your request: "Hey Jarvis, what's on my list?" Ctrl+Alt+B or the tray icon also calls it. |
| Follow up | After it answers it listens for about 7 seconds, so you can reply without the wake word ("and tomorrow?"). If you say nothing it leaves quietly. |
| Interrupt | Say "Hey Jarvis" while it is talking or thinking: it stops and listens. The hotkey or a click also works. |
| Send it away | Say "stop", "never mind", "that's all" or "thanks" instead of a request. It leaves without asking Claude. |
| Stop and hide | Ctrl+Alt+X (or tray menu → Stop and hide). Whatever it is doing, talking, thinking, listening, dictating, a listening session or a rephrase, stops at once and it hides, even with Stay on screen on. Nothing is pasted or kept. |
| Dictate | Ctrl+Alt+D (or tray menu → Dictate). What you say is typed where your cursor is, in any app, sentence by sentence. Press again to stop; it also stops after 12 seconds of silence. It pastes through the clipboard and puts your clipboard back afterwards. |
| Summarise or rephrase | Say "Hey Jarvis, summarise this" (or "rephrase this as a formal email", "turn this into bullet points"), or press Ctrl+Alt+S. A chime means it is listening: talk as long as you like, pauses are fine. Say "stop" (or "that's it", "I'm done", "Hey Jarvis") as a sentence on its own to finish; "cancel that" throws it away. It speaks a short version, puts the full text on your clipboard and the chat page, then listens for "paste it", "make it shorter" or "save it as a note". It also finishes after two minutes of silence or fifteen minutes in all. One Claude turn per session; the listening itself is local. |
| Rephrase selected text | Select text in any app and press Ctrl+Alt+R. It is rewritten in your tone (simple, professional, not AI-sounding) and pasted over the selection in 2 to 4 seconds; Ctrl+Z in that app brings your original back. Select the rewrite and press Ctrl+Alt+R again for a different version. It never speaks, so it works during calls and meetings. Set your tone on the chat page under My writing style: a short description plus a few samples of your own writing. If you switch windows while it works, the rewrite is left on your clipboard instead. Up to 5000 characters, pasted as plain text (bold and colours in the selection are lost). One Claude request each, outside the conversation; neither text is saved to the chat history. |
| Snooze | Right after a reminder or alert: "snooze that", "remind me again in ten minutes". |
| Type | Tray menu → Type a message, or double-click the pet while it is up. |
| Pause it | Ctrl+Alt+P, or tray menu → Pause. It stops listening for the wake word and stops speaking; reminders still appear as a bubble and a Windows notification. Press again to resume. |
| Silent mode | For the office: it shows everything in the speech bubble instead of speaking, and the bubble stays up long enough to read. Press Ctrl+Alt+M (or tray menu → Silent mode), say "Hey Jarvis, silent mode on" / "silent mode off" (or "go silent", "you can talk again"), or set it on the Settings page (tray menu → Settings): off, on, or office days, which turns it on by itself during work hours on the weekdays you pick. On an office day the hotkey and voice switch it only for that day. The wake word still works. |
| Calls | While another app is using the microphone (a Teams call, a recording), it goes quiet in the same way on its own. Tray menu → Go quiet automatically during calls turns this off. The hotkey still calls it during a call. |
| Meetings | While a meeting on your Outlook calendar is in progress (shown as busy), it also goes quiet, and holds back everything except reminders until the meeting ends. "Stop going quiet in meetings" turns this off. |
| Status | Tray menu → Status and usage, or http://127.0.0.1:47821/status: microphone, wake word, desktop bridge and each app's last call, what is being held back, and turns and cost per day. |
| Keep it visible | Say "Hey Jarvis, stay on screen" ("turn off stay on screen" or "only come when I call you" to switch it off), or use the Stay on screen switch on the settings page or in the tray menu. It is a saved setting, so it is kept across restarts, and all three stay in step. While on, it wanders along the taskbar; while off, it comes up only when called or when it has something to say, and hides again when done. |
| How it shows up | Called up, it rises from behind the taskbar and waves. A task about to be due or a meeting coming up: a rope drops halfway down the screen, it slides to the end and jumps down onto the taskbar. The morning briefing and meeting follow-ups: it floats down under an umbrella. In silent mode, a call or a meeting it only peeks in from the right edge of the screen, then walks in. To leave it walks off the edge (after peeking or the rope), hops down behind the taskbar (after the rope), or waves and sinks behind the taskbar. |
| Reminder poses | Water: it holds out a glass of water; click it (or say "done") and it has a sip too. Break: it stretches. Meetings and reminders: it taps its watch and points at you. Tasks due: it ticks off a checklist. Focus time over, or a task marked done: a thumbs up. |
| Little things | Its eyes follow the mouse pointer when it is near. With Stay on screen on it wanders, sits on the taskbar swinging its feet, codes on its laptop or yawns; click it while it is busy with that and it jumps. From 8 pm to 6 am it is sleepy and keeps a mug of something warm in its hand, even while talking. |
| Pick it up | Drag it to carry it anywhere, including onto another monitor; let go to drop it, or flick to throw it. It bounces off screen edges and lands on the taskbar of the screen it is over (dizzy after a hard landing), and stays on that screen. A plain click still calls it. |
| Wake word off | Tray menu → Listen for the wake word. The microphone is then only used after the hotkey or a click. |
| Mute voice | Tray menu → Mute voice. Replies still appear in the bubble. |
| Start with Windows | Tray menu → Start with Windows. The containers restart on their own once Docker Desktop is running. |

### How the wake word works

The voice container runs [openWakeWord](https://github.com/dscripka/openWakeWord) with its pre-trained `hey_jarvis` model.
While the wake word is on, the pet streams microphone audio to that detector over the existing connection. The detector
only scores the audio for the wake phrase: nothing is transcribed or stored until it fires. Audio never leaves this PC.
When it fires, the pet comes up and records what you say next (or the rest of the sentence you are already saying),
and only that recording is transcribed and sent to Claude.

- **Too many false wakes, or it misses you:** change `WAKE_THRESHOLD` in `.env` (0 to 1, default 0.5) and run `docker compose up -d`.
- **A different wake phrase:** it needs an openWakeWord model for that phrase. Pre-trained ones are `hey_jarvis`, `alexa`,
  `hey_mycroft` and `hey_rhasspy`; set `WAKE_MODEL`, `PET_NAME` and `PET_WAKE_WORDS` in `.env`, then `docker compose build voice`
  and `docker compose up -d`. Any other name needs a custom-trained model.
- **Diagnosing:** http://127.0.0.1:47821/api/heard shows the last few detections (kept in memory only).

### How it knows you have finished speaking

While it records a request, the same live audio also goes through a speech detector in the voice container (Silero VAD,
the one faster-whisper already ships, so nothing extra is downloaded). It only answers "is this a human voice right now?",
many times a second. The request ends about a second after it stops hearing a voice that is loud enough to be you at the PC,
so a fan, clatter, music, traffic, or a TV or people across the room no longer keep it listening. If the detector is not
available, loudness decides, measured against the room's own background noise.

- **A request that runs past ~15 seconds of speech** becomes a listening session ("Still listening. Say 'stop' when you're done.").
  If it then hears only noise for three pieces in a row, it takes that as the end and answers. Sessions you start on purpose
  ("summarise this", "take meeting minutes") keep waiting through pauses until you say "stop".
- **Still slow to stop in a noisy room:** someone talking near you at normal volume counts as speech. Move the microphone
  closer, or press the talk hotkey again to send what it has.

Things to say:

- "Remind me in twenty minutes to stretch", "Remind me every weekday at nine thirty about standup"
- "Add a high priority todo to send the weekly report by six", "What's due today?"
- "What meetings do I have tomorrow?", "Am I free at three?", "Block two to three tomorrow for focus time"
- "Note this down in OneNote under Meetings: …", "Add to my groceries note: milk and eggs"
- "Don't disturb me after nine", "Warn me five minutes before meetings", "Move my briefing to eight thirty"
- "What did we talk about yesterday?"

## Planning features

- **Task triage.** "Help me sort my tasks." It groups your open tasks into overdue, due soon, no due date and stale
  (open more than 14 days), looks at free time today and on the next working day, and goes through them a couple at a
  time, proposing a time or a calendar block. The morning briefing mentions undated and stale tasks and offers this.
- **Day plan.** The morning briefing ends by asking what you are working on today, or say it any time: "Today I'm
  working on the API auth, the weekly report and the client demo." New tasks are added to To Do due today; ones already
  on your list are moved to today instead of added twice. Talk as long as you like; after about 15 seconds of
  speech (pauses don't count) it keeps listening until you say "stop". A long request is handled like any other: only
  a text you asked it to write (a summary, draft, email or minutes) goes on your clipboard.
- **Task notes.** Tell it what you did on a task or what questions you have ("for the API task, I finished login, and
  I need to ask Priya about rate limits"), while planning or later. Notes go on one OneNote page per day,
  "Task notes - 21 Nov 2026", with a heading per task, progress as bullets and questions as checkboxes. The task's
  details in To Do get a line naming that page. A task without notes gets no page, and a day without notes has no page.
  Later notes on a task go under that task's heading, in the same block of text. Ben only adds to pages; it never
  changes or removes what is there.
- **Note formatting.** Every note Ben writes uses OneNote's own Heading 1-3 styles, real bullet lists (nested when
  indented) and To Do checkboxes, and reads pages back with that structure. With the Microsoft sign-in, additions
  under a heading go at the end of the page rather than inside that heading's part.
- **End-of-day wrap-up.** On working days at 18:00 (once you are at the PC) it says what got done, what is still due,
  and what is first tomorrow, and asks whether to move anything. On Fridays it offers the weekly report.
- **Weekly report.** "Draft my weekly report." It collects the week's completed tasks, meetings and changed note pages,
  writes the report with Done, Meetings, In progress, Next week and Blockers, and saves it to OneNote.
- **Meetings.** The alert before a meeting mentions a OneNote page that seems to be about it. When a meeting ends it
  offers to take notes; tell it the notes and action items and it saves one page (in a "Meetings" section if you have
  one) and adds your own action items to To Do. Other people's items stay on the page only.
- **Focus time.** "Focus on the report for 45 minutes." It blocks the time on your calendar and stays silent except
  for reminders and meeting alerts, then checks in at the end. "Stop focus" ends it early.
- **Breaks.** Off by default. "Remind me to take a break every hour" sets it; it counts only time you are actually at
  the keyboard (five minutes away resets it) and only in working hours.
- **Water.** On by default: every 60 minutes at the PC it reminds you to drink some water. Tell it "I just had a glass"
  and the clock starts again (it also counts your glasses for the day). "Remind me to drink water every 45 minutes"
  changes it, "stop the water reminders" turns it off. It stays quiet while you are away, in quiet hours, in meetings
  and in focus time, and "snooze that" works on it.
- Briefings and wrap-ups wait until you are at the PC, and are skipped if you don't turn up within a few hours.

## Work log for appraisals

The log lives in OneNote and nowhere else: by default a "Work Log" section with one page per month ("Work log - November 2026");
`PET_WORKLOG_SECTION`, `PET_WORKLOG_PAGE` and `PET_WORKLOG_YEAR_PAGE` in `.env` fit it into your own layout (e.g. "Nov, 2026" under "2026" in "Daily Progress"),
one line per entry such as `Tue 11 Nov · Delivery · Fixed the payment timeout bug (errors down 40%)`. Edit or delete
lines there freely; Jarvis reads the pages back as they are.

- **Automatic, once a day:** tasks you completed in To Do and the day's calendar meetings (one line with titles and
  lengths). Missed days are caught up, up to a week back. Saved meeting notes and labelled focus sessions are added too.
- **From conversation:** the nightly summary of the day's conversation picks out things you said you did or achieved.
- **By voice:** "Hey Jarvis, log that I fixed the payment bug", "note a win: the client praised the demo". The wrap-up
  also asks whether anything from the day is worth noting.
- **Using it:** "What did I do in October?", "Draft my self-review for April to September" (saved as a note).
- If OneNote can't be reached, entries wait in a small local queue and are written later. A local list of what was
  logged automatically keeps a task or a day's meetings from being logged twice. Neither keeps a copy of the log.

## What it remembers

- **Chat history.** Every exchange and every reminder it spoke is saved in the database. A new conversation starts with the last few exchanges, and the pet can search older ones (full-text search, best matches first). The chat page at http://127.0.0.1:47821/ shows the history.
- **Daily summaries.** Each day's conversation is summarised in a few sentences the next day; the last week's summaries are part of every new conversation, so things said days ago are not lost.
- **Facts about you.** Things you tell it about yourself are stored and included in every conversation. Once a week duplicates are merged and facts that a newer one contradicts are removed; the previous list is kept as a backup in the database.
- **Settings.** Quiet hours, briefing and wrap-up times, working hours, how early to warn about tasks and meetings,
  meeting follow-ups and quiet, break interval, when a task counts as stale, and what to call you can all be changed
  by asking. The status page lists the current values.

## Settings

All in `.env` (see `.env.example`): pet name and wake words, time zone, briefing time, quiet hours
(nudges and briefings wait; reminders you set still fire), daily turn limit, Claude model and effort, port, and voice models.
After changing a voice model run `docker compose build voice`.

## Checks

```
docker compose exec server python tests/offline_check.py   # tools, reminders, speech delivery; no Claude needed
docker compose exec server python tests/voice_check.py     # text -> speech -> text round trip with timings
docker compose exec voice python wake_check.py             # wake-word detector against spoken test phrases
docker compose exec server python tests/wake_check.py      # wake word end to end through the server (one Claude turn)
docker compose exec server python tests/ms_fake_check.py   # To Do and OneNote tools against a fake Graph; no sign-in needed
docker compose exec -e PET_DATA_DIR=/tmp/planner-check server python tests/planner_check.py   # planning features, throwaway database
docker compose exec -e PET_DATA_DIR=/tmp/worklog-check server python tests/worklog_check.py   # work log and memory, fake OneNote
docker compose exec -e PET_DATA_DIR=/tmp/rephrase-check server python tests/rephrase_check.py   # Ctrl+Alt+R rewrites, no Claude needed
docker compose exec server python tests/smoke.py           # two real conversation turns and a reminder (uses Claude)
docker compose logs -f server
```

## Microsoft To Do and OneNote through the desktop apps

When the desktop shell is running, the pet can use the Office apps installed on this PC, with no sign-in or IT approval:

- **Notes → OneNote desktop app.** Create pages (in Quick Notes or a section you name; set `PET_ONENOTE_NOTEBOOK` in `.env` to pick the notebook, by name or by part of its OneDrive path when two notebooks share a name) with headings, bullets and checkbox lines; search titles and page text, read, append, delete (to the recycle bin).
- **Calendar → classic Outlook.** Read events (recurring ones included) and add a personal appointment. It cannot invite people, send anything, or change or delete existing events. The pet also speaks up shortly before a meeting starts.
- **Todos → Microsoft To Do, through classic Outlook's task folders.** Outlook syncs them with To Do. This needs classic
  Outlook to have your work account: open classic Outlook once and sign in. The new Outlook cannot be automated.
  Until then, todos stay in the pet's local list. The pet notices within five minutes of Outlook being set up.

How it works: the server sends a request such as `tasks_list` over the shell's existing WebSocket; the shell runs
`client/bridge.ps1` once for it and returns the result. No extra port is opened and nothing new is installed.
The bridge is limited to a fixed list of thirteen task, note and calendar operations, checked in both `client/main.js` and the script.
It never opens mail or contacts, never sends anything, and refuses to change or delete an Outlook item that is not a task.

`http://127.0.0.1:47821/health` shows what is in use under `todos_and_notes` (`local`, `desktop` or `graph`).
It only works while the shell is running on this PC; in a browser tab the pet falls back to its local lists.

### When calendar and To Do stop working

The bridge drives a hidden copy of classic Outlook (`OUTLOOK.EXE -Embedding`, no window). Calls go one at a time, so a stuck
Outlook also makes OneNote requests wait. The pet guards against this:

- If the hidden Outlook loses its mailbox logon (after sleep or a network change), the bridge closes and restarts it, at most
  every ten minutes, and only when it has no window open, so an Outlook you are working in is never closed.
- If Outlook shows an error box, the call fails at once with a clear message instead of hanging for 45 seconds.
- After a failed calendar or To Do check, the next background check waits twice as long (5, 10, 20, 40, then 60 minutes),
  and goes back to every five minutes after the first success.

The server log (`docker compose logs server`) shows "could not read the calendar" or "could not check Microsoft To Do" when
this happens. The usual fix is **"Outlook has exhausted all shared resources"**: Outlook's shared memory is used up, often by
OneNote left open for days (it loads Outlook's mail libraries) together with repeated Outlook restarts. Close OneNote and
Outlook, then restart the pet (`Stop-Process -Name electron`, then start it again). If that is not enough, sign out of
Windows or restart.

## Microsoft To Do and OneNote through Microsoft Graph (optional alternative)

This route works without the desktop apps but needs an Entra app registration. If you sign in, it takes priority over the desktop bridge.
Once you sign in, the pet's todo tools work on your Microsoft To Do lists and its note tools on your OneNote pages,
instead of its local database. Reminders and memory stay local. Signing out switches back.

1. Register an app in the Entra admin center (entra.microsoft.com → App registrations → New registration):
   - Supported account types: accounts in this organizational directory only.
   - No redirect URI.
   - After creating it: Authentication → Advanced settings → Allow public client flows → Yes.
   - API permissions → Add → Microsoft Graph → Delegated: `Tasks.ReadWrite`, `Notes.ReadWrite`, `Calendars.ReadWrite`, `User.Read`.
     Nothing for mail or contacts.
2. Put the Application (client) ID in `.env` as `MS_CLIENT_ID`, and optionally your Directory (tenant) ID as `MS_TENANT_ID`, then `docker compose up -d`.
3. Sign in. The command prints a code and a link; finish in your browser:

   ```
   docker compose exec server python -m app.msgraph login
   ```

`python -m app.msgraph status` shows who is signed in and `logout` removes the saved sign-in.
The sign-in is stored only in the `pet-data` volume. If your organisation blocks app registration or user consent,
step 1 or 3 fails with a message saying an administrator must approve; it cannot work without IT in that case.

Limits: OneNote pages can be found by title only (not by page text) and can be appended to but not rewritten.
A due time on a To Do task is stored as its reminder time, since To Do due dates are dates only.

## Adding integrations

Tools live in `server/app/tools.py` (local) and `server/app/ms_tools.py` (Microsoft Graph). To add Teams later,
write more `@tool` functions that call `msgraph.call`, add the `Chat.Read` scope in `msgraph.py`, and include the
tools in `active_tools`; the agent, voice and client need no changes.

## Remove everything

```
docker compose down --volumes --rmi local
```
