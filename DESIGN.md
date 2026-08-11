# Personal Audio Archive Radio — Product Specification

## 1. Concept

A locally hosted web application for exploring a large personal archive of unfinished music projects, recordings, jams, samples, field recordings, and other audio material.

The archive may contain many years of WAV/AIFF files stored on a Linux home server. The application should turn this passive archive into an **instrument for discovering future musical ideas**.

The central interaction is a radio-like continuous stream that randomly surfaces musically active parts of recordings rather than requiring the user to browse folders and files.

The guiding principle:

> Don't organize the archive so the user can search it. Make the archive discoverable so it can surprise the user.

---

## 2. Goals

### Primary goals

- Make a large, messy audio archive enjoyable to browse.
- Surface forgotten material and unfinished ideas.
- Skip silence and near-empty sections automatically.
- Allow continuous, hands-off listening.
- Make it extremely easy to mark something worth returning to.
- Preserve links back to the original recordings and project locations.
- Run locally on the user's home server.
- Analyze the archive offline so playback remains lightweight.

### Non-goals for the first version

Do not initially attempt to:

- Replace a DAW.
- Edit audio.
- Mix or master audio.
- Automatically turn clips into songs.
- Provide sophisticated AI music analysis.
- Perfectly classify musical genres.
- Understand the artistic quality of a recording.
- Create a complex media-library management system.

---

## 2a. Version 1 Feasibility Decisions

A feasibility pass surfaced a few places where the original spec was internally inconsistent or under-specified. These decisions resolve them for v1; sections below have been updated to match.

- **Silence detection is just silence detection.** v1 does not attempt to distinguish "quiet but musically important" from "genuinely empty." It only skips near-silent regions (e.g. a take waiting on a beat, or a DAW-trimmed edit where the untouched file on disk still has dead space around the used part). Recognizing musically-active-but-quiet material (drones, textures) is explicitly deferred — see §10.
- **No content hashing.** Change detection for incremental scans does not hash file contents. A file already present in the index (by path) is simply skipped. This trades "detect edited files" for "scan is cheap to resume," which matches how this archive is actually used (files are rarely modified in place).
- **Scanning is resumable, not necessarily fast.** The indexer is pointed at a folder, walks it recursively, and can be paused and resumed. Progress (files done, remaining) is tracked so a long scan doesn't need to complete in one sitting. This removes the need to optimize raw scan throughput for v1.
- **Playback uses compiled clips, not the original files.** Each detected segment is rendered to a small standalone file: mono, 16-bit, 22.05 kHz WAV, capped at 10 seconds. The web app only ever serves these compiled clips. Original files are opened only during rendering and are never modified or served directly — see §8a, §18.
- **LAN-only for now.** The app is reachable from the local network, not exposed externally. No auth is required for v1.
- **Jottacloud Archive ingestion is strictly read-only, batched by year, and its own pipeline stage.** Source material that lives only in Jottacloud's Archive is pulled down in year-prefix batches (e.g. "process all `2014-*` project folders") via the official `jotta-cli` (list/download only — never its archive/sync/upload commands), filtered by type and size *before* downloading, verified after, and staged locally for the existing scanner to pick up. Staged originals are only removed by an explicit, manual cleanup step once a batch is confirmed good — never automatically right after rendering, since batches will likely be reprocessed while the pipeline is still being tested — see §5a.
- **Separate scripts per pipeline stage, one shared database, no per-script side-state.** See §20.
- **Very short files (down to ~500ms) are supported as real material**, not treated as noise — drum hits/one-shot samples are a normal part of this archive. Minimum segment length and padding scale down to fit files shorter than the 3-second default rather than rejecting them outright — see §9.

Deferred, not resolved — revisit later:
- Seeded session reproducibility (§17) still degrades as the archive is incrementally rescanned; no snapshot/versioning mechanism defined yet.
- Raw archive scan throughput on very large (100s of GB) archives is unaddressed beyond "it's resumable," since that's judged sufficient for now.

---

# 3. Core User Experience

## 3.1 Endless Radio

The main screen is essentially a personal radio station.

Press **Play** and the application continuously plays selected sections from the archive.

Example:

```text
old synth jam
        ↓
field recording
        ↓
Octatrack experiment
        ↓
unfinished song
        ↓
bass improvisation
        ↓
strange noise recording
        ↓
another forgotten session
```

The user should not need to choose a file before listening.

---

## 3.2 Random Interesting Moment

The fundamental unit of playback is not necessarily an entire file.

Instead:

```text
recording.wav
    03:14.6 → 04:01.3
```

The application selects an interesting segment and starts playback at that position.

A **Next** button immediately selects another segment.

Random selection should preferably be deterministic when requested, using a seed.

This makes it possible to reproduce a particular exploration session.

Example:

```text
Session seed: 482731
```

The same seed should produce the same sequence if the underlying archive has not changed.

---

## 3.3 Continuous Stream (Radio)

The player should preselect the next segment before the current segment finishes.

The experience should therefore feel continuous rather than like repeatedly loading files.

**Crossfading is cheap now, not a "future maybe."** §8a's decision to render small, fully-local compiled clips (mono, ≤10s, a few hundred KB) instead of seeking into large originals means the *entire* next clip can be prefetched and decoded via the Web Audio API (`decodeAudioData`) ahead of time. A scheduled `GainNode` ramp between outgoing and incoming sources gives a real crossfade with no gap — this wasn't practical under the original "seek into a multi-GB file" assumption, but it is now, so it's in scope for v1 rather than deferred. Hard-cut remains the fallback if crossfading isn't ready in time.

**Transport is tape-deck style:** Play/Pause, Skip Back, Skip Ahead — not just "Next." Skip Back replays the actual previous segment (not a fresh random pick), so hearing something interesting and wanting to rewind ~10 seconds works as expected. This reuses §17's `sessions`/`session_items` log as real playback history: every segment played (whether freshly chosen or replayed via Skip Back) is appended to the current session's item list, and Skip Back/Skip Ahead just move a play-head index through that list rather than generating new random picks.

---

## 3.4 Favorites Pad

A second, independent playback surface alongside the Radio: a grid/list of your touched clips (§3.5), each with its own trigger.

- Triggering a pad clip **loops it until you stop it** — the point is building up a layered texture by triggering several at once, not a one-shot preview.
- Pad playback **never affects the Radio** — triggering a favorite does not pause, duck, or otherwise touch the main stream. If you want quiet, you pause the Radio yourself.
- Each pad clip plays through its own independent Web Audio source, so multiple can genuinely overlap.

---

## 3.5 Clip Detail, Tagging & Rating

Clicking the currently-playing clip (in the Radio or, later, elsewhere) opens **Clip Detail**: the Radio's forward progress pauses and that one clip loops, giving you time to tag/rate it without the stream moving on. Closing Clip Detail resumes the Radio going forward.

- **Merely opening Clip Detail marks the clip `touched`** — tagging or rating is optional on top of that, not required to count as touched. Given how much of an auto-segmented archive is going to be incoherent fragments (one track drums, one vocals, etc.), tagging *every* clip that streams by isn't realistic — this is deliberately an opt-in, per-interesting-clip action, not a mandatory step.
- **A touched clip is a favorite by default.** There is no separate "add to pad" toggle — Favorites Pad (§3.4) membership is simply *touched AND not thumbed-down* (see rating, below). Touching a clip and thumbing it down at the same time means "I looked, I don't want this" — it's excluded from the pad (and everywhere else), not added to it.
- **Tags:** free-form, but picked via a quick-select over tags you've already used rather than typed fresh each time. A clip may also carry a free-text note (kept separate from tags, for the "great unstable bass texture"-style remark that doesn't fit a tag).
- **Default year tag:** for Jottacloud-sourced clips, the year is the batch year already known at ingestion time (§5a) — not derived from cloud file-modified-time, which is unverified to survive the round trip. For already-local files, it falls back to filesystem modified time.
- **Rating is per-clip/segment, not per-file.** Thumbs-down excludes only that specific clip from ever being selected again (Radio or Curated Radio, §3.6) — other segments from the same source file remain fully eligible, since a file can easily contain both junk and something worth keeping.

---

## 3.6 Effects & Ambient Mode

A simple effects block in the player: light reverb (`ConvolverNode` with a small static impulse-response asset) and delay (`DelayNode` + feedback `GainNode`), inserted into the same Web Audio graph used for crossfading (§3.3), with a wet/dry mix control.

This is entirely client-side — it runs in the listener's browser and adds no load to the home server or the processing pipeline. Settings (reverb/delay amount, ambient toggle) are ephemeral UI state, not written to the database.

**Ambient Mode is the same knob turned up, not a separate feature.** Cranking the wet mix all the way drenches every clip in reverb/delay, and combined with crossfading, the reverb tail of one clip bleeds into the next one's onset — turning the same Radio stream into a generative ambient wash without any additional logic beyond the effect parameters themselves.

---

# 4. Archive Processing Pipeline

Before the web application becomes useful, the archive must be processed.

This is an **offline indexing stage**.

```text
Jottacloud Archive (cloud-only files)
     ↓
Ingestion: list → filter → download → verify (§5a)
     ↓
Local archive (Jottacloud sync + already-local files)
     ↓
File scanner (skips already-indexed files)
     ↓
Audio metadata extraction
     ↓
Loudness / activity analysis
     ↓
Interesting-segment detection
     ↓
Segment rendering (compiled mono clip, ≤10s)
     ↓
SQLite archive index
     ↓
Web application
```

The scan should be repeatable, incremental, and resumable — it can be paused mid-archive and continued later without redoing already-indexed files.

---

# 5. Supported Audio

Initial target formats:

- WAV
- AIFF / AIF

The architecture should allow additional formats later.

Use a reliable command-line audio tool such as **FFmpeg** for decoding and metadata extraction where practical.

The application should not require conversion of the original archive.

Original audio files remain untouched.

---

# 5a. Jottacloud Archive Ingestion

Part of the source material lives only in Jottacloud's **Archive** — a cloud-only area for manually-uploaded files that are deliberately not mirrored to any local device (distinct from a synced folder). Those files must be pulled to local disk before anything in this pipeline can touch them.

**Tool:** the official `jotta-cli`, used strictly for its read operations (`ls`/`list` to enumerate, `download` to fetch — the latter is an async job you poll via `list downloads` / `list downloadinfo`). The tool also has mutating commands (`archive`, `sync`, `add`); this project never calls them. Read-only is enforced by omission, not by a permissions flag the tool provides — so code review of the sync script matters here: it should not be possible to accidentally construct a mutating call.

**Ingestion runs in year-batches, not as one sweep of the whole Archive.** Project folders under `Archive/Musikkprosjekter/` are named inconsistently (`2014-something`, `2014-q1`, `2016Q1-...`, `2026-8 Summer jam`, ...) but every observed name so far starts with a 4-digit year. A batch = "process every top-level folder starting with `2014`" (etc.), picked by the user one (or a few) years at a time. This bounds how much sits in local staging at once to roughly 5–10 project folders' worth of files rather than the entire archive, and sidesteps needing an automatic "what's new" picker — you just name the next year.

```text
Jottacloud Archive (Musikkprosjekter/)
     ↓
List folders matching a year prefix (jotta-cli ls)
     ↓
Recurse into each matching project folder (WAVs may be at any depth, e.g. under a "media" subfolder — no fixed layout)
     ↓
Filter (extension: wav/aiff, size > threshold, not already synced)
     ↓
Download (jotta-cli download)
     ↓
Verify (size/checksum match against listing)
     ↓
Local staging directory  →  File scanner (§6) picks it up from here like any local file
```

**Unmatched folders are reported, not silently skipped.** Every run also lists top-level folders that don't start with a 4-digit year at all (or belong to a year never processed) so a naming outlier can't quietly go forever unprocessed with nothing surfacing it.

**Filter before download, not after — but degrades gracefully if `ls` doesn't report size.** The candidate list from `ls` is filtered by extension and a minimum size (default: 100 KB, to skip empty/placeholder files — tunable) *before* any transfer starts, so bandwidth and the async download queue are never spent on files that would just be discarded. If `ls` turns out not to expose size per entry (unconfirmed, see earlier open question), the size check is simply dropped from this pre-download stage and the extension filter still applies — a handful of near-empty files getting downloaded needlessly is a small, one-time cost, and both real emptiness guards (near-zero local size, zero detected segments) still catch them once local, at essentially no extra cost since the file is already there.

**Resumability, on the shared DB, not a private state file.** A file already recorded as synced (by its Jottacloud path) is skipped if a batch is re-run. This uses the same `files` table as the local scanner — see §11 — rather than a separate index kept only by the sync script, specifically to avoid two scripts disagreeing about what's already been handled (see §20's "shared DB, no side-state" rule).

**Download verification is a distinct concern from §2a's "no hashing" decision.** §2a's no-hashing rule is about not re-checking already-indexed *local* files for in-place edits — that's still correct. A network transfer is a different failure mode (truncation, corruption in transit), so each download is checked against the size (and checksum, where `jotta-cli` exposes one) reported by `ls` before being marked synced. A failed/mismatched download is retried, not silently accepted.

**Filename fidelity.** Jottacloud's Archive doesn't guarantee byte-identical filenames survive a round trip (case-insensitivity, Unicode remapping of special characters). The synced-down local path is what the scanner indexes as usual, but the original Jottacloud path is also stored (see §11) so the "Open Source" control (§14) can still point back to the true cloud location even if the local filename differs slightly.

**Local staging is cleaned up manually, per batch, not automatically after render.** Deleting a batch's staged originals right after a successful render sounds tidy, but while the analysis/rendering logic is still being tested, the same batch will likely need reprocessing more than once — automatic deletion would force a re-download from Jottacloud every time that happens. Instead, a separate `cleanup_batch.py` (§20) removes a batch's staged originals only when explicitly run, once you're satisfied with its results. Disk cost is bounded to "whatever batches haven't been cleaned up yet," not the whole archive.

---

# 6. File Scanner

The scanner recursively walks configured archive directories.

For each audio file it should record:

- Absolute or server-relative path
- Filename
- Extension
- File size
- Modification time
- Duration
- Sample rate
- Number of channels
- Bit depth where available

No content hashing. A file is identified by its path: if a matching path already exists in the index, it is skipped. This is a deliberate v1 simplification — files in this archive are rarely edited in place, so detecting in-place modification isn't worth the cost of hashing every file on every scan.

The scanner runs as a long-lived, resumable job:

- It can be started, paused, and resumed without losing progress.
- Progress is tracked per file as it's committed to the index, so a paused/interrupted scan simply continues from wherever it left off — no need to re-walk or re-check already-indexed files.
- Deleted/missing source files are not actively reconciled in v1 (their index entries and compiled clips are just left in place). Handling deletions is a future improvement, not a v1 requirement.

---

# 7. Audio Activity Analysis

The first important analysis is identifying and skipping near-silent sections — not judging what's "musically interesting" (see §10). Real-world source material for this archive commonly has dead space that isn't musically active: a take waiting on a beat before it starts, or a file that was trimmed to a section in the DAW while the untouched file on disk still has silence/room tone around it.

The initial implementation should use relatively simple signal analysis rather than machine learning.

Possible approach:

1. Decode audio at a reduced analysis resolution.
2. Divide the recording into short windows, e.g. 100–250 ms.
3. Calculate RMS/loudness for each window.
4. Smooth the resulting envelope.
5. Apply an activity threshold.
6. Merge nearby active windows.
7. Remove very short active regions.
8. Add configurable padding around detected regions.

Example:

```text
Raw recording:

| silence |     audio      | silence | audio |

Detected segments:

          [-----------]
                         [------]
```

**Window duration is fixed, not scaled to file length.** Windows are a fixed duration (100–250 ms) so detection resolution stays constant regardless of how long a file is — a short active take doesn't get missed just because it sits inside an otherwise-silent multi-hour recording. (A fixed *count* of windows per file, scaled by file size, was considered as a simpler alternative but rejected: it would make window duration grow with file length, and a long enough file could make windows too coarse to catch short bursts of activity — the opposite of what this section exists to do.)

**This same pass is also the emptiness check for ingested files (§5a), not a separate mechanism.** A file that produces zero active windows anywhere is simply a file with zero segments — no extra "is this file silent" step is needed on top of activity analysis, it's the same computation.

**A file with zero detected segments still gets a row in `files`** (scanned, zero segments — not absent from the index). This mirrors §5a's "unmatched folders are reported, not silently skipped": a file that never produces any radio clips should be visible as "processed, nothing found," not indistinguishable from a file that was never scanned at all.

---

# 8. Interesting Segments

Each detected segment should contain at least:

- File ID
- Start time
- End time
- Duration
- Mean RMS
- Peak level

Potential future attributes:

- Loudness
- Spectral centroid
- Spectral flux
- Zero-crossing rate
- MFCCs
- Estimated tempo
- Tonal/noise characteristics

The MVP should not depend on these advanced features.

---

# 8a. Segment Rendering (Compiled Clips)

Once a segment is detected, it is rendered to a small standalone audio file — this compiled clip, not the original file, is what the web app actually plays.

- **Format:** mono, 22.05 kHz, 16-bit PCM WAV. (8-bit was considered for extra size savings, but linear 8-bit PCM has audible quantization noise, especially on the quiet tails/decays common in this kind of unfinished/exploratory material — at these lengths the size difference is a few hundred KB per clip either way, not worth the quality loss. Staying at 16-bit.)
- **Length cap:** 10 seconds. If the detected active region is longer, take the first 10 seconds of it for v1 rather than the full region.
- **Storage:** written to a local cache/render directory, sharded by source file — `/clip-cache/<file_id>/<segment_id>.wav` — rather than one flat directory, since a large archive can produce hundreds of thousands of clips and most filesystems handle that badly as a single directory. Path stored on the segment's DB row.
- **Originals are read-only.** The original file is opened only to render the clip; it is never modified, moved, or served directly to the browser.

This sidesteps having the browser seek into arbitrary offsets of large, multi-GB original files (unreliable across formats and slow over network-attached storage), at the cost of some fidelity in the browsing copy. The "Open Source" control (§14) is how the user gets back to the original, full-fidelity file when a clip is worth pursuing further.

### One source file, many segments

A single large or long recording can produce many segments. This is handled as two distinct costs, not one:

- **Analysis (decode + RMS envelope) happens once per source file**, regardless of how many segments it ultimately yields.
- **Extraction is cheap per segment.** Because sources are uncompressed WAV/AIFF, seeking to an arbitrary offset is just pointer arithmetic — not a decode-from-keyframe like a compressed codec — so rendering N clips from one file is N cheap `ffmpeg -ss ... -t 10` calls, not N expensive full decodes.

The real risk here isn't performance, it's **fairness of random selection**: a single dense, hours-long session could produce hundreds of segments while most files produce a handful, which would make that one file dominate the radio even with the "don't repeat the same file back-to-back" rule in §16 (that rule prevents consecutive repeats, not overall weighting). §16 addresses this with a configurable per-file segment cap and file-first selection.

---

# 9. Segment Rules

The detection algorithm should be configurable.

Example defaults:

```text
Analysis window:       200 ms
Minimum segment:       min(3 sec, file duration)  — see below
Maximum segment:       10 sec (compiled clip length cap, see §8a)
Padding before:        1 sec, clamped to not extend before the start of the file
Padding after:         2 sec, clamped to not extend past the end of the file
Silence merge gap:     1–2 sec
Activity threshold:    configurable
```

**Minimum segment length is relative to the source file, not a fixed floor.** Real source material in this archive ranges from roughly 500ms one-shots up to a few minutes — a hard 3-second minimum would reject an entire category of legitimate material: short drum hits/samples and other one-shot recordings that are fully active for their whole (short) duration. Instead, the minimum is the smaller of the default (3 sec) and the file's own duration, so a fully-active 500ms file still produces one short segment rather than zero. The near-zero-size and zero-active-window guards (§7) already exclude genuinely empty files, so this doesn't reopen the "silent junk gets indexed" problem — it only affects files that are short *and* actually contain audio.

Padding is clamped the same way, for the same reason: 1s-before/2s-after padding trivially overruns a 500ms file's actual boundaries otherwise.

These values are starting points, not fixed requirements.

The user should eventually be able to tune how aggressively the system skips quiet material.

---

# 10. Musical Context

For v1, "active" simply means "not near-silent" — the system does not attempt to judge whether quiet material is musically important.

This is a known simplification: a quiet drone, texture, or sparse field recording may be just as valuable as something loud, and a naive silence detector will discard it. Solving this properly needs a better signal than absolute RMS (e.g. a threshold relative to each file's own noise floor, or spectral-based activity detection) and is deferred rather than solved in v1.

The v1 system only distinguishes:

- near-silence
- clearly active audio

Future versions may use more sophisticated feature extraction to identify musically interesting-but-quiet regions, closing this gap.

---

# 11. Database

Use SQLite for the initial implementation.

Suggested structure:

### files

```text
id
path
filename
extension
size
modified_time
duration
sample_rate
channels
bit_depth
analysis_version
source            ('local' | 'jottacloud')
cloud_path        (Jottacloud Archive path, if source = 'jottacloud')
synced_at         (when the Jottacloud download was verified, §5a)
indexed_at
created_at
```

`path` is unique and is the identity used to skip already-indexed files on rescan (no hash). `indexed_at` marks a file as fully processed — this is what makes scanning resumable. `cloud_path`/`synced_at` are how the ingestion stage (§5a) and the scanner (§6) share one row per file instead of keeping separate state; `cloud_path` is also what "Open Source" (§14) uses to point back at the true Jottacloud location when it differs from the synced local filename.

### segments

```text
id
file_id
start_time
end_time
duration
mean_rms
peak
rendered_path
rendered_duration
rendered_sample_rate
touched_at        (set the first time Clip Detail is opened, §3.5)
rating            (-1 | 0 | 1 — thumbs down/none/up, per-clip not per-file)
note              (free text, separate from tags)
```

`rendered_path` points at the compiled clip file (§8a) — this is what the player actually streams, never the original. There is no separate `bookmarks` table: Favorites Pad (§3.4) membership is derived directly from `touched_at IS NOT NULL AND rating != -1`, so "remembering" a clip doesn't need its own row — it's the same action as opening Clip Detail.

### tags

```text
id
name
```

### segment_tags

```text
segment_id
tag_id
source            ('user' | 'auto')
```

`source` distinguishes tags you picked via quick-select from any auto-generated tags (e.g. a default year tag, or future auto-tagging — see §8) — kept separate so an improved auto-tagger can be re-run later without touching anything you tagged by hand.

### sessions

```text
id
seed
created_at
```

### session_items

```text
session_id
segment_id
sequence_number
```

The schema should remain simple and easy to migrate.

---

# 12. Web Application

The web app should be accessible from the local network.

Example:

```text
http://audio-radio.local
```

or

```text
http://192.168.x.x:port
```

No external cloud service is required.

---

# 13. Main UI

The main screen should be deliberately minimal.

Possible layout:

```text
┌─────────────────────────────────────┐
│                                     │
│          PERSONAL RADIO             │
│                                     │
│        [ album / artwork ]          │
│                                     │
│     project_2019/session_03.wav     │
│             03:14 → 04:01           │
│                                     │
│          ▶ / ❚❚                     │
│                                     │
│       ♥ Remember      ⏭ Next        │
│                                     │
│             482731                   │
│                                     │
└─────────────────────────────────────┘
```

The interface should feel more like a radio player than a file manager.

---

# 14. Controls

MVP controls:

### Play / Pause

Start or stop the stream.

### Next

Immediately skip to another randomly selected segment.

### Previous

Return to the previous segment.

### Remember

Save the current segment as a bookmark.

### Open Source

Open/show the original file location.

### Seed

Display or optionally set the random seed.

Future controls:

- Rating
- Add note
- Add tag
- "Play more like this"
- "Play from this project"
- "Play only old recordings"
- "Play only favorites"

---

# 15. Remember / Bookmarking

This is a core feature, not an afterthought.

When the user presses **Remember**, the application stores:

- Source file
- Exact start/end position
- Timestamp when bookmarked
- Optional rating
- Optional note

Example:

```text
REMEMBERED

Growth_Maximizer/session_07.wav
12:42 → 13:27

"great unstable bass texture"
```

The original file is never modified.

---

# 16. Randomization

Randomization is **file-first, then segment-within-file** — pick a source file, then pick one of its segments — rather than picking uniformly across the flat pool of all segments. This matters because a single dense recording can legitimately produce hundreds of segments while most files produce a handful (§8a); picking uniformly across all segments would make that one file dominate playback regardless of the back-to-back repeat rule below, since that rule only blocks *consecutive* repeats, not overall weighting.

A configurable per-file segment cap (e.g. "index at most N segments per file") is an additional, simpler backstop against the same problem, usable on its own or combined with file-first selection.

Basic mode:

```text
random(file) → random(segment in file)
```

Better mode:

```text
random weighted by:
    file
    age
    previous listening
    bookmark status
```

The first implementation should avoid repeatedly selecting the same file.

Possible rule:

> Do not select another segment from the same source file until N other files have been played.

This should be configurable.

---

# 17. Seeded Exploration

The random sequence can be generated from a seed.

Example:

```text
Seed: 104928

1. old_session.wav @ 04:12
2. synthjam.aif @ 11:37
3. field_recording.wav @ 00:48
4. project_x.wav @ 07:22
...
```

This creates a reproducible "radio journey."

A future feature could allow sharing/exporting a session as a list of references.

---

# 18. Levels / Playback

The archive will contain recordings with wildly different levels.

Playback is always from the compiled clip (§8a), rendered at reduced fidelity for browsing — never the original file. The MVP preserves the original level from the source when rendering (no normalization), so level differences between recordings will still be audible.

Future versions could optionally apply, at render time or at playback time:

- Loudness normalization
- Peak protection
- Gentle gain matching

Avoid destructive processing on the originals in all cases.

The source archive must remain untouched — the compiled clip is the only artifact ever (re-)generated, and it can always be deleted/re-rendered from the original without any loss.

---

# 19. Artwork / Visual Identity

The application does not need sophisticated artwork generation.

Potential sources:

1. Project artwork if available.
2. Folder artwork.
3. Generated abstract placeholder.
4. Simple typography based on filename/project.

This could eventually connect strongly with the user's Notoms visual identity.

However, artwork should not delay the functional MVP.

---

# 20. Architecture

A simple initial architecture:

```text
Linux Home Server
│
├── /audio-archive          (already-local originals, read-only)
│     ├── project A
│     ├── project B
│     ├── old sessions
│     └── field recordings
│
├── /jottacloud-staging     (synced down from Jottacloud Archive, §5a)
│
├── /clip-cache             (compiled mono clips, generated)
│
├── archive.db              ← the ONLY shared state between scripts
│
├── jotta_sync.py           (§5a: list a year-batch → filter → download → verify)
├── cleanup_batch.py        (§5a: manually delete a batch's staged originals once confirmed)
├── scan.py                 (§6: walk local dirs → metadata)
├── analyze.py              (§7–§9: activity detection → segments)
├── render.py               (§8a: compiled clips)
│
└── web application         (LAN-only, no external exposure in v1)
        │
        └── browser
```

**Separate scripts, one shared database — no side-state.** Each stage above is an independently runnable script rather than one monolith, which matches the pipeline already described in §4/§5a. Each script owns specific columns on shared rows (e.g. `jotta_sync.py` writes `synced_at`, `scan.py` writes `indexed_at`) and reads whatever prior stages wrote — none of them keep a private index/state file of their own. This is deliberate: two scripts each tracking "what's been done" independently is how they end up disagreeing about it. `archive.db` is the only source of truth about pipeline progress.

Potential stack:

### Backend

- Python
- FastAPI
- SQLite
- FFmpeg
- `jotta-cli` (Jottacloud Archive ingestion, §5a)
- NumPy / SciPy
- Optional librosa

### Frontend

Start simple:

- HTML
- CSS
- JavaScript

Move to Svelte/React only if the UI becomes complex.

---

# 21. Processing Performance

The archive may contain many hundreds of GB. A full scan may realistically take a long time (hours, possibly longer on network-attached storage) — this is accepted for v1, not optimized away, because the scan is resumable (§6, §2a): it can be paused and continued across multiple sessions without redoing finished work.

Therefore analysis must be designed as a batch process.

Requirements:

- Process files sequentially or with controlled parallelism.
- Never load an entire large WAV into memory unnecessarily.
- Analyze downsampled audio where possible.
- Store results incrementally.
- Resume after interruption.
- Log failures.
- Allow the scanner to continue if one file is corrupt.

Example:

```text
Scanning archive...

[██████████████████░░] 87%

Files:       8,421
Processed:   7,330
Segments:    31,842
Errors:      3

Estimated remaining: 18 min
```

---

# 22. Incremental Updates

The scanner should support:

```text
scan
```

and:

```text
scan --incremental
```

Incremental mode walks the archive and skips any file whose path already exists in the database (§2a, §6) — it does not detect in-place edits to already-indexed files.

New recordings should automatically become available to the radio after analysis.

---

# 23. Archive Cleanup

Before indexing, the user may want to remove unnecessary files.

This should preferably happen outside the application initially.

The archive should distinguish between:

- Original project files
- Rendered mixes
- Temporary files
- Samples
- Takes
- Exported masters

The scanner should not delete anything.

A future analysis report could identify:

- Duplicate files
- Very large files
- Near-identical renders
- Silent files
- Extremely short files

But deletion should remain a deliberate user action.

---

# 24. Future: Similarity Search

Once feature vectors are stored, the archive can support:

> Find something like this.

For the current segment, compare its audio features against other indexed segments.

Potential implementation:

- MFCC embeddings
- Spectral features
- Audio embeddings
- Vector database or SQLite-compatible vector extension

This is a second-generation feature.

Do not make it a prerequisite for the first version.

---

# 25. Future: AI / Semantic Search

Potential later functionality:

```text
"Find weird vocal fragments"

"Find recordings that sound like machines"

"Find slow evolving drones"

"Find things I recorded around 2018"

"Find bass ideas with lots of distortion"
```

This could combine:

- audio embeddings
- filenames
- folder names
- manually added notes
- speech/transcription where relevant

Again: optional future direction.

---

# 26. Future: Creative Modes

Possible listening modes:

### Endless

Pure random exploration.

### Forgotten

Prefer old and rarely played recordings.

### Deep Dive

Stay within one project/session for several segments.

### Contrast

Deliberately alternate very different material.

### Similar

Continue with material acoustically similar to the current clip.

### Album Mining

Focus on recordings associated with one project or era.

### Night Radio

Long-form, slower, quieter material.

---

# 27. Most Important Design Principle

The application should create **productive accidents**.

It should not become another system that requires the user to curate, classify, tag, rename, and organize everything.

The archive is already valuable because it contains the history of experimentation.

The application's job is simply to make that history audible again.

---

# 28. MVP Definition

The first usable version is complete when the following works:

- [x] Point scanner at a directory tree.
- [x] Find WAV and AIFF files recursively, skipping already-indexed paths.
- [x] Extract metadata.
- [x] Detect near-silent vs. active audio regions.
- [x] Render each active segment to a compiled mono clip (§8a).
- [x] Store segments (with compiled clip path) in SQLite.
- [x] Pause and resume a scan without redoing finished files.
- [x] Start a local web server (LAN-only).
- [x] Select a random active segment.
- [x] Play its compiled clip in the browser.
- [x] Automatically select the next segment.
- [x] Skip to another segment with Next.
- [x] Remember/bookmark the current segment — implemented as "touch" (§3.5):
      opening Clip Detail marks it favorited by default, no separate Remember
      button. Equivalent capability, different interaction than originally written here.
- [ ] View remembered segments — not built yet; see STATUS.md's "Favorites Pad UI".
- [~] Link each remembered segment back to its source file — the source
      path is shown as text in the player; not yet a click-to-open action.
- [x] Use a deterministic random seed — seeded server-side (§17); no UI to
      view/set the seed yet (see STATUS.md).

Verified against real data: see STATUS.md for current processed counts and
what's still open.

Everything beyond this is optional.

---

# 29. Proposed Development Order

## Phase 1 — Scanner

Build a command-line tool that can:

```text
scan /audio/archive
```

and produce:

```text
archive.db
```

with file metadata.

## Phase 2 — Activity Detection

Add segment detection and inspect the results.

A CLI report should make it possible to verify:

```text
file.wav
  00:00–00:03 silence
  00:04–00:42 active
  00:43–01:10 silence
  01:11–02:08 active
```

Tune the algorithm before building much UI.

## Phase 3 — Minimal Player

Build the simplest possible web player:

```text
PLAY
NEXT
REMEMBER
```

If this is already fun, the project has succeeded.

## Phase 4 — Endless Radio

Add automatic preselection and playback of the next segment.

## Phase 5 — Archive Browser

Add remembered clips, source information, notes, and basic filtering.

## Phase 6 — Advanced Discovery

Only after the basic system is enjoyable:

- Similarity
- Audio embeddings
- Better classification
- Advanced randomization
- Creative listening modes

---

# 30. Success Criteria

The project is successful if the user can sit down with no specific musical goal, press **Play**, and within 10–20 minutes encounter something from their own archive that makes them think:

> "Wait. That's actually interesting."

The application should turn hundreds of gigabytes of forgotten recordings into a source of new ideas.

It is not primarily an archive-management tool.

It is a **creative instrument for mining the user's own musical history.**
