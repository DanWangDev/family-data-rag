import json
import unittest

import db
import extract
import spend
from regression_helpers import database, seed


class TransactionEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.conn, self.generation = database()
        self.addCleanup(self.conn.close)
        self.run, self.ids = seed(self.conn, self.generation, 1, ["Alice paid $142.35."])
        self.other, self.foreign = seed(self.conn, self.generation, 2, ["An unrelated note."])

    def insert(self, **kwargs):
        data = {"date": "2026-08-01", "amount_minor": 14235, "chunk_ids": self.ids, **kwargs}
        extract._insert_extraction(self.conn, self.run, extract.Extraction(transactions=[extract.Transaction(**data)]))

    def test_no_evidence_foreign_evidence_and_wrong_scale_are_dropped(self):
        for ids in ([], [999999], self.foreign):
            self.insert(chunk_ids=ids)
        self.insert(scale=1, currency="USD")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0], 0)

    def test_valid_evidence_is_retained_and_review_gates_every_analytic(self):
        self.insert(chunk_ids=self.ids + self.foreign + [999999])
        row = self.conn.execute("SELECT * FROM transactions").fetchone()
        self.assertEqual(row["status"], "pending")
        self.assertEqual(json.loads(row["evidence_chunk_ids"]), self.ids)
        self.assertEqual(json.loads(row["raw"])["chunk_ids"], self.ids)
        for fn in (spend.month_totals, spend.monthly_by_category, spend.detect_recurring, spend.forecast, spend.propose_dedup):
            self.assertEqual(fn(self.conn), [])
        self.assertIn("pending review", spend.reconcile(self.conn, "2026-08", 14235)["explanation"])
        spend.review_transaction(self.conn, row["id"], "confirmed")
        self.assertEqual(spend.month_totals(self.conn)[0]["minor"], 14235)
        self.assertEqual(spend.reconcile(self.conn, "2026-08", 14235)["difference_minor"], 0)
        spend.review_transaction(self.conn, row["id"], "rejected")
        self.assertEqual(spend.month_totals(self.conn), [])

    def test_legacy_migration_preserves_rows_and_quarantines_bad_evidence(self):
        conn = db.connect(":memory:")
        self.addCleanup(conn.close)
        schema = db.SCHEMA.replace("  evidence_chunk_ids TEXT NOT NULL DEFAULT '[]',\n", "").replace(
            "  status TEXT NOT NULL DEFAULT 'pending', -- pending|confirmed|rejected\n", "")
        conn.executescript(schema)
        # Migration also tolerates malformed legacy raw payloads.
        for raw in ('{"chunk_ids":[99999]}', 'bad JSON', '{"chunk_ids":null}'):
            conn.execute("INSERT INTO transactions(date,amount_minor,scale,raw) VALUES('2026-08-01',100,100,?)", (raw,))
        conn.commit()
        db.migrate_transaction_review(conn)
        db.migrate_transaction_review(conn)
        rows = conn.execute("SELECT status,evidence_chunk_ids FROM transactions").fetchall()
        self.assertEqual([tuple(r) for r in rows], [("pending", "[]")] * 3)

    def test_legacy_extracted_row_without_evidence_cannot_be_confirmed(self):
        tid = self.conn.execute("INSERT INTO transactions(run_id,date,amount_minor,scale) VALUES(?,'2026-08-01',100,100)",
                                (self.run,)).lastrowid
        self.conn.commit()
        with self.assertRaises(ValueError):
            spend.review_transaction(self.conn, tid, "confirmed")
