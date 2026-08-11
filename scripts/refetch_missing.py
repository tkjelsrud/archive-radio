#!/usr/bin/env python3
"""
Recovery tool: re-fetch Jottacloud-sourced files whose local staging copy
was deleted (e.g. by a manual cleanup pass, §5a) but are needed again —
e.g. a later reprocess reset segmented_at and needs to re-decode from
source, which no longer exists locally.

Does NOT touch the files table's existing metadata (size, cloud_path,
etc.) — only re-downloads the bytes back to the same local `path` each
row already has, so nothing downstream needs to change. Read-only
against Jottacloud except the download itself, same as jotta_sync.py.
"""

import json
import subprocess
import sys
import time
from pathlib import Path

import db
from jotta_sync import local_filename_for  # noqa: F401 (kept for reference, path already fixed)


def find_missing(conn):
    rows = conn.execute(
        "SELECT id, path, cloud_path, filename FROM files WHERE source = 'jottacloud' AND cloud_path IS NOT NULL"
    ).fetchall()
    return [r for r in rows if not Path(r["path"]).exists()]


def main():
    sys.stdout.reconfigure(line_buffering=True)
    conn = db.connect()
    missing = find_missing(conn)
    print(f"{len(missing)} file(s) marked synced but missing locally.")
    if not missing:
        return

    subprocess.run(["jotta-cli", "download", "--clear=all"], capture_output=True, text=True)

    pending = {}
    for i, row in enumerate(missing):
        local_path = Path(row["path"])
        tmp_dir = local_path.parent / ".incoming" / local_path.name
        tmp_dir.mkdir(parents=True, exist_ok=True)
        result = subprocess.run(
            ["jotta-cli", "download", "--merge", row["cloud_path"], str(tmp_dir)],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            print(f"  ! queuing failed for {row['cloud_path']}: {result.stderr.strip()}", file=sys.stderr)
            continue
        pending[row["cloud_path"]] = (tmp_dir, row)
        if (i + 1) % 100 == 0 or (i + 1) == len(missing):
            print(f"  ...queued {i + 1}/{len(missing)}")

    print(f"Waiting for {len(pending)} download(s) to finish ...")
    deadline = time.monotonic() + max(600, len(pending) * 3)
    last_print = time.monotonic()
    total_pending = len(pending)
    recovered = 0
    errors = 0
    while pending and time.monotonic() < deadline:
        if time.monotonic() - last_print > 15:
            print(f"  ...{total_pending - len(pending)}/{total_pending} done, {len(pending)} in flight")
            last_print = time.monotonic()
        result = subprocess.run(["jotta-cli", "list", "downloads", "--json"], capture_output=True, text=True)
        statuses = {d["Remote"]: d for d in json.loads(result.stdout or "[]")}
        for remote in list(pending):
            status = statuses.get(remote)
            if status and status.get("CompletedTimeMs"):
                tmp_dir, row = pending.pop(remote)
                if status.get("Errors"):
                    print(f"  ! download error for {remote}: {status['Errors']}", file=sys.stderr)
                    errors += 1
                    continue
                try:
                    downloaded = tmp_dir / row["filename"]
                    for _ in range(10):
                        if downloaded.exists():
                            break
                        time.sleep(0.5)
                    else:
                        raise FileNotFoundError(str(downloaded))
                    downloaded.rename(row["path"])
                    tmp_dir.rmdir()
                    recovered += 1
                    print(f"  OK {remote}")
                except Exception as exc:
                    print(f"  ! finalize failed for {remote}: {exc!r}", file=sys.stderr)
                    errors += 1
        if pending:
            time.sleep(2)

    if pending:
        print(f"  ! {len(pending)} still not finished after the deadline:", file=sys.stderr)
        for remote in pending:
            print(f"    - {remote}", file=sys.stderr)

    print()
    print(f"Recovered {recovered}, {errors} error(s), out of {total_pending} pending.")


if __name__ == "__main__":
    main()
