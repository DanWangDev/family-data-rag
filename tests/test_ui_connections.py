import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import config
import db
import streamlit as st
from streamlit.testing.v1 import AppTest


class UIConnectionTests(unittest.TestCase):
    def test_sessions_and_reruns_have_owned_closed_connections(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = config.Config(source_dirs=(), db_path=Path(td) / "family.db", blob_dir=Path(td) / "blob",
                                max_file_mb=100, photo_captions=False,
                                models=config.Models(chat="c", extract="e", embed="fake-embed", vision="v"))
            connections = []
            class TrackedConnection(sqlite3.Connection):
                closed = False
                def close(self):
                    super().close()
                    self.closed = True
                    self.closed_on = threading.get_ident()

            def connect(path):
                conn = sqlite3.connect(path, factory=TrackedConnection)
                conn.row_factory = sqlite3.Row
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA foreign_keys=ON")
                conn.owner = threading.get_ident()
                connections.append(conn)
                return conn
            st.cache_resource.clear()
            try:
                with patch("config.Config.load", return_value=cfg), patch("models.embed_dims", return_value=4), \
                     patch("models.model_digest", return_value="test-digest"), patch("db.connect", side_effect=connect), \
                     patch("chat.ask", return_value={"answer": "Synthetic answer", "sources": []}):
                    path = str(Path(__file__).resolve().parents[1] / "app.py")
                    first = AppTest.from_file(path).run(timeout=20)
                    second = AppTest.from_file(path).run(timeout=20)
                    for app in (first, second):
                        self.assertEqual(list(app.exception), [])
                        app.run(timeout=20)
                        self.assertEqual(list(app.exception), [])
                    # Chat triggers st.rerun(): finally must close that interrupted run too.
                    first.chat_input[0].set_value("Synthetic question").run(timeout=20)
                    self.assertEqual(list(first.exception), [])
                self.assertGreaterEqual(len(connections), 5)
                self.assertEqual(len({id(c) for c in connections}), len(connections))
                for conn in connections:
                    self.assertTrue(conn.closed)
                    self.assertEqual(conn.owner, conn.closed_on)
            finally:
                st.cache_resource.clear()
