"""Institutional memory: what the agent learned on prior runs.

Two stores, deliberately separate:
  - SQLite (`config.FLUX_DB_PATH`) -- append-only history of runs, findings,
    and analyst feedback. The system of record for "what did we say, when."
  - A JSON graph (`config.FLUX_GRAPH_PATH`) -- account <-> driver edges keyed
    by period, used to answer "has this exact driver fired before" and "what
    share of this account does this driver normally carry" in O(1) without
    re-deriving it from the SQLite history on every run.

Both survive `data/seed.py` (which only rebuilds the AP ledger) and accumulate
across `run_flux.py` invocations, which is what turns "revenue increased 18%"
into "this is the third consecutive month this driver has recurred."
"""

import json
import sqlite3
from datetime import datetime, timezone

from app import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY,
    period TEXT NOT NULL,
    prior_period TEXT NOT NULL,
    run_id TEXT,
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS findings (
    id INTEGER PRIMARY KEY,
    run_id INTEGER REFERENCES runs(id),
    period TEXT,
    account_code TEXT,
    account_name TEXT,
    headline TEXT,
    why TEXT,
    confidence TEXT,
    recurring_count INTEGER DEFAULT 1,
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS feedback (
    id INTEGER PRIMARY KEY,
    finding_id INTEGER REFERENCES findings(id),
    verdict TEXT,
    note TEXT,
    created_at TEXT
);
"""


def connect():
    config.FLUX_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(config.FLUX_DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load_graph() -> dict:
    if config.FLUX_GRAPH_PATH.exists():
        return json.loads(config.FLUX_GRAPH_PATH.read_text(encoding="utf-8"))
    return {"edges": {}}


def save_graph(graph: dict) -> None:
    config.FLUX_GRAPH_PATH.parent.mkdir(parents=True, exist_ok=True)
    config.FLUX_GRAPH_PATH.write_text(json.dumps(graph, indent=2, sort_keys=True), encoding="utf-8")


def _edge_key(account_code: str, driver_key: str) -> str:
    return f"{account_code}::{driver_key}"


def recall(graph: dict, account_code: str, driver_keys: list[str], before_period: str) -> dict:
    """What do we already know about this account and these specific drivers?

    `driver_keys` are e.g. customer/vendor ids from slicer.slice_drivers.
    Returns, per driver: how many prior periods it fired in (streak, only
    counting a run of periods immediately preceding `before_period`), its
    historical average contribution share, and the latest analyst verdict.
    """
    context = {}
    for driver_key in driver_keys:
        edge = graph["edges"].get(_edge_key(account_code, driver_key))
        if not edge:
            context[driver_key] = {"seen_before": False}
            continue
        periods = sorted(p for p in edge["periods"] if p < before_period)
        streak = 0
        cursor = before_period
        for _ in range(24):
            prev = _shift(cursor, -1)
            if prev in edge["periods"]:
                streak += 1
                cursor = prev
            else:
                break
        shares = [edge["periods"][p]["share"] for p in periods] if periods else []
        context[driver_key] = {
            "seen_before": bool(periods),
            "streak": streak,
            "avg_share": round(sum(shares) / len(shares), 4) if shares else None,
            "last_verdict": edge.get("last_verdict"),
            "last_note": edge.get("last_note"),
        }
    return context


def _shift(period: str, months: int) -> str:
    year, month = (int(p) for p in period.split("-"))
    total = year * 12 + (month - 1) + months
    return f"{total // 12:04d}-{total % 12 + 1:02d}"


def persist_findings(conn, graph: dict, period: str, prior_period: str, run_id: str, findings: list[dict]) -> int:
    """Write findings to SQLite and update the driver graph. Returns the SQLite run row id."""
    cur = conn.execute(
        "INSERT INTO runs (period, prior_period, run_id, created_at) VALUES (?,?,?,?)",
        (period, prior_period, run_id, _now()),
    )
    db_run_id = cur.lastrowid

    for finding in findings:
        conn.execute(
            """INSERT INTO findings (run_id, period, account_code, account_name, headline, why,
                   confidence, recurring_count, created_at)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (
                db_run_id,
                period,
                finding["account_code"],
                finding.get("account_name"),
                finding.get("headline"),
                finding.get("why"),
                finding.get("confidence"),
                finding.get("recurring_count", 1),
                _now(),
            ),
        )
        for driver in finding.get("drivers", []):
            key = _edge_key(finding["account_code"], driver["member_key"])
            edge = graph["edges"].setdefault(
                key,
                {
                    "account_code": finding["account_code"],
                    "driver_key": driver["member_key"],
                    "driver_name": driver.get("member_name"),
                    "periods": {},
                },
            )
            edge["periods"][period] = {"share": driver.get("contribution_share", 0.0), "delta": driver.get("delta")}
    conn.commit()
    save_graph(graph)
    return db_run_id


def record_feedback(conn, graph: dict, finding_id: int, verdict: str, note: str = "") -> None:
    conn.execute(
        "INSERT INTO feedback (finding_id, verdict, note, created_at) VALUES (?,?,?,?)",
        (finding_id, verdict, note, _now()),
    )
    row = conn.execute(
        "SELECT account_code FROM findings WHERE id = ?", (finding_id,)
    ).fetchone()
    conn.commit()
    if not row:
        return
    account_code = row["account_code"]
    for edge in graph["edges"].values():
        if edge["account_code"] == account_code:
            edge["last_verdict"] = verdict
            edge["last_note"] = note
    save_graph(graph)


def recent_findings(conn, account_code: str, limit: int = 5) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM findings WHERE account_code = ? ORDER BY id DESC LIMIT ?",
        (account_code, limit),
    ).fetchall()
    return [dict(r) for r in rows]
