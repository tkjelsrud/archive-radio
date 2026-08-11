#!/usr/bin/env python3
"""
Web player backend (DESIGN.md §3.3, §12-14, §16-17).

Endpoints:
  GET  /                              -> player page
  POST /api/session                   -> {session_id, seed} — start a new session
  GET  /api/session/{sid}/item/{seq}  -> segment at that position in the session's
                                          timeline, generating it deterministically
                                          from (seed, seq) if it doesn't exist yet,
                                          or returning the existing pick if it does
                                          (so going back then forward again replays
                                          the same segment, not a new random one)
  GET  /clip/{segment_id}             -> the compiled clip audio (Range-capable)

  GET    /api/tags                          -> [{id, name}] for quick-select (§3.5)
  GET    /api/segment/{id}                  -> {touched_at, rating, note, tags: [...]}
  POST   /api/segment/{id}/touch            -> marks touched_at if not already set
  POST   /api/segment/{id}/rating           -> body {rating: -1|0|1}
  POST   /api/segment/{id}/tags             -> body {name} — add (creates tag if new, source='user')
  DELETE /api/segment/{id}/tags/{tag_name}  -> remove that tag from the segment
  POST   /api/segment/{id}/note             -> body {note}

  GET  /log     -> pipeline processing log + recent git commits (operational visibility)
  GET  /status  -> processed-so-far stats (live from archive.db) alongside real full-archive
                   totals (cached from scripts/archive_stats.py — not recomputed per request,
                   that's a few hundred jotta-cli calls). Numbers shown are always real,
                   never a projection of what the full archive *might* eventually contain.

Selection is file-first, then segment-within-file (§16): picking uniformly across
all segments would let one dense source file dominate playback. The immediately
previous file is excluded from the candidate pool where possible (simple
anti-repeat — not yet the full "N files must pass" rule from §16). rating = -1
means "not usable" (a hard exclude for objectively broken/unfit clips — clipped
garbage, a bad render — not a subjective taste rating), and is excluded from
selection entirely.
"""

import datetime
import hashlib
import html
import json
import random
import time
from pathlib import Path

from pydantic import BaseModel

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse

import db

app = FastAPI()
ROOT_DIR = Path(__file__).parent.parent
WEB_DIR = ROOT_DIR / "web"
LOGS_DIR = ROOT_DIR / "logs"


def rng_for(seed, sequence_number):
    """Deterministic RNG for one (seed, sequence_number) pair — stateless
    across requests, and reproducible given the same seed/DB state (§17).
    """
    key = f"{seed}:{sequence_number}".encode()
    digest = hashlib.sha256(key).digest()
    return random.Random(int.from_bytes(digest[:8], "big"))


def pick_segment(conn, seed, sequence_number, previous_file_id):
    rng = rng_for(seed, sequence_number)

    file_ids = [
        row["file_id"]
        for row in conn.execute(
            "SELECT DISTINCT file_id FROM segments WHERE rendered_path IS NOT NULL AND rating != -1"
        )
    ]
    if not file_ids:
        return None

    candidates = [f for f in file_ids if f != previous_file_id] or file_ids
    chosen_file_id = rng.choice(candidates)

    segment_ids = [
        row["id"]
        for row in conn.execute(
            "SELECT id FROM segments WHERE file_id = ? AND rendered_path IS NOT NULL AND rating != -1",
            (chosen_file_id,),
        )
    ]
    return rng.choice(segment_ids)


def segment_payload(conn, segment_id, sequence_number):
    row = conn.execute(
        """
        SELECT s.id, s.start_time, s.end_time, s.rendered_duration,
               f.filename, f.cloud_path, f.path
        FROM segments s JOIN files f ON f.id = s.file_id
        WHERE s.id = ?
        """,
        (segment_id,),
    ).fetchone()
    if not row:
        return None
    total_available = conn.execute(
        "SELECT COUNT(*) AS n FROM segments WHERE rendered_path IS NOT NULL AND rating != -1"
    ).fetchone()["n"]
    return {
        "sequence_number": sequence_number,
        "segment_id": row["id"],
        "clip_url": f"/clip/{row['id']}",
        "duration": row["rendered_duration"],
        "source_filename": row["filename"],
        "source_location": row["cloud_path"] or row["path"],
        "original_start_time": row["start_time"],
        "original_end_time": row["end_time"],
        "total_available": total_available,
    }


@app.post("/api/session")
def start_session():
    conn = db.connect()
    seed = int(time.time() * 1000)
    cur = conn.execute("INSERT INTO sessions (seed) VALUES (?)", (seed,))
    conn.commit()
    return {"session_id": cur.lastrowid, "seed": seed}


@app.get("/api/session/{session_id}/item/{sequence_number}")
def get_item(session_id: int, sequence_number: int):
    conn = db.connect()
    session = conn.execute("SELECT id, seed FROM sessions WHERE id = ?", (session_id,)).fetchone()
    if not session:
        raise HTTPException(404, "no such session")
    if sequence_number < 0:
        raise HTTPException(400, "sequence_number must be >= 0")

    existing = conn.execute(
        "SELECT segment_id FROM session_items WHERE session_id = ? AND sequence_number = ?",
        (session_id, sequence_number),
    ).fetchone()
    if existing:
        payload = segment_payload(conn, existing["segment_id"], sequence_number)
        if payload:
            return payload

    previous = conn.execute(
        "SELECT segment_id FROM session_items WHERE session_id = ? AND sequence_number = ?",
        (session_id, sequence_number - 1),
    ).fetchone()
    previous_file_id = None
    if previous:
        prev_seg = conn.execute(
            "SELECT file_id FROM segments WHERE id = ?", (previous["segment_id"],)
        ).fetchone()
        previous_file_id = prev_seg["file_id"] if prev_seg else None

    segment_id = pick_segment(conn, session["seed"], sequence_number, previous_file_id)
    if segment_id is None:
        raise HTTPException(503, "no rendered clips available yet")

    conn.execute(
        "INSERT INTO session_items (session_id, segment_id, sequence_number) VALUES (?, ?, ?)",
        (session_id, segment_id, sequence_number),
    )
    conn.commit()
    return segment_payload(conn, segment_id, sequence_number)


@app.get("/clip/{segment_id}")
def get_clip(segment_id: int):
    conn = db.connect()
    row = conn.execute("SELECT rendered_path FROM segments WHERE id = ?", (segment_id,)).fetchone()
    if not row or not row["rendered_path"]:
        raise HTTPException(404, "no rendered clip for this segment")
    return FileResponse(row["rendered_path"], media_type="audio/wav")


def require_segment(conn, segment_id):
    row = conn.execute("SELECT id FROM segments WHERE id = ?", (segment_id,)).fetchone()
    if not row:
        raise HTTPException(404, "no such segment")


def segment_detail(conn, segment_id):
    row = conn.execute(
        "SELECT id, touched_at, rating, note FROM segments WHERE id = ?", (segment_id,)
    ).fetchone()
    tags = conn.execute(
        """
        SELECT t.name, st.source FROM segment_tags st
        JOIN tags t ON t.id = st.tag_id
        WHERE st.segment_id = ?
        ORDER BY t.name
        """,
        (segment_id,),
    ).fetchall()
    return {
        "segment_id": row["id"],
        "touched_at": row["touched_at"],
        "rating": row["rating"],
        "note": row["note"],
        "tags": [{"name": t["name"], "source": t["source"]} for t in tags],
    }


@app.get("/api/tags")
def list_tags():
    conn = db.connect()
    rows = conn.execute("SELECT id, name FROM tags ORDER BY name").fetchall()
    return [{"id": r["id"], "name": r["name"]} for r in rows]


@app.get("/api/segment/{segment_id}")
def get_segment(segment_id: int):
    conn = db.connect()
    require_segment(conn, segment_id)
    return segment_detail(conn, segment_id)


@app.post("/api/segment/{segment_id}/touch")
def touch_segment(segment_id: int):
    conn = db.connect()
    require_segment(conn, segment_id)
    conn.execute(
        "UPDATE segments SET touched_at = COALESCE(touched_at, ?) WHERE id = ?",
        (int(time.time()), segment_id),
    )
    conn.commit()
    return segment_detail(conn, segment_id)


class RatingBody(BaseModel):
    rating: int


@app.post("/api/segment/{segment_id}/rating")
def set_rating(segment_id: int, body: RatingBody):
    if body.rating not in (-1, 0, 1):
        raise HTTPException(400, "rating must be -1, 0, or 1")
    conn = db.connect()
    require_segment(conn, segment_id)
    conn.execute("UPDATE segments SET rating = ? WHERE id = ?", (body.rating, segment_id))
    conn.commit()
    return segment_detail(conn, segment_id)


class TagBody(BaseModel):
    name: str


@app.post("/api/segment/{segment_id}/tags")
def add_tag(segment_id: int, body: TagBody):
    name = body.name.strip()
    if not name:
        raise HTTPException(400, "tag name required")
    conn = db.connect()
    require_segment(conn, segment_id)
    conn.execute("INSERT OR IGNORE INTO tags (name) VALUES (?)", (name,))
    tag_id = conn.execute("SELECT id FROM tags WHERE name = ?", (name,)).fetchone()["id"]
    conn.execute(
        "INSERT OR IGNORE INTO segment_tags (segment_id, tag_id, source) VALUES (?, ?, 'user')",
        (segment_id, tag_id),
    )
    conn.commit()
    return segment_detail(conn, segment_id)


@app.delete("/api/segment/{segment_id}/tags/{tag_name}")
def remove_tag(segment_id: int, tag_name: str):
    conn = db.connect()
    require_segment(conn, segment_id)
    tag = conn.execute("SELECT id FROM tags WHERE name = ?", (tag_name,)).fetchone()
    if tag:
        conn.execute(
            "DELETE FROM segment_tags WHERE segment_id = ? AND tag_id = ?", (segment_id, tag["id"])
        )
        conn.commit()
    return segment_detail(conn, segment_id)


class NoteBody(BaseModel):
    note: str


@app.post("/api/segment/{segment_id}/note")
def set_note(segment_id: int, body: NoteBody):
    conn = db.connect()
    require_segment(conn, segment_id)
    conn.execute("UPDATE segments SET note = ? WHERE id = ?", (body.note, segment_id))
    conn.commit()
    return segment_detail(conn, segment_id)


NO_CACHE = {"Cache-Control": "no-cache"}


@app.get("/", response_class=HTMLResponse)
def index():
    return HTMLResponse((WEB_DIR / "index.html").read_text(), headers=NO_CACHE)


@app.get("/style.css")
def style():
    return FileResponse(WEB_DIR / "style.css", media_type="text/css", headers=NO_CACHE)


@app.get("/player.js")
def player_js():
    return FileResponse(WEB_DIR / "player.js", media_type="application/javascript", headers=NO_CACHE)


@app.get("/badger-studio.png")
def logo():
    return FileResponse(WEB_DIR / "badger-studio.png", media_type="image/png")


def tail_text(path, max_lines=400):
    if not path.exists():
        return "(nothing logged yet)"
    lines = path.read_text(errors="replace").splitlines()
    return "\n".join(lines[-max_lines:]) or "(empty)"


@app.get("/log", response_class=HTMLResponse)
def log_page():
    pipeline_log = html.escape(tail_text(LOGS_DIR / "pipeline.log"))
    changelog = html.escape(tail_text(ROOT_DIR / "web" / "changelog.txt", max_lines=40))
    page = f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"><title>Pipeline Log</title>
<style>
  body {{ background:#000; color:#ddd; font-family: ui-monospace, Menlo, Consolas, monospace;
          font-size:13px; padding:24px; max-width:900px; margin:0 auto; }}
  h2 {{ color:#888; font-size:13px; text-transform:uppercase; letter-spacing:0.08em;
        border-bottom:1px solid #262626; padding-bottom:8px; margin-top:36px; }}
  pre {{ white-space:pre-wrap; word-break:break-word; color:#bbb; line-height:1.5; }}
  a {{ color:#6cf; }}
</style></head>
<body>
  <p><a href="/">&larr; back to player</a></p>
  <h2>Recent development (git log)</h2>
  <pre>{changelog}</pre>
  <h2>Pipeline activity (jotta_sync / scan / analyze / render)</h2>
  <pre>{pipeline_log}</pre>
</body></html>"""
    return HTMLResponse(page, headers=NO_CACHE)


def gib(num_bytes):
    return f"{num_bytes / 1_073_741_824:.2f} GiB"


@app.get("/status", response_class=HTMLResponse)
def status_page():
    conn = db.connect()
    files_indexed = conn.execute("SELECT COUNT(*) AS n FROM files WHERE indexed_at IS NOT NULL").fetchone()["n"]
    segments_total = conn.execute("SELECT COUNT(*) AS n FROM segments").fetchone()["n"]
    segments_available = conn.execute(
        "SELECT COUNT(*) AS n FROM segments WHERE rendered_path IS NOT NULL AND rating != -1"
    ).fetchone()["n"]
    total_hours = (conn.execute("SELECT COALESCE(SUM(duration), 0) AS s FROM segments").fetchone()["s"] or 0) / 3600
    touched = conn.execute("SELECT COUNT(*) AS n FROM segments WHERE touched_at IS NOT NULL").fetchone()["n"]
    not_usable = conn.execute("SELECT COUNT(*) AS n FROM segments WHERE rating = -1").fetchone()["n"]

    totals_path = LOGS_DIR / "archive_totals.json"
    if totals_path.exists():
        totals = json.loads(totals_path.read_text())
        scanned_at = datetime.datetime.fromtimestamp(totals["scanned_at"], tz=datetime.timezone.utc)
        full_archive_html = f"""
        <div class="stat"><span>{totals['total_files']:,}</span> files total ({gib(totals['total_bytes'])})</div>
        <div class="stat"><span>{totals['audio_files']:,}</span> match the WAV/AIFF filter ({gib(totals['audio_bytes'])})</div>
        <div class="note">Measured {scanned_at:%Y-%m-%d} via scripts/archive_stats.py.</div>
        """
    else:
        full_archive_html = '<div class="note">Run scripts/archive_stats.py to measure the full archive.</div>'

    page = f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"><title>Notoms Archive Radio — Status</title>
<style>
  body {{ background:#000; color:#ddd; font-family: -apple-system, BlinkMacSystemFont, sans-serif;
          padding:32px; max-width:640px; margin:0 auto; }}
  h1 {{ font-size:15px; letter-spacing:0.15em; color:#888; text-transform:uppercase; }}
  h2 {{ color:#888; font-size:12px; text-transform:uppercase; letter-spacing:0.08em;
        border-bottom:1px solid #262626; padding-bottom:8px; margin-top:36px; }}
  .stat {{ font-size:15px; color:#ccc; margin:6px 0; }}
  .stat span {{ color:#eee; font-weight:600; }}
  .note {{ font-size:12px; color:#666; margin-top:10px; }}
  a {{ color:#6cf; }}
</style></head>
<body>
  <p><a href="/">&larr; back to player</a></p>
  <h1>Notoms Archive Radio</h1>

  <h2>Processed so far</h2>
  <div class="stat"><span>{files_indexed:,}</span> source files scanned</div>
  <div class="stat"><span>{segments_total:,}</span> segments detected ({segments_available:,} currently playable)</div>
  <div class="stat"><span>{total_hours:.1f}</span> hours of segment audio</div>
  <div class="stat"><span>{touched:,}</span> clips touched/favorited, <span>{not_usable:,}</span> marked not usable</div>

  <h2>Full archive (Jottacloud, not yet processed)</h2>
  {full_archive_html}
</body></html>"""
    return HTMLResponse(page, headers=NO_CACHE)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8420)
