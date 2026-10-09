import unittest

import extract
from regression_helpers import database, seed


class RelationEndpointTests(unittest.TestCase):
    def test_other_documents_cannot_supply_missing_endpoints(self):
        conn, generation = database()
        self.addCleanup(conn.close)
        old, old_ids = seed(conn, generation, 1, ["Alice lives here."])
        extract._insert_extraction(conn, old, extract.Extraction(
            entities=[extract.Entity(name="Alice", type="person", chunk_ids=old_ids)]))
        run, ids = seed(conn, generation, 2, ["Utility serves an unnamed customer."])
        extract._insert_extraction(conn, run, extract.Extraction(
            entities=[extract.Entity(name="Utility", type="org", chunk_ids=ids)],
            relations=[extract.Relation(source="Utility", target="Alice", relation="bills", chunk_ids=ids)]))
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM relations").fetchone()[0], 0)

        local, local_ids = seed(conn, generation, 3, ["Utility bills Alice."])
        extract._insert_extraction(conn, local, extract.Extraction(
            entities=[extract.Entity(name=n, type=t, chunk_ids=local_ids) for n, t in (("Utility", "org"), ("Alice", "person"))],
            relations=[extract.Relation(source="Utility", target="Alice", relation="bills", quote="Utility bills Alice", chunk_ids=local_ids)]))
        target = conn.execute("SELECT r.dst FROM relations r WHERE run_id=?", (local,)).fetchone()[0]
        old_target = conn.execute("SELECT entity_id FROM chunk_entities WHERE chunk_id=?", (old_ids[0],)).fetchone()[0]
        self.assertNotEqual(target, old_target)

    def test_unknown_ambiguous_and_unsupported_relations_are_dropped(self):
        conn, generation = database()
        self.addCleanup(conn.close)
        run, ids = seed(conn, generation, 1, ["Alice bills Bob.", "An unrelated line."])
        entities = [extract.Entity(name=n, type=t, chunk_ids=[ids[0]]) for n, t in
                    (("Alice", "person"), ("Bob", "person"), ("Bob", "org"))]
        extract._insert_extraction(conn, run, extract.Extraction(entities=entities, relations=[
            extract.Relation(source="Alice", target="Bob", relation="bills", chunk_ids=[ids[0]]),
            extract.Relation(source="Alice", target="Alice", relation="owns", chunk_ids=[99999]),
            extract.Relation(source="Alice", target="Alice", relation="owns", chunk_ids=[ids[1]]),
        ]))
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM relations").fetchone()[0], 0)

    def test_quote_verification_requires_a_verbatim_substring(self):
        conn, generation = database()
        self.addCleanup(conn.close)
        run, ids = seed(conn, generation, 1, ["Alice bills Bob."])
        extract._insert_extraction(conn, run, extract.Extraction(
            entities=[extract.Entity(name=n, type="person", chunk_ids=ids) for n in ("Alice", "Bob")],
            relations=[extract.Relation(source="Alice", target="Bob", relation="bills", quote=q, chunk_ids=ids)
                       for q in ("Alice bills Bob", "alicebillsbob")]))
        self.assertEqual([r[0] for r in conn.execute("SELECT quote_verified FROM relations ORDER BY id")], [1, 0])
