#!/usr/bin/env python3
"""
File scanner (DESIGN.md §6).

Walks a local directory of audio files and extracts metadata via ffprobe
(duration, sample_rate, channels, bit_depth, embedded encoded_by/software
tag) and a best-effort content_kind classification from filename shape.
Resumable: a file already indexed at the current ANALYSIS_VERSION is
skipped, no hashing (§2a). Bumping ANALYSIS_VERSION causes already-scanned
files to be reprocessed on the next run.

Handles two cases for a given path:
  - Already in `files` (inserted by jotta_sync.py with source='jottacloud')
    but not yet indexed: fills in the ffprobe metadata and sets indexed_at.
  - Not in `files` at all (a genuinely local file, source='local'): inserts
    a fresh row from filesystem stat + ffprobe metadata, then indexed_at.

content_kind is deliberately binary (raw_take | render_or_mix) — filename
shape gives no reliable signal for stock samples or field recordings, so
this doesn't try to guess those; see DESIGN.md discussion.
"""

import argparse
import json
import re
import subprocess
import sys
import time
from pathlib import Path

import db

AUDIO_EXTENSIONS = {".wav", ".aif", ".aiff"}
ANALYSIS_VERSION = 3

# Reaper's own auto-generated per-take recording filename: a leading track
# number, then anything, then a YYMMDD_HHMM timestamp — e.g.
# "01-1-150719_1212.wav", "03-MOdular-150111_2040 [chan 1].wav". Matching
# this is a stronger, more consistent signal than the embedded encoded_by
# tag (which is present on only ~91% of files that are otherwise identical
# in origin — see DESIGN.md discussion).
RAW_TAKE_FILENAME_RE = re.compile(r"^\d{1,2}[-_].*\b\d{6}_\d{4}\b")


def probe(path):
    """Run ffprobe and return (duration, sample_rate, channels, bit_depth, source_tag).

    Returns all-None fields plus an error string if ffprobe fails or finds
    no audio stream.
    """
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        return None, None, None, None, None, result.stderr.strip()

    data = json.loads(result.stdout)
    audio_streams = [s for s in data.get("streams", []) if s.get("codec_type") == "audio"]
    if not audio_streams:
        return None, None, None, None, None, "no audio stream found"

    stream = audio_streams[0]
    duration = float(data.get("format", {}).get("duration") or stream.get("duration") or 0) or None
    sample_rate = int(stream["sample_rate"]) if stream.get("sample_rate") else None
    channels = stream.get("channels")
    bit_depth = stream.get("bits_per_sample") or None

    tags = data.get("format", {}).get("tags", {}) or {}
    source_tag = tags.get("encoded_by") or tags.get("software") or tags.get("originator") or None

    return duration, sample_rate, channels, bit_depth, source_tag, None


def classify_content_kind(filename):
    return "raw_take" if RAW_TAKE_FILENAME_RE.match(filename) else "render_or_mix"


def iter_audio_files(root):
    for path in sorted(Path(root).rglob("*")):
        if path.is_file() and path.suffix.lower() in AUDIO_EXTENSIONS:
            yield path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dir", required=True, help="Local directory to scan (recursive).")
    parser.add_argument("--db", default=str(db.DEFAULT_DB_PATH))
    args = parser.parse_args()

    conn = db.init_db(args.db)

    scanned = 0
    skipped_already_indexed = 0
    inserted_new = 0
    errors = 0
    kind_counts = {"raw_take": 0, "render_or_mix": 0}

    for path in iter_audio_files(args.dir):
        abs_path = str(path.resolve())
        row = conn.execute(
            "SELECT id, filename, indexed_at, analysis_version FROM files WHERE path = ?", (abs_path,)
        ).fetchone()

        if row and row["indexed_at"] and (row["analysis_version"] or 0) >= ANALYSIS_VERSION:
            skipped_already_indexed += 1
            continue

        duration, sample_rate, channels, bit_depth, source_tag, probe_error = probe(abs_path)
        if probe_error:
            print(f"  ! {abs_path}: ffprobe error: {probe_error}", file=sys.stderr)
            errors += 1
            continue

        # Local staging filenames are opaque hashes (§5a) — classify against
        # the real original filename (from jotta_sync.py's DB row), not the
        # local one, or a jottacloud-sourced file's take/mix shape is lost.
        original_name = row["filename"] if row else path.name
        content_kind = classify_content_kind(original_name)
        kind_counts[content_kind] += 1
        now = int(time.time())

        if row:
            conn.execute(
                """
                UPDATE files
                SET duration = ?, sample_rate = ?, channels = ?, bit_depth = ?,
                    source_tag = ?, content_kind = ?, analysis_version = ?, indexed_at = ?
                WHERE id = ?
                """,
                (duration, sample_rate, channels, bit_depth, source_tag, content_kind,
                 ANALYSIS_VERSION, now, row["id"]),
            )
        else:
            stat = path.stat()
            conn.execute(
                """
                INSERT INTO files
                    (path, filename, extension, size, modified_time, duration,
                     sample_rate, channels, bit_depth, source_tag, content_kind,
                     analysis_version, source, indexed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'local', ?)
                """,
                (
                    abs_path, path.name, path.suffix.lower(), stat.st_size, int(stat.st_mtime),
                    duration, sample_rate, channels, bit_depth, source_tag, content_kind,
                    ANALYSIS_VERSION, now,
                ),
            )
            inserted_new += 1
        conn.commit()
        scanned += 1
        print(f"  {path.name}: {duration:.2f}s, {sample_rate}Hz, {channels}ch, {bit_depth}-bit, "
              f"{content_kind}, tag={source_tag or '-'}")

    print()
    print(f"Scanned {scanned} file(s) ({inserted_new} newly discovered local files), "
          f"{skipped_already_indexed} already up to date, {errors} error(s).")
    print(f"content_kind: {kind_counts}")


if __name__ == "__main__":
    main()
