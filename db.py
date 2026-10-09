"""db.py — schema, connection, generation checks, and the queryable-set switch.

The correctness contract (plan v3):

- `files.current_run_id` is the ONLY live pointer; updates move it, never delete index rows.
- Live vec/fts tables hold exactly the queryable set: completed runs referenced by
  files with status IN ('active', 'missing_on_source'). When a switch costs a run its
  last valid reference, its rows leave the live tables in the same transaction —
  "was promoted" never implies "queryable".
- Retrieval refuses to start when the live generation doesn't match the current config.
"""

import json
import sqlite3

PARSER_VERSION = "1"  # bump when a parser's output shape changes
CHUNK_CONFIG = json.dumps({"target_chars": 1000, "overlap_chars": 150})

# Valid reference for a run: every files.status EXCEPT 'removed' — 'new'/'embedded'/'extracted'/
# 'error'/'missing_on_source' all keep their index queryable (missing_on_source = NAS-offline rule).
NOT_REMOVED = "status != 'removed'"

RUN_TERMINAL_STATES = ("embedded", "extracted", "extract_failed", "error")

SCHEMA = """
CREATE TABLE IF NOT EXISTS sources(
  id INTEGER PRIMARY KEY,
  path TEXT NOT NULL UNIQUE,
  status TEXT NOT NULL DEFAULT 'active',   -- active|missing_on_share|removed
  scan_seen_at TEXT
);
CREATE TABLE IF NOT EXISTS file_versions(
  id INTEGER PRIMARY KEY,
  sha256 TEXT NOT NULL UNIQUE,
  size_bytes INTEGER NOT NULL,
  blob_path TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS index_meta(
  id INTEGER PRIMARY KEY,
  parser_version TEXT NOT NULL,
  chunk_config TEXT NOT NULL,
  embed_model TEXT NOT NULL,
  embed_digest TEXT NOT NULL,
  dims INTEGER NOT NULL,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS index_runs(
  id INTEGER PRIMARY KEY,
  version_id INTEGER NOT NULL REFERENCES file_versions(id),
  generation_id INTEGER NOT NULL REFERENCES index_meta(id),
  state TEXT NOT NULL DEFAULT 'chunking',  -- chunking|embedding|embedded|extracting|extracted|extract_failed|error
  chunk_count INTEGER NOT NULL DEFAULT 0,
  updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS files(
  id INTEGER PRIMARY KEY,
  source_path TEXT NOT NULL UNIQUE,
  name TEXT NOT NULL,
  kind TEXT NOT NULL,
  ext TEXT,
  current_version_id INTEGER REFERENCES file_versions(id),  -- content pointer (scan sets it)
  current_run_id INTEGER REFERENCES index_runs(id),         -- index pointer (switch sets it)
  scan_seen_at TEXT,
  exif_date TEXT,
  status TEXT NOT NULL DEFAULT 'new',      -- new|copied|embedded|extracted|error|missing_on_source|removed
  meta TEXT,
  updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS chunks(
  id INTEGER PRIMARY KEY,
  run_id INTEGER NOT NULL REFERENCES index_runs(id),
  seq INTEGER NOT NULL,
  text TEXT NOT NULL,
  page INTEGER,
  kind TEXT NOT NULL DEFAULT 'body',       -- body|caption|table
  position TEXT,                           -- JSON: {page, char_range} | {sheet, rows} | {para_range} | {bbox}
  UNIQUE(run_id, seq)
);

-- knowledge graph (Phase 2 extraction writes here; schema lands now)
CREATE TABLE IF NOT EXISTS entities(
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  type TEXT NOT NULL,                      -- person|org|property|policy|vendor|event|account|document|other
  canonical TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending',  -- pending|confirmed|conflicting
  attrs TEXT
);
CREATE TABLE IF NOT EXISTS aliases(
  id INTEGER PRIMARY KEY,
  entity_id INTEGER NOT NULL REFERENCES entities(id),
  alias TEXT NOT NULL,
  source TEXT NOT NULL DEFAULT 'human'
);
CREATE TABLE IF NOT EXISTS entity_merge_candidates(
  id INTEGER PRIMARY KEY,
  a INTEGER NOT NULL REFERENCES entities(id),
  b INTEGER NOT NULL REFERENCES entities(id),
  reason TEXT,
  status TEXT NOT NULL DEFAULT 'pending'   -- pending|accepted|rejected
);
CREATE TABLE IF NOT EXISTS relations(
  id INTEGER PRIMARY KEY,
  src INTEGER NOT NULL REFERENCES entities(id),
  dst INTEGER NOT NULL REFERENCES entities(id),
  rel TEXT NOT NULL,
  run_id INTEGER NOT NULL REFERENCES index_runs(id),
  evidence_chunk_ids TEXT,                 -- JSON list of chunk ids
  quote TEXT,
  valid_from TEXT,
  valid_to TEXT,
  quote_verified INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'pending'   -- pending|confirmed|conflicting
);
CREATE TABLE IF NOT EXISTS chunk_entities(
  chunk_id INTEGER NOT NULL REFERENCES chunks(id),
  entity_id INTEGER NOT NULL REFERENCES entities(id),
  PRIMARY KEY(chunk_id, entity_id)
);
CREATE TABLE IF NOT EXISTS entity_corrections(
  entity_id INTEGER PRIMARY KEY REFERENCES entities(id),
  new_name TEXT,
  new_type TEXT,
  note TEXT,
  by TEXT,
  at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- spending (Phase 3)
CREATE TABLE IF NOT EXISTS transactions(
  id INTEGER PRIMARY KEY,
  file_id INTEGER REFERENCES files(id),
  run_id INTEGER REFERENCES index_runs(id),
  date TEXT NOT NULL,
  amount_minor INTEGER NOT NULL,
  scale INTEGER NOT NULL,
  currency TEXT NOT NULL DEFAULT 'USD',
  merchant TEXT,
  category TEXT,
  kind TEXT NOT NULL DEFAULT 'consumption', -- consumption|refund|transfer|credit_card_payment
  description TEXT,
  raw TEXT,
  is_recurring INTEGER NOT NULL DEFAULT 0,
  recurrence TEXT
);
CREATE TABLE IF NOT EXISTS txn_links(
  a INTEGER NOT NULL REFERENCES transactions(id),
  b INTEGER NOT NULL REFERENCES transactions(id),
  match_kind TEXT,
  status TEXT NOT NULL DEFAULT 'pending',  -- pending|confirmed|rejected
  PRIMARY KEY(a, b)
);

-- Phase 4 hooks (DDL now, no code)
CREATE TABLE IF NOT EXISTS events(
  id INTEGER PRIMARY KEY,
  entity_id INTEGER REFERENCES entities(id),
  event_type TEXT,
  occurred_at TEXT,
  data TEXT
);
CREATE TABLE IF NOT EXISTS series(
  series_id TEXT NOT NULL,
  ts TEXT NOT NULL,
  value REAL NOT NULL,
  PRIMARY KEY(series_id, ts)
);
"""


def connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def vec_names(dims: int) -> tuple[str, str]:
    """vec0 tables are fixed-dimension, so they are named per dims."""
    return f"vec_chunks_{dims}", f"vec_staging_{dims}"


def ensure_vec_tables(conn: sqlite3.Connection, dims: int) -> None:
    import sqlite_vec

    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    live, staging = vec_names(dims)
    conn.execute(f"CREATE VIRTUAL TABLE IF NOT EXISTS {live} USING vec0(embedding float[{dims}])")
    conn.execute(f"CREATE VIRTUAL TABLE IF NOT EXISTS {staging} USING vec0(embedding float[{dims}])")
    conn.commit()


def run_vec_names(conn: sqlite3.Connection, run_id: int) -> tuple[str, str]:
    row = conn.execute(
        "SELECT m.dims AS d FROM index_meta m JOIN index_runs r ON r.generation_id = m.id WHERE r.id=?",
        (run_id,),
    ).fetchone()
    if not row:
        raise ValueError(f"unknown run {run_id}")
    return vec_names(row["d"])


def init_db(conn: sqlite3.Connection, *, dims: int, embed_model: str, embed_digest: str) -> int:
    """Create tables (vec dims fixed at creation) and the first index_meta row. Returns generation id."""
    conn.executescript(SCHEMA)
    ensure_vec_tables(conn, dims)
    conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS fts_chunks USING fts5(text)")
    conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS fts_staging USING fts5(text)")
    conn.commit()
    return get_or_create_generation(
        conn, parser_version=PARSER_VERSION, chunk_config=CHUNK_CONFIG,
        embed_model=embed_model, embed_digest=embed_digest, dims=dims,
    )


def get_or_create_generation(
    conn: sqlite3.Connection, *, parser_version: str, chunk_config: str,
    embed_model: str, embed_digest: str, dims: int,
) -> int:
    row = conn.execute(
        "SELECT id FROM index_meta ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if row and tuple(
        conn.execute(
            "SELECT parser_version, chunk_config, embed_model, embed_digest, dims FROM index_meta WHERE id=?",
            (row["id"],),
        ).fetchone()
    ) == (parser_version, chunk_config, embed_model, embed_digest, dims):
        return row["id"]
    cur = conn.execute(
        "INSERT INTO index_meta(parser_version, chunk_config, embed_model, embed_digest, dims) VALUES(?,?,?,?,?)",
        (parser_version, chunk_config, embed_model, embed_digest, dims),
    )
    conn.commit()
    return cur.lastrowid


def generation_mismatch(
    conn: sqlite3.Connection, *, parser_version: str, chunk_config: str,
    embed_model: str, embed_digest: str, dims: int,
) -> bool:
    """True when live index generation differs from config (retrieval must refuse)."""
    gens = [
        tuple(g)
        for g in conn.execute(
            """
            SELECT DISTINCT m.parser_version, m.chunk_config, m.embed_model, m.embed_digest, m.dims
            FROM index_meta m
            JOIN index_runs r ON r.generation_id = m.id
            JOIN files f ON f.current_run_id = r.id
            WHERE f.status != 'removed'
            """
        )
    ]
    if not gens:
        return False  # fresh index, nothing live yet
    return gens != [(parser_version, chunk_config, embed_model, embed_digest, dims)]


def queryable_run_ids(conn: sqlite3.Connection) -> list[int]:
    return [
        row["current_run_id"]
        for row in conn.execute(
            "SELECT current_run_id FROM files WHERE status != 'removed' AND current_run_id IS NOT NULL"
        )
    ]


def set_run_state(conn: sqlite3.Connection, run_id: int, state: str, chunk_count: int | None = None) -> None:
    conn.execute(
        "UPDATE index_runs SET state=?, updated_at=datetime('now'), chunk_count=COALESCE(?, chunk_count) WHERE id=?",
        (state, chunk_count, run_id),
    )
    conn.commit()


def _evict_live_rows(conn: sqlite3.Connection, run_id: int) -> None:
    """Remove a run's rows from live vec/fts (chunks/graph rows stay; GC decides later)."""
    live, _ = run_vec_names(conn, run_id)
    conn.execute(f"DELETE FROM {live} WHERE rowid IN (SELECT id FROM chunks WHERE run_id=?)", (run_id,))
    conn.execute("DELETE FROM fts_chunks WHERE rowid IN (SELECT id FROM chunks WHERE run_id=?)", (run_id,))


def _orphaned(conn: sqlite3.Connection, run_id: int, *, excluding_file: int | None = None) -> bool:
    """True when no valid file (other than excluded) still references this run."""
    sql = "SELECT COUNT(*) FROM files WHERE current_run_id=? AND status != 'removed'"
    params: list = [run_id]
    if excluding_file is not None:
        sql += " AND id != ?"
        params.append(excluding_file)
    return conn.execute(sql, params).fetchone()[0] == 0


def switch_run(conn: sqlite3.Connection, file_id: int, new_run_id: int) -> None:
    """Atomic switch: promote staging rows, flip the pointer, evict the old run if orphaned.

    Single BEGIN IMMEDIATE transaction — also the write lock that serializes
    ingest switches against backup snapshots and GC.

    Staging rows are DURABLE (vectors live there once embedded); live tables are the
    queryable projection. An evicted run keeps its staging rows, so a later file with
    the same content can re-promote it without re-embedding.
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        old = conn.execute("SELECT current_run_id FROM files WHERE id=?", (file_id,)).fetchone()
        old_run_id = old["current_run_id"] if old else None

        chunk_sel = "SELECT id FROM chunks WHERE run_id=?"
        live, staging = run_vec_names(conn, new_run_id)
        conn.execute(
            f"INSERT INTO {live}(rowid, embedding) SELECT rowid, embedding FROM {staging} "
            f"WHERE rowid IN ({chunk_sel}) AND rowid NOT IN (SELECT rowid FROM {live})",
            (new_run_id,),
        )
        conn.execute(
            f"INSERT INTO fts_chunks(rowid, text) SELECT rowid, text FROM fts_staging "
            f"WHERE rowid IN ({chunk_sel}) AND rowid NOT IN (SELECT rowid FROM fts_chunks)",
            (new_run_id,),
        )

        conn.execute(
            "UPDATE files SET current_run_id=?, status='embedded', updated_at=datetime('now') WHERE id=?",
            (new_run_id, file_id),
        )

        if old_run_id and old_run_id != new_run_id and _orphaned(conn, old_run_id, excluding_file=file_id):
            _evict_live_rows(conn, old_run_id)
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def remove_file(conn: sqlite3.Connection, file_id: int) -> None:
    """Remove a SOURCE (not its content): mark removed, evict its run if orphaned."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        old = conn.execute("SELECT current_run_id FROM files WHERE id=?", (file_id,)).fetchone()
        conn.execute("UPDATE files SET status='removed', updated_at=datetime('now') WHERE id=?", (file_id,))
        if old and old["current_run_id"] and _orphaned(conn, old["current_run_id"]):
            _evict_live_rows(conn, old["current_run_id"])
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def delete_run_rows(conn: sqlite3.Connection, run_id: int) -> None:
    """Delete every index artifact of a run: chunks, live+staging vec/fts, graph, transactions, the run row."""
    live, staging = run_vec_names(conn, run_id)
    conn.execute("DELETE FROM chunk_entities WHERE chunk_id IN (SELECT id FROM chunks WHERE run_id=?)", (run_id,))
    conn.execute("DELETE FROM relations WHERE run_id=?", (run_id,))
    conn.execute("DELETE FROM transactions WHERE run_id=?", (run_id,))
    conn.execute(f"DELETE FROM {live} WHERE rowid IN (SELECT id FROM chunks WHERE run_id=?)", (run_id,))
    conn.execute("DELETE FROM fts_chunks WHERE rowid IN (SELECT id FROM chunks WHERE run_id=?)", (run_id,))
    conn.execute(f"DELETE FROM {staging} WHERE rowid IN (SELECT id FROM chunks WHERE run_id=?)", (run_id,))
    conn.execute("DELETE FROM fts_staging WHERE rowid IN (SELECT id FROM chunks WHERE run_id=?)", (run_id,))
    conn.execute("DELETE FROM chunks WHERE run_id=?", (run_id,))
    conn.execute("DELETE FROM index_runs WHERE id=?", (run_id,))


def purge_version(conn: sqlite3.Connection, version_id: int) -> list[str]:
    """Purge CONTENT entirely. Refuses while any file references it or a run is in-flight.
    Returns blob paths the caller should unlink from disk."""
    refs = conn.execute(
        """
        SELECT f.id, f.source_path FROM files f
        JOIN index_runs r ON r.id = f.current_run_id
        WHERE r.version_id = ? AND f.status != 'removed'
        """,
        (version_id,),
    ).fetchall()
    if refs:
        paths = ", ".join(r["source_path"] for r in refs)
        raise ValueError(f"version {version_id} still referenced by files: {paths}")

    in_flight = conn.execute(
        "SELECT COUNT(*) FROM index_runs WHERE version_id=? AND state NOT IN ('embedded', 'extracted', 'extract_failed', 'error')",
        (version_id,),
    ).fetchone()[0]
    if in_flight:
        raise ValueError(f"version {version_id} has in-flight index runs; retry after they settle")

    conn.execute("BEGIN IMMEDIATE")
    try:
        # removed files tied to this content become tombstones (pointers cleared, row kept)
        # so future scans never re-import the purged source; retrieval ignores them.
        conn.execute(
            "UPDATE files SET current_run_id=NULL, current_version_id=NULL "
            "WHERE status='removed' AND (current_run_id IN "
            "(SELECT id FROM index_runs WHERE version_id=?) OR current_version_id=?)",
            (version_id, version_id),
        )
        run_ids = [r["id"] for r in conn.execute("SELECT id FROM index_runs WHERE version_id=?", (version_id,))]
        for run_id in run_ids:
            delete_run_rows(conn, run_id)
        blob_paths = [
            r["blob_path"]
            for r in conn.execute("SELECT blob_path FROM file_versions WHERE id=?", (version_id,))
        ]
        conn.execute("DELETE FROM file_versions WHERE id=?", (version_id,))
        conn.commit()
        return blob_paths
    except Exception:
        conn.rollback()
        raise
