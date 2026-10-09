"""retrieve.py — hybrid retrieval: vec + fts + entity/graph expansion, RRF-fused.

Live tables hold exactly the queryable set; retrieval never touches staging.
Graph expansion is filtered to confirmed relations on queryable runs.
"""

import json

import db
import models
import textutil

VEC_K = 30
FTS_K = 20
TOP = 12


def _vec_search(conn, embed_fn, model: str, query: str, table: str, k: int = VEC_K) -> list[int]:
    q = embed_fn(model, [query])[0]
    return [
        r["rowid"]
        for r in conn.execute(
            f"SELECT rowid, distance FROM {table} WHERE embedding MATCH ? ORDER BY distance LIMIT ?",
            (json.dumps(q), k),
        )
    ]


def _fts_search(conn, query: str, k: int = FTS_K) -> list[int]:
    terms = [f'"{t}"' for t in textutil.segment(query).split()]
    if not terms:
        return []
    return [
        r["rowid"]
        for r in conn.execute(
            "SELECT rowid, bm25(fts_chunks) AS score FROM fts_chunks "
            "WHERE fts_chunks MATCH ? ORDER BY score LIMIT ?",
            (" ".join(terms), k),
        )
    ]


def _entity_chunks(conn, query: str) -> list[int]:
    """Chunks mentioning entities whose name/alias contains the query."""
    queryable = db.queryable_run_ids(conn)
    if not queryable:
        return []
    ph_r = ",".join("?" * len(queryable))
    rows = conn.execute(
        f"SELECT DISTINCT e.id FROM effective_entities e JOIN chunk_entities ce ON ce.entity_id=e.id "
        f"JOIN chunks c ON c.id=ce.chunk_id WHERE e.status != 'rejected' AND c.run_id IN ({ph_r}) "
        "AND (e.name LIKE ? OR e.original_name LIKE ? OR e.id IN (SELECT entity_id FROM aliases WHERE alias LIKE ?)) LIMIT 5",
        queryable + [f"%{query}%"] * 3,
    ).fetchall()
    ids = [r["id"] for r in rows]
    if not ids:
        return []
    ph = ",".join("?" * len(ids))
    return [
        r["chunk_id"]
        for r in conn.execute(
            f"SELECT DISTINCT ce.chunk_id FROM chunk_entities ce JOIN chunks c ON c.id=ce.chunk_id "
            f"WHERE ce.entity_id IN ({ph}) AND c.run_id IN ({ph_r})", ids + queryable)
    ]


def _graph_chunks(conn, seed_chunks: list[int], max_depth: int = 2) -> list[int]:
    """2-hop walk from entities in the seed chunks over CONFIRMED relations on queryable runs."""
    if not seed_chunks:
        return []
    queryable = db.queryable_run_ids(conn)
    if not queryable:
        return []
    ph_c = ",".join("?" * len(seed_chunks))
    ph_r = ",".join("?" * len(queryable))
    sql = f"""
        WITH RECURSIVE walk(entity_id, depth) AS (
            SELECT ce.entity_id, 0 FROM chunk_entities ce
            JOIN entities e ON e.id=ce.entity_id JOIN chunks c ON c.id=ce.chunk_id
            WHERE ce.chunk_id IN ({ph_c}) AND e.status != 'rejected' AND c.run_id IN ({ph_r})
            UNION
            SELECT r.dst, w.depth + 1 FROM relations r
            JOIN walk w ON r.src = w.entity_id
            JOIN entities e ON e.id=r.dst
            WHERE r.status = 'confirmed' AND e.status != 'rejected' AND w.depth < {max_depth} AND r.run_id IN ({ph_r})
        )
        SELECT DISTINCT ce.chunk_id FROM chunk_entities ce
        JOIN chunks c ON c.id=ce.chunk_id
        WHERE ce.entity_id IN (SELECT entity_id FROM walk) AND c.run_id IN ({ph_r})
    """
    return [r["chunk_id"] for r in conn.execute(sql, seed_chunks + queryable * 3)]


def rrf(ranked_lists: list[list[int]], k: int = 60) -> list[int]:
    """Reciprocal rank fusion: k penalizes deep ranks; deterministic, no tunables."""
    scores: dict[int, float] = {}
    for lst in ranked_lists:
        for rank, cid in enumerate(lst):
            scores[cid] = scores.get(cid, 0.0) + 1.0 / (k + rank + 1)
    return [cid for cid, _ in sorted(scores.items(), key=lambda kv: kv[1], reverse=True)]


def _chunk_details(conn, ids: list[int]) -> list[dict]:
    ph = ",".join("?" * len(ids))
    return [
        dict(r)
        for r in conn.execute(
            f"""
            SELECT c.id, c.text, c.page, c.kind, c.position,
                   MIN(f.source_path) AS source_path, MIN(f.name) AS name
            FROM chunks c
            JOIN index_runs r ON r.id = c.run_id
            JOIN files f ON f.current_run_id = r.id AND f.status != 'removed'
            WHERE c.id IN ({ph})
            GROUP BY c.id
            """,
            ids,
        )
    ]


def retrieve(conn, cfg, query: str, *, embed_fn=None, top: int = TOP, dims: int | None = None, digest: str | None = None) -> list[dict]:
    """Hybrid retrieve: vec + fts + entity/graph, RRF-fused, with citations.
    Refuses to run when the live generation doesn't match the current config."""
    embed_fn = embed_fn or models.embed_batch
    if db.generation_mismatch(
        conn, parser_version=db.PARSER_VERSION, chunk_config=db.CHUNK_CONFIG,
        embed_model=cfg.models.embed, embed_digest=digest or models.model_digest(cfg.models.embed),
        dims=dims if dims is not None else models.embed_dims(cfg.models.embed),
    ):
        raise RuntimeError("index generation mismatch — run `python -m ingest parse` after a rebuild")

    query = query.strip()
    if not query:
        return []
    live_table = db.vec_names(dims if dims is not None else models.embed_dims(cfg.models.embed))[0]
    vec_hits = _vec_search(conn, embed_fn, cfg.models.embed, query, live_table)
    ranked = [
        vec_hits,
        _fts_search(conn, query),
        _entity_chunks(conn, query),
        _graph_chunks(conn, vec_hits),
    ]
    fused = rrf(ranked)[:top]
    return _chunk_details(conn, fused)
