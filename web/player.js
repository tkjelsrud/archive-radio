// Radio playback via Web Audio (DESIGN.md §3.3): clips are small enough to
// fully prefetch and decode ahead of time, so transitions crossfade with a
// scheduled gain ramp instead of a hard `<audio src>` swap. Natural
// end-of-clip transitions get a longer overlapping crossfade; manual
// skip/back get a quick declick fade since the user wants it *now*.
//
// Scheduling is driven off AudioContext.currentTime rather than wall-clock
// timers, specifically so pause (ctx.suspend()) freezes the countdown for
// free — no separate bookkeeping needed to "remember where we were".

const CROSSFADE_SEC = 1.2;
const SKIP_FADE_SEC = 0.15;

const btnPlay = document.getElementById('btnPlay');
const btnNext = document.getElementById('btnNext');
const btnPrev = document.getElementById('btnPrev');
const statusEl = document.getElementById('status');
const filenameEl = document.getElementById('sourceFilename');
const locationEl = document.getElementById('sourceLocation');
const waveformCanvas = document.getElementById('waveform');
const waveformCtx = waveformCanvas.getContext('2d');
const artPlaceholderEl = document.getElementById('artPlaceholder');

let ctx = null;
let sessionId = null;
let current = null;   // { source, gain, buffer, startTime, sequenceNumber, item, crossfadeTriggered }
let preload = null;   // { sequenceNumber, promise -> { item, buffer } }
let rafHandle = null;
let detailOpen = false; // Clip Detail (§3.5): pauses auto-advance, loops current clip

function ensureContext() {
  if (!ctx) ctx = new (window.AudioContext || window.webkitAudioContext)();
  return ctx;
}

async function ensureSession() {
  if (sessionId !== null) return;
  const res = await fetch('/api/session', { method: 'POST' });
  sessionId = (await res.json()).session_id;
}

async function fetchItem(seq) {
  await ensureSession();
  const res = await fetch(`/api/session/${sessionId}/item/${seq}`);
  if (!res.ok) return null;
  return res.json();
}

async function fetchAndDecode(seq) {
  const item = await fetchItem(seq);
  if (!item) return null;
  const arrayBuffer = await (await fetch(item.clip_url)).arrayBuffer();
  const buffer = await ensureContext().decodeAudioData(arrayBuffer);
  return { item, buffer };
}

function startPreload(seq) {
  preload = { sequenceNumber: seq, promise: fetchAndDecode(seq) };
}

function updateMeta(item) {
  filenameEl.textContent = item.source_filename;
  locationEl.textContent = item.source_location;
  statusEl.textContent = `${item.sequence_number + 1}/${item.total_available}`;
}

// Approximate waveform (DESIGN.md §13's "even if just an option" ask): the
// full clip is already decoded client-side for playback, so this is just
// reading the raw samples once and drawing min/max per pixel column — no
// extra fetch, no backend involvement, no scrolling playhead for v1.
function drawWaveform(buffer) {
  const dpr = window.devicePixelRatio || 1;
  const cssWidth = waveformCanvas.clientWidth || 220;
  const cssHeight = waveformCanvas.clientHeight || 220;
  waveformCanvas.width = cssWidth * dpr;
  waveformCanvas.height = cssHeight * dpr;
  waveformCtx.setTransform(dpr, 0, 0, dpr, 0, 0);
  waveformCtx.clearRect(0, 0, cssWidth, cssHeight);

  const data = buffer.getChannelData(0);
  const columns = Math.max(1, Math.floor(cssWidth));
  const samplesPerColumn = Math.max(1, Math.floor(data.length / columns));
  const midY = cssHeight / 2;

  waveformCtx.strokeStyle = '#4a9';
  waveformCtx.lineWidth = 1;
  waveformCtx.beginPath();
  for (let col = 0; col < columns; col++) {
    const start = col * samplesPerColumn;
    let min = 0, max = 0;
    for (let i = 0; i < samplesPerColumn; i++) {
      const v = data[start + i] || 0;
      if (v < min) min = v;
      if (v > max) max = v;
    }
    waveformCtx.moveTo(col + 0.5, midY - max * midY * 0.9);
    waveformCtx.lineTo(col + 0.5, midY - min * midY * 0.9);
  }
  waveformCtx.stroke();

  artPlaceholderEl.style.display = 'none';
}

function makeVoice(buffer, item, startGain) {
  const gain = ensureContext().createGain();
  gain.gain.value = startGain;
  gain.connect(ctx.destination);
  const source = ctx.createBufferSource();
  source.buffer = buffer;
  source.connect(gain);
  return { source, gain, buffer, item, crossfadeTriggered: false };
}

function stopVoiceWithFade(voice, fadeSec) {
  if (!voice) return;
  const now = ctx.currentTime;
  voice.gain.gain.cancelScheduledValues(now);
  voice.gain.gain.setValueAtTime(voice.gain.gain.value, now);
  voice.gain.gain.linearRampToValueAtTime(0, now + fadeSec);
  voice.source.stop(now + fadeSec + 0.02);
}

function playFresh(voice, fadeInSec) {
  const now = ctx.currentTime;
  if (fadeInSec > 0) {
    voice.gain.gain.setValueAtTime(0, now);
    voice.gain.gain.linearRampToValueAtTime(1, now + fadeInSec);
  }
  voice.source.start(now);
  voice.startTime = now;
  updateMeta(voice.item);
  drawWaveform(voice.buffer);
}

async function loadAndPlay(seq, fadeInSec) {
  const data = (preload && preload.sequenceNumber === seq)
    ? await preload.promise
    : await fetchAndDecode(seq);
  preload = null;
  if (!data) {
    statusEl.textContent = 'nothing to play yet';
    return;
  }
  const voice = makeVoice(data.buffer, data.item, fadeInSec > 0 ? 0 : 1);
  playFresh(voice, fadeInSec);
  current = voice;
  startPreload(data.item.sequence_number + 1);
  runScheduler();
}

function crossfadeDurationFor(buffer) {
  // A fixed 1.2s fade would eat most of a sub-second one-shot clip —
  // scale it down for short clips instead of always using CROSSFADE_SEC.
  return Math.min(CROSSFADE_SEC, buffer.duration * 0.3);
}

function runScheduler() {
  if (rafHandle) cancelAnimationFrame(rafHandle);
  const tick = () => {
    if (!detailOpen && current && ctx && ctx.state === 'running' && !current.crossfadeTriggered) {
      const elapsed = ctx.currentTime - current.startTime;
      const remaining = current.buffer.duration - elapsed;
      if (remaining <= crossfadeDurationFor(current.buffer)) {
        current.crossfadeTriggered = true;
        crossfadeToNext();
      }
    }
    rafHandle = requestAnimationFrame(tick);
  };
  rafHandle = requestAnimationFrame(tick);
}

async function crossfadeToNext() {
  const outgoing = current;
  const fadeSec = crossfadeDurationFor(outgoing.buffer);
  const nextSeq = outgoing.item.sequence_number + 1;
  const data = (preload && preload.sequenceNumber === nextSeq)
    ? await preload.promise
    : await fetchAndDecode(nextSeq);
  if (!data) return; // nothing to advance to yet — let the outgoing clip just finish

  // The smooth-transition feeling comes from the OUTGOING clip fading out —
  // the incoming one doesn't need a slow ramp too. A full fadeSec fade-in
  // made every clip start quiet regardless of its actual content; a short
  // declick fade avoids a click without adding an artificial quiet start.
  const incoming = makeVoice(data.buffer, data.item, 0);
  const now = ctx.currentTime;
  const fadeInSec = Math.min(SKIP_FADE_SEC, fadeSec);
  incoming.gain.gain.setValueAtTime(0, now);
  incoming.gain.gain.linearRampToValueAtTime(1, now + fadeInSec);
  incoming.source.start(now);
  incoming.startTime = now;

  outgoing.gain.gain.cancelScheduledValues(now);
  outgoing.gain.gain.setValueAtTime(outgoing.gain.gain.value, now);
  outgoing.gain.gain.linearRampToValueAtTime(0, now + fadeSec);
  outgoing.source.stop(now + fadeSec + 0.05);

  current = incoming;
  updateMeta(incoming.item);
  drawWaveform(incoming.buffer);
  startPreload(incoming.item.sequence_number + 1);
}

async function skipTo(seq) {
  if (current) stopVoiceWithFade(current, SKIP_FADE_SEC);
  current = null;
  await new Promise((r) => setTimeout(r, SKIP_FADE_SEC * 1000));
  await loadAndPlay(seq, SKIP_FADE_SEC);
}

async function togglePlay() {
  ensureContext();
  if (!current) {
    btnPlay.textContent = '❚❚';
    await loadAndPlay(0, 0);
    return;
  }
  if (ctx.state === 'running') {
    await ctx.suspend();
    btnPlay.textContent = '▶';
  } else {
    await ctx.resume();
    btnPlay.textContent = '❚❚';
  }
}

function goNext() {
  if (detailOpen) return;
  const seq = current ? current.item.sequence_number + 1 : 0;
  skipTo(seq);
}

function goPrev() {
  if (detailOpen || !current || current.item.sequence_number === 0) return;
  skipTo(current.item.sequence_number - 1);
}

btnPlay.addEventListener('click', togglePlay);
btnNext.addEventListener('click', goNext);
btnPrev.addEventListener('click', goPrev);

// Keyboard shortcuts: space = play/pause, arrows = skip. Ignored while
// typing in the tag/note inputs (or the detail panel is otherwise
// capturing input) so typing a tag doesn't also skip tracks.
document.addEventListener('keydown', (e) => {
  const typingTarget = e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA';
  if (typingTarget) return;

  if (e.code === 'Space') {
    e.preventDefault();
    togglePlay();
  } else if (e.code === 'ArrowRight') {
    e.preventDefault();
    goNext();
  } else if (e.code === 'ArrowLeft') {
    e.preventDefault();
    goPrev();
  }
});

// --- Clip Detail (§3.5): click the clip to pause/loop it, tag/rate, then
// close to return to the Radio (advances to the next clip, doesn't resume
// the interrupted loop). "Not usable" is a hard exclude for objectively
// broken/unfit clips, not a subjective rating — see rating=-1 server-side.

const artSlot = document.getElementById('artSlot');
const detailOverlay = document.getElementById('detailOverlay');
const detailFilenameEl = document.getElementById('detailFilename');
const tagChipsEl = document.getElementById('tagChips');
const tagInputEl = document.getElementById('tagInput');
const tagSuggestionsEl = document.getElementById('tagSuggestions');
const noteInputEl = document.getElementById('noteInput');
const btnNotUsable = document.getElementById('btnNotUsable');
const btnDetailDone = document.getElementById('btnDetailDone');

let detailSegmentId = null;

function renderTagChips(tags) {
  tagChipsEl.innerHTML = '';
  for (const tag of tags) {
    const chip = document.createElement('span');
    chip.className = 'tag-chip' + (tag.source === 'auto' ? ' auto' : '');
    chip.textContent = tag.name;
    if (tag.source !== 'auto') {
      const remove = document.createElement('button');
      remove.textContent = '×';
      remove.addEventListener('click', async () => {
        const detail = await (await fetch(
          `/api/segment/${detailSegmentId}/tags/${encodeURIComponent(tag.name)}`,
          { method: 'DELETE' },
        )).json();
        renderTagChips(detail.tags);
      });
      chip.appendChild(remove);
    }
    tagChipsEl.appendChild(chip);
  }
}

async function refreshTagSuggestions() {
  const tags = await (await fetch('/api/tags')).json();
  tagSuggestionsEl.innerHTML = '';
  for (const tag of tags) {
    const option = document.createElement('option');
    option.value = tag.name;
    tagSuggestionsEl.appendChild(option);
  }
}

async function openDetail() {
  if (!current || detailOpen) return;
  detailOpen = true;
  current.source.loop = true;
  detailSegmentId = current.item.segment_id;

  detailFilenameEl.textContent = current.item.source_filename;
  tagInputEl.value = '';
  noteInputEl.value = '';
  refreshTagSuggestions();

  const detail = await (await fetch(`/api/segment/${detailSegmentId}/touch`, { method: 'POST' })).json();
  renderTagChips(detail.tags);
  noteInputEl.value = detail.note || '';
  btnNotUsable.classList.toggle('active', detail.rating === -1);
  btnNotUsable.textContent = detail.rating === -1 ? 'Marked not usable' : 'Not usable';

  detailOverlay.classList.remove('hidden');
}

async function closeDetail() {
  if (!detailOpen) return;
  if (current) current.source.loop = false;
  detailOverlay.classList.add('hidden');
  detailOpen = false;
  const nextSeq = current ? current.item.sequence_number + 1 : 0;
  await skipTo(nextSeq);
}

artSlot.addEventListener('click', openDetail);
btnDetailDone.addEventListener('click', closeDetail);

tagInputEl.addEventListener('keydown', async (e) => {
  if (e.key !== 'Enter') return;
  const name = tagInputEl.value.trim();
  if (!name) return;
  const detail = await (await fetch(`/api/segment/${detailSegmentId}/tags`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name }),
  })).json();
  renderTagChips(detail.tags);
  tagInputEl.value = '';
  refreshTagSuggestions();
});

noteInputEl.addEventListener('blur', () => {
  fetch(`/api/segment/${detailSegmentId}/note`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ note: noteInputEl.value }),
  });
});

btnNotUsable.addEventListener('click', async () => {
  const nowActive = !btnNotUsable.classList.contains('active');
  const detail = await (await fetch(`/api/segment/${detailSegmentId}/rating`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ rating: nowActive ? -1 : 0 }),
  })).json();
  btnNotUsable.classList.toggle('active', detail.rating === -1);
  btnNotUsable.textContent = detail.rating === -1 ? 'Marked not usable' : 'Not usable';
});
