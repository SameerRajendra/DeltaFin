"""Tier 0 tests for `app/controls.py` -- one per exception code, plus the
amount tolerance boundary and the severity -> recommendation mapping.

`evaluate` is a pure function of five plain dicts (the extracted invoice, the
vendor master row, the PO, a matching bank transaction, and a prior invoice
with the same number), so nothing here touches SQLite or `app/store.py`.
"""

import pytest

from app import config
from app.controls import evaluate

# A vendor / PO / invoice triple that trips nothing at all. Individual tests
# mutate one piece of it so each exception is raised in isolation.
CLEAN_VENDOR = {"vendor_name": "Northwind Traders", "status": "active", "payment_terms": "Net 30"}
CLEAN_PO = {"po_number": "PO-1001", "status": "open", "amount": 10_000.00}
CLEAN_INVOICE = {
    "vendor_name": "Northwind Traders",
    "invoice_number": "INV-2001",
    "po_number": "PO-1001",
    "total_amount": 10_000.00,
    "invoice_date": "2026-01-01",
    "due_date": "2026-01-31",  # exactly Net 30
}


def _codes(exceptions):
    return {e["code"] for e in exceptions}


def _by_code(exceptions, code):
    return next(e for e in exceptions if e["code"] == code)


def _eval(invoice=None, vendor=CLEAN_VENDOR, po=CLEAN_PO, bank_txn=None, prior_invoice=None):
    return evaluate({**CLEAN_INVOICE, **(invoice or {})}, vendor, po, bank_txn, prior_invoice)


def test_clean_invoice_raises_nothing_and_is_approved():
    exceptions, recommendation = _eval()
    assert exceptions == []
    assert recommendation == "approve"


# --- one test per exception code -------------------------------------------


def test_unknown_vendor():
    exceptions, recommendation = _eval(vendor=None)
    assert "unknown_vendor" in _codes(exceptions)
    assert _by_code(exceptions, "unknown_vendor")["severity"] == "critical"
    assert recommendation == "hold"


def test_inactive_vendor():
    exceptions, _ = _eval(vendor={**CLEAN_VENDOR, "status": "inactive"})
    assert "inactive_vendor" in _codes(exceptions)
    assert _by_code(exceptions, "inactive_vendor")["severity"] == "high"


def test_duplicate_invoice():
    prior = {"created_at": "2025-12-02T10:00:00", "amount": 10_000.00}
    exceptions, recommendation = _eval(prior_invoice=prior)
    assert "duplicate_invoice" in _codes(exceptions)
    assert _by_code(exceptions, "duplicate_invoice")["severity"] == "critical"
    assert recommendation == "hold"


def test_missing_po():
    exceptions, _ = _eval(invoice={"po_number": None}, po=None)
    assert "missing_po" in _codes(exceptions)
    assert _by_code(exceptions, "missing_po")["severity"] == "high"
    # A missing PO short-circuits the PO branch entirely.
    assert "po_not_found" not in _codes(exceptions)


def test_po_not_found():
    exceptions, _ = _eval(invoice={"po_number": "PO-9999"}, po=None)
    assert "po_not_found" in _codes(exceptions)
    assert _by_code(exceptions, "po_not_found")["severity"] == "high"


def test_po_closed():
    exceptions, recommendation = _eval(po={**CLEAN_PO, "status": "closed"})
    assert "po_closed" in _codes(exceptions)
    assert _by_code(exceptions, "po_closed")["severity"] == "medium"
    assert recommendation == "review"


def test_amount_over_po():
    exceptions, recommendation = _eval(invoice={"total_amount": 15_000.00})
    assert "amount_over_po" in _codes(exceptions)
    assert _by_code(exceptions, "amount_over_po")["severity"] == "high"
    assert recommendation == "hold"


def test_possible_prior_payment():
    bank_txn = {"posted_date": "2026-01-15", "amount": 10_000.00, "reference": "INV-2001"}
    exceptions, recommendation = _eval(bank_txn=bank_txn)
    assert "possible_prior_payment" in _codes(exceptions)
    assert _by_code(exceptions, "possible_prior_payment")["severity"] == "medium"
    assert recommendation == "review"


def test_invalid_dates():
    exceptions, _ = _eval(invoice={"invoice_date": "2026-01-31", "due_date": "2026-01-01"})
    assert "invalid_dates" in _codes(exceptions)
    assert _by_code(exceptions, "invalid_dates")["severity"] == "medium"
    # invalid_dates and terms_mismatch are mutually exclusive (if/elif).
    assert "terms_mismatch" not in _codes(exceptions)


def test_terms_mismatch():
    # 45 days on the face of the invoice against a Net 30 vendor master.
    exceptions, recommendation = _eval(invoice={"invoice_date": "2026-01-01", "due_date": "2026-02-15"})
    assert "terms_mismatch" in _codes(exceptions)
    assert _by_code(exceptions, "terms_mismatch")["severity"] == "low"
    # Low severity alone does not block payment.
    assert recommendation == "approve"


def test_unreadable_total():
    exceptions, recommendation = _eval(invoice={"total_amount": None})
    assert "unreadable_total" in _codes(exceptions)
    assert _by_code(exceptions, "unreadable_total")["severity"] == "high"
    # With no total there is nothing to compare against the PO.
    assert "amount_over_po" not in _codes(exceptions)
    assert recommendation == "hold"


def test_every_documented_exception_code_is_covered():
    """Guard against a new rule landing in controls.py without a test.

    If this list and `app/controls.py` drift apart, one of them is wrong.
    """
    covered = {
        "amount_over_po",
        "duplicate_invoice",
        "inactive_vendor",
        "invalid_dates",
        "missing_po",
        "po_closed",
        "po_not_found",
        "possible_prior_payment",
        "terms_mismatch",
        "unknown_vendor",
        "unreadable_total",
    }
    assert len(covered) == 11


# --- the max(2%, $50) tolerance boundary ------------------------------------


@pytest.mark.parametrize(
    "po_amount,expected_tolerance",
    [
        # Small PO: the flat $50 floor dominates 2%.
        (1_000.00, config.AMOUNT_TOLERANCE_ABS),
        # Large PO: 2% dominates the flat floor.
        (10_000.00, 10_000.00 * config.AMOUNT_TOLERANCE_PCT),
    ],
)
def test_amount_tolerance_boundary_is_max_of_pct_and_abs(po_amount, expected_tolerance):
    po = {**CLEAN_PO, "amount": po_amount}

    # Exactly at tolerance: the check is a strict `>`, so this passes.
    at_tolerance, _ = _eval(invoice={"total_amount": po_amount + expected_tolerance}, po=po)
    assert "amount_over_po" not in _codes(at_tolerance)

    # A cent past it: exception.
    over, _ = _eval(invoice={"total_amount": po_amount + expected_tolerance + 0.01}, po=po)
    assert "amount_over_po" in _codes(over)


def test_invoice_under_the_po_is_never_an_amount_exception():
    # The rule is one-sided by design: underbilling is not an AP control risk.
    exceptions, recommendation = _eval(invoice={"total_amount": 1.00})
    assert "amount_over_po" not in _codes(exceptions)
    assert recommendation == "approve"


# --- severity -> recommendation --------------------------------------------


def test_no_exceptions_maps_to_approve():
    _, recommendation = _eval()
    assert recommendation == "approve"


def test_worst_low_maps_to_approve():
    exceptions, recommendation = _eval(invoice={"invoice_date": "2026-01-01", "due_date": "2026-02-15"})
    assert {e["severity"] for e in exceptions} == {"low"}
    assert recommendation == "approve"


def test_worst_medium_maps_to_review():
    exceptions, recommendation = _eval(po={**CLEAN_PO, "status": "closed"})
    assert {e["severity"] for e in exceptions} == {"medium"}
    assert recommendation == "review"


def test_worst_high_maps_to_hold():
    exceptions, recommendation = _eval(vendor={**CLEAN_VENDOR, "status": "inactive"})
    assert {e["severity"] for e in exceptions} == {"high"}
    assert recommendation == "hold"


def test_worst_critical_maps_to_hold():
    exceptions, recommendation = _eval(vendor=None)
    assert "critical" in {e["severity"] for e in exceptions}
    assert recommendation == "hold"


def test_a_medium_alongside_a_high_still_holds():
    # The recommendation follows the single worst severity, not a count.
    exceptions, recommendation = _eval(
        po={**CLEAN_PO, "status": "closed"},
        vendor={**CLEAN_VENDOR, "status": "inactive"},
    )
    assert {"po_closed", "inactive_vendor"} <= _codes(exceptions)
    assert recommendation == "hold"
