"""Synthetic, model-free fixtures for review regressions."""
import json
from types import SimpleNamespace

import db

CFG = SimpleNamespace(models=SimpleNamespace(extract="fake-extract", embed="fake-embed"))


def database():
    conn = db.connect(":memory:")
    generation = db.init_db(conn, dims=4, embed_model="fake-embed", embed_digest="test-digest")
    return conn, generation


def seed(conn, generation, number, texts, paths=1):
    version = conn.execute(
        "INSERT INTO file_versions(sha256,size_bytes,blob_path) VALUES(?,?,?)",
        (str(number).zfill(64), sum(map(len, texts)), f"synthetic-{number}.txt"),
    ).lastrowid
    run = conn.execute(
        "INSERT INTO index_runs(version_id,generation_id,state) VALUES(?,?,'embedded')",
        (version, generation),
    ).lastrowid
    ids = []
    for seq, text in enumerate(texts):
        cid = conn.execute("INSERT INTO chunks(run_id,seq,text) VALUES(?,?,?)", (run, seq, text)).lastrowid
        ids.append(cid)
        conn.execute("INSERT INTO vec_staging_4(rowid,embedding) VALUES(?,?)", (cid, json.dumps([.25] * 4)))
        conn.execute("INSERT INTO fts_staging(rowid,text) VALUES(?,?)", (cid, text))
    files = []
    for i in range(paths):
        path = f"synthetic/{number}/{i}.txt"
        files.append(conn.execute(
            "INSERT INTO files(source_path,name,kind,current_version_id,status) VALUES(?,?,'txt',?,'embedded')",
            (path, path, version),
        ).lastrowid)
    conn.commit()
    for fid in files:
        db.switch_run(conn, fid, run)
    return run, ids
