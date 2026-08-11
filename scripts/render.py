#!/usr/bin/env python3
"""
Compiled clip rendering (DESIGN.md §8a).

For each segment without a rendered_path yet: extract mono/22.05kHz/16-bit
PCM WAV, capped at 10 seconds (if the detected region is longer, take the
first 10 seconds of it — not the full region), into clip-cache, sharded by
source file_id so one flat directory doesn't end up with hundreds of
thousands of files. This compiled clip — never the original — is what the
player actually streams.

Also applies a gentle, clamped RMS-based gain correction (see
gain_db_for()) — real listening feedback was that raw levels varied too
much between clips. Not full loudness normalization: outliers are pulled
partway toward a target, not flattened, so the archive's real dynamic
range (a quiet ambient recording vs. a loud drum take) isn't erased.

Resumable: a segment with rendered_path already set is skipped.
"""

import argparse
import math
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import db

CLIP_SAMPLE_RATE = 22050
MAX_CLIP_SECONDS = 10.0
DEFAULT_WORKERS = 3  # conservative start on a small 4-core box running other services too

# Gentle, clamped RMS-based gain correction — not full loudness normalization.
# Real distribution across 1152 segments: mean_rms ranged 0.0075-0.52 (~37dB
# spread), median 0.059. Pulling outliers partway toward a target evens out
# the "constantly reaching for the volume knob" feeling from real listening
# without flattening the archive's natural dynamic variation — a genuinely
# quiet ambient recording should stay quieter than a loud drum take, just
# not by 30+dB.
TARGET_RMS = 0.07
MAX_GAIN_ADJUST_DB = 9.0


def gain_db_for(mean_rms):
    if not mean_rms or mean_rms <= 0:
        return 0.0
    gain_db = 20 * math.log10(TARGET_RMS / mean_rms)
    return max(-MAX_GAIN_ADJUST_DB, min(MAX_GAIN_ADJUST_DB, gain_db))


def render_clip(source_path, start_time, duration, dest_path, gain_db):
    clip_duration = min(duration, MAX_CLIP_SECONDS)
    result = subprocess.run(
        [
            "ffmpeg", "-v", "error", "-y",
            "-ss", str(start_time), "-t", str(clip_duration), "-i", str(source_path),
            "-af", f"volume={gain_db:.2f}dB",
            "-ac", "1", "-ar", str(CLIP_SAMPLE_RATE), "-sample_fmt", "s16",
            str(dest_path),
        ],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        return None, result.stderr.strip()
    return clip_duration, None


def render_one(row, cache_root):
    """Worker-thread body: mkdir + one ffmpeg call, no DB access."""
    shard_dir = cache_root / str(row["file_id"])
    shard_dir.mkdir(parents=True, exist_ok=True)
    dest_path = shard_dir / f"{row['segment_id']}.wav"
    gain_db = gain_db_for(row["mean_rms"])
    clip_duration, error = render_clip(row["source_path"], row["start_time"], row["duration"], dest_path, gain_db)
    return dest_path, clip_duration, error


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(db.DEFAULT_DB_PATH))
    parser.add_argument("--clip-cache", default=str(Path(db.DEFAULT_DB_PATH).parent / "clip-cache"))
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS,
                         help="Concurrent ffmpeg renders. Kept conservative by default.")
    args = parser.parse_args()

    conn = db.init_db(args.db)
    rows = conn.execute(
        """
        SELECT s.id AS segment_id, s.file_id, s.start_time, s.duration, s.mean_rms,
               f.path AS source_path
        FROM segments s
        JOIN files f ON f.id = s.file_id
        WHERE s.rendered_path IS NULL
        ORDER BY s.id
        """
    ).fetchall()

    rendered = 0
    errors = 0
    cache_root = Path(args.clip_cache)

    # ffmpeg calls run concurrently in worker threads; every DB write stays
    # in the main thread so only one connection ever writes at a time.
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        future_to_row = {pool.submit(render_one, row, cache_root): row for row in rows}

        for future in as_completed(future_to_row):
            row = future_to_row[future]
            dest_path, clip_duration, error = future.result()
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
