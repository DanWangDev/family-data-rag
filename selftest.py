"""Assert-based self-checks, model-free. CI runs `python -m selftest`.

Grows with the codebase; keep every check here runnable without a GPU,
Ollama server, or real family data.
"""

import json
import sqlite3


def check_dep_imports() -> None:
    import jieba
    import ollama
    import openpyxl
    import onnxruntime
    import PIL
    import plotly
    import pymupdf  # pymupdf (fitz API deprecated)
    import rapidocr
    import sqlite_vec
    import streamlit
    from docx import Document  # python-docx

    # silence "imported but unused" style lints; this check is about importability
    assert all(
        x is not None
        for x in (jieba, ollama, openpyxl, onnxruntime, PIL, plotly, pymupdf, rapidocr, sqlite_vec, streamlit, Document)
    )


def check_sqlite_vec_roundtrip() -> None:
    db = sqlite3.connect(":memory:")
    db.enable_load_extension(True)
    import sqlite_vec

    sqlite_vec.load(db)
    db.enable_load_extension(False)

    db.execute("CREATE VIRTUAL TABLE vec USING vec0(embedding float[4])")
    rows = [(1, json.dumps([0.1, 0.2, 0.3, 0.4])), (2, json.dumps([0.9, 0.8, 0.7, 0.6]))]
    db.executemany("INSERT INTO vec(rowid, embedding) VALUES (?, ?)", rows)
    got = db.execute(
        "SELECT rowid, distance FROM vec WHERE embedding MATCH ? ORDER BY distance LIMIT 1",
        (json.dumps([0.1, 0.2, 0.3, 0.4]),),
    ).fetchall()
    assert got[0][0] == 1, f"kNN roundtrip failed: {got}"


def check_ocr_roundtrip() -> None:
    import numpy as np
    from PIL import Image, ImageDraw
    from rapidocr import RapidOCR

    img = Image.new("RGB", (320, 100), "white")
    ImageDraw.Draw(img).text((20, 30), "Family 1234", fill="black", font_size=32)
    result = RapidOCR()(np.asarray(img))
    text = " ".join(result.txts)
    assert "Family" in text and "1234" in text, f"unexpected OCR text: {text}"


def check_db_switch_contract() -> None:
    """CI scenarios 1-4, 6, 7: the ingest-correctness suite at the db layer."""
    import db

    conn = db.connect(":memory:")
    gen = db.init_db(conn, dims=4, embed_model="nomic-embed-text", embed_digest="deadbeef")

    def make_version(sha: str) -> int:
        cur = conn.execute(
            "INSERT INTO file_versions(sha256, size_bytes, blob_path) VALUES(?, 10, ?)",
            (sha, f"blob/{sha}.txt"),
        )
        conn.commit()
        return cur.lastrowid

    def make_run(version_id: int) -> int:
        cur = conn.execute(
            "INSERT INTO index_runs(version_id, generation_id, state) VALUES(?,?, 'embedded')",
            (version_id, gen),
        )
        conn.commit()
        return cur.lastrowid

    def make_file(path: str, run_id: int) -> int:
        cur = conn.execute(
            "INSERT INTO files(source_path, name, kind, ext, current_run_id, status) "
            "VALUES(?,?, 'txt', 'txt', ?, 'embedded')",
            (path, path.split("/")[-1], run_id),
        )
        conn.commit()
        return cur.lastrowid

    def stage_chunks(run_id: int, n: int, base: int) -> None:
        for i in range(n):
            cur = conn.execute(
                "INSERT INTO chunks(run_id, seq, text, kind) VALUES(?,?,?,'body')",
                (run_id, i, f"chunk {base + i}"),
            )
            cid = cur.lastrowid
            conn.execute(
                "INSERT INTO vec_staging_4(rowid, embedding) VALUES(?,?)",
                (cid, json.dumps([0.1, 0.2, 0.3, 0.4])),
            )
            conn.execute("INSERT INTO fts_staging(rowid, text) VALUES(?,?)", (cid, f"chunk {base + i}"))
        conn.commit()

    def live_counts() -> tuple[int, int]:
        return (
            conn.execute("SELECT COUNT(*) FROM vec_chunks_4").fetchone()[0],
            conn.execute("SELECT COUNT(*) FROM fts_chunks").fetchone()[0],
        )

    v1, v2 = make_version("a" * 64), make_version("b" * 64)
    r1, r2 = make_run(v1), make_run(v2)
    a = make_file("src/a.txt", r1)
    stage_chunks(r1, 2, base=1)

    # scenario 3 (staging visibility): staged rows are NOT live
    assert live_counts() == (0, 0)

    db.switch_run(conn, a, r1)
    assert live_counts() == (2, 2)

    # scenario 1 (shared-version isolation): B references the same run R1
    b = make_file("src/b.txt", r1)
    stage_chunks(r2, 2, base=3)
    db.switch_run(conn, a, r2)  # A updates; R1 must stay live for B
    assert live_counts() == (4, 4)

    # scenario 4 (failed switch): nonexistent run -> refused up front, old index intact
    try:
        db.switch_run(conn, a, 9999)
        raise AssertionError("switch to nonexistent run should raise")
    except ValueError:
        pass
    assert conn.execute("SELECT current_run_id FROM files WHERE id=?", (a,)).fetchone()[0] == r2
    assert live_counts() == (4, 4)

    # scenario 2 (last-reference eviction): B also updates -> R1 evicted now, before any GC
    db.switch_run(conn, b, r2)
    assert live_counts() == (2, 2)

    # scenario 6 (generation mismatch)
    cfg = dict(parser_version=db.PARSER_VERSION, chunk_config=db.CHUNK_CONFIG,
               embed_model="nomic-embed-text", embed_digest="deadbeef", dims=4)
    assert not db.generation_mismatch(conn, **cfg)
    assert db.generation_mismatch(conn, **{**cfg, "embed_model": "bge-m3", "embed_digest": "cafebabe", "dims": 1024})

    # scenario 7 (remove vs purge)
    db.remove_file(conn, b)  # A still references R2 -> stays live
    assert live_counts() == (2, 2)
    db.remove_file(conn, a)  # last reference -> evicted immediately
    assert live_counts() == (0, 0)

    assert db.purge_version(conn, v1) == [f"blob/{'a' * 64}.txt"]  # no refs, terminal run -> purged
    c = make_file("src/c.txt", r2)
    try:
        db.purge_version(conn, v2)
        raise AssertionError("purge of a referenced version should raise")
    except ValueError:
        pass
    db.remove_file(conn, c)  # removed rows are audit trail, not blockers
    db.purge_version(conn, v2)  # clears removed rows, runs, version in one transaction


def check_ingest_flow() -> None:
    """CI scenarios 1-3 + idempotence + rename dedup + re-promotion + remove/purge/gc at the ingest layer."""
    import tempfile
    from pathlib import Path

    import config
    import db
    import ingest

    fake_cfg = dict(
        max_file_mb=100,
        photo_captions=False,
        models=config.Models(chat="qwen3:14b", extract="qwen3:8b", embed="fake-embed", vision="qwen3-vl:8b"),
    )
    fake_embed = lambda model, texts: [[0.25] * 4 for _ in texts]

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        src = root / "src"
        src.mkdir()
        cfg = config.Config(source_dirs=(src,), db_path=root / "family.db", blob_dir=root / "blob", **fake_cfg)
        conn = db.connect(str(cfg.db_path))
        db.init_db(conn, dims=4, embed_model="fake-embed", embed_digest="deadbeef")

        versions = lambda: conn.execute("SELECT COUNT(*) FROM file_versions").fetchone()[0]
        runs = lambda: conn.execute("SELECT COUNT(*) FROM index_runs").fetchone()[0]
        live_by_run = lambda: {
            r["run_id"]: r["n"]
            for r in conn.execute(
                "SELECT c.run_id, COUNT(*) AS n FROM vec_chunks_4 v JOIN chunks c ON c.id = v.rowid GROUP BY c.run_id"
            )
        }

        def write(name: str, content: str) -> None:
            (src / name).write_text(content, encoding="utf-8")

        def scan() -> None:
            ingest.scan(conn, cfg, embed_fn=fake_embed, dims=4, digest="deadbeef")

        # identical A and B -> one version, one run, both queryable
        write("a.txt", "alpha " * 200)
        write("b.txt", "alpha " * 200)
        scan()
        assert versions() == 1 and runs() == 1, f"v={versions()} r={runs()}"
        r_v1 = next(iter(live_by_run()))
        assert live_by_run() == {r_v1: 2}

        # duplicate import idempotence
        scan()
        assert versions() == 1 and runs() == 1 and live_by_run() == {r_v1: 2}

        # scenario 1: A changes -> B still queryable on V1
        write("a.txt", "beta " * 200)
        scan()
        assert versions() == 2 and runs() == 2
        r_v2 = [r for r in live_by_run() if r != r_v1][0]
        assert set(live_by_run()) == {r_v1, r_v2}
        n_v1 = live_by_run()[r_v1]
        assert live_by_run()[r_v2] > 0

        # scenario 2: B also changes -> V1 evicted immediately, before any GC
        write("b.txt", "gamma " * 200)
        scan()
        assert versions() == 3 and runs() == 3
        r_v3 = [r for r in live_by_run() if r not in (r_v1, r_v2)][0]
        assert set(live_by_run()) == {r_v2, r_v3}

        # rename with identical content -> no new version/run (dedup by content, not path)
        write("c.txt", "gamma " * 200)
        scan()
        assert versions() == 3 and runs() == 3

        # re-promotion: A returns to V1's content -> durable staging rows go live again,
        # no re-embed; A's old run (V2) loses its last reference and is evicted in the same switch
        write("a.txt", "alpha " * 200)
        scan()
        assert versions() == 3 and runs() == 3
        assert set(live_by_run()) == {r_v1, r_v3}
        assert live_by_run()[r_v1] == n_v1

        # remove a source: its run evicts only when it loses its last valid reference
        write("d.txt", "delta " * 200)
        scan()
        assert versions() == 4 and runs() == 4
        r_v4 = [r for r in live_by_run() if r not in (r_v1, r_v3)][0]
        ingest.remove_source(conn, str(src / "d.txt"))
        assert live_by_run() == {r_v1: 2, r_v3: 2}

        # purge content: removed rows are audit trail, not blockers -> succeeds immediately
        sha_d = conn.execute(
            "SELECT v.sha256 FROM file_versions v JOIN files f ON f.current_version_id=v.id WHERE f.source_path=?",
            (str(src / "d.txt"),),
        ).fetchone()[0]
        ingest.purge_content(conn, cfg.blob_dir, sha_d)
        assert versions() == 3 and runs() == 3
        assert live_by_run() == {r_v1: 2, r_v3: 2}

        # gc: collects orphan run V2 (A moved back to V1) and the removed-file version V5
        write("e.txt", "epsilon " * 200)
        scan()
        assert versions() == 4
        ingest.remove_source(conn, str(src / "e.txt"))
        removed_blobs = ingest.gc(conn, cfg.blob_dir)
        assert removed_blobs == 2  # V2's blob + e's blob
        assert versions() == 2 and runs() == 2
        assert live_by_run() == {r_v1: 2, r_v3: 2}

        # scenario 4 (generation reindex): same content under a new generation —
        # embedding-only change reuses chunks; old index stays live until each switch
        fake5 = lambda model, texts: [[0.25] * 5 for _ in texts]
        ingest.rebuild(conn, cfg, embed_fn=fake5, dims=5, digest="other-digest")
        new_runs = [
            r["run_id"]
            for r in conn.execute("SELECT DISTINCT c.run_id FROM vec_chunks_5 v JOIN chunks c ON c.id = v.rowid")
        ]
        assert len(new_runs) == 2  # a (V1) + b/c sharing one V3 run
        assert not live_by_run()  # old generation evicted from vec_chunks_4
        for nr in new_runs:
            assert conn.execute("SELECT chunk_count FROM index_runs WHERE id=?", (nr,)).fetchone()["chunk_count"] == 2

        # failed rebuild keeps the old index live
        def failing_embed(model, texts):
            raise RuntimeError("simulated embed outage")

        ingest.rebuild(conn, cfg, embed_fn=failing_embed, dims=6, digest="broken-digest")
        still = [
            r["run_id"]
            for r in conn.execute("SELECT DISTINCT c.run_id FROM vec_chunks_5 v JOIN chunks c ON c.id = v.rowid")
        ]
        assert still == new_runs

        # missing share: scan logs and continues, nothing is deleted
        cfg2 = config.Config(source_dirs=(root / "nonexistent",), db_path=root / "family.db",
                             blob_dir=root / "blob", **fake_cfg)
        ingest.scan(conn, cfg2, embed_fn=fake_embed, dims=4, digest="deadbeef")
        assert conn.execute(
            "SELECT status FROM sources WHERE path=?", (str(root / "nonexistent"),)
        ).fetchone()[0] == "missing_on_share"
        # untouched: the new generation's live vectors (2 chunks per run, 2 runs)
        assert conn.execute("SELECT COUNT(*) FROM vec_chunks_5").fetchone()[0] == 4
        conn.close()


def check_retrieve_and_chat() -> None:
    """RRF math, hybrid retrieval with citations, generation refusal, prompt contract — model-free."""
    import json
    from pathlib import Path

    import chat
    import config
    import db
    import retrieve

    # RRF ordering on a fixed small input (scores: 2 > 3 > 1 > 4)
    assert retrieve.rrf([[1, 2, 3], [2, 3, 4]]) == [2, 3, 1, 4]

    cfg = config.Config(
        source_dirs=(), db_path="unused", blob_dir=Path("unused"),
        max_file_mb=100, photo_captions=False,
        models=config.Models(chat="qwen3:14b", extract="qwen3:8b", embed="fake-embed", vision="qwen3-vl:8b"),
    )
    fake_embed = lambda model, texts: [[0.25] * 4 for _ in texts]

    conn = db.connect(":memory:")
    gen = db.init_db(conn, dims=4, embed_model="fake-embed", embed_digest="deadbeef")
    cur = conn.execute("INSERT INTO file_versions(sha256, size_bytes, blob_path) VALUES(?, 10, 'blob/x')", ("a" * 64,))
    version_id = cur.lastrowid
    cur = conn.execute("INSERT INTO index_runs(version_id, generation_id, state) VALUES(?,?, 'embedded')", (version_id, gen))
    run_id = cur.lastrowid
    cur = conn.execute(
        "INSERT INTO files(source_path, name, kind, ext, current_version_id, current_run_id, status) "
        "VALUES('src/bills.txt', 'bills.txt', 'txt', 'txt', ?, ?, 'embedded')",
        (version_id, run_id),
    )
    conn.commit()

    texts = ["electricity bill for the house in March", "shopping list for groceries"]
    chunk_ids: list[int] = []
    for seq, text in enumerate(texts):
        cur = conn.execute("INSERT INTO chunks(run_id, seq, text, page, kind) VALUES(?,?,?,?, 'body')", (run_id, seq, text, seq + 1))
        cid = cur.lastrowid
        chunk_ids.append(cid)
        conn.execute("INSERT INTO vec_chunks_4(rowid, embedding) VALUES(?,?)", (cid, json.dumps([0.25] * 4)))
        conn.execute("INSERT INTO fts_chunks(rowid, text) VALUES(?,?)", (cid, "electricity bill house march" if seq == 0 else "shopping list groceries"))
    conn.commit()

    hits = retrieve.retrieve(conn, cfg, "electricity bill", embed_fn=fake_embed, dims=4, digest="deadbeef")
    assert hits, "retrieval returned nothing"
    assert hits[0]["id"] == chunk_ids[0], f"fts/vec should rank the bill chunk first: {hits}"
    assert hits[0]["source_path"] == "src/bills.txt" and hits[0]["name"] == "bills.txt"

    # generation mismatch -> refusal
    try:
        retrieve.retrieve(conn, cfg, "x", embed_fn=fake_embed, dims=8, digest="deadbeef")
        raise AssertionError("retrieval on a mismatched generation should refuse")
    except RuntimeError:
        pass

    # prompt contract: citations present; document content is data, not instructions
    msgs = chat.build_prompt("what is the bill?", hits)
    assert "DATA" in msgs[0]["content"] and "instructions" in msgs[0]["content"]
    assert "[1]" in msgs[1]["content"] and "bills.txt" in msgs[1]["content"]

    # empty-result guard: no chunks -> refusal message, no model call
    real_retrieve = chat.retrieve.retrieve
    chat.retrieve.retrieve = lambda conn, cfg, q, **kw: []
    try:
        empty = chat.ask(conn, cfg, "anything", embed_fn=fake_embed, dims=4, digest="deadbeef")
    finally:
        chat.retrieve.retrieve = real_retrieve
    assert empty["sources"] == [] and "No relevant documents" in empty["answer"]


def check_backup_restore() -> None:
    """Backup snapshot + manifest + SHA-verified restore drill; GC refuses during backup."""
    import tempfile
    from pathlib import Path

    import config
    import db
    import ingest

    fake_cfg = dict(
        max_file_mb=100,
        photo_captions=False,
        models=config.Models(chat="qwen3:14b", extract="qwen3:8b", embed="fake-embed", vision="qwen3-vl:8b"),
    )
    fake_embed = lambda model, texts: [[0.25] * 4 for _ in texts]

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        src = root / "src"
        src.mkdir()
        cfg = config.Config(source_dirs=(src,), db_path=root / "family.db", blob_dir=root / "blob", **fake_cfg)
        conn = db.connect(str(cfg.db_path))
        db.init_db(conn, dims=4, embed_model="fake-embed", embed_digest="deadbeef")
        (src / "a.txt").write_text("alpha " * 200, encoding="utf-8")
        ingest.scan(conn, cfg, embed_fn=fake_embed, dims=4, digest="deadbeef")

        snap = ingest.backup(conn, cfg.blob_dir)  # dest defaults to blob_dir.parent/backups
        backups = root / "backups"
        assert snap.parent == backups and snap.exists()
        manifests = list(backups.glob("*.manifest.json"))
        assert len(manifests) == 1
        manifest = json.loads(manifests[0].read_text(encoding="utf-8"))
        assert manifest["snapshot"] == snap.name
        assert len(manifest["blobs"]) == 1

        # restore drill passes on a healthy backup
        assert ingest.restore_test(backups) == []

        # corrupted blob -> flagged
        blob = backups / "blobs" / manifest["blobs"][0]
        blob.write_bytes(b"corrupted")
        problems = ingest.restore_test(backups)
        assert problems and any("hash mismatch" in p for p in problems)

        # missing blob -> flagged
        blob.unlink()
        problems = ingest.restore_test(backups)
        assert problems and any("missing" in p for p in problems)

        # GC refuses while a backup lock is held
        lock = backups / "backup.lock"
        lock.touch()
        try:
            ingest.gc(conn, cfg.blob_dir)
            raise AssertionError("gc should refuse during a backup")
        except RuntimeError:
            pass
        finally:
            lock.unlink(missing_ok=True)
        conn.close()


def check_client_hardening() -> None:
    """Security review item 1: hostile env (remote host, API key, proxy) must not
    influence the client — pinned loopback, no proxies, no inherited Authorization."""
    import os

    import models

    saved = {k: os.environ.get(k) for k in ("OLLAMA_HOST", "OLLAMA_API_KEY", "HTTP_PROXY")}
    try:
        os.environ["OLLAMA_HOST"] = "http://evil.example.com:9999"
        os.environ["OLLAMA_API_KEY"] = "sk-synthetic-secret-1234"
        os.environ["HTTP_PROXY"] = "http://proxy.example.com:8080"
        client = models._client()  # raises if an Authorization header survives
        assert str(client._client.base_url).startswith("http://127.0.0.1:11434")
        assert client._client.trust_env is False
        assert not any("authorization" in k.lower() for k in client._client.headers)
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def check_error_log_redaction() -> None:
    """Security review item 2: raw exceptions (which may embed document content or
    credentials) never reach stderr; they go to the protected diagnostics file only."""
    import contextlib
    import io
    import tempfile
    from pathlib import Path

    import config
    import ingest

    secret = "sk-synthetic-secret-9999"
    with tempfile.TemporaryDirectory() as td:
        cfg = config.Config(
            source_dirs=(), db_path=Path(td) / "family.db", blob_dir=Path(td) / "blob",
            max_file_mb=100, photo_captions=False,
            models=config.Models(chat="c", extract="e", embed="m", vision="v"),
        )
        buf = io.StringIO()
        try:
            raise RuntimeError(f"boom {secret}")
        except RuntimeError as e:
            with contextlib.redirect_stderr(buf):
                ingest._log_failure(cfg, "index failed: file 1 run 1", e)
        stderr_text = buf.getvalue()
        assert "RuntimeError" in stderr_text  # classified
        assert secret not in stderr_text  # raw content redacted
        diag = (Path(td) / "diagnostics.log").read_text(encoding="utf-8")
        assert secret in diag  # protected local channel keeps the detail


def main() -> None:
    check_dep_imports()
    check_sqlite_vec_roundtrip()
    check_ocr_roundtrip()
    check_db_switch_contract()
    check_ingest_flow()
    check_retrieve_and_chat()
    check_backup_restore()
    check_client_hardening()
    check_error_log_redaction()
    print("selftest: all checks passed")


if __name__ == "__main__":
    main()
