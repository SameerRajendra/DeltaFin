"""Materialized-on-connect ingestion of period summaries and transaction-level CSVs via DuckDB."""

import duckdb

from app import config

# DuckDB's auto-detected memory limit OOMs on a plain multi-file glob read, so
# threads and memory_limit are pinned; connect() tries this ladder in order and
# keeps the first limit that fits the machine's current free RAM.
_MEMORY_LADDER = ["256MB", "512MB", "1GB", "2GB", "4GB"]

_SUMMARY_TYPES = {
    "period": "VARCHAR",
    "account_code": "VARCHAR",
    "account_name": "VARCHAR",
    "account_type": "VARCHAR",
    "amount": "DOUBLE",
}
_TXN_TYPES = {
    "txn_id": "VARCHAR",
    "period": "VARCHAR",
    "txn_date": "VARCHAR",
    "account_code": "VARCHAR",
    "entity": "VARCHAR",
    "department": "VARCHAR",
    "region": "VARCHAR",
    "segment": "VARCHAR",
    "customer_id": "VARCHAR",
    "customer_name": "VARCHAR",
    "vendor": "VARCHAR",
    "amount": "DOUBLE",
    "memo": "VARCHAR",
}


def connect():
    """A DuckDB connection with `summary` and `txn` materialized as tables.

    Materialized, not views, so a `--replay` walk's many queries hit an
    already-parsed in-memory table instead of re-scanning every period CSV
    each time. Since DuckDB's memory_limit must be pinned explicitly, we probe
    `_MEMORY_LADDER` and keep the first limit the machine can currently satisfy.
    """
    last_error = None
    for limit in _MEMORY_LADDER:
        conn = duckdb.connect(config={"threads": 1, "memory_limit": limit})
        try:
            # CREATE TABLE can't bind prepared-statement parameters; the globs
            # come from our own config (not user input), so inlining is safe.
            conn.execute(
                f"CREATE TABLE summary AS SELECT * FROM "
                f"read_csv_auto('{config.SUMMARIES_GLOB}', types={_SUMMARY_TYPES})"
            )
            conn.execute(
                f"CREATE TABLE txn AS SELECT * FROM "
                f"read_csv_auto('{config.TRANSACTIONS_GLOB}', types={_TXN_TYPES})"
            )
            return conn
        except duckdb.OutOfMemoryException as exc:
            last_error = exc
            conn.close()
    raise last_error


def list_periods(conn) -> list[str]:
    rows = conn.execute("SELECT DISTINCT period FROM summary ORDER BY period").fetchall()
    return [r[0] for r in rows]


def shift_period(period: str, months: int) -> str:
    year, month = (int(p) for p in period.split("-"))
    total = year * 12 + (month - 1) + months
    return f"{total // 12:04d}-{total % 12 + 1:02d}"


def get_summary(conn, period: str) -> list[dict]:
    rows = conn.execute(
        "SELECT account_code, account_name, account_type, amount FROM summary "
        "WHERE period = ? ORDER BY account_code",
        [period],
    ).fetchall()
    return [
        {"account_code": r[0], "account_name": r[1], "account_type": r[2], "amount": r[3]}
        for r in rows
    ]


def tie_out(conn, period: str) -> list[dict]:
    """Per-account: does the subledger (txn detail) reconcile to the summary total?

    Returns one row per account with the summary amount, the subledger total,
    and the coverage percentage -- 0% for a summary-only accrual line like
    Insurance, honestly, rather than hiding the gap.
    """
    rows = conn.execute(
        """
        SELECT s.account_code, s.account_name, s.amount AS summary_amt,
               COALESCE(t.txn_total, 0) AS txn_amt
        FROM summary s
        LEFT JOIN (
            SELECT account_code, SUM(amount) AS txn_total
            FROM txn WHERE period = ?
            GROUP BY account_code
        ) t ON t.account_code = s.account_code
        WHERE s.period = ?
        ORDER BY s.account_code
        """,
        [period, period],
    ).fetchall()
    out = []
    for account_code, account_name, summary_amt, txn_amt in rows:
        coverage = (txn_amt / summary_amt) if summary_amt else (1.0 if txn_amt == 0 else 0.0)
        out.append(
            {
                "account_code": account_code,
                "account_name": account_name,
                "summary_amt": summary_amt,
                "txn_amt": txn_amt,
                "gap": round(summary_amt - txn_amt, 2),
                "coverage_pct": round(coverage * 100, 1),
            }
        )
    return out
