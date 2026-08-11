# Audio Archive Radio

A locally-hosted radio for exploring a large personal archive of unfinished
music projects, jams, and recordings. Design/spec: [DESIGN.md](DESIGN.md).

Runs on `ost.local` (Ubuntu, LAN-only). Source material is ingested from a
Jottacloud Archive folder in year-batches (§5a) via the official `jotta-cli`,
read-only.

## Status

Early build. Currently working: `scripts/jotta_sync.py` (list/filter/download
a year-batch, dry-run by default).
