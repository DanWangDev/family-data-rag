import json
import unittest

import db
import extract
import spend
from regression_helpers import database, seed


class SpendRunScopeTests(unittest.TestCase):
    def test_rebuild_switch_stops_counting_old_confirmed_transactions(self):
        conn, generation = database()
        self.addCleanup(conn.close)
        run, ids = seed(conn, generation, 1, ["Alice paid $142.35."])
        def write(run_id, chunk_ids):
            extract._insert_extraction(conn, run_id, extract.Extraction(transactions=[
                extract.Transaction(date="2026-08-01", amount_minor=14235, merchant="Utility", chunk_ids=chunk_ids)]))
            tid = conn.execute("SELECT id FROM transactions WHERE run_id=?", (run_id,)).fetchone()[0]
            spend.review_transaction(conn, tid, "confirmed")
        write(run, ids)
        self.assertEqual(spend.month_totals(conn)[0]["minor"], 14235)
        version = conn.execute("SELECT version_id FROM index_runs WHERE id=?", (run,)).fetchone()[0]
        new = conn.execute("INSERT INTO index_runs(version_id,generation_id,state) VALUES(?,?,'embedded')", (version, generation)).lastrowid
        cid = conn.execute("INSERT INTO chunks(run_id,seq,text) VALUES(?,0,'Alice paid $142.35.')", (new,)).lastrowid
        conn.execute("INSERT INTO vec_staging_4(rowid,embedding) VALUES(?,?)", (cid, json.dumps([.25] * 4)))
        conn.execute("INSERT INTO fts_staging(rowid,text) VALUES(?, 'Alice paid $142.35.')", (cid,))
        fid = conn.execute("SELECT id FROM files WHERE current_run_id=?", (run,)).fetchone()[0]
        conn.commit()
        db.switch_run(conn, fid, new)
        self.assertEqual(spend.month_totals(conn), [])
        write(new, [cid])
        self.assertEqual(spend.month_totals(conn)[0]["minor"], 14235)
        self.assertEqual(spend.reconcile(conn, "2026-08", 14235)["difference_minor"], 0)
        db.remove_file(conn, fid)
        for fn in (spend.month_totals, spend.monthly_by_category, spend.forecast, spend.detect_recurring, spend.propose_dedup):
            self.assertEqual(fn(conn), [])

    def test_removed_duplicate_source_cannot_hide_current_transaction(self):
        conn, generation = database()
        self.addCleanup(conn.close)
        txns = []
        for number in (1, 2):
            run, ids = seed(conn, generation, number, ["Utility paid $40."])
            extract._insert_extraction(conn, run, extract.Extraction(transactions=[
                extract.Transaction(date="2026-08-01", amount_minor=4000, merchant="Utility", chunk_ids=ids)]))
            tid = conn.execute("SELECT id FROM transactions WHERE run_id=?", (run,)).fetchone()[0]
            spend.review_transaction(conn, tid, "confirmed")
            txns.append(tid)
        conn.execute("INSERT INTO txn_links(a,b,status) VALUES(?,?,'confirmed')", txns)
        conn.commit()
        self.assertEqual(spend.month_totals(conn)[0]["minor"], 4000)
        fid = conn.execute("SELECT id FROM files ORDER BY id LIMIT 1").fetchone()[0]
        db.remove_file(conn, fid)
        spend.review_transaction(conn, txns[0], "rejected")
        self.assertEqual(spend.month_totals(conn)[0]["minor"], 4000)
