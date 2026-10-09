import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import config
import db
import extract
import retrieve
import streamlit as st
from streamlit.testing.v1 import AppTest
from regression_helpers import database, seed


def facts(conn, generation):
    run, ids = seed(conn, generation, 1, ["Alice has an account.", "Bob keeps a policy.", "Alice hires Bob.", "2026-01 through 2026-02"])
    extract._insert_extraction(conn, run, extract.Extraction(
        entities=[extract.Entity(name="Alice", type="person", chunk_ids=[ids[0], ids[2]]),
                  extract.Entity(name="Bob", type="person", chunk_ids=[ids[1], ids[2]]),
                  extract.Entity(name="2026-01 through 2026-02", type="person", chunk_ids=[ids[3]])],
        relations=[extract.Relation(source="Alice", target="Bob", relation="hires", quote="Alice hires Bob", chunk_ids=[ids[2]])]))
    entities = {r["name"]: r["id"] for r in conn.execute("SELECT id,name FROM entities")}
    relation = conn.execute("SELECT id FROM relations").fetchone()[0]
    return ids, entities, relation


class FactReviewTests(unittest.TestCase):
    def test_confirmation_enables_graph_and_rejection_removes_signals(self):
        conn, generation = database()
        self.addCleanup(conn.close)
        ids, entities, relation = facts(conn, generation)
        self.assertNotIn(ids[1], retrieve._graph_chunks(conn, [ids[0]]))
        db.review_fact(conn, "relation", relation, "confirmed")
        self.assertIn(ids[1], retrieve._graph_chunks(conn, [ids[0]]))
        db.review_fact(conn, "entity", entities["Bob"], "rejected")
        self.assertEqual(retrieve._entity_chunks(conn, "Bob"), [])
        self.assertNotIn(ids[1], retrieve._graph_chunks(conn, [ids[0]]))
        with self.assertRaises(ValueError):
            db.review_fact(conn, "relation", relation, "confirmed")
        db.review_fact(conn, "relation", relation, "rejected")

    def test_ghost_chunks_and_unsupported_confirmation_are_excluded(self):
        conn, generation = database()
        self.addCleanup(conn.close)
        ids, entities, relation = facts(conn, generation)
        ghost, ghost_ids = seed(conn, generation, 2, ["Alice in an old document."])
        conn.execute("INSERT INTO chunk_entities(chunk_id,entity_id) VALUES(?,?)", (ghost_ids[0], entities["Alice"]))
        conn.commit()
        fid = conn.execute("SELECT id FROM files WHERE current_run_id=?", (ghost,)).fetchone()[0]
        db.remove_file(conn, fid)
        self.assertNotIn(ghost_ids[0], retrieve._entity_chunks(conn, "Alice"))
        self.assertNotIn(ghost_ids[0], retrieve._graph_chunks(conn, [ids[0]]))
        conn.execute("UPDATE relations SET evidence_chunk_ids='[]' WHERE id=?", (relation,))
        conn.commit()
        with self.assertRaises(ValueError):
            db.review_fact(conn, "relation", relation, "confirmed")

    def test_actual_ui_buttons_persist_confirm_and_reject(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = config.Config(source_dirs=(), db_path=Path(td) / "family.db", blob_dir=Path(td) / "blob",
                                max_file_mb=100, photo_captions=False,
                                models=config.Models(chat="c", extract="e", embed="fake-embed", vision="v"))
            conn = db.connect(str(cfg.db_path))
            generation = db.init_db(conn, dims=4, embed_model="fake-embed", embed_digest="test-digest")
            ids, entities, relation = facts(conn, generation)
            conn.close()
            st.cache_resource.clear()
            try:
                with patch("config.Config.load", return_value=cfg), patch("models.embed_dims", return_value=4), \
                     patch("models.model_digest", return_value="test-digest"):
                    app = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "app.py")).run(timeout=20)
                    app.button(key=f"entity_reject{entities['2026-01 through 2026-02']}").click().run(timeout=20)
                    app.button(key=f"entity_confirm{entities['Alice']}").click().run(timeout=20)
                    app.button(key=f"relation_confirm{relation}").click().run(timeout=20)
                    self.assertEqual(list(app.exception), [])
                conn = db.connect(str(cfg.db_path))
                try:
                    self.assertEqual(conn.execute("SELECT status FROM entities WHERE id=?", (entities["Alice"],)).fetchone()[0], "confirmed")
                    self.assertEqual(conn.execute("SELECT status FROM entities WHERE id=?", (entities["2026-01 through 2026-02"],)).fetchone()[0], "rejected")
                    self.assertEqual(conn.execute("SELECT status FROM relations WHERE id=?", (relation,)).fetchone()[0], "confirmed")
                    self.assertIn(ids[1], retrieve._graph_chunks(conn, [ids[0]]))
                finally:
                    conn.close()
            finally:
                st.cache_resource.clear()
