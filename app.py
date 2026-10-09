"""app.py — Streamlit UI: chat with citations, files/ingest control. Launched by run.bat (localhost only)."""

import streamlit as st

import chat
import db
import extract
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

tab_chat, tab_files, tab_entities = st.tabs(["Chat", "Files", "Entities"])

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
                            f"**[{i}] {s['name']}** — page {s['page'] + 1 if s['page'] is not None else 'n/a'} "
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

with tab_entities:
    st.caption(
        "Everything the model extracted is pending until you confirm it. "
        "Corrections are an override layer — queries and display use them, re-extraction never overwrites them."
    )
    corrections = extract.corrected_entities(conn)
    entities = conn.execute("SELECT id, name, type, status, attrs FROM entities ORDER BY type, name").fetchall()
    st.dataframe(
        [{
            "id": e["id"],
            "name": corrections.get(e["id"], {}).get("new_name") or e["name"],
            "type": corrections.get(e["id"], {}).get("new_type") or e["type"],
            "status": e["status"],
            "attrs": e["attrs"],
        } for e in entities],
        use_container_width=True,
    )

    st.subheader("Relations (quote_verified = citation exists; status = human judgment)")
    rels = conn.execute(
        "SELECT a.name AS src, r.rel, b.name AS dst, r.valid_from, r.valid_to, r.quote_verified, r.status, r.quote "
        "FROM relations r JOIN entities a ON a.id = r.src JOIN entities b ON b.id = r.dst ORDER BY r.id"
    ).fetchall()
    st.dataframe([dict(x) for x in rels], use_container_width=True)

    with st.form("correction"):
        eid = st.selectbox("Entity", options=[(e["id"], e["name"]) for e in entities], format_func=lambda t: t[1])
        new_name = st.text_input("Corrected name (blank = keep)")
        new_type = st.text_input("Corrected type (blank = keep)")
        note = st.text_input("Note")
        if st.form_submit_button("Save correction"):
            conn.execute(
                "INSERT INTO entity_corrections(entity_id, new_name, new_type, note, by) VALUES(?,?,?,?, 'human') "
                "ON CONFLICT(entity_id) DO UPDATE SET new_name=excluded.new_name, new_type=excluded.new_type, note=excluded.note",
                (eid[0], new_name or None, new_type or None, note or None),
            )
            conn.commit()
            st.rerun()

    st.subheader("Merge candidates — same name, never auto-merged")
    candidates = conn.execute("SELECT * FROM entity_merge_candidates WHERE status='pending'").fetchall()
    if not candidates:
        st.caption("No pending merge candidates.")
    for c in candidates:
        ea = conn.execute("SELECT name FROM entities WHERE id=?", (c["a"],)).fetchone()["name"]
        eb = conn.execute("SELECT name FROM entities WHERE id=?", (c["b"],)).fetchone()["name"]
        st.markdown(f"`{ea}` (id {c['a']}) and `{eb}` (id {c['b']}) share a name. Same person?")
        b1, b2 = st.columns(2)
        if b1.button("Yes — alias them", key=f"yes{c['id']}"):
            conn.execute("INSERT OR IGNORE INTO aliases(entity_id, alias, source) VALUES(?,?, 'human')", (c["a"], eb))
            conn.execute("UPDATE entity_merge_candidates SET status='accepted' WHERE id=?", (c["id"],))
            conn.commit()
            st.rerun()
        if b2.button("No — different people", key=f"no{c['id']}"):
            conn.execute("UPDATE entity_merge_candidates SET status='rejected' WHERE id=?", (c["id"],))
            conn.commit()
            st.rerun()
