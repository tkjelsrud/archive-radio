# Status — 2026-08-11

Living document. `DESIGN.md` is the original spec/reasoning; this is "what's
actually built, what it looks like operationally, and what's left." Update
this as work continues rather than trying to keep it perfectly in sync with
every commit — it's a resume point, not a changelog (see `/log` on the
running server, and `git log`, for that).

## What's working right now

Full pipeline, verified end to end against real data:

```
jotta_sync.py --year Y --execute   (Jottacloud Archive -> local staging, §5a)
       |
scan.py                            (metadata + content_kind/source_tag)
       |
analyze.py                         (activity detection -> segments, §7-9)
       |
render.py                          (segments -> compiled clips, §8a)
       |
server.py                          (FastAPI web player)
```

Web player is live at `http://ost.local:8420/`:

- `/` — the radio (play/pause, skip back/ahead, keyboard shortcuts space+arrows,
  Web Audio crossfade between clips, Clip Detail overlay — click the art
  box to loop/tag/rate the current clip)
- `/log` — pipeline processing log + recent git commits
- `/status` — real live numbers (processed so far vs. actual full-archive
  totals) — never fabricated/projected figures

**Processed so far:** years 2015, 2016, 2017. 777 source files -> 1152
segments -> 1152 rendered clips. Local staging for these years has been
**deleted** (source WAVs reclaimed, ~10.5GB freed) — DB rows, segments, and
rendered clips are untouched and fully playable; only the raw local copy is
gone. Re-fetching from Jottacloud is possible later if ever needed, but
that's not the expectation — the goal is to not re-download processed years.

**Full archive** (measured via `scripts/archive_stats.py`, cached in
`logs/archive_totals.json`): 37,891 files / 243.02 GiB total, of which
14,872 files / 111.87 GiB actually match the WAV/AIFF filter. So roughly
13.7 GiB done, ~98 GiB of matching audio still unprocessed across other years.

## Operational details

- **Server:** `ost.local`, Ubuntu 24.04, 4 cores, 7.6GB RAM, `/home` has
  ~51GB free (the real disk constraint driving the whole batch-then-cleanup
  workflow — the full archive is >2x that).
- **Deploy:** NOT git-pull based. Deployed by `rsync` from the local Mac dev
  checkout (`~/Dev/audio-archive-radio`) straight to `~/audio-radio/` on the
  server, e.g.:
  ```
  rsync -az --exclude='.git' --exclude='jottacloud-staging' --exclude='clip-cache' \
    --exclude='*.db' --exclude='.venv' --exclude='logs' \
    /Users/Thomas.Kjelsrud/Dev/audio-archive-radio/ ost.local:~/audio-radio/
  ```
  Then `systemctl --user restart audio-radio` to pick up server.py changes
  (static file changes like player.js/style.css are picked up on next
  request — no-cache headers are already set on those routes).
- **Repo:** `git@github.com:tkjelsrud/archive-radio.git`.
- **Python:** a `.venv` on the server (`~/audio-radio/.venv`) has
  `fastapi`/`uvicorn` — Ubuntu 24.04 blocks bare `pip install` into system
  Python (PEP 668), hence the venv.
- **systemd (user-level, `~/.config/systemd/user/`):**
  - `audio-radio.service` — the web player. `Restart=on-failure`. **Do not
    add `User=` to a user-level unit** — it always runs as the logged-in
    user already, and setting it causes a cryptic exit 216/GROUP failure.
  - `jottad.service` — the Jottacloud client daemon `jotta-cli` talks to.
    Must be running for any `jotta-cli` command to work at all.
  - `loginctl enable-linger tkjelsrud` is set, so both survive reboots/logout.
- **Jottacloud auth:** `jotta-cli login`, personal token pasted directly into
  the terminal (never through this session — tokens are single-use/short-lived
  and shouldn't touch a chat log or a file). Already done; nothing to redo
  unless it expires.

## Config knobs (all CLI flags, see each script's `--help`)

| Script | Flag | Default | What it does |
|---|---|---|---|
| jotta_sync.py | `--min-size` | 100,000 bytes | pre-download size filter |
| analyze.py | `--threshold` | 0.01 RMS (~-40dB) | activity threshold |
| analyze.py | `--merge-gap` | 1.5s | merge active runs closer than this |
| analyze.py | `--pad-before` / `--pad-after` | 1.0s / 2.0s | padding around detected regions |
| analyze.py | `--seconds-per-clip` | 60s | ~1 clip per this many seconds of a long region |
| analyze.py | `--max-clips-per-segment` | 10 | hard ceiling regardless of region length (§16 fairness) |
| analyze.py / render.py | `--workers` | 3 | concurrent ffmpeg subprocesses (conservative on a 4-core box) |

## Real bugs found running this against actual data (worth knowing, not rediscovering)

- **`jotta-cli ls --json` omits the `Size` field entirely for zero-byte
  files** (Go's `omitempty`) instead of reporting `0`. Fixed via
  `entry.get("Size", 0)`.
- **`jotta-cli ls --json` prints the literal text `nothing found`** instead
  of valid JSON for a genuinely empty folder (e.g. "Ableton Project Info"
  placeholder dirs) — not an error, just needs handling as `{}`.
- **`jotta-cli download` returns as soon as a transfer is *queued*, not when
  it finishes.** Checking for the file immediately after the subprocess
  call races the real transfer. Fixed by queuing everything, then polling
  `jotta-cli list downloads --json` until each one's `CompletedTimeMs` is
  set — and even then, allow a few hundred ms of retry, since the file can
  lag slightly behind that timestamp being set.
- **Local staging filenames are deterministic (hash of cloud path)**, so a
  past run's `jotta-cli` download-history entry has the exact same
  Remote+Local pair as a fresh run — clear history (`download --clear=all`,
  harmless, doesn't touch files) at the start of each sync run or polling
  can match a stale completed entry instead of waiting for the real one.
- **The plain `sqlite3` CLI does not enable foreign-key enforcement by
  default** (unlike `db.py`'s connections, which set `PRAGMA foreign_keys =
  ON`). Running `DELETE FROM segments` directly via `sqlite3` silently does
  *not* cascade to `segment_tags` — leaves orphaned rows. Either enable the
  pragma explicitly in the CLI session or clean up orphans manually
  (`DELETE FROM segment_tags WHERE segment_id NOT IN (SELECT id FROM segments)`).
- **A stuck background monitor loop**: `until ! pgrep -f 'jotta_sync.py
  --year 2016' ...` can match its own command line (the pattern string
  appears in the loop's own invocation), looping forever even after the
  real process exits. Use a more specific pattern or check by PID instead.
- Browser cache served a stale `player.js` after a rewrite (it referenced a
  removed `<audio>` element) — added `Cache-Control: no-cache` on
  `/`, `/style.css`, `/player.js` since these get iterated on a lot.

## Deliberately not done yet

- **Favorites Pad UI** (§3.4) — touching a clip already marks it
  favorited server-side (`touched_at` + not rating=-1), but there's no
  screen to browse/trigger touched clips yet.
- **Waveform display** — canvas element is scaffolded in `index.html`
  (`#waveform`), CSS positions it correctly, but the actual drawing logic
  (decode `AudioBuffer.getChannelData()`, downsample to min/max per pixel
  column) isn't written. Should be cheap since the buffer's already
  decoded client-side for playback.
- **Effects/ambient mode** (§3.6) — reverb/delay via Web Audio, client-side
  only, not started.
- **Curated Radio mode** (§3.6/§26) — random selection restricted to
  touched-and-not-excluded clips only.
- **"Open Source" as a real action** — the source path is displayed as
  text in the player, but there's no click-to-open/reveal action.
- **Seed display/set in the UI** — sessions are seeded and reproducible
  server-side (§17), but the player doesn't show or let you type a seed.
- **`cleanup_batch.py` as an actual script** — the 2015-2017 staging
  cleanup was done manually (SQL query + `rm`) this session. The logic is
  simple enough to script now that it's been done once by hand: given a
  year (or an explicit path list), delete local files whose `segments` are
  all rendered, leave DB rows alone.
- **`ANALYSIS_VERSION` in `analyze.py` is defined but never actually
  checked** — resumability there is purely `segmented_at IS NULL`. Bumping
  the constant currently does nothing; `scan.py`'s equivalent is wired up
  correctly and could be used as the template if this needs fixing.
- Auto-tagging (BPM/tempo/key, bass-heaviness, transient sharpness) —
  discussed and explicitly deferred, low confidence without real research
  time (see conversation history / DESIGN.md §8 discussion).
- Remote access — discussed; recommendation was a VPN (e.g. Tailscale) back
  into the LAN rather than port-forwarding 8420 directly, since there's
  currently zero auth on the API. Not set up.

## Next batch candidates

- Any year not yet processed (everything except 2015-2017) — `jotta_sync.py
  --year YYYY --execute` per the usual flow. 2018-2026 all still fully
  untouched in the cloud archive.
- **4,247 `.aif` files (34.08 GiB) + 1 `.aiff`** exist archive-wide — already
  supported by the extension filter, just not yet encountered since
  2015-2017 happened to be all-WAV. Ableton (which defaults to AIFF on Mac)
  usage looks concentrated in 2023+ based on "Ableton Project Info" folders
  seen during the full-tree scan.
- **45 top-level folders have no usable year prefix** (numbered project
  folders like `000 Drones`, sample libraries like `Found Samples`/`Old
  samples`/`Zebra 2 Patches`, a few typos like `2926-6 Camera Angle`) — these
  are reported by `jotta_sync.py` every run but need manual handling
  (renaming in Jottacloud, or a dedicated non-year batch mode) since the
  batching logic is purely year-prefix-based.
