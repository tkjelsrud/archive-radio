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
  Web Audio crossfade between clips, waveform drawn per-clip, Clip Detail
  overlay — click the art box to loop/tag/rate/blacklist/**download** the
  current clip, and a global **FX panel** (reverb/delay, toggle + wet + decay
  each) applied to the whole playback chain, not per-clip)
- `/log` — pipeline processing log + recent git commits
- `/status` — real live numbers (processed so far vs. actual full-archive
  totals) — never fabricated/projected figures

**Processed so far:** years 2015-2020 plus the no-year anomalous batch (45
folders). 4,821 files in the DB -> 9,355 segments, 0 pending render. Local
staging for 2014-2017 has been reclaimed (~11GB freed via
`scripts/cleanup_batch.py`, see below) — DB rows/segments/rendered clips are
untouched, only the raw local copy is gone, re-fetchable later via
`cloud_path` if ever needed. 2018-2020 staging is still on disk (not yet
reclaimed).

Two data-loss-adjacent incidents happened getting here — both documented in
"Real bugs" below, both recovered from without losing anything that
mattered: (1) 777 files from 2015-2017 lost their local copy *after* an
unrelated reprocess needed to re-decode from a source that had already been
cleaned up — 749 recovered, 28 are gone from Jottacloud too and will always
show as analyze errors (expected, not a regression); (2) the 2020 batch hit
a completely full disk mid-download, corrupting no data but silently losing
progress in two scripts. **Lesson applied twice now: don't delete local
staging for a year until it's fully done reprocessing, and check `df -h`
before starting a batch bigger than remaining free space.**

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
| analyze.py | `--pad-before` / `--pad-after` | 0.3s / 2.0s | padding around detected regions (pad-before reduced from 1.0s per listening feedback) |
| analyze.py | `--target-coverage` | 0.35 | fraction of a long region's duration covered by its windows (replaced the old fixed seconds-per-clip formula) |
| analyze.py | `--max-clips-per-segment` | 10 | hard ceiling regardless of region length (§16 fairness) |
| analyze.py / render.py | `--workers` | 3 | concurrent ffmpeg subprocesses (conservative on a 4-core box) |
| backup_db.py | `--tag` / `--keep` | none / 30 | label a backup, prune to the N most recent (§ below) |

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
- **Data-loss incident**: an ad-hoc `DELETE FROM segments` run directly via
  the `sqlite3` CLI (no backup) during a schema/reprocess pass destroyed
  real curation data (`touched_at`/`rating`/`note`) for a handful of
  segments. Nothing recovered these — they're gone. This is why
  `scripts/backup_db.py` exists now; see the section below.
- **777-file 2015-2017 total data loss**: an earlier disk-cleanup pass
  (§ above) deleted local staging for 2015-2017 right after first render.
  A later reprocess (triggered by the pad/coverage/normalization fixes)
  reset `segmented_at` for everything and needed to re-decode from that
  now-missing source — all 777 files failed with an ffmpeg "No such file
  or directory" error. Fixed by `scripts/refetch_missing.py` (re-downloads
  from Jottacloud back to each row's original local `path`, using
  `cloud_path` which was never touched) — recovered 749/777; the remaining
  28 are gone from Jottacloud too, permanently unrecoverable.
- **`scan.py --dir` is a required argument** — chaining
  `scan.py && analyze.py && render.py` blindly without it fails scan.py
  immediately with a usage error, but analyze.py/render.py still run
  "successfully" against whatever was already scanned, silently skipping
  every newly-downloaded file for that batch. Always check a chained
  script's actual output, not just its exit code / "done" marker.
- **Disk-full mid-batch, twice in a row**: none of the pipeline scripts
  check free disk space before starting. A batch (~21.5GB) landed on a
  volume that was already mostly full from years of local staging that had
  never been cleaned up (only 2015-2017 had ever been reclaimed). Downloads
  started failing partway through with cryptic `{'Files': N, 'Bytes': N}`
  errors from `jotta-cli`, and — worse — once the disk hit 0 bytes free,
  `scan.py`'s buffered stdout silently lost its entire run's output on the
  final flush (no traceback, no partial log), and separately `analyze.py`
  crashed with a raw `OSError: No space left on device` from inside its own
  error-printing code (writing the log line itself failed). Neither failure
  mode is obvious from the log alone — the only reliable tell was `files`
  rows with `indexed_at`/`segmented_at` still NULL that should've been set,
  plus `df -h` actually showing 0 available. DB integrity was unaffected
  both times (SQLite's transactional writes just failed clean) — this was
  a lost-progress problem, not a corruption problem. Recovery: freed space
  by reclaiming already-fully-rendered older years via the new
  `scripts/cleanup_batch.py` (see below), then simply re-ran
  `jotta_sync.py --year Y --execute` — files that failed to download never
  got a DB row in the first place, so a plain re-run retried exactly the
  missing ones and skipped everything already synced. **Lesson: check `df
  -h` before starting any batch bigger than remaining free space** — these
  scripts have no built-in guard for this.

## Curation-data safety net

`scripts/backup_db.py` — full SQLite backup via the `sqlite3.Connection.backup()`
API (WAL-safe, no locking races with the live server or pipeline scripts).
Writes to `backups/archive_<timestamp>[_<tag>].db`, prints a curation-data
summary (segments touched/rated/noted, user tag count) so it's obvious the
backup actually captured something, and prunes to the `--keep` most recent
(default 30).

**Rule going forward: run this before any manual operation that touches the
`segments` or `files` tables** — a `DELETE`/`TRUNCATE` via the `sqlite3` CLI,
a schema migration, a full re-analyze that resets `segmented_at`. This is a
direct response to the two incidents above; it does not run automatically
(nothing currently *triggers* a destructive op on its own), so it only helps
if it's actually run first.

## Deliberately not done yet

- **Favorites Pad UI** (§3.4) — touching a clip already marks it
  favorited server-side (`touched_at` + not rating=-1), but there's no
  screen to browse/trigger touched clips yet.
- **Curated Radio mode** (§3.6/§26) — random selection restricted to
  touched-and-not-excluded clips only.
- **"Open Source" as a real action** — the source path is displayed as
  text in the player, but there's no click-to-open/reveal action.
- **Seed display/set in the UI** — sessions are seeded and reproducible
  server-side (§17), but the player doesn't show or let you type a seed.
- ~~`cleanup_batch.py` as an actual script~~ — done: `scripts/cleanup_batch.py
  --year Y [--year Y2 ...] --execute`. Calls `backup_db.py` automatically
  first, only deletes a file's local copy if `segmented_at` is set AND
  every one of its segments already has a `rendered_path` (never touches
  DB rows, `cloud_path` stays valid for a later re-fetch). Still no
  automatic "don't run against a year mid-reprocess" guard — that's a
  judgment call left to whoever runs it.
- **`ANALYSIS_VERSION` in `analyze.py` is defined but never actually
  checked** — resumability there is purely `segmented_at IS NULL`. Bumping
  the constant currently does nothing; `scan.py`'s equivalent is wired up
  correctly and could be used as the template if this needs fixing.
- Auto-tagging (BPM/tempo/key, bass-heaviness, transient sharpness) —
  discussed and explicitly deferred, low confidence without real research
  time (see conversation history / DESIGN.md §8 discussion).

## Next batch candidates

- **2021-2026** — everything through 2020 (plus the no-year batch) is done;
  `jotta_sync.py --year YYYY --execute` per the usual flow, remembering
  `scan.py --dir jottacloud-staging` needs its `--dir` flag explicitly, and
  checking `df -h` first — 2020 alone was ~21.5GB and ran the disk to 0
  (see "Real bugs"). Reclaim an older fully-rendered year with
  `cleanup_batch.py` first if there isn't clearly enough headroom.
- The no-year batch's 45 folders are now handled: synced via `--no-year`,
  and the "Old samples" stock-sample folder was truncated (4,430 non-`P-*`
  files deleted from DB+disk, real copyrighted/stock content) while `P-*`
  subfolders (personal MPC1000 CF-card projects, mixed in with stock kicks/
  snares) were deliberately left alone — extracting the personal bits from
  those is still an open question, not started.
- **4,247 `.aif` files (34.08 GiB) + 1 `.aiff`** exist archive-wide — already
  supported by the extension filter; 2015-2019 turned out to include some
  (via `Samples/Recorded` subfolders), but the bulk is still ahead. Ableton
  (which defaults to AIFF on Mac) usage looks concentrated in 2023+ based on
  "Ableton Project Info" folders seen during the full-tree scan.
