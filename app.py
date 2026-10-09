"""app.py — Streamlit UI: chat with citations, files/ingest control. Launched by run.bat (localhost only)."""

import json

import plotly.express as px
import streamlit as st

import chat
import db
import extract
import ingest
import models
import spend
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

tab_chat, tab_files, tab_entities, tab_spend = st.tabs(["Chat", "Files", "Entities", "Spend"])

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

with tab_spend:
    st.caption(
        "Reconcile first. Only human-confirmed transactions enter totals — "
        "refunds subtract, transfers and card payments never count, confirmed duplicates count once."
    )
    by_cat = spend.monthly_by_category(conn)
    if by_cat:
        import pandas as pd

        df = pd.DataFrame([{**r, "major": r["minor"] / r["scale"]} for r in by_cat])
        st.plotly_chart(
            px.bar(df, x="month", y="major", color="category", barmode="stack",
                   labels={"major": "amount", "month": "month"}, height=380),
            use_container_width=True,
        )
        fc = pd.DataFrame(spend.forecast(conn, months_ahead=3))
        if not fc.empty:
            st.plotly_chart(
                px.line(fc, x="month", y=["moving_average", "trend"],
                        labels={"value": "forecast", "variable": "method"}, height=300),
                use_container_width=True,
            )
            st.caption("Forecast: 3-month moving average vs linear trend — computed, not model-generated.")
    else:
        st.info("No transactions yet — run `python -m ingest extract` after indexing bills.")

    st.subheader("Recurring payments (median inter-arrival, computed)")
    rec = spend.detect_recurring(conn, apply=False)
    st.dataframe(rec, use_container_width=True)

    st.subheader("Duplicate review — same transaction in an invoice AND a bank statement")
    pairs = spend.propose_dedup(conn)
    if not pairs:
        st.caption("No candidates.")
    for p in pairs:
        st.markdown(
            f"txn {p['a']} and {p['b']}: {p['merchant']} — {p['amount_minor'] / p['scale']:.2f} {p['currency']}. Same purchase?"
        )
        b1, b2 = st.columns(2)
        if b1.button("Yes — keep once", key=f"dup_yes{p['a']}_{p['b']}"):
            conn.execute("UPDATE txn_links SET status='confirmed' WHERE a=? AND b=?", (p["a"], p["b"]))
            conn.commit()
            st.rerun()
        if b2.button("No — both are real", key=f"dup_no{p['a']}_{p['b']}"):
            conn.execute("UPDATE txn_links SET status='rejected' WHERE a=? AND b=?", (p["a"], p["b"]))
            conn.commit()
            st.rerun()

    st.subheader("Reconciliation — check a month against a known statement total")
    scales = {"USD": 100, "CNY": 100, "GBP": 100, "EUR": 100, "JPY": 1}
    with st.form("reconcile"):
        month = st.text_input("Month (YYYY-MM)", value="2026-08")
        currency = st.selectbox("Currency", list(scales))
        expected = st.number_input("Statement total (major units)", min_value=0.0, step=1.0)
        if st.form_submit_button("Reconcile"):
            result = spend.reconcile(conn, month, round(expected * scales[currency]), currency)
            st.info(result["explanation"])

    st.subheader("Review and edit transactions")
    txns = conn.execute(
        "SELECT id, date, merchant, category, kind, amount_minor, scale, currency, status, evidence_chunk_ids, is_recurring, recurrence "
        "FROM transactions ORDER BY date"
    ).fetchall()
    if txns:
        st.caption("Review source evidence, then change status to confirmed or rejected and save. Pending rows are excluded from analytics.")
        with st.expander("Transaction source evidence"):
            evidence_txn = st.selectbox("Transaction", [t["id"] for t in txns])
            evidence_row = next(t for t in txns if t["id"] == evidence_txn)
            for cid in json.loads(evidence_row["evidence_chunk_ids"]):
                chunk = conn.execute("SELECT text FROM chunks WHERE id=?", (cid,)).fetchone()
                if chunk:
                    st.text(f"Chunk {cid}: {chunk['text']}")
        edited = st.data_editor(
            [dict(t) for t in txns],
            column_config={
                "id": st.column_config.NumberColumn(disabled=True),
                "date": st.column_config.TextColumn(disabled=True),
                "amount_minor": st.column_config.NumberColumn(disabled=True),
                "scale": st.column_config.NumberColumn(disabled=True),
                "currency": st.column_config.TextColumn(disabled=True),
                "evidence_chunk_ids": st.column_config.TextColumn(disabled=True),
                "status": st.column_config.SelectboxColumn(options=["pending", "confirmed", "rejected"], required=True),
                "kind": st.column_config.SelectboxColumn(options=["consumption", "refund", "transfer", "credit_card_payment"], required=True),
                "is_recurring": st.column_config.NumberColumn(disabled=True),
                "recurrence": st.column_config.TextColumn(disabled=True),
            },
            use_container_width=True,
        )
        if st.button("Save edits"):
            by_id = {t["id"]: t for t in txns}
            try:
                with conn:
                    for row in edited:
                        orig = by_id[row["id"]]
                        if row["status"] != orig["status"]:
                            spend.review_transaction(conn, row["id"], row["status"], commit=False)
                        if (row["merchant"], row["category"], row["kind"]) != (orig["merchant"], orig["category"], orig["kind"]):
                            conn.execute(
                                "UPDATE transactions SET merchant=?, category=?, kind=? WHERE id=?",
                                (row["merchant"], row["category"], row["kind"], row["id"]),
                            )
            except ValueError as e:
                st.error(str(e))
            else:
                st.rerun()
