"""Build the local ERP/bank/CRM stand-in with synthetic finance data."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import config, store  # noqa: E402

VENDORS = [
    (1, "Acme Industrial Supply", "22-1234567", "Net 30", "active"),
    (2, "Northwind Logistics", "45-9988776", "Net 45", "active"),
    (3, "Contoso Cloud Services", "94-5551212", "Net 15", "active"),
    (4, "Fabrikam Facilities", "31-2223344", "Net 30", "active"),
]

PURCHASE_ORDERS = [
    ("PO-5001", 1, "Fasteners restock Q3", 950.00, "USD", "open", "2026-07-28"),
    ("PO-5002", 2, "Inbound freight August", 4200.00, "USD", "open", "2026-08-01"),
    ("PO-5003", 3, "Cloud platform annual", 18000.00, "USD", "open", "2026-06-15"),
    ("PO-5004", 4, "HVAC preventive maintenance", 2750.00, "USD", "closed", "2026-05-20"),
]

BANK_TRANSACTIONS = [
    ("2026-08-14", "ACH DEBIT ACME INDUSTRIAL", -950.00, "Acme Industrial Supply", "INV-1001"),
    ("2026-08-20", "ACH DEBIT CONTOSO CLOUD", -18000.00, "Contoso Cloud Services", "INV-3001"),
    ("2026-08-22", "WIRE OUT FABRIKAM FAC", -2750.00, "Fabrikam Facilities", "INV-4001"),
]


def main():
    if config.DB_PATH.exists():
        config.DB_PATH.unlink()
    conn = store.connect()
    store.init_schema(conn)
    conn.executemany(
        "INSERT INTO vendors (id, name, tax_id, payment_terms, status) VALUES (?,?,?,?,?)",
        VENDORS,
    )
    conn.executemany(
        """INSERT INTO purchase_orders (po_number, vendor_id, description, amount, currency, status, issued_date)
           VALUES (?,?,?,?,?,?,?)""",
        PURCHASE_ORDERS,
    )
    conn.executemany(
        """INSERT INTO bank_transactions (posted_date, description, amount, counterparty, reference)
           VALUES (?,?,?,?,?)""",
        BANK_TRANSACTIONS,
    )
    conn.commit()
    print(f"seeded {config.DB_PATH}")
    print(f"  vendors={len(VENDORS)} pos={len(PURCHASE_ORDERS)} bank_txns={len(BANK_TRANSACTIONS)}")


if __name__ == "__main__":
    main()
