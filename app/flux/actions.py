"""Turn findings into a prioritized, owned action plan.

A finding says what changed and why. This module answers the question a
reader actually has to act on: who should look at this, how urgently, and
what should they do. Priority and owner are assigned deterministically from
the same materiality/coverage signals already computed upstream, so the plan
is reproducible even when the LLM path is skipped entirely.
"""

from app import config

_OWNER_BY_CODE = {
    "4000": "Sales / RevOps (Enterprise)",
    "4010": "Sales / RevOps (Mid-Market)",
    "4020": "Sales / RevOps (SMB)",
    "5000": "Vendor Management / Engineering",
    "6000": "Marketing",
    "6010": "Finance / Legal",
    "6020": "Engineering Leadership",
    "6100": "Finance / T&E Policy",
    "7000": "Controller / Accounting",
}

_PRIORITY_ORDER = {"P1": 0, "P2": 1, "P3": 2}
_PRIORITY_LABEL = {
    "P1": "Act this week",
    "P2": "Review this close cycle",
    "P3": "Monitor, no action yet",
}


def owner_for(account_code: str, account_type: str = "") -> str:
    if account_code in _OWNER_BY_CODE:
        return _OWNER_BY_CODE[account_code]
    if account_code.startswith("4"):
        return "Sales / RevOps"
    if account_code.startswith("5"):
        return "Vendor Management / Engineering"
    if account_type == "other":
        return "Controller / Accounting"
    return "FP&A"


def default_priority(drilldown: dict) -> str:
    """Priority for a material finding, from the same signals `variance.is_material` used."""
    abs_delta = abs(drilldown["delta"])
    z = abs(drilldown.get("z_score") or 0)
    if abs_delta >= config.MATERIALITY_ABS * 2 or z >= config.ANOMALY_Z * 1.5:
        return "P1"
    if drilldown["slice"].get("available"):
        return "P2"
    # No subledger detail to point to -- nothing concrete to act on yet.
    return "P3"


def gap_priority(coverage_pct: float, gap_amount: float = 0.0) -> str | None:
    """Priority for a subledger tie-out gap, from coverage AND the money behind it.

    Coverage alone is not enough. Judging on percentage only made a $5,000
    accrual that has not moved in twenty periods outrank a $96,000 revenue
    swing, and -- because that accrual is 0% covered in every period -- put the
    identical row at the top of every month's action list. An unreconciled
    balance is urgent in proportion to what is unreconciled, so this uses the
    same materiality constants that gate everything else.
    """
    if coverage_pct >= 99.9:
        return None
    gap = abs(gap_amount)
    if gap >= config.MATERIALITY_ABS and coverage_pct < 50:
        return "P1"
    if gap >= config.MATERIALITY_FLOOR:
        return "P2"
    return "P3"


def build_plan(findings: list[dict], tie_out: list[dict]) -> list[dict]:
    """One prioritized, owned task per material finding and per uncovered tie-out gap."""
    items = []
    for f in findings:
        priority = f.get("priority") or "P2"
        items.append(
            {
                "priority": priority,
                "priority_label": _PRIORITY_LABEL.get(priority, ""),
                "account_code": f["account_code"],
                "account_name": f["account_name"],
                "owner": f.get("owner") or "FP&A",
                "task": f.get("action") or "Review this variance.",
                "context": f.get("headline", ""),
                "amount": abs(f.get("delta") or 0),
            }
        )
    by_account = {i["account_code"]: i for i in items}
    for row in tie_out:
        priority = gap_priority(row["coverage_pct"], row.get("gap", 0.0))
        if not priority:
            continue
        gap_task = (
            f"Confirm the {row['account_name']} accrual: only {row['coverage_pct']}% "
            f"traced to subledger detail (gap ${row['gap']:,.2f})."
        )
        existing = by_account.get(row["account_code"])
        if existing is not None:
            # One row per account. An account can be both materially moved AND
            # short on subledger coverage -- Insurance in the premium-true-up
            # month is exactly that -- and emitting a task for each produced two
            # near-identical rows at different priorities for one underlying
            # problem. Keep the more urgent of the two, and say both reasons.
            if _PRIORITY_ORDER.get(priority, 9) < _PRIORITY_ORDER.get(existing["priority"], 9):
                existing["priority"] = priority
                existing["priority_label"] = _PRIORITY_LABEL.get(priority, "")
                existing["owner"] = "Controller / Accounting"
                existing["task"] = gap_task
            existing["context"] = (
                f"{existing['context']} · Subledger tie-out gap"
                if existing["context"]
                else "Subledger tie-out gap"
            )
            existing["amount"] = max(existing["amount"], abs(row["gap"]))
            continue
        item = {
            "priority": priority,
            "priority_label": _PRIORITY_LABEL.get(priority, ""),
            "account_code": row["account_code"],
            "account_name": row["account_name"],
            "owner": "Controller / Accounting",
            "task": gap_task,
            "context": "Subledger tie-out gap",
            "amount": abs(row["gap"]),
        }
        items.append(item)
        by_account[row["account_code"]] = item
    items.sort(key=lambda i: (_PRIORITY_ORDER.get(i["priority"], 9), -i["amount"]))
    return items
