"""Tier 0 tests for `app/flux/slicer.py`.

`_cohort` and `_dimension_for` are pure. `slice_drivers` needs a `txn` table,
so the tests build a tiny in-memory DuckDB one rather than reading the seeded
CSVs -- every share asserted below is hand-computed from the rows inserted in
the same test.
"""

import duckdb
import pytest

from app import config
from app.flux import slicer


# --- _cohort ----------------------------------------------------------------


def test_cohort_new():
    assert slicer._cohort(prior_amt=0.0, current_amt=25_000.0) == "new"


def test_cohort_churned():
    assert slicer._cohort(prior_amt=34_000.0, current_amt=0.0) == "churned"


def test_cohort_expansion():
    assert slicer._cohort(prior_amt=70_000.0, current_amt=100_000.0) == "expansion"


def test_cohort_contraction():
    assert slicer._cohort(prior_amt=100_000.0, current_amt=70_000.0) == "contraction"


def test_cohort_flat_member_reads_as_contraction():
    # Documented quirk, not a bug report: the expansion test is a strict `>`,
    # so a member that did not move at all falls through to "contraction".
    # Such a member has a zero delta and therefore a zero contribution share,
    # so it never leads a driver table -- but the label is what it is.
    assert slicer._cohort(prior_amt=50_000.0, current_amt=50_000.0) == "contraction"


# --- _dimension_for ---------------------------------------------------------


def test_dimension_for_revenue_accounts_is_customer():
    assert slicer._dimension_for("4000") == ("customer_id", "customer_name")
    assert slicer._dimension_for("4020") == ("customer_id", "customer_name")


def test_dimension_for_cogs_accounts_is_vendor():
    assert slicer._dimension_for("5000") == ("vendor", "vendor")


def test_dimension_for_opex_accounts_is_vendor():
    assert slicer._dimension_for("6010") == ("vendor", "vendor")
    assert slicer._dimension_for("6100") == ("vendor", "vendor")


def test_dimension_for_other_accounts_is_none():
    # 7xxx: balance-sheet / accrual codes have no customer or vendor dimension
    # to slice, so there is nothing to drill even when the account moves.
    assert slicer._dimension_for("7000") is None


# --- slice_drivers ----------------------------------------------------------

PRIOR = "2026-07"
CURRENT = "2026-08"


@pytest.fixture
def txn_conn():
    conn = duckdb.connect()
    conn.execute(
        "CREATE TABLE txn (period VARCHAR, txn_date VARCHAR, account_code VARCHAR, "
        "customer_id VARCHAR, customer_name VARCHAR, vendor VARCHAR, amount DOUBLE, memo VARCHAR)"
    )
    yield conn
    conn.close()


def _txn(conn, period, account_code, amount, customer_id=None, customer_name=None, vendor=None):
    conn.execute(
        "INSERT INTO txn VALUES (?,?,?,?,?,?,?,?)",
        [period, f"{period}-10", account_code, customer_id, customer_name, vendor, amount, "memo"],
    )


def test_slice_unavailable_for_an_account_with_no_modeled_dimension(txn_conn):
    result = slicer.slice_drivers(txn_conn, "7000", CURRENT, PRIOR)
    assert result["available"] is False
    assert "no dimensional detail modeled" in result["reason"]


def test_slice_unavailable_when_the_account_has_no_subledger_rows(txn_conn):
    result = slicer.slice_drivers(txn_conn, "4000", CURRENT, PRIOR)
    assert result["available"] is False
    assert "summary-only" in result["reason"]


def test_contribution_and_cumulative_shares(txn_conn):
    # C1: 100 -> 200 (+100), C2: 100 -> 100 (0), C3: absent -> 50 (+50).
    # total_delta = 150.
    _txn(txn_conn, PRIOR, "4000", 100.0, "C1", "Cust One")
    _txn(txn_conn, PRIOR, "4000", 100.0, "C2", "Cust Two")
    _txn(txn_conn, CURRENT, "4000", 200.0, "C1", "Cust One")
    _txn(txn_conn, CURRENT, "4000", 100.0, "C2", "Cust Two")
    _txn(txn_conn, CURRENT, "4000", 50.0, "C3", "Cust Three")

    result = slicer.slice_drivers(txn_conn, "4000", CURRENT, PRIOR)
    assert result["available"] is True
    assert result["dimension"] == "customer_id"
    assert result["total_delta"] == 150.0
    assert result["all_members"] == 3

    # Ranked by abs(delta) descending.
    assert [d["member_key"] for d in result["drivers"]] == ["C1", "C3", "C2"]
    c1, c3, c2 = result["drivers"]

    assert c1["delta"] == 100.0
    assert c1["cohort"] == "expansion"
    assert c1["contribution_share"] == 0.6667
    assert c1["cumulative_share"] == 0.6667

    assert c3["delta"] == 50.0
    assert c3["cohort"] == "new"
    assert c3["contribution_share"] == 0.3333
    # The two movers together explain the whole delta.
    assert c3["cumulative_share"] == 1.0

    assert c2["delta"] == 0.0
    assert c2["contribution_share"] == 0.0
    assert c2["cumulative_share"] == 1.0

    # The shares are a decomposition of the account delta, so they sum to 1.
    assert round(sum(d["contribution_share"] for d in result["drivers"]), 4) == 1.0


def test_total_delta_zero_guard_yields_zero_shares_not_a_division_error(txn_conn):
    # Offsetting movements: +100 and -100 -> total_delta == 0.
    _txn(txn_conn, PRIOR, "4000", 100.0, "C1", "Cust One")
    _txn(txn_conn, PRIOR, "4000", 200.0, "C2", "Cust Two")
    _txn(txn_conn, CURRENT, "4000", 200.0, "C1", "Cust One")
    _txn(txn_conn, CURRENT, "4000", 100.0, "C2", "Cust Two")

    result = slicer.slice_drivers(txn_conn, "4000", CURRENT, PRIOR)
    assert result["total_delta"] == 0.0
    assert all(d["contribution_share"] == 0.0 for d in result["drivers"])
    assert all(d["cumulative_share"] == 0.0 for d in result["drivers"])


def test_churned_member_disappears_once_both_periods_are_zero(txn_conn):
    # The HAVING clause keeps a member only while it has money in one of the
    # two periods, which is why a churned customer shows up exactly once (in
    # its churn period) and never again.
    _txn(txn_conn, PRIOR, "4010", 34_000.0, "MID-04", "Fabrikam Mid")
    _txn(txn_conn, PRIOR, "4010", 30_000.0, "MID-01", "Acme Mid")
    _txn(txn_conn, CURRENT, "4010", 30_000.0, "MID-01", "Acme Mid")

    churn_period = slicer.slice_drivers(txn_conn, "4010", CURRENT, PRIOR)
    assert [d["member_key"] for d in churn_period["drivers"]][0] == "MID-04"
    assert churn_period["drivers"][0]["cohort"] == "churned"

    # A later comparison in which MID-04 is absent from both sides.
    _txn(txn_conn, "2026-09", "4010", 30_000.0, "MID-01", "Acme Mid")
    after = slicer.slice_drivers(txn_conn, "4010", "2026-09", CURRENT)
    assert "MID-04" not in [d["member_key"] for d in after["drivers"]]


def test_unattributed_rows_are_bucketed_rather_than_dropped(txn_conn):
    # A cost row with no vendor on it is still real money. COALESCE buckets it
    # as '(unattributed)' so the driver shares still sum to the account delta.
    _txn(txn_conn, PRIOR, "5000", 38_000.0, vendor="NimbusHost Cloud")
    _txn(txn_conn, CURRENT, "5000", 40_000.0, vendor="NimbusHost Cloud")
    _txn(txn_conn, CURRENT, "5000", 6_000.0, vendor=None)

    result = slicer.slice_drivers(txn_conn, "5000", CURRENT, PRIOR)
    assert result["dimension"] == "vendor"
    keys = [d["member_key"] for d in result["drivers"]]
    assert "(unattributed)" in keys
    unattributed = next(d for d in result["drivers"] if d["member_key"] == "(unattributed)")
    # member_name falls back to member_key when the name column is NULL.
    assert unattributed["member_name"] == "(unattributed)"
    assert unattributed["delta"] == 6_000.0
    assert unattributed["cohort"] == "new"
    assert result["total_delta"] == 8_000.0


def test_driver_list_is_capped_at_max_drivers(txn_conn):
    for i in range(config.MAX_DRIVERS + 2):
        _txn(txn_conn, CURRENT, "4000", 1_000.0 * (i + 1), f"C{i}", f"Cust {i}")
    result = slicer.slice_drivers(txn_conn, "4000", CURRENT, PRIOR)
    assert result["all_members"] == config.MAX_DRIVERS + 2
    assert len(result["drivers"]) == config.MAX_DRIVERS
    # Evidence is gathered for the top 3 drivers only.
    assert len(result["evidence"]) == 3
