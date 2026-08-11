#!/usr/bin/env python3
"""
Jottacloud Archive ingestion (DESIGN.md §5a).

Lists project folders under Archive/Musikkprosjekter, batches them by a
leading-4-digit year prefix, recurses into each matching folder to find
WAV/AIFF files above a minimum size, and (unless --dry-run) downloads
them into a local staging directory via `jotta-cli download`.

Read-only against Jottacloud except for the `download` call itself:
never calls archive/sync/add or anything else that mutates the cloud side.
"""

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

ARCHIVE_ROOT = "Archive/Musikkprosjekter"
PLAUSIBLE_YEAR_RANGE = (2000, 2030)
AUDIO_EXTENSIONS = {".wav", ".aif", ".aiff"}
DEFAULT_MIN_SIZE = 100_000  # bytes; §5a default, tunable

YEAR_PREFIX_RE = re.compile(r"^(\d{4})")


def jotta_ls(path):
    """Run `jotta-cli ls <path> --json` and return the parsed dict.

    Raises RuntimeError with stderr on failure (e.g. path not found) so
    callers can decide whether to skip or abort.
    """
    result = subprocess.run(
        ["jotta-cli", "ls", path, "--json"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"jotta-cli ls '{path}' failed: {result.stderr.strip()}")
    if not result.stdout.strip():
        return {}
    return json.loads(result.stdout)


def extract_year(folder_name):
    """Return a plausible 4-digit year prefix from a folder name, or None.

    Only the leading digits matter (§5a) — everything after is free text
    in this archive and deliberately not parsed. A match outside
    PLAUSIBLE_YEAR_RANGE (e.g. a typo like "2926-6 Camera Angle") is
    treated as no match, not a bogus batch.
    """
    match = YEAR_PREFIX_RE.match(folder_name)
    if not match:
        return None
    year = int(match.group(1))
    if PLAUSIBLE_YEAR_RANGE[0] <= year <= PLAUSIBLE_YEAR_RANGE[1]:
        return year
    return None


def is_audio_file(name, size, min_size):
    """Strict trailing-suffix match only — never a substring/contains check.

    Guards specifically against files like "take.wav.reapeaks" (Reaper's
    waveform-cache sidecar), which contain ".wav" but are not audio.
    """
    suffix = Path(name).suffix.lower()
    return suffix in AUDIO_EXTENSIONS and size >= min_size


def walk_project_folder(path, depth=0, max_depth=6):
    """Recursively yield every file entry under `path` (Jottacloud path).

    WAVs may be at any depth (e.g. under a "media" subfolder) — there is
    no fixed layout in this archive, so this always recurses rather than
    assuming a fixed structure. max_depth is a sanity backstop, not an
    expected limit.
    """
    if depth > max_depth:
        print(f"  ! max recursion depth hit at {path!r}, stopping here", file=sys.stderr)
        return

    try:
        listing = jotta_ls(path)
    except RuntimeError as exc:
        print(f"  ! could not list {path!r}: {exc}", file=sys.stderr)
        return

    for f in listing.get("Files", []):
        yield f

    for folder in listing.get("Folders", []):
        yield from walk_project_folder(folder["Path"], depth=depth + 1, max_depth=max_depth)


def find_batch_folders(target_years):
    """List Archive/Musikkprosjekter and split into three groups.

    matched: folders whose leading year is in target_years.
    other_year: folders with a plausible year, just not one we asked for
      this run — normal, not an anomaly, so callers should only count
      these, not print each one every run.
    anomalous: folders with no year prefix at all, or an implausible one
      (e.g. a typo like "2926-6 Camera Angle") — these are the real
      "unmatched, reported" cases from §5a and should be shown in full so
      nothing silently falls through the pattern.
    """
    listing = jotta_ls(ARCHIVE_ROOT)
    matched, other_year, anomalous = [], [], []
    for folder in listing.get("Folders", []):
        year = extract_year(folder["Name"])
        if year is None:
            anomalous.append(folder)
        elif year in target_years:
            matched.append(folder)
        else:
            other_year.append(folder)
    return matched, other_year, anomalous


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--year", type=int, action="append", required=True, dest="years",
        help="Year to include in this batch. Repeatable, e.g. --year 2014 --year 2015.",
    )
    parser.add_argument(
        "--min-size", type=int, default=DEFAULT_MIN_SIZE,
        help=f"Minimum file size in bytes (default {DEFAULT_MIN_SIZE}).",
    )
    parser.add_argument(
        "--dry-run", action="store_true", default=True,
        help="List/filter only, download nothing. This is the default.",
    )
    parser.add_argument(
        "--execute", dest="dry_run", action="store_false",
        help="Actually download matching files (overrides --dry-run).",
    )
    parser.add_argument(
        "--dest", default="./jottacloud-staging",
        help="Local staging directory for downloads (only used with --execute).",
    )
    args = parser.parse_args()

    target_years = set(args.years)
    print(f"Batch years: {sorted(target_years)}")
    print(f"Min size: {args.min_size:,} bytes")
    print(f"Mode: {'DRY RUN (listing only)' if args.dry_run else 'EXECUTE (will download)'}")
    print()

    matched_folders, other_year_folders, anomalous_folders = find_batch_folders(target_years)

    print(f"Matched {len(matched_folders)} project folder(s) for {sorted(target_years)}:")
    for f in matched_folders:
        print(f"  - {f['Name']}")
    print()
    print(f"{len(other_year_folders)} folder(s) belong to other years — not an anomaly, just not this batch.")
    print()
    print(f"{len(anomalous_folders)} folder(s) have no plausible year prefix at all "
          f"(no year, or outside {PLAUSIBLE_YEAR_RANGE}) — these need manual handling later:")
    for f in anomalous_folders:
        print(f"  ? {f['Name']}")
    print()

    total_files = 0
    total_bytes = 0
    to_download = []

    for folder in matched_folders:
        folder_files = 0
        folder_bytes = 0
        for entry in walk_project_folder(folder["Path"]):
            if is_audio_file(entry["Name"], entry["Size"], args.min_size):
                folder_files += 1
                folder_bytes += entry["Size"]
                to_download.append(entry)
        total_files += folder_files
        total_bytes += folder_bytes
        note = "" if folder_files else "  (no matching audio found)"
        print(f"  {folder['Name']}: {folder_files} file(s), {folder_bytes:,} bytes{note}")

    print()
    print(f"TOTAL: {total_files} file(s), {total_bytes:,} bytes ({total_bytes / 1_048_576:.1f} MiB)")

    if args.dry_run:
        print("\nDry run only — nothing downloaded. Re-run with --execute to download.")
        return

    dest = Path(args.dest)
    dest.mkdir(parents=True, exist_ok=True)
    print(f"\nDownloading {len(to_download)} file(s) to {dest}/ ...")
    for entry in to_download:
        print(f"  -> {entry['Path']}")
        result = subprocess.run(
            ["jotta-cli", "download", "--merge", entry["Path"], str(dest)],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            print(f"     ! download failed: {result.stderr.strip()}", file=sys.stderr)


if __name__ == "__main__":
    main()
