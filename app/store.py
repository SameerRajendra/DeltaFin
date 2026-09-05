"""Financial semantic layer: a local SQLite stand-in for ERP + bank feed + CRM."""

import json
import sqlite3
from datetime import datetime, timezone

from app import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS vendors (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    tax_id TEXT,
    payment_terms TEXT,
    status TEXT DEFAULT 'active'
);
CREATE TABLE IF NOT EXISTS purchase_orders (
    id INTEGER PRIMARY KEY,
    po_number TEXT UNIQUE NOT NULL,
    vendor_id INTEGER REFERENCES vendors(id),
    description TEXT,
    amount REAL NOT NULL,
    currency TEXT DEFAULT 'USD',
    status TEXT DEFAULT 'open',
    issued_date TEXT
);
CREATE TABLE IF NOT EXISTS bank_transactions (
    id INTEGER PRIMARY KEY,
    posted_date TEXT,
    description TEXT,
    amount REAL,
    counterparty TEXT,
    reference TEXT
);
CREATE TABLE IF NOT EXISTS invoices (
    id INTEGER PRIMARY KEY,
    invoice_number TEXT,
    vendor_name TEXT,
    vendor_id INTEGER,
    amount REAL,
    currency TEXT,
    invoice_date TEXT,
    due_date TEXT,
    po_number TEXT,
    source_file TEXT,
    status TEXT DEFAULT 'pending_review',
    recommendation TEXT,
    narrative TEXT,
    workpaper_path TEXT,
    extracted_json TEXT,
    run_id TEXT,
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS invoice_exceptions (
    id INTEGER PRIMARY KEY,
    invoice_id INTEGER REFERENCES invoices(id),
    code TEXT,
    severity TEXT,
    detail TEXT
);
CREATE TABLE IF NOT EXISTS approvals (
    id INTEGER PRIMARY KEY,
    invoice_id INTEGER REFERENCES invoices(id),
    decision TEXT,
    decided_by TEXT,
    decided_at TEXT,
    note TEXT
);
"""


def connect():
    config.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    # check_same_thread=False: Streamlit reruns the script on a different thread
    # than the one that opened the cached connection.
    conn = sqlite3.connect(config.DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_schema(conn):
    conn.executescript(SCHEMA)
    conn.commit()


def _row_to_dict(row):
    return dict(row) if row is not None else None


def find_vendor(conn, name):
    if not name:
        return None
    row = conn.execute(
        "SELECT * FROM vendors WHERE lower(name) = lower(?)", (name.strip(),)
    ).fetchone()
    if row is None:
        row = conn.execute(
            "SELECT * FROM vendors WHERE lower(name) LIKE lower(?)",
            (f"%{name.strip().split()[0]}%",),
        ).fetchone()
    return _row_to_dict(row)


def find_po(conn, po_number):
    if not po_number:
        return None
    row = conn.execute(
        "SELECT * FROM purchase_orders WHERE upper(po_number) = upper(?)",
        (po_number.strip(),),
    ).fetchone()
    return _row_to_dict(row)


def find_bank_payment(conn, invoice_number, amount):
    """Look for a settled payment referencing this invoice, or matching its amount."""
    if invoice_number:
        row = conn.execute(
            "SELECT * FROM bank_transactions WHERE upper(reference) = upper(?)",
            (invoice_number.strip(),),
        ).fetchone()
        if row:
            return _row_to_dict(row)
    if amount:
        row = conn.execute(
            "SELECT * FROM bank_transactions WHERE abs(abs(amount) - ?) < 0.01",
            (abs(float(amount)),),
        ).fetchone()
        return _row_to_dict(row)
    return None


def find_prior_invoice(conn, invoice_number, exclude_id=None):
    if not invoice_number:
        return None
    sql = "SELECT * FROM invoices WHERE upper(invoice_number) = upper(?)"
    params = [invoice_number.strip()]
    if exclude_id is not None:
        sql += " AND id != ?"
        params.append(exclude_id)
    return _row_to_dict(conn.execute(sql, params).fetchone())


def save_invoice(conn, extracted, exceptions, recommendation, narrative, workpaper_path, run_id, source_file):
    cur = conn.execute(
        """INSERT INTO invoices (invoice_number, vendor_name, vendor_id, amount, currency,
               invoice_date, due_date, po_number, source_file, status, recommendation,
               narrative, workpaper_path, extracted_json, run_id, created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            extracted.get("invoice_number"),
            extracted.get("vendor_name"),
            extracted.get("vendor_id"),
            extracted.get("total_amount"),
            extracted.get("currency", "USD"),
            extracted.get("invoice_date"),
            extracted.get("due_date"),
            extracted.get("po_number"),
            source_file,
            "pending_review",
            recommendation,
            narrative,
            workpaper_path,
            json.dumps(extracted),
            run_id,
            datetime.now(timezone.utc).isoformat(timespec="seconds"),
        ),
    )
    invoice_id = cur.lastrowid
    for exc in exceptions:
        conn.execute(
            "INSERT INTO invoice_exceptions (invoice_id, code, severity, detail) VALUES (?,?,?,?)",
            (invoice_id, exc["code"], exc["severity"], exc["detail"]),
        )
    conn.commit()
    return invoice_id


def list_invoices(conn, status=None):
    sql = "SELECT * FROM invoices"
    params = ()
    if status:
        sql += " WHERE status = ?"
        params = (status,)
    sql += " ORDER BY id DESC"
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def get_exceptions(conn, invoice_id):
    return [
        dict(r)
        for r in conn.execute(
            "SELECT * FROM invoice_exceptions WHERE invoice_id = ?", (invoice_id,)
        ).fetchall()
    ]


def record_decision(conn, invoice_id, decision, decided_by, note=""):
    conn.execute(
        "INSERT INTO approvals (invoice_id, decision, decided_by, decided_at, note) VALUES (?,?,?,?,?)",
        (
            invoice_id,
            decision,
            decided_by,
            datetime.now(timezone.utc).isoformat(timespec="seconds"),
            note,
        ),
    )
    conn.execute("UPDATE invoices SET status = ? WHERE id = ?", (decision, invoice_id))
    conn.commit()
