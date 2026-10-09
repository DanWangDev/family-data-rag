import json
import unittest

import extract
from regression_helpers import CFG, database, seed


class AtomicExtractionTests(unittest.TestCase):
    def test_late_insert_failure_rolls_back_all_new_rows_and_allows_retry(self):
        for failing_table in ("relations", "transactions"):
            with self.subTest(table=failing_table):
                conn, generation = database()
                try:
                    old_run, old_ids = seed(conn, generation, 1, ["Alice lives here."])
                    extract._insert_extraction(conn, old_run, extract.Extraction(
                        entities=[extract.Entity(name="Alice", type="person", chunk_ids=old_ids)]))
                    old_entity = conn.execute("SELECT id FROM entities").fetchone()[0]
                    conn.execute("INSERT INTO entity_corrections(entity_id,new_name) VALUES(?, 'Alice Zhang')", (old_entity,))
                    conn.commit()
                    run, ids = seed(conn, generation, 2, ["Alice paid Utility $142.35."])
                    payload = {
                        "entities": [{"name": n, "type": "person", "chunk_ids": ids} for n in ("Alice", "Utility")],
                        "relations": [{"source": "Alice", "target": "Utility", "relation": "pays", "chunk_ids": ids}],
                        "transactions": [{"date": "2026-08-01", "amount_minor": 14235, "chunk_ids": ids}],
                    }
                    conn.executescript(f"CREATE TRIGGER fail_insert BEFORE INSERT ON {failing_table} "
                                       "BEGIN SELECT RAISE(ABORT, 'synthetic failure'); END;")
                    fake = lambda *_: json.dumps(payload)
                    extract.extract_all(conn, CFG, extract_fn=fake)
                    self.assertEqual(conn.execute("SELECT state FROM index_runs WHERE id=?", (run,)).fetchone()[0], "extract_failed")
                    self.assertEqual(conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0], 1)
                    for table in ("relations", "transactions", "entity_merge_candidates"):
                        self.assertEqual(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0)
                    self.assertEqual(extract.corrected_entities(conn)[old_entity]["new_name"], "Alice Zhang")
                    self.assertEqual(conn.execute("SELECT COUNT(*) FROM vec_chunks_4").fetchone()[0], 2)
                    conn.execute("DROP TRIGGER fail_insert")
                    conn.execute("UPDATE index_runs SET state='embedded' WHERE id=?", (run,))
                    conn.commit()
                    extract.extract_all(conn, CFG, extract_fn=fake)
                    self.assertEqual(conn.execute("SELECT state FROM index_runs WHERE id=?", (run,)).fetchone()[0], "extracted")
                    self.assertEqual(conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0], 1)
                finally:
                    conn.close()
