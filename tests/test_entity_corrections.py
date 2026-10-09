import json
import unittest

import db
import extract
import retrieve
from regression_helpers import CFG, database, seed


class EntityCorrectionTests(unittest.TestCase):
    def setUp(self):
        self.conn, self.generation = database()
        self.addCleanup(self.conn.close)
        self.run, self.ids = seed(self.conn, self.generation, 1, ["Alice paid the bill."])
        self.payload = lambda ids: json.dumps({"entities": [{"name": "Alice", "type": "person", "chunk_ids": ids}]})
        extract.extract_run(self.conn, CFG, self.run, extract_fn=lambda *_: self.payload(self.ids))
        self.entity = self.conn.execute("SELECT id FROM entities").fetchone()[0]
        self.conn.execute("INSERT INTO entity_corrections(entity_id,new_name,new_type,note) VALUES(?, 'Alice Zhang', 'other', 'human correction')", (self.entity,))
        self.conn.commit()

    def test_reextraction_preserves_identity_review_and_corrected_lookup(self):
        db.review_fact(self.conn, "entity", self.entity, "confirmed")
        extract.extract_run(self.conn, CFG, self.run, extract_fn=lambda *_: self.payload(self.ids))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0], 1)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM entity_merge_candidates").fetchone()[0], 0)
        self.assertEqual(retrieve._entity_chunks(self.conn, "Alice Zhang"), self.ids)
        row = self.conn.execute("SELECT name,type,status FROM effective_entities WHERE id=?", (self.entity,)).fetchone()
        self.assertEqual(tuple(row), ("Alice Zhang", "other", "confirmed"))

    def test_new_run_and_collection_of_old_chunks_preserve_corrections(self):
        version = self.conn.execute("SELECT version_id FROM index_runs WHERE id=?", (self.run,)).fetchone()[0]
        new_run = self.conn.execute("INSERT INTO index_runs(version_id,generation_id,state) VALUES(?,?,'embedded')", (version, self.generation)).lastrowid
        cid = self.conn.execute("INSERT INTO chunks(run_id,seq,text) VALUES(?,0,'Alice paid the bill.')", (new_run,)).lastrowid
        self.conn.execute("INSERT INTO vec_staging_4(rowid,embedding) VALUES(?,?)", (cid, json.dumps([.25] * 4)))
        self.conn.execute("INSERT INTO fts_staging(rowid,text) VALUES(?, 'Alice paid the bill.')", (cid,))
        fid = self.conn.execute("SELECT id FROM files WHERE current_run_id=?", (self.run,)).fetchone()[0]
        self.conn.commit()
        db.switch_run(self.conn, fid, new_run)
        db.delete_run_rows(self.conn, self.run)
        self.conn.commit()
        extract.extract_run(self.conn, CFG, new_run, extract_fn=lambda *_: self.payload([cid]))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0], 1)
        self.assertEqual(retrieve._entity_chunks(self.conn, "Alice Zhang"), [cid])
        self.assertEqual(extract.corrected_entities(self.conn)[self.entity]["new_name"], "Alice Zhang")

    def test_same_name_in_another_content_version_remains_a_candidate(self):
        run, ids = seed(self.conn, self.generation, 2, ["Alice has another policy."])
        extract.extract_run(self.conn, CFG, run, extract_fn=lambda *_: self.payload(ids))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0], 2)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM entity_merge_candidates WHERE status='pending'").fetchone()[0], 1)
        self.assertEqual(retrieve._entity_chunks(self.conn, "Alice Zhang"), self.ids)

    def test_backfill_preserves_unique_identity_and_does_not_merge_ambiguity(self):
        self.conn.execute("DELETE FROM version_entities")
        self.conn.commit()
        db.migrate_entity_versions(self.conn)
        self.assertEqual(self.conn.execute("SELECT entity_id FROM version_entities").fetchone()[0], self.entity)
        duplicate = self.conn.execute("INSERT INTO entities(name,type,canonical) VALUES('Alice','person','alice')").lastrowid
        self.conn.execute("INSERT INTO chunk_entities(chunk_id,entity_id) VALUES(?,?)", (self.ids[0], duplicate))
        self.conn.execute("DELETE FROM version_entities")
        self.conn.commit()
        db.migrate_entity_versions(self.conn)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM version_entities").fetchone()[0], 0)
        self.assertEqual(extract.corrected_entities(self.conn)[self.entity]["new_name"], "Alice Zhang")
