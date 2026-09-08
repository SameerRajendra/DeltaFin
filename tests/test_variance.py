"""Tier 0 tests for `app/flux/variance.py`.

Pure math plus one in-memory DuckDB fixture. No LLM, no seeded data files,
no network. Every expected number here is derived from `app/config.py`'s
thresholds or computed by hand from the fixture rows in the test itself.
"""

import duckdb
import pytest

from app import config
from app.flux import variance


def _v(delta=0.0, pct=0.0, z=0.0):
    """The minimal shape `is_material` reads off a variance row."""
    return {"delta": delta, "pct": pct, "z_score": z}


# --- the three materiality gates -------------------------------------------


def test_absolute_gate_at_boundary():
    material, reason = variance.is_material(_v(delta=config.MATERIALITY_ABS))
    assert material is True
    assert reason == "absolute-threshold"


def test_absolute_gate_just_below_boundary_is_not_material():
    # $24,999.99 with no percentage or z signal clears nothing.
    material, reason = variance.is_material(_v(delta=config.MATERIALITY_ABS - 0.01))
    assert material is False
    assert reason == ""


def test_absolute_gate_is_sign_agnostic():
    material, reason = variance.is_material(_v(delta=-config.MATERIALITY_ABS))
    assert (material, reason) == (True, "absolute-threshold")


def test_percentage_gate_at_both_boundaries():
    # Needs BOTH: >= MATERIALITY_FLOOR in dollars AND >= MATERIALITY_PCT.
    material, reason = variance.is_material(
        _v(delta=config.MATERIALITY_FLOOR, pct=config.MATERIALITY_PCT)
    )
    assert (material, reason) == (True, "percentage-threshold")


def test_percentage_gate_below_the_dollar_floor_is_not_material():
    # A 50% move on a tiny account is still de minimis.
    material, _ = variance.is_material(_v(delta=config.MATERIALITY_FLOOR - 0.01, pct=0.5))
    assert material is False


def test_percentage_gate_below_the_pct_threshold_is_not_material():
    # Clears the floor in dollars, but only 9.99% -- and not enough to trip
    # the absolute gate either.
    material, _ = variance.is_material(_v(delta=10_000.0, pct=config.MATERIALITY_PCT - 0.0001))
    assert material is False


def test_anomaly_gate_at_boundary_catches_a_small_dollar_move():
    material, reason = variance.is_material(_v(delta=100.0, pct=0.0, z=config.ANOMALY_Z))
    assert (material, reason) == (True, "statistical-anomaly")


def test_anomaly_gate_just_below_boundary_is_not_material():
    material, _ = variance.is_material(_v(delta=100.0, pct=0.0, z=config.ANOMALY_Z - 0.01))
    assert material is False


def test_gate_precedence_absolute_wins_over_percentage_and_anomaly():
    # All three would fire; the reported reason is the first gate checked.
    material, reason = variance.is_material(_v(delta=100_000.0, pct=0.9, z=9.0))
    assert (material, reason) == (True, "absolute-threshold")


def test_infinite_pct_clears_the_percentage_gate():
    # `compute_variances` emits inf when prior_amt == 0; abs(inf) >= 0.10.
    material, reason = variance.is_material(_v(delta=6_000.0, pct=float("inf")))
    assert (material, reason) == (True, "percentage-threshold")


# --- rank_materiality -------------------------------------------------------


def _row(code, delta, pct=0.0, z=0.0):
    return {
        "account_code": code,
        "account_name": code,
        "delta": delta,
        "pct": pct,
        "z_score": z,
    }


def test_rank_materiality_drops_immaterial_rows_and_stamps_the_reason():
    ranked = variance.rank_materiality(
        [_row("4000", 96_000.0), _row("6100", 12.0), _row("6020", 300.0, z=3.0)]
    )
    assert [r["account_code"] for r in ranked] == ["4000", "6020"]
    assert ranked[0]["materiality_reason"] == "absolute-threshold"
    assert ranked[1]["materiality_reason"] == "statistical-anomaly"


def test_rank_materiality_orders_by_abs_delta_then_abs_z():
    ranked = variance.rank_materiality(
        [
            _row("A", 30_000.0, z=1.0),
            _row("B", 100_000.0),
            _row("C", -60_000.0),
            _row("D", 30_000.0, z=5.0),
        ]
    )
    # abs(delta) descending; the 30k tie breaks on abs(z_score).
    assert [r["account_code"] for r in ranked] == ["B", "C", "D", "A"]


def test_rank_materiality_itself_does_not_cap_at_max_drilldowns():
    """The MAX_DRILLDOWNS cap lives in the caller, not in this function.

    `app/flux/graph.py`'s `rank_materiality` node slices
    `variance.rank_materiality(...)[: config.MAX_DRILLDOWNS]`. Pinning that
    split matters: anything reading `rank_materiality` directly (an eval, a
    notebook, the UI) gets the *full* material queue, not the drill-down
    shortlist.
    """
    rows = [_row(f"90{i:02d}", 100_000.0 - i) for i in range(config.MAX_DRILLDOWNS + 4)]
    ranked = variance.rank_materiality(rows)
    assert len(ranked) == config.MAX_DRILLDOWNS + 4
    assert len(ranked[: config.MAX_DRILLDOWNS]) == config.MAX_DRILLDOWNS


# --- compute_variances against an in-memory DuckDB --------------------------


@pytest.fixture
def summary_conn():
    """A DuckDB connection carrying only the `summary` table.

    `compute_variances` never touches `txn`, so the fixture stays minimal --
    and deliberately does NOT use `ingest.connect()`, which would glob the
    seeded CSVs off disk.
    """
    conn = duckdb.connect()
    conn.execute(
        "CREATE TABLE summary (period VARCHAR, account_code VARCHAR, "
        "account_name VARCHAR, account_type VARCHAR, amount DOUBLE)"
    )
    yield conn
    conn.close()


def _insert(conn, period, code, amount, name="Test Account", atype="revenue"):
    conn.execute("INSERT INTO summary VALUES (?,?,?,?,?)", [period, code, name, atype, amount])


def test_z_score_is_zero_with_fewer_than_two_history_deltas(summary_conn):
    # Only two periods exist, so there are no periods strictly before the
    # prior period -> `_trailing_deltas` returns [] -> z falls back to 0.0.
    _insert(summary_conn, "2025-01", "4000", 100_000.0)
    _insert(summary_conn, "2025-02", "4000", 400_000.0)
    (row,) = variance.compute_variances(summary_conn, "2025-02", "2025-01")
    assert row["delta"] == 300_000.0
    assert row["z_score"] == 0.0


def test_z_score_is_zero_when_history_has_no_variation(summary_conn):
    # Trailing deltas [10_000, 10_000] -> std == 0 -> the guard returns 0.0
    # rather than dividing by zero.
    for period, amt in [
        ("2025-01", 100_000.0),
        ("2025-02", 110_000.0),
        ("2025-03", 120_000.0),
        ("2025-04", 130_000.0),
        ("2025-05", 200_000.0),
    ]:
        _insert(summary_conn, period, "4000", amt)
    rows = {r["account_code"]: r for r in variance.compute_variances(summary_conn, "2025-05", "2025-04")}
    assert rows["4000"]["z_score"] == 0.0


def test_z_score_uses_the_trailing_delta_distribution(summary_conn):
    # Periods strictly before the prior period (2025-04) are 01/02/03, with
    # amounts 100k/110k/130k -> deltas [10_000, 20_000], mean 15_000,
    # population std 5_000. Current delta is 200k - 150k = 50_000, so
    # z = (50_000 - 15_000) / 5_000 = 7.0.
    for period, amt in [
        ("2025-01", 100_000.0),
        ("2025-02", 110_000.0),
        ("2025-03", 130_000.0),
        ("2025-04", 150_000.0),
        ("2025-05", 200_000.0),
    ]:
        _insert(summary_conn, period, "4000", amt)
    rows = {r["account_code"]: r for r in variance.compute_variances(summary_conn, "2025-05", "2025-04")}
    assert rows["4000"]["z_score"] == 7.0


def test_pct_is_infinite_when_prior_amount_is_zero(summary_conn):
    # Account only exists in the current period -> prior_amt 0.0.
    _insert(summary_conn, "2025-01", "4010", 50_000.0)
    _insert(summary_conn, "2025-02", "4010", 50_000.0)
    _insert(summary_conn, "2025-02", "4000", 80_000.0)
    rows = {r["account_code"]: r for r in variance.compute_variances(summary_conn, "2025-02", "2025-01")}
    assert rows["4000"]["prior_amt"] == 0.0
    assert rows["4000"]["pct"] == float("inf")


def test_pct_is_zero_when_prior_is_zero_and_nothing_moved(summary_conn):
    _insert(summary_conn, "2025-01", "7000", 0.0)
    _insert(summary_conn, "2025-02", "7000", 0.0)
    rows = {r["account_code"]: r for r in variance.compute_variances(summary_conn, "2025-02", "2025-01")}
    assert rows["7000"]["delta"] == 0.0
    assert rows["7000"]["pct"] == 0.0


def test_yoy_columns_are_none_without_a_year_ago_period(summary_conn):
    _insert(summary_conn, "2025-01", "4000", 100_000.0)
    _insert(summary_conn, "2025-02", "4000", 120_000.0)
    (row,) = variance.compute_variances(summary_conn, "2025-02", "2025-01")
    assert row["yoy_period"] == "2024-02"
    assert row["yoy_amt"] is None
    assert row["yoy_delta"] is None
    assert row["yoy_pct"] is None


# --- top_movers: the quiet-period fallback ----------------------------------


def _mover(code, name, delta):
    return {"account_code": code, "account_name": name, "delta": delta, "pct": 0.01, "z_score": 0.0}


def test_top_movers_returns_the_largest_absolute_movements():
    movers = variance.top_movers(
        [_mover("4000", "Revenue", 9_351.0), _mover("6000", "S&M", -2_616.0), _mover("5000", "COGS", 871.0)], 2
    )
    assert [m["account_code"] for m in movers] == ["4000", "6000"]


def test_top_movers_tags_every_row_as_below_threshold():
    movers = variance.top_movers([_mover("4000", "Revenue", 9_351.0)], 3)
    assert movers[0]["materiality_reason"] == variance.BELOW_THRESHOLD_REASON


def test_top_movers_skips_accounts_that_did_not_move():
    """A flat account moves 0.00 in every period. Drilling it would fill the
    fallback with the one account that has nothing to explain."""
    movers = variance.top_movers([_mover("7000", "Accrual", 0.0), _mover("4000", "Revenue", 100.0)], 3)
    assert [m["account_code"] for m in movers] == ["4000"]
