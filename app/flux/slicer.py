"""Cohort slicing of an account's variance into its transaction-level drivers.

Groups the subledger by whichever dimension actually explains a given account
(customers for revenue, vendors for cost lines), ranks members by their
contribution to the delta, and classifies each as new / churned / expansion /
contraction so "who changed" reads as a story, not a pivot table.
"""

from app import config

_REVENUE_DIMENSION = ("customer_id", "customer_name")
_COST_DIMENSION = ("vendor", "vendor")


def _dimension_for(account_code: str) -> tuple[str, str] | None:
    if account_code.startswith("4"):
        return _REVENUE_DIMENSION
    if account_code.startswith("5") or account_code.startswith("6"):
        return _COST_DIMENSION
    return None


def _cohort(prior_amt: float, current_amt: float) -> str:
    if prior_amt == 0 and current_amt != 0:
        return "new"
    if prior_amt != 0 and current_amt == 0:
        return "churned"
    return "expansion" if current_amt > prior_amt else "contraction"


def slice_drivers(conn, account_code: str, current_period: str, prior_period: str) -> dict:
    dims = _dimension_for(account_code)
    if dims is None:
        return {"available": False, "reason": "no dimensional detail modeled for this account"}
    key_col, name_col = dims

    # COALESCE, not a NULL filter: a subledger row with no customer/vendor on it
    # is still real money moving through the account. Dropping those rows would
    # leave the driver shares silently failing to sum to the account delta;
    # bucketing them keeps the arithmetic honest and makes the gap visible.
    # It also keeps the memory-graph key stable -- an un-coalesced NULL becomes
    # an edge literally keyed `<account>::None` that occupies a MAX_DRIVERS slot
    # and carries a concentration share forever.
    rows = conn.execute(
        f"""
        SELECT COALESCE(CAST({key_col} AS VARCHAR), '(unattributed)') AS member_key,
               ANY_VALUE({name_col}) AS member_name,
               SUM(CASE WHEN period = ? THEN amount ELSE 0 END) AS current_amt,
               SUM(CASE WHEN period = ? THEN amount ELSE 0 END) AS prior_amt
        FROM txn
        WHERE account_code = ? AND period IN (?, ?)
        GROUP BY member_key
        HAVING current_amt != 0 OR prior_amt != 0
        """,
        [current_period, prior_period, account_code, current_period, prior_period],
    ).fetchall()

    if not rows:
        return {"available": False, "reason": "no subledger detail available (summary-only account)"}

    members = []
    for member_key, member_name, current_amt, prior_amt in rows:
        members.append(
            {
                "member_key": member_key,
                "member_name": member_name or member_key,
                "current_amt": current_amt,
                "prior_amt": prior_amt,
                "delta": round(current_amt - prior_amt, 2),
                "cohort": _cohort(prior_amt, current_amt),
            }
        )
    members.sort(key=lambda m: abs(m["delta"]), reverse=True)

    total_delta = sum(m["delta"] for m in members)
    running = 0.0
    for m in members:
        share = (m["delta"] / total_delta) if total_delta else 0.0
        m["contribution_share"] = round(share, 4)
        running += share
        m["cumulative_share"] = round(running, 4)

    top = members[: config.MAX_DRIVERS]
    evidence = []
    for m in top[:3]:
        ev_rows = conn.execute(
            f"""
            SELECT txn_date, vendor, memo, amount FROM txn
            WHERE account_code = ? AND period = ? AND {key_col} = ?
            ORDER BY ABS(amount) DESC LIMIT 2
            """,
            [account_code, current_period, m["member_key"]],
        ).fetchall()
        evidence.append(
            {
                "member_key": m["member_key"],
                "member_name": m["member_name"],
                "rows": [{"txn_date": r[0], "vendor": r[1], "memo": r[2], "amount": r[3]} for r in ev_rows],
            }
        )

    return {
        "available": True,
        "dimension": key_col,
        "total_delta": round(total_delta, 2),
        "drivers": top,
        "all_members": len(members),
        "evidence": evidence,
    }
