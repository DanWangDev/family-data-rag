"""ingest.py — scan sources, blob store, versioned indexing, lifecycle CLI.

python -m ingest scan|parse|status|remove <path>|purge <sha>|gc|backup|restore-test [dir]
"""

import argparse
import hashlib
import json
import re
import shutil
import sqlite3
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path

import db
import models
import parsers
import textutil
from config import Config

EMBED_BATCH = 32
CHUNK_TARGET = 1000
CHUNK_OVERLAP = 150
TERMINAL = ("embedded", "extracted", "extract_failed", "error")


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _log(msg: str) -> None:
    print(msg, file=sys.stderr)


def _log_failure(cfg: Config, label: str, exc: Exception) -> None:
    """stderr gets a classification only; the raw exception (which may embed document
    content, URLs, or server text) goes to a protected diagnostics file inside data/."""
    _log(f"{label} ({type(exc).__name__})")
    try:
        diag = cfg.db_path.parent / "diagnostics.log"
        with open(diag, "a", encoding="utf-8") as f:
            f.write(f"{now_iso()} {label}\n{traceback.format_exc()}\n")
    except OSError:
        pass  # never let diagnostics logging break the pipeline


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def store_blob(src: Path, blob_dir: Path, sha: str, ext: str) -> Path:
    """Content-addressed copy; path built ONLY from validated sha + sanitized ext (traversal guard)."""
    if not re.fullmatch(r"[0-9a-f]{64}", sha):
        raise ValueError(f"invalid sha256: {sha!r}")
    if not ext or not re.fullmatch(r"[A-Za-z0-9.]+", ext):
        raise ValueError(f"invalid ext: {ext!r}")
    target = blob_dir / f"{sha}.{ext.lower()}"
    if not target.exists():
        blob_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, target)
    return target.resolve()


def _file_meta(path: Path) -> str:
    st = path.stat()
    return json.dumps({"size_bytes": st.st_size, "source_mtime_ns": st.st_mtime_ns})


def _meta_matches(meta: str | None, path: Path) -> bool:
    if not meta:
        return False
    try:
        d = json.loads(meta)
    except ValueError:
        return False
    st = path.stat()
    return d.get("size_bytes") == st.st_size and d.get("source_mtime_ns") == st.st_mtime_ns


def _chunk(parsed: parsers.Parsed) -> tuple[list[str], list[int | None], list[str], list[str | None]]:
    """Split segments into chunks; body segments get exact char_range offsets into the page text."""
    texts: list[str] = []
    pages: list[int | None] = []
    kinds: list[str] = []
    positions: list[str | None] = []
    for seg in parsed.segments:
        spans = textutil.split_text_spans(seg.text, target=CHUNK_TARGET, overlap=CHUNK_OVERLAP)
        for start, end in spans:
            part = seg.text[start:end]
            if not part.strip():
                continue
            if seg.position and "page" in seg.position and seg.kind == "body":
                pos = {"page": seg.page, "char_range": [start, end]}
            else:
                pos = seg.position
            texts.append(part)
            pages.append(seg.page)
            kinds.append(seg.kind)
            positions.append(json.dumps(pos) if pos else None)
    return texts, pages, kinds, positions


def _embed(embed_fn, model: str, texts: list[str]) -> list[list[float]]:
    out: list[list[float]] = []
    for i in range(0, len(texts), EMBED_BATCH):
        out.extend(embed_fn(model, texts[i : i + EMBED_BATCH]))
    return out


def _insert_chunks(
    conn, run_id: int, texts: list[str], pages: list[int | None],
    kinds: list[str], positions: list[str | None], embs: list[list[float]],
) -> None:
    _, staging = db.run_vec_names(conn, run_id)
    conn.execute("BEGIN IMMEDIATE")
    try:
        for seq, (text, page, kind, pos, emb) in enumerate(zip(texts, pages, kinds, positions, embs)):
            cur = conn.execute(
                "INSERT INTO chunks(run_id, seq, text, page, kind, position) VALUES(?,?,?,?,?,?)",
                (run_id, seq, text, page, kind, pos),
            )
            cid = cur.lastrowid
            conn.execute(f"INSERT INTO {staging}(rowid, embedding) VALUES(?,?)", (cid, json.dumps(emb)))
            conn.execute("INSERT INTO fts_staging(rowid, text) VALUES(?,?)", (cid, textutil.segment(text)))
        conn.execute("UPDATE index_runs SET chunk_count=?, updated_at=datetime('now') WHERE id=?", (len(texts), run_id))
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def build_index(
    conn, cfg: Config, file_id: int, version_id: int,
    *, embed_fn=None, dims: int | None = None, digest: str | None = None,
) -> None:
    """Index a version for a file; reuses an existing run of this version+generation;
    on any failure the old live index is untouched."""
    embed_fn = embed_fn or models.embed_batch
    if dims is None:
        dims = models.embed_dims(cfg.models.embed)
    if digest is None:
        digest = models.model_digest(cfg.models.embed)
    gen = db.get_or_create_generation(
        conn, parser_version=db.PARSER_VERSION, chunk_config=db.CHUNK_CONFIG,
        embed_model=cfg.models.embed, embed_digest=digest, dims=dims,
    )

    existing = conn.execute(
        "SELECT id FROM index_runs WHERE version_id=? AND generation_id=? AND state IN ('embedded', 'extracted', 'extract_failed')",
        (version_id, gen),
    ).fetchone()
    if existing:  # same content already indexed: re-point (re-promotes from durable staging if evicted)
        current = conn.execute("SELECT current_run_id FROM files WHERE id=?", (file_id,)).fetchone()
        if current and current["current_run_id"] == existing["id"]:
            return  # idempotent
        db.switch_run(conn, file_id, existing["id"])
        return

    cur = conn.execute("INSERT INTO index_runs(version_id, generation_id, state) VALUES(?,?, 'chunking')", (version_id, gen))
    run_id = cur.lastrowid
    conn.commit()
    blob_row = None
    try:
        blob_row = conn.execute("SELECT blob_path FROM file_versions WHERE id=?", (version_id,)).fetchone()
        parsed = parsers.parse(Path(blob_row["blob_path"]))
        if parsed.exif_date:
            conn.execute("UPDATE files SET exif_date=? WHERE id=?", (parsed.exif_date, file_id))
            conn.commit()
        texts, pages, kinds, positions = _chunk(parsed)
        embs = _embed(embed_fn, cfg.models.embed, texts)
        if not embs:
            raise ValueError("no chunks produced")
        if len(embs[0]) != dims:
            raise ValueError(f"embedding dims mismatch: got {len(embs[0])}, generation expects {dims}")
        _insert_chunks(conn, run_id, texts, pages, kinds, positions, embs)
        db.set_run_state(conn, run_id, "embedded", chunk_count=len(texts))
        db.switch_run(conn, file_id, run_id)
    except Exception as e:
        _log_failure(cfg, f"index failed: file {file_id} run {run_id}", e)
        db.set_run_state(conn, run_id, "error")


def scan(conn, cfg: Config, *, embed_fn=None, dims: int | None = None, digest: str | None = None) -> None:
    cfg.blob_dir.mkdir(parents=True, exist_ok=True)
    stamp = now_iso()
    for src_dir in cfg.source_dirs:
        if not src_dir.is_dir():
            conn.execute(
                "INSERT INTO sources(path, status) VALUES(?, 'missing_on_share') "
                "ON CONFLICT(path) DO UPDATE SET status='missing_on_share'",
                (str(src_dir),),
            )
            conn.commit()
            _log(f"share offline, skipping: {src_dir}")
            continue
        conn.execute(
            "INSERT INTO sources(path, status) VALUES(?, 'active') ON CONFLICT(path) DO UPDATE SET status='active'",
            (str(src_dir),),
        )
        conn.commit()
        for path in src_dir.rglob("*"):
            if not path.is_file() or path.name.startswith(("~$", ".")):
                continue
            ext = path.suffix.lower()
            if ext in parsers.SKIP_EXTS:
                _log(f"skipped (macro format): {path}")
                continue
            if ext not in parsers.WHITELIST:
                continue
            size = path.stat().st_size
            if size == 0 or size > cfg.max_file_mb * 1024 * 1024:
                continue
            _scan_one(conn, cfg, path, stamp, embed_fn, dims, digest)
        # files under this share not seen this scan -> deleted on NAS -> keep index, mark
        conn.execute(
            "UPDATE files SET status='missing_on_source', updated_at=datetime('now') "
            "WHERE scan_seen_at IS NOT NULL AND scan_seen_at != ? AND status != 'removed' AND source_path LIKE ?",
            (stamp, str(src_dir) + "\\%"),
        )
        conn.commit()


def _scan_one(conn, cfg: Config, path: Path, stamp: str, embed_fn, dims, digest) -> None:
    row = conn.execute("SELECT * FROM files WHERE source_path=?", (str(path),)).fetchone()
    if row and _meta_matches(row["meta"], path):
        conn.execute("UPDATE files SET scan_seen_at=? WHERE id=?", (stamp, row["id"]))
        conn.commit()
        return
    sha = sha256_of(path)
    vrow = conn.execute("SELECT id FROM file_versions WHERE sha256=?", (sha,)).fetchone()
    if vrow:
        version_id = vrow["id"]
    else:
        blob_path = store_blob(path, cfg.blob_dir, sha, path.suffix.lower())
        cur = conn.execute(
            "INSERT INTO file_versions(sha256, size_bytes, blob_path) VALUES(?,?,?)",
            (sha, path.stat().st_size, str(blob_path)),
        )
        version_id = cur.lastrowid
    if row:
        changed = row["current_version_id"] != version_id
        conn.execute(
            "UPDATE files SET current_version_id=?, scan_seen_at=?, meta=?, "
            "status=CASE WHEN ? THEN 'copied' ELSE status END, updated_at=datetime('now') WHERE id=?",
            (version_id, stamp, _file_meta(path), changed, row["id"]),
        )
        file_id = row["id"]
    else:
        cur = conn.execute(
            "INSERT INTO files(source_path, name, kind, ext, current_version_id, scan_seen_at, meta) "
            "VALUES(?,?,?,?,?,?,?)",
            (str(path), path.name, parsers.KIND_BY_EXT[path.suffix.lower()], path.suffix.lower(), version_id, stamp, _file_meta(path)),
        )
        file_id = cur.lastrowid
    conn.commit()
    if not row or changed:
        build_index(conn, cfg, file_id, version_id, embed_fn=embed_fn, dims=dims, digest=digest)


def rebuild(conn, cfg: Config, *, embed_fn=None, dims: int | None = None, digest: str | None = None) -> None:
    """Re-index every file under a NEW generation.

    Two-tiered per plan: embedding-only change reuses existing chunks (re-embed, no
    re-parse); parser/chunker change re-parses from the blob. Old indexes stay live
    until each file's switch succeeds.
    """
    embed_fn = embed_fn or models.embed_batch
    if dims is None:
        dims = models.embed_dims(cfg.models.embed)
    if digest is None:
        digest = models.model_digest(cfg.models.embed)
    db.ensure_vec_tables(conn, dims)  # new dims need their own vec tables
    gen = db.get_or_create_generation(
        conn, parser_version=db.PARSER_VERSION, chunk_config=db.CHUNK_CONFIG,
        embed_model=cfg.models.embed, embed_digest=digest, dims=dims,
    )
    for frow in conn.execute(
        "SELECT id, current_version_id FROM files WHERE current_version_id IS NOT NULL AND status != 'removed'"
    ):
        version_id = frow["current_version_id"]
        cur_run = conn.execute("SELECT current_run_id FROM files WHERE id=?", (frow["id"],)).fetchone()
        if cur_run and cur_run["current_run_id"]:
            cur_gen = conn.execute(
                "SELECT generation_id FROM index_runs WHERE id=?", (cur_run["current_run_id"],)
            ).fetchone()
            if cur_gen and cur_gen["generation_id"] == gen:
                continue  # already on the new generation
        existing = conn.execute(
            "SELECT id FROM index_runs WHERE version_id=? AND generation_id=? AND state IN ('embedded', 'extracted', 'extract_failed')",
            (version_id, gen),
        ).fetchone()
        if existing:  # another file already rebuilt this content under the new generation
            db.switch_run(conn, frow["id"], existing["id"])
            continue
        src = conn.execute(
            """
            SELECT r.id FROM index_runs r JOIN index_meta m ON m.id = r.generation_id
            WHERE r.version_id=? AND m.parser_version=? AND m.chunk_config=?
              AND r.state IN ('embedded', 'extracted', 'extract_failed')
            ORDER BY r.id DESC LIMIT 1
            """,
            (version_id, db.PARSER_VERSION, db.CHUNK_CONFIG),
        ).fetchone()
        if not src:
            build_index(conn, cfg, frow["id"], version_id, embed_fn=embed_fn, dims=dims, digest=digest)
            continue
        _rebuild_from_chunks(conn, cfg, frow["id"], version_id, gen, src["id"], embed_fn, dims)


def _rebuild_from_chunks(conn, cfg: Config, file_id: int, version_id: int, gen: int,
                         src_run_id: int, embed_fn, dims: int) -> None:
    """Embedding-only rebuild: copy chunks from a prior run, re-embed, stage, switch."""
    cur = conn.execute("INSERT INTO index_runs(version_id, generation_id, state) VALUES(?,?, 'embedding')", (version_id, gen))
    run_id = cur.lastrowid
    conn.commit()
    try:
        rows = conn.execute("SELECT seq, text, page, kind, position FROM chunks WHERE run_id=? ORDER BY seq", (src_run_id,)).fetchall()
        if not rows:
            raise ValueError("source run has no chunks")
        texts = [r["text"] for r in rows]
        embs = _embed(embed_fn, cfg.models.embed, texts)
        if len(embs[0]) != dims:
            raise ValueError(f"embedding dims mismatch: got {len(embs[0])}, generation expects {dims}")
        _insert_chunks(
            conn, run_id, texts,
            [r["page"] for r in rows], [r["kind"] for r in rows], [r["position"] for r in rows], embs,
        )
        db.set_run_state(conn, run_id, "embedded", chunk_count=len(texts))
        db.switch_run(conn, file_id, run_id)
    except Exception as e:
        _log_failure(cfg, f"rebuild failed: file {file_id} run {run_id}", e)
        db.set_run_state(conn, run_id, "error")


def parse_all(conn, cfg: Config, *, embed_fn=None, dims=None, digest=None) -> None:
    """Retry files whose current content has no successful current run (recovery after failures)."""
    rows = conn.execute(
        """
        SELECT f.id AS file_id, f.current_version_id AS version_id
        FROM files f
        WHERE f.current_version_id IS NOT NULL AND f.status != 'removed'
          AND NOT EXISTS (
            SELECT 1 FROM index_runs r
            WHERE r.id = f.current_run_id AND r.version_id = f.current_version_id
              AND r.state IN ('embedded', 'extracted')
          )
        """
    ).fetchall()
    for row in rows:
        build_index(conn, cfg, row["file_id"], row["version_id"], embed_fn=embed_fn, dims=dims, digest=digest)


def remove_source(conn, path: str) -> None:
    row = conn.execute("SELECT id FROM files WHERE source_path=?", (path,)).fetchone()
    if not row:
        raise ValueError(f"not indexed: {path}")
    db.remove_file(conn, row["id"])


def purge_content(conn, blob_dir: Path, sha: str) -> None:
    row = conn.execute("SELECT id FROM file_versions WHERE sha256=?", (sha,)).fetchone()
    if not row:
        raise ValueError(f"unknown sha256: {sha}")
    for blob in db.purge_version(conn, row["id"]):
        Path(blob).unlink(missing_ok=True)


def backup(conn, blob_dir: Path, dest: Path | None = None) -> Path:
    """WAL-safe snapshot via the SQLite Backup API + manifest of referenced blobs.

    Blobs copy into dest/blobs only when missing (append to the backup chain); the
    manifest ties the snapshot to the chain and restore_test verifies every SHA.
    """
    dest = dest or (blob_dir.parent / "backups")
    dest.mkdir(parents=True, exist_ok=True)
    lock = dest / "backup.lock"
    try:
        lock.touch(exist_ok=False)
    except FileExistsError:
        raise RuntimeError("a backup is already running")
    try:
        snap = dest / f"family-{datetime.now().strftime('%Y%m%d-%H%M%S')}.db"
        target = sqlite3.connect(snap)
        conn.backup(target)
        target.close()
        blob_store = dest / "blobs"
        blob_store.mkdir(exist_ok=True)
        manifest = {"snapshot": snap.name, "blobs": []}
        for b in conn.execute("SELECT blob_path FROM file_versions"):
            src = Path(b["blob_path"])
            dst = blob_store / src.name
            if not dst.exists():
                shutil.copy2(src, dst)
            manifest["blobs"].append(src.name)
        (dest / f"{snap.name}.manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        return snap
    finally:
        lock.unlink(missing_ok=True)


def restore_test(backup_dir: Path) -> list[str]:
    """Verify the newest snapshot: DB opens, index tables readable, every manifest blob
    present with matching SHA. Empty list = restore would work."""
    problems: list[str] = []
    manifests = sorted(backup_dir.glob("*.manifest.json"))
    if not manifests:
        return ["no manifest found"]
    manifest = json.loads(manifests[-1].read_text(encoding="utf-8"))
    snap = backup_dir / manifest["snapshot"]
    if not snap.exists():
        return [f"snapshot missing: {snap}"]
    try:
        rconn = sqlite3.connect(f"file:{snap}?mode=ro", uri=True)
        for table in ("files", "file_versions", "index_runs", "chunks"):
            rconn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
        rconn.close()
    except Exception as e:
        problems.append(f"snapshot unreadable: {e}")
    for name in manifest["blobs"]:
        p = backup_dir / "blobs" / name
        if not p.exists():
            problems.append(f"blob missing: {name}")
            continue
        if sha256_of(p) != name.split(".")[0]:
            problems.append(f"blob hash mismatch: {name}")
    return problems


def gc(conn, blob_dir: Path) -> int:
    """Collect unreferenced runs/versions/chunks/blobs. Terminal-state runs only; the
    BEGIN IMMEDIATE lock keeps this out of concurrent switches, backups, and builds."""
    # ponytail: gc only checks the DEFAULT backups dir for an open backup lock; a
    # custom backup dest bypasses the check — share one dest until this matters.
    if (blob_dir.parent / "backups" / "backup.lock").exists():
        raise RuntimeError("a backup is running — GC refused")
    conn.execute("BEGIN IMMEDIATE")
    try:
        # removed rows become tombstones: pointers cleared so orphan detection can collect
        # their runs/versions, but the rows stay so scans never re-import those sources.
        conn.execute("UPDATE files SET current_run_id=NULL, current_version_id=NULL WHERE status='removed'")
        orphans = [
            r["id"]
            for r in conn.execute(
                """
                SELECT r.id FROM index_runs r
                LEFT JOIN files f ON f.current_run_id = r.id
                WHERE f.id IS NULL AND r.state IN ('embedded', 'extracted', 'extract_failed', 'error')
                """
            )
        ]
        for run_id in orphans:
            db.delete_run_rows(conn, run_id)
        blob_candidates = [
            r["blob_path"]
            for r in conn.execute(
                """
                SELECT v.blob_path FROM file_versions v
                WHERE NOT EXISTS (SELECT 1 FROM index_runs r WHERE r.version_id = v.id)
                  AND NOT EXISTS (SELECT 1 FROM files f WHERE f.current_version_id = v.id)
                """
            )
        ]
        conn.execute(
            """
            DELETE FROM file_versions
            WHERE NOT EXISTS (SELECT 1 FROM index_runs r WHERE r.version_id = file_versions.id)
              AND NOT EXISTS (SELECT 1 FROM files f WHERE f.current_version_id = file_versions.id)
            """
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    removed = 0
    for blob in blob_candidates:
        p = Path(blob)
        if p.exists():
            p.unlink()
            removed += 1
    return removed


def status(conn) -> None:
    for row in conn.execute("SELECT status, COUNT(*) AS n FROM files GROUP BY status ORDER BY status"):
        print(f"files {row['status']}: {row['n']}")
    vec_live = sum(
        conn.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
        for (name,) in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name GLOB 'vec_chunks_[0-9]*' AND name NOT GLOB 'vec_chunks_[0-9]*_*'"
        )
    )
    for label, sql in (
        ("versions", "SELECT COUNT(*) FROM file_versions"),
        ("runs", "SELECT COUNT(*) FROM index_runs"),
        ("chunks", "SELECT COUNT(*) FROM chunks"),
        ("live vectors", f"SELECT {vec_live}"),
    ):
        print(f"{label}: {conn.execute(sql).fetchone()[0]}")


def main(argv=None) -> None:
    p = argparse.ArgumentParser(prog="ingest")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("scan")
    sub.add_parser("parse")
    sub.add_parser("status")
    r = sub.add_parser("remove")
    r.add_argument("path")
    pu = sub.add_parser("purge")
    pu.add_argument("sha256")
    sub.add_parser("gc")
    sub.add_parser("backup")
    sub.add_parser("rebuild")
    sub.add_parser("extract")
    rt = sub.add_parser("restore-test")
    rt.add_argument("backup_dir", nargs="?")
    args = p.parse_args(argv)

    cfg = Config.load()
    cfg.db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = db.connect(str(cfg.db_path))
    db.init_db(
        conn, dims=models.embed_dims(cfg.models.embed),
        embed_model=cfg.models.embed, embed_digest=models.model_digest(cfg.models.embed),
    )
    if args.cmd == "scan":
        scan(conn, cfg)
    elif args.cmd == "parse":
        parse_all(conn, cfg)
    elif args.cmd == "status":
        status(conn)
    elif args.cmd == "remove":
        remove_source(conn, args.path)
    elif args.cmd == "purge":
        purge_content(conn, cfg.blob_dir, args.sha256)
    elif args.cmd == "gc":
        print(f"gc: removed {gc(conn, cfg.blob_dir)} blobs")
    elif args.cmd == "backup":
        print(f"backup: {backup(conn, cfg.blob_dir)}")
    elif args.cmd == "rebuild":
        rebuild(conn, cfg)
    elif args.cmd == "extract":
        import extract

        print(f"extract: processed {extract.extract_all(conn, cfg)} runs")
    elif args.cmd == "restore-test":
        dest = Path(args.backup_dir) if args.backup_dir else cfg.blob_dir.parent / "backups"
        problems = restore_test(dest)
        if problems:
            for prob in problems:
                _log(f"restore problem: {prob}")
        else:
            print("restore-test: OK")


if __name__ == "__main__":
    main()
