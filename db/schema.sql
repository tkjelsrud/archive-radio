-- Personal Audio Archive Radio — SQLite schema (DESIGN.md §11)
-- The single shared source of truth across jotta_sync.py / scan.py /
-- analyze.py / render.py / the web app — no script keeps its own
-- side-state (DESIGN.md §20).

PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS files (
    id                INTEGER PRIMARY KEY,
    path              TEXT NOT NULL UNIQUE,
    filename          TEXT NOT NULL,
    extension         TEXT NOT NULL,
    size              INTEGER NOT NULL,
    modified_time     INTEGER,
    duration          REAL,
    sample_rate       INTEGER,
    channels          INTEGER,
    bit_depth         INTEGER,
    source_tag        TEXT,     -- embedded encoded_by/software tag, e.g. "REAPER" (may be absent even for Reaper files — not fully reliable alone)
    content_kind      TEXT CHECK (content_kind IN ('raw_take', 'render_or_mix')),  -- best-effort, from filename shape only; NULL when ambiguous. Deliberately no stock-sample/field-recording value yet — no reliable signature found for those (see DESIGN.md discussion)
    analysis_version  INTEGER,
    source            TEXT NOT NULL CHECK (source IN ('local', 'jottacloud')),
    cloud_path        TEXT,
    synced_at         INTEGER,
    indexed_at        INTEGER,
    created_at        INTEGER NOT NULL DEFAULT (strftime('%s', 'now'))
);

-- `path` is the resumability key (§6): a file already present is skipped
-- on rescan, no hashing. `indexed_at IS NULL` means "not yet scanned".
CREATE INDEX IF NOT EXISTS idx_files_indexed_at ON files(indexed_at);

CREATE TABLE IF NOT EXISTS segments (
    id                    INTEGER PRIMARY KEY,
    file_id               INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    start_time            REAL NOT NULL,
    end_time              REAL NOT NULL,
    duration              REAL NOT NULL,
    mean_rms              REAL,
    peak                  REAL,
    rendered_path         TEXT,
    rendered_duration     REAL,
    rendered_sample_rate  INTEGER,
    touched_at            INTEGER,
    rating                INTEGER NOT NULL DEFAULT 0 CHECK (rating IN (-1, 0, 1)),
    note                  TEXT,
    created_at            INTEGER NOT NULL DEFAULT (strftime('%s', 'now'))
);

-- No bookmarks table (§11): Favorites Pad membership is
-- touched_at IS NOT NULL AND rating != -1, derived at query time.
CREATE INDEX IF NOT EXISTS idx_segments_file_id ON segments(file_id);
CREATE INDEX IF NOT EXISTS idx_segments_touched_at ON segments(touched_at);
CREATE INDEX IF NOT EXISTS idx_segments_rating ON segments(rating);

CREATE TABLE IF NOT EXISTS tags (
    id    INTEGER PRIMARY KEY,
    name  TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS segment_tags (
    segment_id  INTEGER NOT NULL REFERENCES segments(id) ON DELETE CASCADE,
    tag_id      INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
    source      TEXT NOT NULL CHECK (source IN ('user', 'auto')),
    PRIMARY KEY (segment_id, tag_id)
);

CREATE TABLE IF NOT EXISTS sessions (
    id          INTEGER PRIMARY KEY,
    seed        INTEGER,
    created_at  INTEGER NOT NULL DEFAULT (strftime('%s', 'now'))
);

CREATE TABLE IF NOT EXISTS session_items (
    session_id       INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    segment_id       INTEGER NOT NULL REFERENCES segments(id) ON DELETE CASCADE,
    sequence_number  INTEGER NOT NULL,
    PRIMARY KEY (session_id, sequence_number)
);
