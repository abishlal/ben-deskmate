/* The pet: animation, coming and going, microphone capture, speech playback and the server connection.
   Runs inside the Electron shell (window.petShell present) or in a plain browser tab for preview.

   Presence: by default the pet is out of sight. It comes up when you call it (wake word, hotkey, tray)
   or when it has something to tell you, stays for the conversation, then leaves. "Stay on screen" in the
   tray menu keeps it visible and wandering instead. */
(() => {
  const shell = window.petShell || null;
  document.body.classList.toggle('browser', !shell);

  const $ = (id) => document.getElementById(id);
  const pet = $('pet'), bubble = $('bubble'), bubbleText = $('bubble-text'), typeForm = $('type-form'), typeBox = $('type-box');
  const store = { get: (k) => { try { return localStorage.getItem(k); } catch { return null; } }, set: (k, v) => { try { localStorage.setItem(k, v); } catch {} } };
  const settings = {
    handsfree: store.get('handsfree') !== '0',   // wake word on unless switched off
    muted: store.get('muted') === '1',
    stay: store.get('stay') === '1',
  };
  const FOLLOW_UP_MS = 7000;    // how long it listens for a follow-up after answering
  const LINGER_MS = 6000;       // how long it stays after the conversation ends
  const HOME_FROM_RIGHT = 330;  // where it comes up, measured from the right edge

  let ws = null, connected = false;
  let mode = 'sleep';            // sleep | ambient | listen | think | talk
  let turnDone = true, replyText = '', youText = '';
  let voiceTurn = false;         // the current exchange was spoken, so listen for a spoken follow-up
  let followUp = false;          // listening for a follow-up (no wake word needed, quiet if nothing is said)
  let paused = false;            // "do not disturb", switched by the user
  let inCall = false;            // another app is using the microphone (a Teams call, a recording)
  let inMeeting = false;         // a meeting on the calendar is happening now (the server says so)
  let skipAudio = 0;             // announcement clips to drop while it must stay silent
  let discard = false;           // a reply was interrupted: drop what is still arriving from it
  let dictating = false;         // dictation mode: speech is typed into the active app, not sent to the assistant
  let idleS = null;              // seconds since the last keyboard or mouse input (from the desktop shell)
  let talkKey = 'the talk shortcut';   // e.g. "Ctrl + Alt + B", sent by the desktop shell
  let quietHintAt = 0;
  let capturing = false;         // listening session: everything said until "stop" is collected, then summarised
  let captureStarted = 0;
  const CAPTURE_SILENCE_MS = 120000, CAPTURE_MAX_MS = 15 * 60000;
  const quiet = () => paused || inCall || inMeeting;   // no wake word, no follow-up listening, no speaking unprompted

  // ---------------------------------------------------------------- presence, animation and movement
  let present = false, anim = 'sleep', leaveTimer = 0;
  let x = 0, targetX = null, speed = 0, facingLeft = false, nextDecision = 0, lastFrame = 0;

  function paint() {
    pet.className = anim + (facingLeft ? ' left' : '') + (present ? '' : ' away') + (window.ben3d ? ' three' : '')
      + (held || flying ? ' physics' : '') + (lifting ? ' lifting' : '');
  }
  // While he is held, flying or dizzy the drag-and-throw code owns the animation; the rest waits (recover() catches up).
  function setAnim(name, force = false) { if (!force && (held || flying || stunned)) return; anim = name; paint(); }

  // Drag and throw (see the section below); declared here because painting and placing use them.
  let y = 0, vx = 0, vy = 0;                     // height above the floor, and velocity, in px and px/s
  let held = false, flying = false, stunned = false, lifted = false, lifting = false;
  const floorShadow = $('floor-shadow'), stage = $('stage');
  // Every screen's bounds and work area in screen DIPs (sent by the shell), and the one he is on now.
  // x and y above stay relative to that screen's work area: x from its left edge, y up from its taskbar.
  let displays = null, homeId = null;

  function arrive() {
    clearTimeout(leaveTimer);
    if (present) return;
    if (!settings.stay) { x = Math.max(0, innerWidth - HOME_FROM_RIGHT); facingLeft = true; targetX = null; }
    present = true; place(); paint();
    waveUntil = performance.now() + 1500;   // the 3D Ben waves hello as he comes up
  }

  // The 3D Ben (pet3d.js sets window.ben3d when WebGL works) follows the same states as the drawn one.
  let waveUntil = 0;
  function syncBen(now, level) {
    const ben = window.ben3d;
    if (!ben) return;
    if (!present) { ben.pause(); return; }   // out of sight: no rendering
    ben.resume();
    let state = { listen: 'listen', think: 'think', talk: 'talk', sleep: 'sleep', held: 'held', fly: 'fly', dizzy: 'dizzy' }[anim] || (quiet() ? 'quiet' : 'idle');
    if (now < waveUntil && (state === 'idle' || state === 'listen')) state = 'wave';
    ben.setState(state);
    ben.setFps(['talk', 'wave', 'held', 'fly', 'dizzy'].includes(state) ? 24 : 12);   // he renders in software: keep it light
    if (ben.setAirborne) ben.setAirborne(held || y > 2);
    ben.setTalkLevel(level);
  }

  function leave() {
    if (settings.stay || !present) return;
    present = false; paint();
    shell && shell.setIgnoreMouse(true);
  }

  function leaveSoon(ms) {
    clearTimeout(leaveTimer);
    if (settings.stay) return;
    leaveTimer = setTimeout(() => {
      if (mode !== 'ambient' && mode !== 'sleep' || !typeForm.hidden || playing || held || flying || stunned) { leaveSoon(3000); return; }
      youText = replyText = ''; render(); leave();
    }, ms);
  }

  function setMode(next) {
    mode = next;
    targetX = null;
    if (next === 'listen' || next === 'think' || next === 'talk') arrive();
    if (next === 'ambient') {
      setAnim('idle'); nextDecision = performance.now() + 3000 + Math.random() * 3000;
      if (settings.stay) arrive(); else leaveSoon(LINGER_MS);
    } else if (next === 'sleep') {
      setAnim('sleep');
      if (settings.stay) arrive(); else leaveSoon(1500);
    } else setAnim({ listen: 'listen', think: 'think', talk: 'talk' }[next]);
  }

  function place() {
    const max = Math.max(0, area().width - 120);
    x = Math.min(max, Math.max(0, x));
    pet.style.transform = `translate(${x}px, ${-y}px)`;
    bubble.classList.toggle('edge-left', x < 110);
    bubble.classList.toggle('edge-right', x > max - 110);
    // A shadow on the floor under him while he is off it: smaller and fainter the higher he is.
    const high = Math.min(1, y / 400), off = present && (held || y > 2);
    floorShadow.style.opacity = off ? (1 - high * 0.6).toFixed(2) : '0';
    floorShadow.style.transform = `translateX(${x}px) scale(${(1 - high * 0.5).toFixed(2)})`;
  }

  function decide(now) {
    const r = Math.random();
    if (r < 0.5) {
      targetX = Math.random() * Math.max(0, innerWidth - 120);
      const run = Math.random() < 0.2;
      speed = run ? 190 : 55;
      facingLeft = targetX < x;
      setAnim(run ? 'run' : 'walk');
    } else {
      setAnim(r < 0.8 ? 'sit' : 'idle');
      nextDecision = now + 5000 + Math.random() * 9000;
    }
  }

  function frame(now) {
    const dt = Math.min(0.1, (now - lastFrame) / 1000);
    lastFrame = now;
    const moved = present && physics(dt, now);
    if (pose(now) || moved) place();
    if (settings.stay && present && mode === 'ambient' && bubble.hidden && !held && !flying && !stunned) {   // wandering only in stay mode
      if (targetX !== null) {
        const step = speed * dt, gap = targetX - x;
        if (Math.abs(gap) <= step) { x = targetX; targetX = null; setAnim('idle'); nextDecision = now + 1500 + Math.random() * 5000; }
        else x += Math.sign(gap) * step;
        place();
      } else if (now > nextDecision) decide(now);
    }
    syncBen(now, updateMouth());
    requestAnimationFrame(frame);
  }

  // ---------------------------------------------------------------- speech bubble
  let hideTimer = 0;
  function render() {
    bubbleText.replaceChildren();
    if (youText) { const you = document.createElement('div'); you.className = 'you'; you.textContent = youText; bubbleText.append(you); }
    if (replyText) bubbleText.append(document.createTextNode(replyText));
    bubble.hidden = !(youText || replyText) && typeForm.hidden;
  }
  function say(text, ms = 0) { youText = ''; replyText = text; render(); hideLater(ms); }
  function hideLater(ms) {
    clearTimeout(hideTimer);
    if (ms) hideTimer = setTimeout(() => { if (!typeForm.hidden) return; youText = replyText = ''; render(); }, ms);
  }

  // ---------------------------------------------------------------- audio out
  let actx = null, analyser = null, currentSrc = null, playing = false;
  const playQueue = [];
  const levels = new Uint8Array(256);

  function audio() {
    if (!actx) {
      actx = new AudioContext();
      analyser = actx.createAnalyser(); analyser.fftSize = 256; analyser.connect(actx.destination);
    }
    if (actx.state === 'suspended') actx.resume();
    return actx;
  }

  function enqueueAudio(buf) {
    if (settings.muted) return;
    playQueue.push(buf);
    if (!playing) playNext();
  }

  async function playNext() {
    const buf = playQueue.shift();
    if (!buf) { playing = false; currentSrc = null; if (turnDone) finishTurn(); return; }
    playing = true;
    try {
      const decoded = await audio().decodeAudioData(buf);
      if (!playing) return;                       // interrupted while decoding
      const src = actx.createBufferSource();
      src.buffer = decoded; src.connect(analyser);
      src.onended = () => { if (currentSrc === src) playNext(); };
      currentSrc = src;
      if (mode !== 'listen' || followUp) { followUp = false; setMode('talk'); }
      src.start();
    } catch { playNext(); }
  }

  function stopSpeaking() {
    playQueue.length = 0; playing = false;
    const src = currentSrc; currentSrc = null;
    try { src && src.stop(); } catch {}
  }

  function updateMouth() {
    let level = 0;
    if (playing && analyser) {
      analyser.getByteTimeDomainData(levels);
      let sum = 0; for (const v of levels) sum += (v - 128) * (v - 128);
      level = Math.min(1, Math.sqrt(sum / levels.length) / 40);
    }
    pet.style.setProperty('--mouth', level.toFixed(2));
    return level;
  }

  /** The pet has finished answering (or announcing). After a spoken exchange it keeps listening for a follow-up. */
  function finishTurn() {
    if (mode === 'listen' && !followUp) return;
    if (!connected) { setMode('sleep'); return; }
    if (voiceTurn && micReady && typeForm.hidden && !quiet()) {
      resetVad(); listenStart = performance.now(); followUp = true;
      setMode('listen');                           // the reply stays in the bubble while it listens
    } else {
      setMode('ambient'); hideLater(LINGER_MS);
    }
  }

  // ---------------------------------------------------------------- microphone and voice activity detection
  let micReady = false, micFailed = false;
  let floor = 0.005, hot = 0, speaking = false, silenceMs = 0, totalMs = 0, listenStart = 0;
  let chunks = [], preroll = [];
  let wakeEngine = false;        // the server has a wake-word detector (openWakeWord)
  let lastUtterance = null;      // the most recent thing heard while waiting, kept for two seconds at most

  let mic = null;                // { stream, src, proc, sink } of the open microphone
  let lastBlockAt = 0;           // when audio last arrived; the watchdog reopens the microphone if this goes stale
  let micOpening = null, micReopens = 0, micPeak = 0;
  let deadSince = 0;             // when the microphone started delivering pure digital silence (a dead capture)

  function closeMic() {
    micReady = false;
    const m = mic; mic = null;
    if (!m) return;
    try { m.proc.onaudioprocess = null; m.src.disconnect(); m.proc.disconnect(); m.sink.disconnect(); } catch {}
    try { m.stream.getTracks().forEach((t) => { t.onended = null; t.stop(); }); } catch {}
  }

  /** Open (or reopen) the microphone. Windows can end the capture after sleep, a device change or another app's call. */
  function openMic() {
    if (micOpening) return micOpening;
    micOpening = (async () => {
      closeMic();
      try {
        const stream = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true, channelCount: 1 } });
        const c = audio(), src = c.createMediaStreamSource(stream), proc = c.createScriptProcessor(2048, 1, 1), sink = c.createGain();
        sink.gain.value = 0;
        src.connect(proc); proc.connect(sink); sink.connect(c.destination);
        proc.onaudioprocess = (e) => onBlock(e.inputBuffer.getChannelData(0), c.sampleRate);
        stream.getAudioTracks().forEach((t) => { t.onended = () => { micReady = false; }; });
        mic = { stream, src, proc, sink };
        lastBlockAt = performance.now(); resetVad();
        micReady = true; micFailed = false;
        return true;
      } catch {
        if (!micFailed && present) say("I can't reach the microphone. Double-click me to type instead.", 7000);
        micFailed = true;
        return false;
      } finally { micOpening = null; }
    })();
    return micOpening;
  }

  const micAlive = () => micReady && performance.now() - lastBlockAt < 4000;

  async function ensureMic() {
    if (micAlive()) { audio(); return true; }
    return openMic();
  }

  // Watchdog: while the wake word is on, make sure audio is really arriving, and report status to the server.
  setInterval(() => {
    if (actx && actx.state === 'suspended') actx.resume();
    if (settings.handsfree && connected && !micAlive() && !micOpening) { micReopens++; openMic(); }
    // After sleep Windows sometimes keeps the capture running but sends only zeros. A real microphone is never
    // exactly silent, so thirty seconds of zeros means it has to be reopened.
    if (settings.handsfree && connected && deadSince && performance.now() - deadSince > 30000 && !micOpening) {
      deadSince = 0; micReopens++; openMic();
    }
    if (ws && ws.readyState === 1) {
      ws.send(JSON.stringify({ type: 'status', mic: micAlive(), mic_reopens: micReopens, mic_level: +micPeak.toFixed(4),
        wake_word: settings.handsfree, paused, in_call: inCall, in_meeting: inMeeting, dictating, capturing, idle_s: idleS,
        muted: settings.muted, mode, present, shell: !!shell }));
    }
    micPeak = 0;
    if (capturing && performance.now() - captureStarted > CAPTURE_MAX_MS) endCapture();   // fifteen minutes at most
  }, 5000);
  if (navigator.mediaDevices && navigator.mediaDevices.addEventListener) {
    navigator.mediaDevices.addEventListener('devicechange', () => { if (settings.handsfree && connected) setTimeout(() => { micReopens++; openMic(); }, 1500); });
  }

  function resetVad() { hot = 0; speaking = false; silenceMs = 0; totalMs = 0; chunks = []; preroll = []; }

  function onBlock(samples, rate) {
    lastBlockAt = performance.now();
    let blockPeak = 0;
    for (let i = 0; i < samples.length; i += 16) { const v = Math.abs(samples[i]); if (v > blockPeak) blockPeak = v; }
    if (blockPeak > micPeak) micPeak = blockPeak;
    if (blockPeak === 0) { if (!deadSince) deadSince = performance.now(); } else deadSince = 0;
    const waitingForWake = settings.handsfree && !quiet() && (mode === 'ambient' || mode === 'sleep') && connected;
    // While it talks or thinks, the wake-word detector keeps listening, so "Hey Jarvis" interrupts it.
    // (Echo cancellation keeps its own voice out of the microphone; its replies never contain the wake phrase.)
    if ((mode === 'talk' || mode === 'think') && settings.handsfree && wakeEngine && connected && !quiet() && !dictating) streamWake(samples, rate);
    // Quiet for a meeting or call: still notice "Hey Jarvis", only to explain silently why it is not answering.
    if (settings.handsfree && wakeEngine && connected && !paused && (inCall || inMeeting) && (mode === 'ambient' || mode === 'sleep')) streamWake(samples, rate);
    // Just after a request was sent, and before any answer, speaking again means you had not finished: keep listening.
    const resumable = mode === 'think' && lastSent && !playing && performance.now() - lastSent.at < RESUME_MS;
    if (mode !== 'listen' && !waitingForWake && !resumable) { if (speaking || preroll.length) resetVad(); return; }
    if (waitingForWake && wakeEngine && mode !== 'listen') streamWake(samples, rate);
    let sum = 0; for (let i = 0; i < samples.length; i++) sum += samples[i] * samples[i];
    const rms = Math.sqrt(sum / samples.length), blockMs = samples.length / rate * 1000;
    const threshold = Math.max(0.012, floor * 3.5);
    const copy = new Float32Array(samples);

    if (!speaking) {
      if (rms < threshold) floor = floor * 0.95 + rms * 0.05;
      preroll.push(copy); if (preroll.length > 8) preroll.shift();
      hot = rms > threshold ? hot + 1 : 0;
      if (hot >= 3) {
        speaking = true; chunks = preroll.slice(); preroll = []; silenceMs = 0; totalMs = chunks.length * blockMs;
        if (resumable) resumeRequest();
      } else if (mode === 'listen' && performance.now() - listenStart > (capturing ? CAPTURE_SILENCE_MS : dictating ? 12000 : followUp ? FOLLOW_UP_MS : 10000)) {
        resetVad();
        if (capturing) { listenStart = performance.now(); endCapture(); }   // two minutes of silence: work with what was said
        else if (dictating) stopDictation('Done dictating.');
        else if (followUp) { followUp = false; setMode('ambient'); hideLater(LINGER_MS); }   // nothing more to say: leave quietly
        else { say("I didn't catch anything.", 3000); chime(true); reportHeard('(listened after the wake word, heard nothing)'); setMode('ambient'); }
      }
      return;
    }
    chunks.push(copy); totalMs += blockMs;
    silenceMs = rms > threshold * 0.6 ? 0 : silenceMs + blockMs;
    const request = mode === 'listen' && !capturing && !dictating;
    // A request that has gone on for a while gets a longer pause before it counts as finished (people pause to think).
    const endSilence = request && totalMs + (carry ? carry.ms : 0) > 4000 ? 1500 : 850;
    // A request still going after ~20 s is dictation (meeting minutes, say): turn it into a listening session.
    if (request && connected && totalMs + (carry ? carry.ms : 0) >= LONG_REQUEST_MS) { goLong(); return; }
    // While waiting for the wake word, cap the length: long background speech is not worth transcribing.
    if (silenceMs >= endSilence || totalMs >= (mode !== 'listen' ? 12000 : capturing ? 40000 : 20000)) finishUtterance(rate);
  }

  const RESUME_MS = 2500, LONG_REQUEST_MS = 20000;
  let lastSent = null;   // the request just sent: { parts, rate, ms, at }
  let carry = null;      // its audio, while you carry on speaking; it goes in front of what comes next

  function sendUtterance(parts, rate, wake) {
    ws.send(JSON.stringify({ type: 'audio', wake }));
    ws.send(encodeWav(parts, rate));
    voiceTurn = true;
    lastSent = wake ? null : { parts, rate, ms: parts.reduce((n, p) => n + p.length, 0) / rate * 1000, at: performance.now() };
  }

  /** You started speaking again right after a request was sent: drop that request and keep listening. */
  function resumeRequest() {
    carry = lastSent; lastSent = null;
    stopSpeaking();
    if (connected) ws.send(JSON.stringify({ type: 'interrupt' }));
    discard = true; turnDone = true;
    listenStart = performance.now(); followUp = false;
    setMode('listen'); say('Listening…');
  }

  /** The request has run long: from here it is a listening session that ends when you say "stop". */
  function goLong() {
    if (carry) { chunks = carry.parts.concat(chunks); totalMs += carry.ms; carry = null; }
    capturing = true; captureStarted = performance.now(); listenStart = performance.now();
    ws.send(JSON.stringify({ type: 'capture_start', auto: true }));   // the recording so far becomes its first piece
    chime();
    say('Still listening. Say "stop" when you\'re done.');
  }

  function finishUtterance(rate) {
    const spokenMs = totalMs - silenceMs;
    let parts = chunks;
    resetVad();
    if (carry && !capturing && !dictating) {     // what you said before the pause goes first
      parts = spokenMs < 350 ? carry.parts : carry.parts.concat(parts);
      carry = null;
    } else if (spokenMs < 350 || !connected) { if (mode === 'listen') listenStart = performance.now(); return; }
    if (!connected) return;
    if (capturing && mode === 'listen') {          // listening session: send this piece and keep listening
      ws.send(JSON.stringify({ type: 'audio', capture: true }));
      ws.send(encodeWav(parts, rate));
      listenStart = performance.now();
      return;
    }
    if (dictating && mode === 'listen') {          // dictation: transcribe this piece and keep listening
      ws.send(JSON.stringify({ type: 'audio', dictate: true }));
      ws.send(encodeWav(parts, rate));
      listenStart = performance.now();
      return;
    }
    if (mode !== 'listen') {
      // Heard while waiting to be called. With the wake-word detector nothing is transcribed: the recording
      // is only kept for a moment in case the detector reports that it was the wake phrase.
      if (wakeEngine) lastUtterance = { parts, rate, at: performance.now() };
      else sendUtterance(parts, rate, true);      // no detector: the server looks for the wake phrase in the text
      return;
    }
    sendUtterance(parts, rate, false);
    wasFollowUp = followUp; followUp = false; setMode('think'); if (!wasFollowUp) say('…');
  }
  let wasFollowUp = false;

  /** Live microphone audio for the wake-word detector: 16 kHz 16-bit samples behind a 'WAKE' tag. */
  function streamWake(samples, rate) {
    if (!ws || ws.readyState !== 1) return;
    const ratio = rate / 16000, n = Math.floor(samples.length / ratio);
    const view = new DataView(new ArrayBuffer(4 + n * 2));
    view.setUint32(0, 0x57414b45);                // "WAKE"
    for (let i = 0; i < n; i++) {
      const from = Math.floor(i * ratio), to = Math.min(samples.length, Math.floor((i + 1) * ratio));
      let acc = 0; for (let j = from; j < to; j++) acc += samples[j];
      view.setInt16(4 + i * 2, Math.max(-1, Math.min(1, acc / Math.max(1, to - from))) * 0x7fff, true);
    }
    ws.send(view.buffer);
  }

  /** The detector heard the wake phrase. A rising chime says "I'm listening" even when you are not looking. */
  function onWakeWord() {
    if (quiet()) {
      if (!paused && performance.now() - quietHintAt > 60000 && (mode === 'ambient' || mode === 'sleep')) {
        quietHintAt = performance.now();
        arrive();
        say(`I'm keeping quiet because you're ${inMeeting ? 'in a meeting' : 'on a call'}. Press ${talkKey} to talk to me.`, 7000);
        leaveSoon(8000);
      }
      return;
    }
    if (dictating || capturing) return;
    chime();
    if (mode === 'talk' || mode === 'think') {      // called while answering: stop and listen
      interrupt(); followUp = false; voiceTurn = true; resetVad(); listenStart = performance.now();
      setMode('listen'); say('Yes?');
      return;
    }
    if (mode !== 'ambient' && mode !== 'sleep') return;
    voiceTurn = true; followUp = false; listenStart = performance.now();
    if (speaking) { setMode('listen'); say('Listening…'); return; }   // still talking: keep recording the same sentence
    const last = lastUtterance; lastUtterance = null;
    if (last && performance.now() - last.at < 2000) {                 // the sentence had just ended: use it
      setMode('listen'); sendUtterance(last.parts, last.rate, false);
      wasFollowUp = false; setMode('think'); say('…');
    } else { resetVad(); setMode('listen'); say('Yes?'); }
  }

  /** Diagnostics for the status page's "heard" list (kept in the server's memory only). */
  function reportHeard(text) { if (ws && ws.readyState === 1) ws.send(JSON.stringify({ type: 'heard', text })); }

  /** 16 kHz mono 16-bit WAV from float blocks at the capture rate. */
  function encodeWav(parts, rate) {
    const total = parts.reduce((n, p) => n + p.length, 0), input = new Float32Array(total);
    let o = 0; for (const p of parts) { input.set(p, o); o += p.length; }
    const ratio = rate / 16000, n = Math.floor(total / ratio);
    const view = new DataView(new ArrayBuffer(44 + n * 2));
    const text = (at, s) => { for (let i = 0; i < s.length; i++) view.setUint8(at + i, s.charCodeAt(i)); };
    text(0, 'RIFF'); view.setUint32(4, 36 + n * 2, true); text(8, 'WAVEfmt '); view.setUint32(16, 16, true);
    view.setUint16(20, 1, true); view.setUint16(22, 1, true); view.setUint32(24, 16000, true); view.setUint32(28, 32000, true);
    view.setUint16(32, 2, true); view.setUint16(34, 16, true); text(36, 'data'); view.setUint32(40, n * 2, true);
    for (let i = 0; i < n; i++) {
      const from = Math.floor(i * ratio), to = Math.min(total, Math.floor((i + 1) * ratio));
      let acc = 0; for (let j = from; j < to; j++) acc += input[j];
      const v = Math.max(-1, Math.min(1, acc / Math.max(1, to - from)));
      view.setInt16(44 + i * 2, v * 0x7fff, true);
    }
    return view.buffer;
  }

  // ---------------------------------------------------------------- actions
  function interrupt() {
    lastSent = carry = null;
    stopSpeaking();
    if (connected && !turnDone) { ws.send(JSON.stringify({ type: 'interrupt' })); discard = true; }
    turnDone = true;
  }

  /** Dictation: what you say is typed into the app that has the keyboard focus. The hotkey again stops it. */
  async function toggleDictation() {
    if (dictating) { stopDictation('Stopped dictating.'); return; }
    arrive();
    if (!connected) { say("I'm not connected to my server yet.", 4000); leaveSoon(5000); return; }
    interrupt(); followUp = false;
    if (!(await ensureMic())) { leaveSoon(8000); return; }
    dictating = true; resetVad(); listenStart = performance.now();
    setMode('listen'); say('Dictating. Speak, and I will type it where your cursor is.');
  }
  /** Listening session: the pet listens until you say "stop", then summarises or rephrases what you said.
      The hotkey starts one; pressing it again ends it. Saying "summarise this" to the pet starts one too. */
  function toggleCapture() {
    if (!connected) { arrive(); say("I'm not connected to my server yet.", 4000); leaveSoon(5000); return; }
    if (capturing) endCapture();
    else ws.send(JSON.stringify({ type: 'capture_start' }));
  }
  function endCapture() {
    if (connected) ws.send(JSON.stringify({ type: 'capture_stop' }));
    say('Working on it…');
  }
  async function beginCapture() {
    if (dictating) stopDictation('');
    arrive(); stopSpeaking(); turnDone = true; followUp = false;   // the server has already stopped any running turn
    if (!(await ensureMic())) { leaveSoon(8000); return; }
    capturing = true; captureStarted = performance.now();
    resetVad(); listenStart = performance.now(); voiceTurn = true;
    setMode('listen'); say('Listening. Say "stop" when you\'re done.');
    chime();
  }
  /** A short two-note sound: rising when it starts listening, falling when it gives up. */
  let lastChime = 0;
  function chime(down = false) {
    if (settings.muted) return;
    lastChime = performance.now();
    try {
      const c = audio(), t = c.currentTime;
      (down ? [660, 495] : [660, 880]).forEach((f, i) => {
        const o = c.createOscillator(), g = c.createGain();
        o.frequency.value = f; g.gain.setValueAtTime(0.0001, t + i * 0.12);
        g.gain.exponentialRampToValueAtTime(0.12, t + i * 0.12 + 0.02);
        g.gain.exponentialRampToValueAtTime(0.0001, t + i * 0.12 + 0.18);
        o.connect(g); g.connect(c.destination); o.start(t + i * 0.12); o.stop(t + i * 0.12 + 0.2);
      });
    } catch {}
  }

  function stopDictation(message) {
    dictating = false; resetVad();
    if (message) say(message, 2500);
    setMode('ambient');
  }

  /** Call the pet: it comes up and listens. Calling it again while it listens sends it away. */
  async function talk() {
    if (capturing) { endCapture(); return; }      // the talk hotkey during a session ends it
    if (dictating) stopDictation('');
    arrive();
    if (!connected) { say("I'm not connected to my server yet.", 4000); leaveSoon(5000); return; }
    if (mode === 'listen' && !followUp) { resetVad(); youText = replyText = ''; render(); setMode('ambient'); leaveSoon(800); return; }
    interrupt(); followUp = false;
    if (!(await ensureMic())) { leaveSoon(8000); return; }
    resetVad(); listenStart = performance.now(); voiceTurn = true;
    setMode('listen'); say('Listening…');
  }

  /** The dismiss hotkey: stop whatever it is doing (talking, thinking, listening, dictating, a listening session,
      a rephrase) and hide at once, even in "stay on screen" mode. Calling it again brings it back. */
  function dismiss() {
    clearTimeout(leaveTimer); clearTimeout(hideTimer);
    interrupt(); followUp = false;
    if (capturing) { capturing = false; if (connected) ws.send(JSON.stringify({ type: 'capture_cancel' })); }
    dictating = false;
    if (rephraseId) { shell && shell.rephraseResult(rephraseId, '', 'dismissed'); rephraseId = null; rephraseOwnsMode = false; }
    resetVad();
    typeForm.hidden = true; typeBox.value = '';
    youText = replyText = ''; render();
    setMode('ambient');
    clearTimeout(leaveTimer);
    if (held || flying || stunned) return;   // mid-air: he lands, then leaves on his own
    present = false; paint();
    shell && shell.setIgnoreMouse(true);
  }

  function openTypeBox() {
    arrive(); interrupt(); followUp = false;
    if (mode === 'listen') setMode('ambient');
    youText = replyText = ''; typeForm.hidden = false; render();
    shell && shell.setIgnoreMouse(false);
    typeBox.focus();
  }
  function closeTypeBox() { typeForm.hidden = true; typeBox.value = ''; render(); if (mode === 'ambient') leaveSoon(LINGER_MS); }

  function sendText(text) {
    if (!connected) return;
    audio(); arrive();
    voiceTurn = false; turnDone = false; youText = text; replyText = ''; render();
    ws.send(JSON.stringify({ type: 'text', text, speak: !settings.muted }));
  }

  function setHandsfree(on) {
    settings.handsfree = on; store.set('handsfree', on ? '1' : '0');
    if (on) ensureMic();
  }
  function setMuted(on) { settings.muted = on; store.set('muted', on ? '1' : '0'); if (on) stopSpeaking(); }
  /** Stop listening on its own. A wake-word capture or follow-up in progress is dropped. */
  function goQuiet() {
    if (speaking || preroll.length) resetVad();
    if (mode === 'listen' && followUp) { followUp = false; setMode('ambient'); }
  }
  function setPaused(on) {
    paused = on;
    if (on) goQuiet();
    arrive(); say(on ? "Paused. I won't listen or speak until you resume me." : "I'm listening again.", 3500);
    if (mode === 'ambient' || mode === 'sleep') leaveSoon(3500);
  }
  function setInCall(on) { inCall = on; if (on) goQuiet(); }

  function setStay(on) {
    settings.stay = on; store.set('stay', on ? '1' : '0');
    if (on) { arrive(); if (mode === 'ambient') setMode('ambient'); }
    else if (mode === 'ambient' || mode === 'sleep') leaveSoon(800);
  }

  // ---------------------------------------------------------------- server connection
  function connect() {
    ws = new WebSocket(`ws://${location.host}/ws`);
    ws.binaryType = 'arraybuffer';
    ws.onclose = () => { connected = false; stopSpeaking(); turnDone = true; followUp = false; setMode('sleep'); setTimeout(connect, 3000); };
    ws.onmessage = (e) => {
      if (typeof e.data !== 'string') { if (skipAudio > 0) skipAudio--; else if (!discard) { lastSent = null; enqueueAudio(e.data); } return; }
      const m = JSON.parse(e.data);
      if (discard && (m.type === 'reply_delta' || m.type === 'reply_done' || m.type === 'audio')) return;   // from an interrupted reply
      if (m.type === 'state' || m.type === 'notify' || m.type === 'transcript') discard = false;
      switch (m.type) {
        case 'host_call':   // the server asks the desktop shell to reach Outlook or OneNote
          if (shell && shell.hostCall) {
            shell.hostCall(m.op, m.args)
              .catch((err) => ({ ok: false, error: String(err && err.message || err) }))
              .then((res) => { if (ws.readyState === 1) ws.send(JSON.stringify({ type: 'host_result', id: m.id, ok: !!res.ok, data: res.data, error: res.error })); });
          }
          break;
        case 'wake_word': onWakeWord(); break;
        case 'hello':
          connected = true; wakeEngine = !!m.wake_engine; inMeeting = !!m.meeting_quiet; setMode('ambient');
          ws.send(JSON.stringify({ type: 'client', host: !!(shell && shell.hostCall) }));
          if (settings.handsfree) ensureMic();
          if (!m.voice) { arrive(); say('My voice service is offline, so I can only type for now.', 6000); leaveSoon(7000); }
          break;
        case 'state':
          if (m.state === 'thinking') { turnDone = false; replyText = ''; if (mode !== 'listen') setMode('think'); }
          break;
        case 'transcript': youText = m.text; replyText = ''; render(); break;
        case 'reply_delta':   // the answer has started: no more resuming
          lastSent = null; replyText += m.text; render(); clearTimeout(hideTimer);
          break;
        case 'reply_done':
          turnDone = true; replyText = m.text; render();
          if (!playing) finishTurn();
          break;
        case 'notify':   // a reminder, nudge or briefing: come up and say it
          arrive();
          youText = ''; replyText = m.body; render(); turnDone = true;
          shell && shell.notify(m.title, m.body);
          voiceTurn = settings.handsfree;      // so "snooze that ten minutes" works straight after
          if (m.has_audio && quiet()) skipAudio++;   // paused or on a call: show it, do not say it aloud
          if (!m.has_audio || settings.muted || quiet()) { if (mode === 'ambient' || mode === 'sleep') { setMode('ambient'); leaveSoon(12000); } hideLater(12000); }
          else if (mode === 'ambient' || mode === 'sleep') setMode('talk');
          break;
        case 'meeting_quiet':   // a calendar meeting started or ended
          inMeeting = !!m.on;
          if (inMeeting) goQuiet();
          break;
        case 'dismissed':   // "stop", "never mind", "thanks": go away without an answer
          stopSpeaking(); turnDone = true; followUp = false; youText = replyText = ''; render();
          setMode('ambient'); leaveSoon(600);
          break;
        case 'capture_start': if (!(m.auto && capturing)) beginCapture(); break;   // auto: already listening (goLong)
        case 'capture_progress':
          youText = `${m.words} words so far`; replyText = `…${m.last}`; render();
          break;
        case 'capture_end':
          if (m.cancelled && !capturing) break;   // thrown away by the dismiss hotkey: nothing to say
          capturing = false; resetVad();
          if (m.cancelled) { say('Okay, I threw that away.', 3000); setMode('ambient'); }
          else if (m.empty) { say("I didn't hear anything to work with.", 4000); setMode('ambient'); }
          else { youText = `${m.words} words`; replyText = '…'; render(); turnDone = false; setMode('think'); }
          break;
        case 'result_text':   // the full summary or rewrite: onto the clipboard
          if (shell && shell.copyText) shell.copyText(m.text);
          break;
        case 'type_text':     // "paste it"
          if (shell && shell.typeText) shell.typeText(m.text);
          break;
        case 'rephrased': endRephrase(m); break;   // the rewrite of the selected text, or why there is none
        case 'dictation':
          if (!m.text) break;
          if (shell && shell.typeText) shell.typeText(m.text + ' ');
          youText = ''; replyText = m.text; render();
          break;
        case 'wake':        // just the name was said: come up and wait for the request
          if (performance.now() - lastChime > 3000) chime();
          listenStart = performance.now(); followUp = false; voiceTurn = true;
          setMode('listen'); say('Yes?');
          break;
        case 'ignored':     // speech without the wake word, or nothing intelligible
          if (mode === 'think') {
            if (wasFollowUp) { setMode('ambient'); hideLater(LINGER_MS); }
            else { say("I didn't catch that.", 3000); setMode('ambient'); }
          }
          break;
        case 'error': turnDone = true; stopSpeaking(); arrive(); say(m.message, 8000); setMode('ambient'); leaveSoon(9000); break;
      }
    };
  }

  // ---------------------------------------------------------------- rephrase (Ctrl+Alt+R)
  // The shell copies the selected text and pastes the rewrite over it; here the text goes to the server and Ben
  // shows he is on it. He never speaks for this, so it works the same during calls, meetings and a pause.
  let rephraseId = null, rephraseOwnsMode = false;
  const REPHRASE_STATUS = {
    empty: ['Select some text first, then press the keys again.', 4000],
    keys: ['Let go of the keys and try again.', 3000],
    long: ["That's too long for one go. Select under 5000 characters.", 5000],
    failed: ["I couldn't copy the selection from that app.", 5000],
    busy: ['Still working on the last one.', 2500],
    clipboard: ['You switched windows, so the rewrite is on your clipboard. Press Ctrl+V.', 7000],
  };
  function rephraseSay(text, ms) {
    arrive(); say(text, ms);
    if (mode === 'ambient') leaveSoon(ms + 500);
  }
  function startRephrase(job) {
    if (!job || !job.id) return;
    if (!connected || !ws || ws.readyState !== 1) {
      shell && shell.rephraseResult(job.id, '', 'offline');
      rephraseSay("I'm not connected to my server, so I can't rephrase right now.", 5000);
      return;
    }
    rephraseId = job.id;
    rephraseOwnsMode = mode === 'ambient' || mode === 'sleep';
    arrive();
    if (rephraseOwnsMode) setMode('think');
    say('Rephrasing…');
    ws.send(JSON.stringify({ type: 'rephrase', id: job.id, text: String(job.text || '') }));
  }
  function endRephrase(m) {
    shell && shell.rephraseResult(m.id, m.text || '', m.error || '');
    if (m.id !== rephraseId) return;
    rephraseId = null;
    if (rephraseOwnsMode && mode === 'think') setMode('ambient');
    rephraseOwnsMode = false;
    if (m.error) rephraseSay(m.error, 6000);
    else rephraseSay('Done.', 1500);
  }

  // ---------------------------------------------------------------- drag and throw
  // Pick Ben up and drop him, or fling him: he flies, bounces off the screen edges and lands with a squash
  // (dizzy after a hard one). While he is off the floor the shell grows the window to the whole work area.
  const GRAVITY = 2600, MAX_THROW = 3200, FLOOR_BOUNCE = 0.42, WALL_BOUNCE = 0.55, DIZZY_IMPACT = 1700;
  let grab = null;            // { id, offX, offY, sx, sy } while the button is down on him
  let samples = [];           // recent positions while held, for the throw speed
  let suppressClick = false, liftTimer = 0, dizzyTimer = 0;
  let tilt = 0, tiltV = 0, squashAt = 0, squashAmt = 0, hardest = 0, lastPose = '';
  const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));
  const pick = (list) => list[Math.floor(Math.random() * list.length)];
  const ceiling = () => Math.max(0, (lifted ? area().height : stage.clientHeight) - 150);

  /** The work area he is on, in screen DIPs. In a browser tab (no shell) it is the page itself, in page pixels. */
  function area() {
    const d = displays && (displays.find((s) => s.id === homeId) || displays[0]);
    if (d) return d.area;
    return { x: 0, y: stage.getBoundingClientRect().bottom - stage.clientHeight, width: innerWidth, height: stage.clientHeight };
  }
  /** Pointer position in the same frame as area(). */
  const pointer = (e) => (displays ? { px: e.screenX, py: e.screenY } : { px: e.clientX, py: e.clientY });
  const screenAt = (px, py) => displays && displays.find((d) => px >= d.bounds.x && px < d.bounds.x + d.bounds.width && py >= d.bounds.y && py < d.bounds.y + d.bounds.height);

  /** He crossed onto another screen: keep where he is on the desktop, and move the window there. */
  function moveToScreen(id) {
    const a = area(), gx = a.x + x, feet = a.y + a.height - y;
    homeId = id;
    const b = area();
    x = gx - b.x; y = b.y + b.height - feet;
    if (shell && shell.lift) {
      lifting = true; paint(); shell.lift(true, homeId);
      clearTimeout(liftTimer); liftTimer = setTimeout(endLifting, 300);
    }
  }

  function setLift(on) {
    if (lifted === on) return;
    lifted = on;
    document.body.classList.toggle('lifted', on);
    if (!shell || !shell.lift) { place(); return; }
    lifting = true; paint();                       // hidden until the window has its new size, so he never jumps
    shell.lift(on, homeId);
    clearTimeout(liftTimer); liftTimer = setTimeout(endLifting, 300);
  }
  function endLifting() { if (!lifting) return; lifting = false; clearTimeout(liftTimer); place(); paint(); }

  /** A one-liner in the bubble, only when it is not showing anything else. */
  function quip(text, ms) { if (bubble.hidden && typeForm.hidden) say(text, ms); }

  function pickUp() {
    held = true; flying = false; stunned = false; samples = []; targetX = null; tiltV = 0;
    clearTimeout(clickTimer); clearTimeout(leaveTimer); clearTimeout(dizzyTimer);
    setLift(true); setAnim('held', true);
    if (Math.random() < 0.6) quip(pick(['Whoa!', 'Hey!', 'Put me down!', 'Where are we going?']), 1200);
  }

  function release() {
    grab = null;
    if (!held) return;
    held = false;
    suppressClick = true; setTimeout(() => { suppressClick = false; }, 400);
    const a = samples[0], b = samples[samples.length - 1], now = performance.now();
    const span = a && b ? (b.t - a.t) / 1000 : 0;
    if (span > 0.01 && now - b.t < 80) { vx = (b.x - a.x) / span; vy = (b.y - a.y) / span; }
    else vx = vy = 0;                               // he was held still before letting go: just drop
    const speed = Math.hypot(vx, vy);
    if (speed > MAX_THROW) { vx *= MAX_THROW / speed; vy *= MAX_THROW / speed; }
    samples = []; hardest = 0; flying = true;
    setAnim('fly', true);
    if (speed > 1600) quip(pick(['Wheee!', 'Aaaah!', 'I can fly!']), 1400);
  }

  function squash(impact, now) { squashAt = now; squashAmt = Math.min(0.32, impact / 5000); }

  function land() {
    flying = false; vx = vy = 0; setLift(false);
    if (hardest > DIZZY_IMPACT) {
      stunned = true; setAnim('dizzy', true);
      quip(pick(['Oof…', 'Ow, my head…', 'Everything is spinning…', 'Again! …No, wait.']), 2200);
      dizzyTimer = setTimeout(recover, 2600);
    } else recover();
  }
  function recover() {
    if (held || flying) return;
    stunned = false;
    setAnim({ listen: 'listen', think: 'think', talk: 'talk', sleep: 'sleep' }[mode] || 'idle', true);
    nextDecision = performance.now() + 2500;
    if (mode === 'ambient' || mode === 'sleep') leaveSoon(LINGER_MS);
  }

  /** A damped spring: he swings when carried and leans into a throw, then settles upright. */
  function swingTo(target, dt) {
    tiltV += ((target - tilt) * 90 - tiltV * 7) * dt; tilt += tiltV * dt;
    if (target === 0 && Math.abs(tilt) < 0.05 && Math.abs(tiltV) < 0.5) tilt = tiltV = 0;
  }

  /** One step of the physics. Returns true when he moved. */
  function physics(dt, now) {
    if (held) {
      const a = samples[0], b = samples[samples.length - 1];
      const v = a && b && b.t - a.t > 10 && now - b.t < 80 ? (b.x - a.x) / ((b.t - a.t) / 1000) : 0;
      swingTo(clamp(-v * 0.015, -40, 40), dt);     // the body trails behind the hand
      return false;
    }
    if (!flying) { if (tilt || tiltV) swingTo(0, dt); return false; }
    vy -= GRAVITY * dt; x += vx * dt; y += vy * dt;
    // Onto other screens. Going up he passes behind the upper screen's taskbar once his middle crosses the edge;
    // if the throw runs out before he clears it, he falls back to the screen his feet are over.
    const other = (s) => (s && s.id !== homeId ? s : null);
    let a = area();
    const cx = () => a.x + x + 60;
    const above = () => other(screenAt(cx(), a.y - 1));
    if (vy > 0 && y > a.height - 75 && above()) { moveToScreen(above().id); a = area(); }
    else if (vy <= 0 && y < 0) {
      const below = other(screenAt(cx(), a.y + a.height - y));
      if (below) { moveToScreen(below.id); a = area(); }
    }
    let max = Math.max(0, a.width - 120);
    if (x < 0 || x > max) {
      const side = other(screenAt(x < 0 ? a.x - 1 : a.x + a.width + 1, a.y + a.height - y - 75));
      if (side) { moveToScreen(side.id); a = area(); max = Math.max(0, a.width - 120); }
    }
    if (x < 0 || x > max) {                        // a screen edge with nothing beyond it
      x = clamp(x, 0, max); hardest = Math.max(hardest, Math.abs(vx));
      vx = -vx * WALL_BOUNCE; tiltV += vx * 0.05;
    }
    const top = ceiling();
    if (y > top && !above()) { y = top; vy = -Math.abs(vy) * WALL_BOUNCE; }   // the top of the desktop
    // (Just after rising onto a screen above he can be below its taskbar for a moment: that is not a landing.)
    if (y <= 0 && vy <= 0) {
      y = 0;
      const impact = -vy;
      hardest = Math.max(hardest, impact);
      if (impact > 380) { vy = impact * FLOOR_BOUNCE; vx *= 0.75; squash(impact, now); }
      else {
        if (impact > 150) squash(impact, now);
        vy = 0; vx *= Math.exp(-7 * dt);           // sliding to a stop
        if (Math.abs(vx) < 25) { land(); return true; }
      }
    }
    if (Math.abs(vx) > 40 && facingLeft !== vx < 0) { facingLeft = vx < 0; paint(); }
    swingTo(clamp(vx * 0.012, -30, 30), dt);
    return true;
  }

  /** Squash, stretch and swing go into --pose on the sprite (not the bubble). Returns true while it changes. */
  function pose(now) {
    const t = (now - squashAt) / 1000;
    let s = 0;
    if (squashAmt) { if (t < 1.2) s = squashAmt * Math.exp(-t * 7) * Math.cos(t * 26); else squashAmt = 0; }
    // scale about his feet, swing about his head:
    // translate(60,150) scale translate(-60,-150) translate(60,30) rotate translate(-60,-30), with the middle two merged
    const next = s || tilt ? `translate(60px,150px) scale(${(1 + s * 0.7).toFixed(3)},${(1 - s).toFixed(3)}) translate(0,-120px) rotate(${tilt.toFixed(2)}deg) translate(-60px,-30px)` : '';
    if (next === lastPose) return false;
    lastPose = next;
    if (next) pet.style.setProperty('--pose', next); else pet.style.removeProperty('--pose');
    return true;
  }

  // Positions are taken in screen coordinates, so they stay right while the window grows or moves to another screen.
  $('flip').addEventListener('pointerdown', (e) => {
    if (e.button !== 0 || !present) return;
    const { px, py } = pointer(e), a = area();
    grab = { id: e.pointerId, offX: px - (a.x + x), offY: a.y + a.height - y - py, sx: px, sy: py };
    try { $('flip').setPointerCapture(e.pointerId); } catch {}
  });
  $('flip').addEventListener('pointermove', (e) => {
    if (!grab || e.pointerId !== grab.id) return;
    const { px, py } = pointer(e);
    if (!held) { if (Math.hypot(px - grab.sx, py - grab.sy) < 6) return; pickUp(); }
    const over = screenAt(px, py);                 // carried onto another screen
    if (over && over.id !== homeId) moveToScreen(over.id);
    const a = area();
    x = px - grab.offX - a.x;
    y = clamp(a.y + a.height - (py + grab.offY), 0, ceiling());
    const now = performance.now();
    samples.push({ t: now, x, y });
    while (samples.length > 2 && now - samples[0].t > 100) samples.shift();
    place();
  });
  for (const type of ['pointerup', 'pointercancel', 'lostpointercapture']) {
    $('flip').addEventListener(type, (e) => { if (grab && e.pointerId === grab.id) release(); });
  }
  addEventListener('blur', () => { if (grab) release(); });

  // ---------------------------------------------------------------- input wiring
  let clickTimer = 0;
  $('flip').addEventListener('click', () => { if (suppressClick) return; clearTimeout(clickTimer); clickTimer = setTimeout(talk, 240); });
  $('flip').addEventListener('dblclick', () => { if (suppressClick) return; clearTimeout(clickTimer); openTypeBox(); });

  typeForm.addEventListener('submit', (e) => { e.preventDefault(); const t = typeBox.value.trim(); closeTypeBox(); if (t) sendText(t); });
  typeBox.addEventListener('keydown', (e) => { if (e.key === 'Escape') closeTypeBox(); });
  addEventListener('resize', () => { place(); if (lifting) requestAnimationFrame(endLifting); });

  if (shell) {
    // The window covers the full width above the taskbar; let clicks through everywhere except the pet.
    addEventListener('mousemove', (e) => shell.setIgnoreMouse(!(held || present && e.target.closest && e.target.closest('#pet'))));
    shell.onCommand((cmd, value) => {
      if (cmd === 'talk') talk();
      else if (cmd === 'dismiss') dismiss();
      else if (cmd === 'type') openTypeBox();
      else if (cmd === 'handsfree') setHandsfree(!!value);
      else if (cmd === 'mute') setMuted(!!value);
      else if (cmd === 'stay') setStay(!!value);
      else if (cmd === 'pause') setPaused(!!value);
      else if (cmd === 'incall') setInCall(!!value);
      else if (cmd === 'dictate') toggleDictation();
      else if (cmd === 'capture') toggleCapture();
      else if (cmd === 'rephrase') startRephrase(value);
      else if (cmd === 'rephrase-status') { const [text, ms] = REPHRASE_STATUS[value] || REPHRASE_STATUS.failed; rephraseSay(text, ms); }
      else if (cmd === 'idle') idleS = Number(value);
      else if (cmd === 'mic-reset') { micReopens++; openMic(); }
      else if (cmd === 'hotkey') talkKey = String(value);
      else if (cmd === 'displays' && value && Array.isArray(value.list)) {
        displays = value.list;
        if (!held && !flying) homeId = value.home;
        place();
      }
    });
    shell.ready({ handsfree: settings.handsfree, muted: settings.muted, stay: settings.stay });
  }

  x = Math.max(0, innerWidth - HOME_FROM_RIGHT);
  place(); paint(); connect();
  requestAnimationFrame((t) => { lastFrame = t; frame(t); });

  // Hooks for automated checks and the browser preview.
  window.__pet = {
    state: () => ({ screen: homeId, mode, connected, present, followUp, paused, inCall, inMeeting, dictating, capturing, wakeEngine, micReady, x: Math.round(x), y: Math.round(y), held, flying, stunned, cls: pet.className, bubble: bubble.hidden ? null : bubbleText.textContent }),
    fling: (sx, sy, fromY = 200) => { arrive(); setLift(true); held = true; y = fromY; samples = [{ t: performance.now() - 50, x, y }, { t: performance.now(), x: x + sx / 20, y: y + sy / 20 }]; release(); },
    sendText, talk, dismiss, setStay, arrive, setAnim, toggleDictation, toggleCapture, setMode, feed: onBlock,
  };
})();
