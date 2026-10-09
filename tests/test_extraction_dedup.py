import json
import unittest

import extract
from regression_helpers import CFG, database, seed


class ExtractionDedupTests(unittest.TestCase):
    def setUp(self):
        self.conn, self.generation = database()
        self.addCleanup(self.conn.close)

    def test_shared_run_is_extracted_once(self):
        run, ids = seed(self.conn, self.generation, 1, ["Alice paid $142.35."], paths=2)
        payload = {"entities": [{"name": "Alice", "type": "person", "chunk_ids": ids}],
                   "transactions": [{"date": "2026-08-01", "amount_minor": 14235, "chunk_ids": ids}]}
        calls = []
        def fake(*args):
            calls.append(args)
            return json.dumps(payload)
        self.assertEqual(extract.extract_all(self.conn, CFG, extract_fn=fake), 1)
        self.assertEqual(extract.extract_all(self.conn, CFG, extract_fn=fake), 0)
        self.assertEqual(len(calls), 1)
        for table, count in (("entities", 1), ("transactions", 1), ("entity_merge_candidates", 0)):
            self.assertEqual(self.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], count)

    def test_segment_overlap_preserves_mentions_without_double_counting(self):
        run, ids = seed(self.conn, self.generation, 2, ["Alice paid Bob $142.35."] * 21)
        responses = iter([
            {"entities": [{"name": n, "type": "person", "chunk_ids": [ids[18]]} for n in ("Alice", "Bob")]},
            {"entities": [{"name": n, "type": "person", "chunk_ids": [ids[20]]} for n in ("Alice", "Bob")]},
        ])
        def fake(*args):
            data = next(responses)
            data["relations"] = [{"source": "Alice", "target": "Bob", "relation": "pays", "chunk_ids": [ids[18]]}]
            data["transactions"] = [{"date": "2026-08-01", "amount_minor": 14235, "chunk_ids": [ids[18]]}]
            return json.dumps(data)
        extract.extract_all(self.conn, CFG, extract_fn=fake)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM relations").fetchone()[0], 1)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0], 1)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM chunk_entities").fetchone()[0], 4)

    def test_same_amount_with_distinct_evidence_is_preserved(self):
        run, ids = seed(self.conn, self.generation, 3, ["Two purchases."] * 21)
        responses = iter([ids[0], ids[20]])
        def fake(*args):
            return json.dumps({"transactions": [{"date": "2026-08-01", "amount_minor": 100,
                                                  "chunk_ids": [next(responses)]}]})
        extract.extract_all(self.conn, CFG, extract_fn=fake)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0], 2)
