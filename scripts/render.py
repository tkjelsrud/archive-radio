#!/usr/bin/env python3
"""
Compiled clip rendering (DESIGN.md §8a).

For each segment without a rendered_path yet: extract mono/22.05kHz/16-bit
PCM WAV, capped at 10 seconds (if the detected region is longer, take the
first 10 seconds of it — not the full region), into clip-cache, sharded by
source file_id so one flat directory doesn't end up with hundreds of
thousands of files. This compiled clip — never the original — is what the
player actually streams.

Resumable: a segment with rendered_path already set is skipped.
"""

import argparse
import subprocess
import sys
import time
from pathlib import Path

import db

CLIP_SAMPLE_RATE = 22050
MAX_CLIP_SECONDS = 10.0


def render_clip(source_path, start_time, duration, dest_path):
    clip_duration = min(duration, MAX_CLIP_SECONDS)
    result = subprocess.run(
        [
            "ffmpeg", "-v", "error", "-y",
            "-ss", str(start_time), "-t", str(clip_duration), "-i", str(source_path),
            "-ac", "1", "-ar", str(CLIP_SAMPLE_RATE), "-sample_fmt", "s16",
            str(dest_path),
        ],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        return None, result.stderr.strip()
    return clip_duration, None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(db.DEFAULT_DB_PATH))
    parser.add_argument("--clip-cache", default=str(Path(db.DEFAULT_DB_PATH).parent / "clip-cache"))
    args = parser.parse_args()

    conn = db.init_db(args.db)
    rows = conn.execute(
        """
        SELECT s.id AS segment_id, s.file_id, s.start_time, s.duration, f.path AS source_path
        FROM segments s
        JOIN files f ON f.id = s.file_id
        WHERE s.rendered_path IS NULL
        ORDER BY s.id
        """
    ).fetchall()

    rendered = 0
    errors = 0
    cache_root = Path(args.clip_cache)

    for row in rows:
        shard_dir = cache_root / str(row["file_id"])
        shard_dir.mkdir(parents=True, exist_ok=True)
        dest_path = shard_dir / f"{row['segment_id']}.wav"

        clip_duration, error = render_clip(row["source_path"], row["start_time"], row["duration"], dest_path)
        if error:
            print(f"  ! segment {row['segment_id']} (file {row['file_id']}): {error}", file=sys.stderr)
            errors += 1
            continue

        conn.execute(
            """
            UPDATE segments
            SET rendered_path = ?, rendered_duration = ?, rendered_sample_rate = ?
            WHERE id = ?
            """,
            (str(dest_path.resolve()), clip_duration, CLIP_SAMPLE_RATE, row["segment_id"]),
        )
        conn.commit()
        rendered += 1
        print(f"  segment {row['segment_id']}: {clip_duration:.1f}s -> {dest_path}")

    print()
    print(f"Rendered {rendered} clip(s), {errors} error(s), out of {len(rows)} pending.")


if __name__ == "__main__":
    main()
