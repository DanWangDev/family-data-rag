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
                "INSERT INTO vec_staging(rowid, embedding) VALUES(?,?)",
                (cid, json.dumps([0.1, 0.2, 0.3, 0.4])),
            )
            conn.execute("INSERT INTO fts_staging(rowid, text) VALUES(?,?)", (cid, f"chunk {base + i}"))
        conn.commit()

    def live_counts() -> tuple[int, int]:
        return (
            conn.execute("SELECT COUNT(*) FROM vec_chunks").fetchone()[0],
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

    # scenario 4 (failed switch): FK error -> transaction rolls back, old index intact
    try:
        db.switch_run(conn, a, 9999)
        raise AssertionError("switch to nonexistent run should raise")
    except sqlite3.IntegrityError:
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
    db.remove_file(conn, c)
    # removed files keep their pointer as audit trail; GC clears their rows first, then purge proceeds
    conn.execute("DELETE FROM files WHERE status='removed'")
    conn.commit()
    db.purge_version(conn, v2)


def main() -> None:
    check_dep_imports()
    check_sqlite_vec_roundtrip()
    check_ocr_roundtrip()
    check_db_switch_contract()
    print("selftest: all checks passed")


if __name__ == "__main__":
    main()
