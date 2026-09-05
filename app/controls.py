"""Three-way-match and AP control rules that produce audit exceptions."""

from datetime import date

from app import config

SEVERITY_RANK = {"critical": 4, "high": 3, "medium": 2, "low": 1}


def _parse_date(value):
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def _exception(code, severity, detail):
    return {"code": code, "severity": severity, "detail": detail}


def evaluate(extracted, vendor, po, bank_txn, prior_invoice):
    """Return (exceptions, recommendation) for one invoice."""
    exceptions = []
    total = extracted.get("total_amount")

    if vendor is None:
        exceptions.append(
            _exception(
                "unknown_vendor",
                "critical",
                f"'{extracted.get('vendor_name')}' is not in the vendor master. "
                "Possible fraudulent or unonboarded payee.",
            )
        )
    elif vendor.get("status") != "active":
        exceptions.append(
            _exception("inactive_vendor", "high", f"Vendor status is '{vendor.get('status')}'.")
        )

    if prior_invoice is not None:
        exceptions.append(
            _exception(
                "duplicate_invoice",
                "critical",
                f"Invoice number {extracted.get('invoice_number')} was already processed "
                f"on {prior_invoice.get('created_at')} for {prior_invoice.get('amount')}.",
            )
        )

    if not extracted.get("po_number"):
        exceptions.append(
            _exception("missing_po", "high", "No PO referenced; three-way match not possible.")
        )
    elif po is None:
        exceptions.append(
            _exception(
                "po_not_found",
                "high",
                f"PO {extracted.get('po_number')} does not exist in the ERP.",
            )
        )
    else:
        if po.get("status") == "closed":
            exceptions.append(
                _exception("po_closed", "medium", f"PO {po['po_number']} is already closed.")
            )
        if total is not None:
            variance = total - po["amount"]
            tolerance = max(po["amount"] * config.AMOUNT_TOLERANCE_PCT, config.AMOUNT_TOLERANCE_ABS)
            if variance > tolerance:
                exceptions.append(
                    _exception(
                        "amount_over_po",
                        "high",
                        f"Invoice {total:,.2f} exceeds PO {po['amount']:,.2f} by {variance:,.2f} "
                        f"(tolerance {tolerance:,.2f}).",
                    )
                )

    if bank_txn is not None:
        exceptions.append(
            _exception(
                "possible_prior_payment",
                "medium",
                f"Bank transaction on {bank_txn['posted_date']} for {bank_txn['amount']:,.2f} "
                f"references {bank_txn['reference']}. Confirm this is not a second payment.",
            )
        )

    invoice_date = _parse_date(extracted.get("invoice_date"))
    due_date = _parse_date(extracted.get("due_date"))
    if invoice_date and due_date:
        if due_date < invoice_date:
            exceptions.append(_exception("invalid_dates", "medium", "Due date precedes invoice date."))
        elif vendor and vendor.get("payment_terms", "").lower().startswith("net"):
            expected_days = int(vendor["payment_terms"].split()[-1])
            actual_days = (due_date - invoice_date).days
            if actual_days != expected_days:
                exceptions.append(
                    _exception(
                        "terms_mismatch",
                        "low",
                        f"Terms show {actual_days} days; vendor master says {vendor['payment_terms']}.",
                    )
                )

    if total is None:
        exceptions.append(_exception("unreadable_total", "high", "No invoice total could be extracted."))

    worst = max((SEVERITY_RANK[e["severity"]] for e in exceptions), default=0)
    if worst >= 3:
        recommendation = "hold"
    elif worst == 2:
        recommendation = "review"
    else:
        recommendation = "approve"
    return exceptions, recommendation
