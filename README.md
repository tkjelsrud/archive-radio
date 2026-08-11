# Audio Archive Radio

A locally-hosted radio for exploring a large personal archive of unfinished
music projects, jams, and recordings — turning years of accumulated
recordings into a source of forgotten musical ideas instead of a pile of
files nobody browses.

- **Design / reasoning:** [DESIGN.md](DESIGN.md) — the full spec, including
  the feasibility discussion behind each decision.
- **Current state / how to run it / known issues:** [STATUS.md](STATUS.md)
  — start here if you're picking this project back up.

Runs on `ost.local` (Ubuntu, LAN-only) at `http://ost.local:8420/`. Source
material is ingested from a Jottacloud Archive folder in year-batches (§5a)
via the official `jotta-cli`, read-only.

## Pipeline

```
scripts/jotta_sync.py --year YYYY --execute   # Jottacloud -> local staging
scripts/scan.py                               # metadata extraction
scripts/analyze.py                            # activity detection -> segments
scripts/render.py                             # segments -> compiled clips
scripts/server.py                             # the web player
```

Each stage is resumable and safe to re-run — see `STATUS.md` for the exact
deploy/run commands currently in use.
