#!/usr/bin/env python3
"""
Full-archive size estimate (read-only, no downloads).

Walks the entire Archive/Musikkprosjekter tree via jotta-cli ls, same
recursive logic as jotta_sync.py's per-batch walk, and writes totals to
logs/archive_totals.json for the /status page to display. This is a full
inventory of everything, not just years already batched — meant to be
re-run occasionally (it's a few hundred `ls` calls, takes a few minutes),
not on every page load.
"""

import argparse
import json
import time
from pathlib import Path

from jotta_sync import jotta_ls, walk_project_folder, is_audio_file, ARCHIVE_ROOT, DEFAULT_MIN_SIZE

DEFAULT_OUTPUT = Path(__file__).parent.parent / "logs" / "archive_totals.json"


def compute_totals(min_size=DEFAULT_MIN_SIZE):
    folders = jotta_ls(ARCHIVE_ROOT).get("Folders", [])
    total_all_bytes = total_all_files = 0
    total_audio_bytes = total_audio_files = 0

    for folder in folders:
        for entry in walk_project_folder(folder["Path"]):
            size = entry.get("Size", 0)
            total_all_bytes += size
            total_all_files += 1
            if is_audio_file(entry["Name"], size, min_size):
                total_audio_bytes += size
                total_audio_files += 1

    return {
        "scanned_at": int(time.time()),
        "total_files": total_all_files,
        "total_bytes": total_all_bytes,
        "audio_files": total_audio_files,
        "audio_bytes": total_audio_bytes,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    args = parser.parse_args()

    print("Walking the full Archive/Musikkprosjekter tree (read-only, no downloads)...")
    totals = compute_totals()

    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(totals, indent=2))

    print(f"Total: {totals['total_files']:,} files, {totals['total_bytes']/1_073_741_824:.2f} GiB")
    print(f"Audio (matching filter): {totals['audio_files']:,} files, "
          f"{totals['audio_bytes']/1_073_741_824:.2f} GiB")
    print(f"Written to {out_path}")


if __name__ == "__main__":
    main()
