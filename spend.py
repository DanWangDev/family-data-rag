"""spend.py — Phase 3: reconciliation FIRST, then cadence and forecasting.

All math is SQL/code; the LLM never computes money. Contract (plan v3):
- consumption/refund count toward spend; transfers and credit-card payments never do;
- refunds subtract;
- confirmed txn_links exclude the duplicate side from every total;
- amounts stay integer minor units with a per-currency scale (USD:100, JPY:1, ...);
- acceptance = reconcile a full month against a known account and explain every difference.
"""

from collections import defaultdict
import json
from datetime import datetime, timedelta
from statistics import fmean, linear_regression, median

import db

SPEND_KINDS = ("consumption", "refund")

# ponytail: O(n^2) pair scan for dedup candidates — family scale, upgrade to
# windowed buckets if statements ever carry thousands of transactions.


def _exclude_confirmed_dups(sql_cond: str = "") -> str:
    return (
        "t.status = 'confirmed' AND NOT EXISTS (SELECT 1 FROM txn_links l WHERE l.b = t.id AND l.status = 'confirmed')"
        + (f" AND {sql_cond}" if sql_cond else "")
    )


def month_totals(conn, months: int = 12) -> list[dict]:
    """Spend per month+currency in integer minor units; transfers/card payments excluded."""
    rows = conn.execute(
        f"""
        SELECT strftime('%Y-%m', t.date) AS month, t.currency,
               SUM(CASE WHEN t.kind='consumption' THEN t.amount_minor ELSE -t.amount_minor END) AS minor,
               MAX(t.scale) AS scale, COUNT(*) AS n
        FROM transactions t
        WHERE t.kind IN ('consumption', 'refund') AND {_exclude_confirmed_dups()}
        GROUP BY month, t.currency
        ORDER BY month DESC LIMIT ?
        """,
        (months,),
    )
    return [dict(r) for r in rows]


def monthly_by_category(conn, months: int = 12) -> list[dict]:
    rows = conn.execute(
        f"""
        SELECT strftime('%Y-%m', t.date) AS month, t.currency,
               COALESCE(NULLIF(t.category, ''), 'uncategorized') AS category,
               SUM(CASE WHEN t.kind='consumption' THEN t.amount_minor ELSE -t.amount_minor END) AS minor,
               MAX(t.scale) AS scale
        FROM transactions t
        WHERE t.kind IN ('consumption', 'refund') AND {_exclude_confirmed_dups()}
        GROUP BY month, t.currency, category
        ORDER BY month, category
        """
    )
    return [dict(r) for r in rows]


def detect_recurring(conn, min_occurrences: int = 2, apply: bool = True) -> list[dict]:
    """Median inter-arrival days per (merchant, amount): 27-33 -> monthly, 350-380 -> annual.
    Refunds and transfers never participate."""
    groups: dict[tuple, list[str]] = defaultdict(list)
    ids: dict[tuple, list[int]] = defaultdict(list)
    for r in conn.execute(
        "SELECT id, merchant, amount_minor, scale, currency, date FROM transactions "
        "WHERE status='confirmed' AND kind='consumption' AND merchant IS NOT NULL AND merchant != ''"
    ):
        key = (r["merchant"].strip().lower(), r["amount_minor"], r["scale"], r["currency"])
        groups[key].append(r["date"])
        ids[key].append(r["id"])
    out = []
    for key, ds in groups.items():
        if len(ds) < min_occurrences:
            continue
        ordered = sorted(ds)
        diffs = [
            (datetime.fromisoformat(b) - datetime.fromisoformat(a)).days
            for a, b in zip(ordered, ordered[1:])
        ]
        if not diffs:
            continue
        med = median(diffs)
        if 27 <= med <= 33:
            cadence = "monthly"
        elif 350 <= med <= 380:
            cadence = "annual"
        else:
            continue
        out.append({
            "merchant": key[0], "amount_minor": key[1], "scale": key[2], "currency": key[3],
            "cadence": cadence, "median_days": med, "count": len(ordered), "last": ordered[-1],
        })
        if apply:
            for tid in ids[key]:
                conn.execute(
                    "UPDATE transactions SET is_recurring=1, recurrence=? WHERE id=?",
                    (cadence, tid),
                )
    if apply:
        conn.commit()
    return out


def forecast(conn, months_ahead: int = 3, months_back: int = 12) -> list[dict]:
    """3-month moving average + linear trend, next `months_ahead` months, per currency."""
    by_currency: dict[str, list[tuple[str, float]]] = defaultdict(list)
    for r in conn.execute(
        f"""
        SELECT strftime('%Y-%m', t.date) AS month, t.currency,
               SUM(CASE WHEN t.kind='consumption' THEN t.amount_minor ELSE -t.amount_minor END) * 1.0 / MAX(t.scale) AS major
        FROM transactions t
        WHERE t.kind IN ('consumption','refund') AND {_exclude_confirmed_dups()}
        GROUP BY month, t.currency
        """
    ):
        by_currency[r["currency"]].append((r["month"], r["major"]))
    out = []
    for currency, series in by_currency.items():
        series.sort()
        if len(series) < 3:
            continue
        x = list(range(len(series)))
        y = [v for _, v in series]
        slope, intercept = linear_regression(x, y)
        ma_window = y[-3:]
        last_month = datetime.strptime(series[-1][0], "%Y-%m")
        for k in range(1, months_ahead + 1):
            label = (last_month + timedelta(days=31 * k)).strftime("%Y-%m")
            out.append({
                "month": label, "currency": currency,
                "moving_average": round(fmean(ma_window), 2),
                "trend": round(intercept + slope * (len(series) + k - 1), 2),
            })
    return out


def propose_dedup(conn, days: int = 2) -> list[dict]:
    """Candidate duplicates: same merchant+amount+currency within ±days, different files.
    Returns proposed pairs; inserts pending txn_links (idempotent)."""
    rows = conn.execute(
        "SELECT id, file_id, date, merchant, amount_minor, scale, currency FROM transactions "
        "WHERE status='confirmed' AND kind='consumption' ORDER BY date"
    ).fetchall()
    pairs = []
    for i in range(len(rows)):
        for j in range(i + 1, len(rows)):
            a, b = rows[i], rows[j]
            if (a["merchant"] or "").strip().lower() != (b["merchant"] or "").strip().lower():
                continue
            if a["amount_minor"] != b["amount_minor"] or a["scale"] != b["scale"] or a["currency"] != b["currency"]:
                continue
            if a["file_id"] == b["file_id"]:
                continue
            d = abs((datetime.fromisoformat(a["date"]) - datetime.fromisoformat(b["date"])).days)
            if d > days:
                continue
            lo, hi = sorted((a["id"], b["id"]))
            conn.execute(
                "INSERT OR IGNORE INTO txn_links(a, b, match_kind, status) VALUES(?,?, 'invoice_vs_statement', 'pending')",
                (lo, hi),
            )
            pairs.append({"a": lo, "b": hi, "merchant": a["merchant"], "amount_minor": a["amount_minor"],
                          "scale": a["scale"], "currency": a["currency"]})
    conn.commit()
    return pairs


def reconcile(conn, month: str, expected_minor: int, currency: str = "USD") -> dict:
    """Extracted total vs a known statement total for one month; deterministic explanation."""
    row = conn.execute(
        f"""
        SELECT COALESCE(SUM(CASE WHEN t.kind='consumption' THEN t.amount_minor ELSE -t.amount_minor END), 0) AS minor
        FROM transactions t
        WHERE strftime('%Y-%m', t.date)=? AND t.kind IN ('consumption','refund') AND t.currency=?
          AND {_exclude_confirmed_dups()}
        """,
        (month, currency),
    ).fetchone()
    extracted = row["minor"]
    diff = expected_minor - extracted
    parts: list[str] = []
    if diff != 0:
        parts.append(f"expected {expected_minor}, extracted {extracted}, difference {diff} minor units")
    n_skipped = conn.execute(
        "SELECT COUNT(*) FROM transactions WHERE status='confirmed' AND currency=? AND strftime('%Y-%m', date)=? AND kind NOT IN ('consumption','refund')",
        (currency, month),
    ).fetchone()[0]
    if n_skipped:
        parts.append(f"{n_skipped} transfer/credit-card-payment row(s) excluded from spend")
    n_dups = conn.execute(
        "SELECT COUNT(*) FROM txn_links l JOIN transactions t ON t.id=l.b "
        "WHERE t.status='confirmed' AND t.currency=? AND strftime('%Y-%m', t.date)=? AND l.status='pending'",
        (currency, month),
    ).fetchone()[0]
    if n_dups:
        parts.append(f"{n_dups} possible duplicate(s) pending review")
    n_uncat = conn.execute(
        "SELECT COUNT(*) FROM transactions WHERE status='confirmed' AND currency=? AND strftime('%Y-%m', date)=? AND kind IN ('consumption','refund') "
        "AND (category IS NULL OR category='')",
        (currency, month),
    ).fetchone()[0]
    if n_uncat:
        parts.append(f"{n_uncat} uncategorized transaction(s)")
    n_pending = conn.execute(
        "SELECT COUNT(*) FROM transactions WHERE status='pending' AND currency=? AND strftime('%Y-%m', date)=?",
        (currency, month),
    ).fetchone()[0]
    if n_pending:
        parts.append(f"{n_pending} transaction(s) pending review, excluded from totals")
    return {
        "month": month, "currency": currency,
        "expected_minor": expected_minor, "extracted_minor": extracted,
        "difference_minor": diff, "explanation": "; ".join(parts) or "totals match",
    }


def review_transaction(conn, transaction_id: int, status: str, *, commit: bool = True) -> None:
    """Human review cannot confirm an extracted row without valid source evidence."""
    if status not in {"pending", "confirmed", "rejected"}:
        raise ValueError("Invalid transaction review status")
    row = conn.execute("SELECT * FROM transactions WHERE id=?", (transaction_id,)).fetchone()
    if not row:
        raise ValueError("Unknown transaction")
    if status == "confirmed" and row["run_id"] is not None:
        ids = json.loads(row["evidence_chunk_ids"])
        valid = {r["id"] for r in conn.execute("SELECT id FROM chunks WHERE run_id=?", (row["run_id"],))}
        if not ids or any(type(cid) is not int or cid not in valid for cid in ids):
            raise ValueError("Cannot confirm a transaction without valid run-local evidence")
        if row["scale"] != db.CURRENCY_SCALES.get(row["currency"], row["scale"]):
            raise ValueError("Currency scale does not match")
    conn.execute("UPDATE transactions SET status=? WHERE id=?", (status, transaction_id))
    if commit:
        conn.commit()
