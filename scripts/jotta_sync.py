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
import hashlib
import json
import subprocess
import sys
import time
from pathlib import Path

import db
from archive_util import extract_year, PLAUSIBLE_YEAR_RANGE

ARCHIVE_ROOT = "Archive/Musikkprosjekter"
AUDIO_EXTENSIONS = {".wav", ".aif", ".aiff"}
DEFAULT_MIN_SIZE = 100_000  # bytes; §5a default, tunable


def local_filename_for(cloud_path):
    """Deterministic, collision-free local filename for a cloud file.

    Local staging deliberately does NOT mirror the archive's project/folder
    structure — it's flat, named from a hash of the cloud path. Two
    different cloud files can't collide, and re-syncing the same cloud
    file always maps to the same local name (idempotent). The real
    project/directory/filename is preserved as metadata (files.cloud_path,
    files.filename), not encoded in the local path.
    """
    digest = hashlib.sha1(cloud_path.encode("utf-8")).hexdigest()[:16]
    suffix = Path(cloud_path).suffix.lower()
    return f"{digest}{suffix}"


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
        "--dest", default=str(Path(__file__).parent.parent / "jottacloud-staging"),
        help="Local staging directory for downloads (only used with --execute).",
    )
    parser.add_argument(
        "--db", default=str(db.DEFAULT_DB_PATH),
        help="Path to archive.db.",
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

    conn = db.init_db(args.db)
    already_synced = {
        row["cloud_path"]
        for row in conn.execute("SELECT cloud_path FROM files WHERE source = 'jottacloud'")
    }

    total_files = 0
    total_bytes = 0
    skipped_already_synced = 0
    to_download = []

    for folder in matched_folders:
        folder_files = 0
        folder_bytes = 0
        for entry in walk_project_folder(folder["Path"]):
            # jotta-cli omits "Size" entirely for zero-byte files (Go's
            # omitempty), rather than reporting 0 — .get() naturally makes
            # such a file fail the min-size check below, no special case needed.
            size = entry.get("Size", 0)
            if not is_audio_file(entry["Name"], size, args.min_size):
                continue
            if entry["Path"] in already_synced:
                skipped_already_synced += 1
                continue
            folder_files += 1
            folder_bytes += size
            to_download.append(entry)
        total_files += folder_files
        total_bytes += folder_bytes
        note = "" if folder_files else "  (no new matching audio)"
        print(f"  {folder['Name']}: {folder_files} file(s), {folder_bytes:,} bytes{note}")

    print()
    print(f"TOTAL: {total_files} new file(s), {total_bytes:,} bytes ({total_bytes / 1_048_576:.1f} MiB)"
          f" — {skipped_already_synced} already synced, skipped")

    if args.dry_run:
        print("\nDry run only — nothing downloaded. Re-run with --execute to download.")
        return

    dest_root = Path(args.dest)
    dest_root.mkdir(parents=True, exist_ok=True)

    # Local staging filenames are deterministic (hash of cloud path), so a
    # past run's history entries have the exact same Remote+Local pair as
    # this run's. Clear jotta-cli's download history first so the polling
    # below can't match a stale completed entry from an earlier run instead
    # of waiting for this run's actual transfer. This only clears jotta-cli's
    # own bookkeeping log, not any downloaded files (per `download --help`).
    subprocess.run(["jotta-cli", "download", "--clear=all"], capture_output=True, text=True)

    # Phase 1: queue every download. `jotta-cli download` returns as soon as
    # the transfer is QUEUED, not when it's finished — the file may not
    # exist yet when this call returns. Don't touch the filesystem yet.
    print(f"\nQueuing {len(to_download)} download(s) into {dest_root}/ (flat, hashed names) ...")
    pending = {}  # cloud path -> (tmp_dir, entry)
    for entry in to_download:
        local_name = local_filename_for(entry["Path"])
        tmp_dir = dest_root / ".incoming" / local_name
        tmp_dir.mkdir(parents=True, exist_ok=True)

        result = subprocess.run(
            ["jotta-cli", "download", "--merge", entry["Path"], str(tmp_dir)],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            print(f"  ! queuing failed for {entry['Path']}: {result.stderr.strip()}", file=sys.stderr)
            continue
        pending[entry["Path"]] = (tmp_dir, entry)

    # Phase 2: poll until every queued download has actually finished.
    print(f"Waiting for {len(pending)} download(s) to finish ...")
    deadline = time.monotonic() + 600  # 10 min backstop, not an expected duration
    while pending and time.monotonic() < deadline:
        result = subprocess.run(
            ["jotta-cli", "list", "downloads", "--json"],
            capture_output=True, text=True,
        )
        statuses = {d["Remote"]: d for d in json.loads(result.stdout or "[]")}
        for remote in list(pending):
            status = statuses.get(remote)
            if status and status.get("CompletedTimeMs"):
                pending_status = "error" if status.get("Errors") else "done"
                tmp_dir, entry = pending.pop(remote)
                if pending_status == "error":
                    print(f"  ! download error for {remote}: {status['Errors']}", file=sys.stderr)
                    continue
                finalize_download(conn, dest_root, tmp_dir, entry)
        if pending:
            time.sleep(2)

    if pending:
        print(f"  ! {len(pending)} download(s) still not finished after 10 minutes, giving up on them:",
              file=sys.stderr)
        for remote in pending:
            print(f"    - {remote}", file=sys.stderr)


def finalize_download(conn, dest_root, tmp_dir, entry):
    """Move a finished download from its temp dir to its flat hashed
    final path, and record it in the files table. Called only once
    jotta-cli reports the transfer as actually complete.
    """
    local_name = local_filename_for(entry["Path"])
    local_path = dest_root / local_name
    downloaded = tmp_dir / entry["Name"]

    # jottad can report CompletedTimeMs a moment before the file is fully
    # visible via stat() — a small race, not a real failure. Give it a
    # few seconds before treating it as an actual problem.
    for _ in range(10):
        if downloaded.exists():
            break
        time.sleep(0.5)
    else:
        print(f"  ! {downloaded} still not found 5s after jotta-cli reported it finished", file=sys.stderr)
        return

    downloaded.rename(local_path)
    tmp_dir.rmdir()

    conn.execute(
        """
        INSERT OR IGNORE INTO files
            (path, filename, extension, size, modified_time, source, cloud_path, synced_at)
        VALUES (?, ?, ?, ?, ?, 'jottacloud', ?, ?)
        """,
        (
            str(local_path.resolve()),
            entry["Name"],
            Path(entry["Name"]).suffix.lower(),
            entry.get("Size", 0),
            entry["Modified"] // 1000,  # ms -> s
            entry["Path"],
            int(time.time()),
        ),
    )
    conn.commit()
    print(f"  OK {entry['Path']}")


if __name__ == "__main__":
    main()
