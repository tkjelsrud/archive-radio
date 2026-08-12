#!/usr/bin/env python3
"""
Reclaim local disk space for a year (or years) that are fully processed
(DESIGN.md, STATUS.md "cleanup_batch.py" item).

Only deletes the LOCAL staged copy of a file, never the DB row: `path` stays
in the files table pointing at a now-missing file, `cloud_path` stays valid,
so a re-fetch is always possible later via refetch_missing.py. Never touches
segments, rendered clips, or curation data.

Safety invariant, per file: only delete if segmented_at IS NOT NULL (activity
detection has actually run) AND every segment for that file already has a
rendered_path (nothing still needs to decode from this source). A file with
zero segments but segmented_at set ("processed, nothing found", §7) also
qualifies — there's nothing left to render from it either way.

This is deliberately conservative: it will silently do nothing for a year
that isn't fully rendered yet rather than guess. Always run backup_db.py
first (this script does so automatically unless --no-backup is passed) —
this exact kind of cleanup caused a real data-loss incident before when a
later reprocess needed to re-decode from a source that had already been
deleted (see STATUS.md).
"""

import argparse
import subprocess
import sys
from pathlib import Path

import db

SCRIPT_DIR = Path(__file__).parent


def find_reclaimable(conn, year):
    rows = conn.execute(
        """
        SELECT f.id, f.path, f.size
        FROM files f
        WHERE f.source = 'jottacloud'
          AND f.cloud_path LIKE ?
          AND f.segmented_at IS NOT NULL
          AND NOT EXISTS (
              SELECT 1 FROM segments s
              WHERE s.file_id = f.id AND s.rendered_path IS NULL
          )
        """,
        (f"%{year}%",),
    ).fetchall()
    return [r for r in rows if Path(r["path"]).exists()]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--year", action="append", required=True, dest="years")
    parser.add_argument("--db", default=db.DEFAULT_DB_PATH)
    parser.add_argument("--dry-run", action="store_true", default=True)
    parser.add_argument("--execute", action="store_false", dest="dry_run")
    parser.add_argument("--no-backup", action="store_true", help="Skip the automatic backup_db.py call (not recommended)")
    args = parser.parse_args()

    if not args.dry_run and not args.no_backup:
        tag = "pre-cleanup-" + "-".join(args.years)
        subprocess.run([sys.executable, str(SCRIPT_DIR / "backup_db.py"), "--db", str(args.db), "--tag", tag], check=True)

    conn = db.connect(args.db)
    total_files = 0
    total_bytes = 0
    for year in args.years:
        candidates = find_reclaimable(conn, year)
        year_bytes = sum(r["size"] or 0 for r in candidates)
        print(f"{year}: {len(candidates)} file(s), {year_bytes / 1e9:.2f} GB reclaimable")
        if not args.dry_run:
            for row in candidates:
                Path(row["path"]).unlink()
        total_files += len(candidates)
        total_bytes += year_bytes

    verb = "Would reclaim" if args.dry_run else "Reclaimed"
    print(f"\n{verb} {total_files} file(s), {total_bytes / 1e9:.2f} GB total.")
    if args.dry_run:
        print("Dry run only — nothing deleted. Re-run with --execute to delete.")


if __name__ == "__main__":
    main()
