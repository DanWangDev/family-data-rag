"""app.py — Streamlit UI: chat with citations, files/ingest control. Launched by run.bat (localhost only)."""

import streamlit as st

import chat
import db
import ingest
import models
from config import Config

st.set_page_config(page_title="Family RAG")


@st.cache_resource
def resources() -> tuple[Config, object]:
    cfg = Config.load()
    cfg.db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = db.connect(str(cfg.db_path))
    db.init_db(
        conn, dims=models.embed_dims(cfg.models.embed),
        embed_model=cfg.models.embed, embed_digest=models.model_digest(cfg.models.embed),
    )
    return cfg, conn


cfg, conn = resources()

tab_chat, tab_files = st.tabs(["Chat", "Files"])

with tab_chat:
    st.caption(
        f"Local RAG — chat {cfg.models.chat}, embed {cfg.models.embed}. "
        "Answers cite sources; thin evidence gets a plain refusal."
    )
    for msg in st.session_state.setdefault("messages", []):
        with st.chat_message(msg["role"]):
            st.markdown(msg["content"])
            if msg.get("sources"):
                with st.expander("Sources"):
                    for i, s in enumerate(msg["sources"], start=1):
                        st.markdown(
                            f"**[{i}] {s['name']}** — page {s['page'] if s['page'] is not None else 'n/a'} "
                            f"(`{s['source_path']}`)"
                        )
                        st.text(s["text"])

    query = st.chat_input("Ask about your documents…")
    if query:
        st.session_state["messages"].append({"role": "user", "content": query})
        with st.chat_message("assistant"):
            with st.spinner("Searching and answering…"):
                try:
                    result = chat.ask(conn, cfg, query)
                except RuntimeError as e:  # generation mismatch etc.
                    result = {"answer": str(e), "sources": []}
            st.markdown(result["answer"])
        st.session_state["messages"].append({"role": "assistant", "content": result["answer"], "sources": result["sources"]})
        st.rerun()

with tab_files:
    c1, c2, c3, c4 = st.columns(4)
    if c1.button("Scan sources"):
        with st.spinner("Scanning…"):
            ingest.scan(conn, cfg)
        st.rerun()
    if c2.button("Retry failed parses"):
        with st.spinner("Parsing…"):
            ingest.parse_all(conn, cfg)
        st.rerun()
    if c3.button("Run GC"):
        with st.spinner("Collecting…"):
            removed = ingest.gc(conn, cfg.blob_dir)
        st.success(f"GC removed {removed} blobs")
    if c4.button("Backup"):
        with st.spinner("Backing up…"):
            snap = ingest.backup(conn, cfg.blob_dir)
        st.success(f"Snapshot: {snap.name}")

    statuses = conn.execute("SELECT status, COUNT(*) AS n FROM files GROUP BY status").fetchall()
    st.caption(" · ".join(f"{r['status']}: {r['n']}" for r in statuses))
    rows = conn.execute(
        "SELECT id, source_path, name, kind, status, exif_date FROM files WHERE status != 'removed' ORDER BY source_path"
    ).fetchall()
    st.dataframe(
        [{"file": r["name"], "source": r["source_path"], "kind": r["kind"], "status": r["status"], "taken": r["exif_date"] or ""} for r in rows],
        use_container_width=True,
    )
