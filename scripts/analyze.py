#!/usr/bin/env python3
"""
Audio activity analysis + segment detection (DESIGN.md §7-9).

For each scanned file (duration known, not yet analyzed): decode at a
reduced resolution, compute an RMS envelope over fixed-duration windows
(NOT a fixed window count — see §7, this matters for long files), smooth
it, threshold it, merge nearby active runs, drop very-short ones (with a
per-file dynamic minimum, §9, so short one-shots aren't rejected), pad
the survivors, and merge any overlaps padding created. Insert one row per
final segment. A file with zero surviving segments still gets
segmented_at set (§7) — it's "processed, nothing found", not
indistinguishable from "not yet analyzed".

No numpy dependency (not installed on the target server, and the
workload's small enough that pure-Python + the stdlib `array` module is
simpler than adding one).

Each new segment also gets an auto year tag (§3.5): the batch year from
cloud_path for Jottacloud-sourced files (known at ingestion, not
dependent on trusting cloud-reported mtimes), or the year of
modified_time for local files.
"""

import argparse
import datetime
import math
import re
import subprocess
import sys
import time
from array import array

import db
from archive_util import extract_year

ANALYSIS_VERSION = 1

DECODE_SAMPLE_RATE = 8000  # Hz — plenty for an activity envelope, cheap to decode/loop over
WINDOW_MS = 200
DEFAULT_ACTIVITY_THRESHOLD = 0.01  # RMS, ~-40dB, on samples normalized to [-1, 1]
DEFAULT_MERGE_GAP = 1.5  # seconds; §9 default range is 1-2s
DEFAULT_MIN_SEGMENT = 3.0  # seconds; §9 default, clamped per-file to file duration
DEFAULT_PAD_BEFORE = 1.0
DEFAULT_PAD_AFTER = 2.0
MAX_CLIP_SECONDS = 10.0  # §8a — segments longer than this get capped at render time, not here

CLOUD_PROJECT_RE = re.compile(r"/Musikkprosjekter/([^/]+)/", re.IGNORECASE)


def decode_mono_samples(path):
    """Decode to mono s16le PCM at DECODE_SAMPLE_RATE via ffmpeg.

    Returns (array('h', ...), None) or (None, error_message).
    """
    result = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(path), "-ac", "1", "-ar", str(DECODE_SAMPLE_RATE),
         "-f", "s16le", "-"],
        capture_output=True,
    )
    if result.returncode != 0:
        return None, result.stderr.decode(errors="replace").strip()
    samples = array("h")
    samples.frombytes(result.stdout)
    return samples, None


def compute_envelope(samples, sr, window_ms):
    """Per-window (rms, peak) tuples, samples normalized to [-1, 1]."""
    window_size = max(1, int(sr * window_ms / 1000))
    envelope = []
    n = len(samples)
    for start in range(0, n, window_size):
        chunk = samples[start:start + window_size]
        if not chunk:
            continue
        sumsq = 0.0
        peak = 0.0
        for s in chunk:
            v = s / 32768.0
            sumsq += v * v
            av = abs(v)
            if av > peak:
                peak = av
        rms = math.sqrt(sumsq / len(chunk))
        envelope.append((rms, peak))
    return envelope, window_size


def smooth(values, radius=1):
    """Simple moving average over a small window either side of each point."""
    n = len(values)
    out = []
    for i in range(n):
        lo, hi = max(0, i - radius), min(n, i + radius + 1)
        window = values[lo:hi]
        out.append(sum(window) / len(window))
    return out


def find_active_runs(is_active):
    """Contiguous [start_idx, end_idx) runs of True in a bool list."""
    runs = []
    start = None
    for i, active in enumerate(is_active):
        if active and start is None:
            start = i
        elif not active and start is not None:
            runs.append((start, i))
            start = None
    if start is not None:
        runs.append((start, len(is_active)))
    return runs


def merge_close_runs(runs, window_seconds, merge_gap):
    """Merge runs whose gap (in seconds) is <= merge_gap."""
    if not runs:
        return []
    merged = [list(runs[0])]
    for start, end in runs[1:]:
        gap_seconds = (start - merged[-1][1]) * window_seconds
        if gap_seconds <= merge_gap:
            merged[-1][1] = end
        else:
            merged.append([start, end])
    return [tuple(r) for r in merged]


def merge_overlapping(regions):
    """Merge (start, end) time regions that touch/overlap after padding."""
    if not regions:
        return []
    regions = sorted(regions)
    merged = [list(regions[0])]
    for start, end in regions[1:]:
        if start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return [tuple(r) for r in merged]


def detect_segments(path, file_duration, threshold, merge_gap, pad_before, pad_after):
    """Returns (list of segment dicts, error_message_or_None).

    Each segment dict: start_time, end_time, duration, mean_rms, peak.
    """
    samples, error = decode_mono_samples(path)
    if error:
        return [], error
    if not samples:
        return [], None  # genuinely empty decode — zero segments, not an error

    envelope, window_size = compute_envelope(samples, DECODE_SAMPLE_RATE, WINDOW_MS)
    if not envelope:
        return [], None

    window_seconds = window_size / DECODE_SAMPLE_RATE
    rms_values = smooth([e[0] for e in envelope])
    is_active = [r >= threshold for r in rms_values]

    runs = find_active_runs(is_active)
    runs = merge_close_runs(runs, window_seconds, merge_gap)

    min_segment = min(DEFAULT_MIN_SEGMENT, file_duration)
    padded_regions = []
    for start_idx, end_idx in runs:
        run_duration = (end_idx - start_idx) * window_seconds
        if run_duration < min_segment:
            continue
        start_time = max(0.0, start_idx * window_seconds - pad_before)
        end_time = min(file_duration, end_idx * window_seconds + pad_after)
        padded_regions.append((start_time, end_time))

    final_regions = merge_overlapping(padded_regions)

    segments = []
    for start_time, end_time in final_regions:
        start_idx = int(start_time / window_seconds)
        end_idx = min(len(envelope), int(math.ceil(end_time / window_seconds)))
        region_windows = envelope[start_idx:end_idx] or [(0.0, 0.0)]
        mean_rms = sum(r for r, _ in region_windows) / len(region_windows)
        peak = max(p for _, p in region_windows)
        segments.append({
            "start_time": start_time,
            "end_time": end_time,
            "duration": end_time - start_time,
            "mean_rms": mean_rms,
            "peak": peak,
        })
    return segments, None


def year_for_file(row):
    """Batch year from cloud_path for Jottacloud files (known at ingestion,
    not dependent on cloud-reported mtime fidelity — see §5a discussion);
    falls back to modified_time's year for local files. None if neither
    is available.
    """
    if row["source"] == "jottacloud" and row["cloud_path"]:
        match = CLOUD_PROJECT_RE.search(row["cloud_path"])
        if match:
            year = extract_year(match.group(1))
            if year:
                return year
    if row["modified_time"]:
        return datetime.datetime.fromtimestamp(row["modified_time"], tz=datetime.timezone.utc).year
    return None


def tag_segment_with_year(conn, segment_id, year):
    if year is None:
        return
    tag_name = str(year)
    conn.execute("INSERT OR IGNORE INTO tags (name) VALUES (?)", (tag_name,))
    tag_id = conn.execute("SELECT id FROM tags WHERE name = ?", (tag_name,)).fetchone()["id"]
    conn.execute(
        "INSERT OR IGNORE INTO segment_tags (segment_id, tag_id, source) VALUES (?, ?, 'auto')",
        (segment_id, tag_id),
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(db.DEFAULT_DB_PATH))
    parser.add_argument("--threshold", type=float, default=DEFAULT_ACTIVITY_THRESHOLD)
    parser.add_argument("--merge-gap", type=float, default=DEFAULT_MERGE_GAP)
    parser.add_argument("--pad-before", type=float, default=DEFAULT_PAD_BEFORE)
    parser.add_argument("--pad-after", type=float, default=DEFAULT_PAD_AFTER)
    args = parser.parse_args()

    conn = db.init_db(args.db)
    rows = conn.execute(
        """
        SELECT id, path, filename, duration, source, cloud_path, modified_time
        FROM files
        WHERE duration IS NOT NULL AND segmented_at IS NULL
        ORDER BY id
        """
    ).fetchall()

    total_segments = 0
    files_with_zero = 0
    errors = 0

    for row in rows:
        segments, error = detect_segments(
            row["path"], row["duration"], args.threshold, args.merge_gap,
            args.pad_before, args.pad_after,
        )
        if error:
            print(f"  ! {row['filename']}: ffmpeg decode error: {error}", file=sys.stderr)
            errors += 1
            continue

        year = year_for_file(row)
        for seg in segments:
            cur = conn.execute(
                """
                INSERT INTO segments (file_id, start_time, end_time, duration, mean_rms, peak)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (row["id"], seg["start_time"], seg["end_time"], seg["duration"],
                 seg["mean_rms"], seg["peak"]),
            )
            tag_segment_with_year(conn, cur.lastrowid, year)

        conn.execute("UPDATE files SET segmented_at = ? WHERE id = ?", (int(time.time()), row["id"]))
        conn.commit()

        total_segments += len(segments)
        if not segments:
            files_with_zero += 1
        print(f"  {row['filename']}: {len(segments)} segment(s)"
              + ("".join(f" [{s['start_time']:.1f}-{s['end_time']:.1f}s]" for s in segments) if segments else " (none found)"))

    print()
    print(f"Analyzed {len(rows)} file(s): {total_segments} segment(s) total, "
          f"{files_with_zero} file(s) with zero segments, {errors} error(s).")


if __name__ == "__main__":
    main()
