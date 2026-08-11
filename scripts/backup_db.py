#!/usr/bin/env python3
"""
Safety-net backup for archive.db (DESIGN.md, curation-data incident).

Segment curation (touched_at, rating, note, user tags) is living data now —
it has no other source of truth, so it must survive every reprocess. Run this
before ANY manual operation that touches the segments or files tables
(DELETE, TRUNCATE, a schema migration, a full re-analyze) — not just from a
script, ad-hoc sqlite3 CLI sessions count too.

Uses sqlite3's backup API rather than a plain file copy so it's safe to run
against a live db (WAL-consistent, no "database is locked" races with the
server or pipeline scripts).
"""

import argparse
import sqlite3
import sys
import time
from pathlib import Path

import db

DEFAULT_BACKUP_DIR = Path(__file__).parent.parent / "backups"
DEFAULT_KEEP = 30


def curation_summary(conn):
    row = conn.execute(
        "SELECT COUNT(*) AS total,"
        " SUM(touched_at IS NOT NULL) AS touched,"
        " SUM(rating != 0) AS rated,"
        " SUM(note IS NOT NULL AND note != '') AS noted"
        " FROM segments"
    ).fetchone()
    tags = conn.execute(
        "SELECT COUNT(*) FROM segment_tags WHERE source = 'user'"
    ).fetchone()[0]
    return row, tags


def prune_old_backups(backup_dir, keep):
    backups = sorted(backup_dir.glob("archive_*.db"), key=lambda p: p.stat().st_mtime)
    for stale in backups[:-keep] if keep > 0 else []:
        stale.unlink()
        print(f"  pruned old backup: {stale.name}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=db.DEFAULT_DB_PATH)
    parser.add_argument("--backup-dir", default=DEFAULT_BACKUP_DIR)
    parser.add_argument("--tag", default=None, help="Label appended to the filename, e.g. --tag pre-reanalyze")
    parser.add_argument("--keep", type=int, default=DEFAULT_KEEP, help="Keep only the N most recent backups (0 = keep all)")
    args = parser.parse_args()

    backup_dir = Path(args.backup_dir)
    backup_dir.mkdir(parents=True, exist_ok=True)

    src_path = Path(args.db)
    if not src_path.exists():
        print(f"! no db at {src_path}", file=sys.stderr)
        sys.exit(1)

    stamp = time.strftime("%Y%m%d_%H%M%S")
    suffix = f"_{args.tag}" if args.tag else ""
    dest_path = backup_dir / f"archive_{stamp}{suffix}.db"

    src_conn = sqlite3.connect(src_path)
    src_conn.row_factory = sqlite3.Row
    row, user_tags = curation_summary(src_conn)
    dest_conn = sqlite3.connect(dest_path)
    with dest_conn:
        src_conn.backup(dest_conn)
    dest_conn.close()
    src_conn.close()

    size_mb = dest_path.stat().st_size / (1024 * 1024)
    print(f"Backed up {src_path} -> {dest_path} ({size_mb:.1f} MiB)")
    print(
        f"  curation snapshot: {row['total']} segment(s), "
        f"{row['touched'] or 0} touched, {row['rated'] or 0} rated, "
        f"{row['noted'] or 0} noted, {user_tags} user tag(s)"
    )

    prune_old_backups(backup_dir, args.keep)


if __name__ == "__main__":
    main()
