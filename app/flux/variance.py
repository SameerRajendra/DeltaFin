"""Delta / percentage / z-score variance computation across two periods."""

import numpy as np

from app import config
from app.flux import ingest

TRAILING_WINDOW = 6


def _trailing_deltas(conn, account_code: str, before_period: str, window: int = TRAILING_WINDOW) -> list[float]:
    """Month-over-month deltas for `account_code` in the `window` periods strictly before `before_period`."""
    periods = [p for p in ingest.list_periods(conn) if p < before_period]
    periods = periods[-(window + 1):]
    if len(periods) < 2:
        return []
    amounts = []
    for p in periods:
        row = conn.execute(
            "SELECT amount FROM summary WHERE period = ? AND account_code = ?", [p, account_code]
        ).fetchone()
        amounts.append(row[0] if row else 0.0)
    return [amounts[i] - amounts[i - 1] for i in range(1, len(amounts))]


def compute_variances(conn, current_period: str, prior_period: str) -> list[dict]:
    """One row per account: current vs prior amount, delta, pct, z-score, and YoY."""
    current = {r["account_code"]: r for r in ingest.get_summary(conn, current_period)}
    prior = {r["account_code"]: r for r in ingest.get_summary(conn, prior_period)}
    yoy_period = ingest.shift_period(current_period, -12)
    yoy = {r["account_code"]: r for r in ingest.get_summary(conn, yoy_period)}

    accounts = sorted(set(current) | set(prior))
    out = []
    for code in accounts:
        cur_row = current.get(code)
        pri_row = prior.get(code)
        cur_amt = cur_row["amount"] if cur_row else 0.0
        pri_amt = pri_row["amount"] if pri_row else 0.0
        name = (cur_row or pri_row)["account_name"]
        account_type = (cur_row or pri_row)["account_type"]

        delta = cur_amt - pri_amt
        pct = (delta / pri_amt) if pri_amt else (float("inf") if delta else 0.0)

        history = _trailing_deltas(conn, code, prior_period)
        if len(history) >= 2 and np.std(history) > 1e-9:
            z = float((delta - np.mean(history)) / np.std(history))
        else:
            z = 0.0

        yoy_row = yoy.get(code)
        yoy_amt = yoy_row["amount"] if yoy_row else None
        yoy_delta = (cur_amt - yoy_amt) if yoy_amt is not None else None
        yoy_pct = (yoy_delta / yoy_amt) if yoy_amt else None

        out.append(
            {
                "account_code": code,
                "account_name": name,
                "account_type": account_type,
                "current_amt": cur_amt,
                "prior_amt": pri_amt,
                "delta": round(delta, 2),
                "pct": pct,
                "z_score": round(z, 2),
                "yoy_period": yoy_period,
                "yoy_amt": yoy_amt,
                "yoy_delta": round(yoy_delta, 2) if yoy_delta is not None else None,
                "yoy_pct": yoy_pct,
            }
        )
    return out


def is_material(v: dict) -> tuple[bool, str]:
    abs_delta = abs(v["delta"])
    if abs_delta >= config.MATERIALITY_ABS:
        return True, "absolute-threshold"
    if abs_delta >= config.MATERIALITY_FLOOR and abs(v["pct"]) >= config.MATERIALITY_PCT:
        return True, "percentage-threshold"
    if abs(v["z_score"]) >= config.ANOMALY_Z:
        return True, "statistical-anomaly"
    return False, ""


def rank_materiality(variances: list[dict]) -> list[dict]:
    """Materiality-filtered, ranked queue: biggest/oddest swings first."""
    ranked = []
    for v in variances:
        material, reason = is_material(v)
        if not material:
            continue
        ranked.append({**v, "materiality_reason": reason})
    ranked.sort(key=lambda v: (abs(v["delta"]), abs(v["z_score"])), reverse=True)
    return ranked
