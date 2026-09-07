"""Tier 0 tests for `app/flux/uploads.py`.

No LLM, no network, no seeded data files under data/ -- every CSV a test
needs is written into pytest's `tmp_path` by the small `_write_csv` helper
below. These pin the upload-boundary contract `uploads.py` exists to own:
role detection by column set, period/amount normalization, and the handful
of regressions the module's own comments call out explicitly: the
"Jul-25" -> `ingest.shift_period(-12)` trap, the duplicate
`(account_code, period)` dict-comprehension trap in
`variance.compute_variances`, the `account_code.startswith("4")`
`AttributeError` trap in `slicer._dimension_for`, and the `customer_id`
BIGINT-bind trap in `slicer.slice_drivers`.

Does not cover: `uploads.run()` (that drives `app.flux.graph.process_period`
end-to-end -- the isolation behavior of that path is covered in
`tests/test_flux_isolation.py`), or the `.xlsx` read branch of
`_peek_columns` / `_read_table` (`pd.read_excel`) -- only CSV is exercised
here.
"""

import csv
import re
from pathlib import Path

import pandas as pd
import pytest

from app.flux import ingest, slicer, uploads


def _write_csv(path: Path, columns: list, rows: list) -> Path:
    """A small CSV written straight to disk -- csv.writer stringifies
    non-string fields itself, so a bare int like 4000 lands as the text
    "4000" with no quoting, exactly like a CFO's real export."""
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(columns)
        writer.writerows(rows)
    return path


# --- group_by_role --------------------------------------------------------


def test_group_by_role_regardless_of_argument_order(tmp_path):
    summary_path = _write_csv(
        tmp_path / "a.csv",
        ["period", "account_code", "account_name", "account_type", "amount"],
        [["2025-06", "4000", "Enterprise Revenue", "revenue", 100000]],
    )
    txn_path = _write_csv(
        tmp_path / "b.csv",
        ["period", "account_code", "amount"],
        [["2025-06", "4000", 100000]],
    )
    assert uploads.group_by_role([summary_path, txn_path]) == ([summary_path], [txn_path])
    assert uploads.group_by_role([txn_path, summary_path]) == ([summary_path], [txn_path])


def test_group_by_role_buckets_any_number_of_files(tmp_path):
    summary_a = _write_csv(
        tmp_path / "s1.csv",
        ["period", "account_code", "account_name", "amount"],
        [["2025-01", "4000", "X", 1]],
    )
    summary_b = _write_csv(
        tmp_path / "s2.csv",
        ["period", "account_code", "account_name", "amount"],
        [["2025-02", "4000", "X", 1]],
    )
    txn_a = _write_csv(tmp_path / "t1.csv", ["period", "account_code", "amount"], [["2025-01", "4000", 1]])
    txn_b = _write_csv(tmp_path / "t2.csv", ["period", "account_code", "amount"], [["2025-02", "4000", 1]])
    summary_paths, txn_paths = uploads.group_by_role([summary_a, txn_a, summary_b, txn_b])
    assert summary_paths == [summary_a, summary_b]
    assert txn_paths == [txn_a, txn_b]


def test_group_by_role_raises_when_no_file_has_account_name(tmp_path):
    path_a = _write_csv(tmp_path / "a.csv", ["period", "account_code", "amount"], [["2025-06", "4000", 1]])
    path_b = _write_csv(tmp_path / "b.csv", ["period", "account_code", "amount"], [["2025-06", "4000", 1]])
    with pytest.raises(uploads.UploadError, match="None of the uploaded files look like a period summary"):
        uploads.group_by_role([path_a, path_b])


def test_group_by_role_raises_when_no_file_lacks_account_name(tmp_path):
    path_a = _write_csv(
        tmp_path / "a.csv", ["period", "account_code", "account_name", "amount"], [["2025-06", "4000", "X", 1]]
    )
    path_b = _write_csv(
        tmp_path / "b.csv", ["period", "account_code", "account_name", "amount"], [["2025-06", "4000", "Y", 1]]
    )
    with pytest.raises(uploads.UploadError, match="Every uploaded file looks like a period summary"):
        uploads.group_by_role([path_a, path_b])


# --- read_summary: required columns, periods, duplicates ----------------


def test_read_summary_raises_when_period_column_missing(tmp_path):
    path = _write_csv(
        tmp_path / "summary.csv",
        ["account_code", "account_name", "amount"],
        [["4000", "Enterprise Revenue", 100000]],
    )
    with pytest.raises(uploads.UploadError, match=re.escape("missing required column(s): period")):
        uploads.read_summary(path)


def test_read_summary_rejects_non_yyyy_mm_period_jul_25(tmp_path):
    """Regression test for the trap that made the old 'upload' sentinel
    fatal: `variance.compute_variances` calls `ingest.shift_period(period, -12)`
    unconditionally, and `shift_period` does `int(p) for p in period.split("-")`
    -- a period spelled 'Jul-25' must be rejected here, at the upload
    boundary, rather than blowing up deep inside the pipeline."""
    path = _write_csv(
        tmp_path / "summary.csv",
        ["period", "account_code", "account_name", "amount"],
        [
            ["Jul-25", "4000", "Enterprise Revenue", 100000],
            ["2025-08", "4000", "Enterprise Revenue", 120000],
        ],
    )
    with pytest.raises(uploads.UploadError, match=re.escape("'Jul-25'")):
        uploads.read_summary(path)


def test_read_summary_normalizes_excel_datetime_period(tmp_path):
    path = _write_csv(
        tmp_path / "summary.csv",
        ["period", "account_code", "account_name", "amount"],
        [
            ["2025-07-01 00:00:00", "4000", "Enterprise Revenue", 100000],
            ["2025-08-01 00:00:00", "4000", "Enterprise Revenue", 120000],
        ],
    )
    df = uploads.read_summary(path)
    assert sorted(set(df["period"])) == ["2025-07", "2025-08"]


def test_read_summary_account_code_is_string_and_slicer_accepts_it(tmp_path):
    """Pins the `account_code.startswith("4")` AttributeError trap: a bare
    int account_code in the CSV must come back as the string "4000", not a
    pandas/numpy int, or slicer._dimension_for raises AttributeError."""
    path = _write_csv(
        tmp_path / "summary.csv",
        ["period", "account_code", "account_name", "amount"],
        [
            ["2025-06", 4000, "Enterprise Revenue", 100000],
            ["2025-07", 4000, "Enterprise Revenue", 120000],
        ],
    )
    df = uploads.read_summary(path)
    code = df["account_code"].iloc[0]
    assert code == "4000"
    assert isinstance(code, str)
    assert slicer._dimension_for(code) == slicer._REVENUE_DIMENSION


# --- amount parsing -------------------------------------------------------


def test_parse_amount_handles_currency_formatting_and_parens_negative():
    parsed = uploads._parse_amount(pd.Series(["$1,234.00", "(500)"]), "test.csv")
    assert parsed.tolist() == [1234.0, -500.0]


def test_parse_amount_raises_when_column_is_mostly_non_numeric():
    # non-empty count = 4 ("abc","def","ghi","100"); 3 of 4 fail to parse,
    # which is 75% > _AMOUNT_FAILURE_THRESHOLD (20%).
    series = pd.Series(["abc", "def", "ghi", "100", ""])
    with pytest.raises(uploads.UploadError, match=re.escape("too many non-numeric values")):
        uploads._parse_amount(series, "test.csv")


# --- latest_two_periods ---------------------------------------------------


def test_latest_two_periods_is_non_contiguous_not_a_shift_by_one():
    """Deliberately not shift_period(period, -1): a file holding 2025-03,
    2025-06, 2025-09 (nothing in between) must compare 2025-06 -> 2025-09,
    not a synthesized (and absent) 2025-08."""
    df = pd.DataFrame(
        {
            "period": ["2025-03", "2025-06", "2025-09"],
            "account_code": ["4000", "4000", "4000"],
            "account_name": ["Enterprise Revenue"] * 3,
            "amount": [1.0, 2.0, 3.0],
        }
    )
    prior, period = uploads.latest_two_periods(df)
    assert (prior, period) == ("2025-06", "2025-09")
    assert prior != ingest.shift_period(period, -1)


# --- build_conn: VARCHAR coercion + tie_out compatibility -----------------


def test_build_conn_forces_varchar_account_code_and_customer_id(tmp_path):
    """Pins two things at once: the injection seam into the existing graph
    is just two table names (`summary` / `txn`) -- ingest.tie_out runs
    unmodified against this connection -- and the customer_id BIGINT trap:
    slicer.slice_drivers binds the string '(unattributed)' back into
    `WHERE customer_id = ?`, which raises at runtime if DuckDB inferred
    customer_id as BIGINT from an all-numeric column."""
    summary_path = _write_csv(
        tmp_path / "summary.csv",
        ["period", "account_code", "account_name", "account_type", "amount"],
        [
            ["2025-05", "4000", "Enterprise Revenue", "revenue", 4000],
            ["2025-06", "4000", "Enterprise Revenue", "revenue", 5000],
        ],
    )
    txn_path = _write_csv(
        tmp_path / "txn.csv",
        ["period", "account_code", "amount", "customer_id"],
        [
            ["2025-05", "4000", 4000, 1001],
            ["2025-06", "4000", 5000, 1001],
        ],
    )
    prepared = uploads.prepare([summary_path, txn_path])
    try:
        types = {row[0]: row[1] for row in prepared.conn.execute("DESCRIBE txn").fetchall()}
        assert types["account_code"] == "VARCHAR"
        assert types["customer_id"] == "VARCHAR"

        rows = ingest.tie_out(prepared.conn, prepared.period)
        assert rows
        row = next(r for r in rows if r["account_code"] == "4000")
        assert row["coverage_pct"] == 100.0
    finally:
        prepared.conn.close()


def test_prepare_warns_rather_than_raises_when_no_txn_rows_for_either_period(tmp_path):
    summary_path = _write_csv(
        tmp_path / "summary.csv",
        ["period", "account_code", "account_name", "account_type", "amount"],
        [
            ["2025-05", "4000", "Enterprise Revenue", "revenue", 100000],
            ["2025-06", "4000", "Enterprise Revenue", "revenue", 120000],
        ],
    )
    txn_path = _write_csv(
        tmp_path / "txn.csv",
        ["period", "account_code", "amount"],
        [["2025-01", "4000", 5000]],
    )
    prepared = uploads.prepare([summary_path, txn_path])
    try:
        assert prepared.warnings
        assert "No transactions found for" in prepared.warnings[0]
    finally:
        prepared.conn.close()


# --- prepare: cross-file validations (moved from read_summary) ------------


def test_prepare_raises_with_only_one_distinct_period_across_summary_files(tmp_path):
    """The validation this whole change exists for: a single-period summary
    file (the shape of every file in data/financials/summaries/) is fine on
    its own -- the "at least two periods" check no longer lives in
    read_summary, only in prepare, once every summary file is combined."""
    summary_path = _write_csv(
        tmp_path / "summary.csv",
        ["period", "account_code", "account_name", "amount"],
        [["2025-06", "4000", "Enterprise Revenue", 100000], ["2025-06", "5000", "COGS", 50000]],
    )
    txn_path = _write_csv(
        tmp_path / "txn.csv",
        ["period", "account_code", "amount"],
        [["2025-06", "4000", 100000]],
    )
    with pytest.raises(uploads.UploadError, match=re.escape("covers only one period (2025-06)")):
        uploads.prepare([summary_path, txn_path])


def test_prepare_raises_on_duplicate_account_period_pair_across_summary_files(tmp_path):
    """Regression test for `variance.compute_variances`' dict comprehension
    `{r["account_code"]: r for r in ingest.get_summary(conn, period)}` --
    a duplicate (account_code, period) row would silently vanish, keeping
    only the last one, and nothing downstream would ever say so. Now checked
    on the concatenated summary, so it also catches two files that overlap a
    period."""
    summary_path = _write_csv(
        tmp_path / "summary.csv",
        ["period", "account_code", "account_name", "amount"],
        [
            ["2025-06", "4000", "Enterprise Revenue", 100000],
            ["2025-06", "4000", "Enterprise Revenue", 105000],
            ["2025-07", "4000", "Enterprise Revenue", 120000],
        ],
    )
    txn_path = _write_csv(
        tmp_path / "txn.csv",
        ["period", "account_code", "amount"],
        [["2025-06", "4000", 100000]],
    )
    with pytest.raises(uploads.UploadError, match=re.escape("appears twice in the same period")):
        uploads.prepare([summary_path, txn_path])


def test_prepare_raises_on_duplicate_when_same_file_dropped_twice(tmp_path):
    """The very likely mistake this change also has to catch: a user drops
    the same file into the widget twice (or two files that overlap a
    period). A second, distinct period is present too, so this exercises the
    duplicate check specifically rather than the one-period check above."""
    summary_jan = _write_csv(
        tmp_path / "2025-01.csv",
        ["period", "account_code", "account_name", "amount"],
        [["2025-01", "4000", "Enterprise Revenue", 100000]],
    )
    summary_feb = _write_csv(
        tmp_path / "2025-02.csv",
        ["period", "account_code", "account_name", "amount"],
        [["2025-02", "4000", "Enterprise Revenue", 110000]],
    )
    txn_path = _write_csv(
        tmp_path / "txn-2025-01.csv",
        ["period", "account_code", "amount"],
        [["2025-01", "4000", 100000]],
    )
    with pytest.raises(uploads.UploadError, match="added twice, or two files overlap"):
        uploads.prepare([summary_jan, summary_jan, summary_feb, txn_path])


def test_prepare_succeeds_with_two_single_period_summary_and_txn_files(tmp_path):
    """The actual bug being fixed: real exports are one period per file, e.g.
    the 20 summary + 20 transaction files under data/financials/. Two
    single-period summaries and their two single-period transaction
    counterparts must combine into a working two-period comparison."""
    summary_jan = _write_csv(
        tmp_path / "2025-01.csv",
        ["period", "account_code", "account_name", "amount"],
        [
            ["2025-01", "4000", "Enterprise Revenue", 100000],
            ["2025-01", "5000", "COGS", 40000],
        ],
    )
    summary_feb = _write_csv(
        tmp_path / "2025-02.csv",
        ["period", "account_code", "account_name", "amount"],
        [
            ["2025-02", "4000", "Enterprise Revenue", 110000],
            ["2025-02", "5000", "COGS", 42000],
        ],
    )
    txn_jan = _write_csv(
        tmp_path / "txn-2025-01.csv",
        ["period", "account_code", "amount"],
        [["2025-01", "4000", 100000], ["2025-01", "5000", 40000]],
    )
    txn_feb = _write_csv(
        tmp_path / "txn-2025-02.csv",
        ["period", "account_code", "amount"],
        [["2025-02", "4000", 110000], ["2025-02", "5000", 42000]],
    )
    prepared = uploads.prepare([summary_jan, txn_jan, summary_feb, txn_feb])
    try:
        assert (prepared.prior_period, prepared.period) == ("2025-01", "2025-02")
        assert prepared.summary_rows == 4
        assert prepared.txn_rows == 4
    finally:
        prepared.conn.close()


def test_prepare_with_no_summary_file_raises(tmp_path):
    txn_a = _write_csv(tmp_path / "t1.csv", ["period", "account_code", "amount"], [["2025-01", "4000", 1]])
    txn_b = _write_csv(tmp_path / "t2.csv", ["period", "account_code", "amount"], [["2025-02", "4000", 1]])
    with pytest.raises(uploads.UploadError, match="None of the uploaded files look like a period summary"):
        uploads.prepare([txn_a, txn_b])


def test_prepare_with_no_transaction_file_raises(tmp_path):
    summary_a = _write_csv(
        tmp_path / "s1.csv",
        ["period", "account_code", "account_name", "amount"],
        [["2025-01", "4000", "X", 1]],
    )
    summary_b = _write_csv(
        tmp_path / "s2.csv",
        ["period", "account_code", "account_name", "amount"],
        [["2025-02", "4000", "X", 1]],
    )
    with pytest.raises(uploads.UploadError, match="Every uploaded file looks like a period summary"):
        uploads.prepare([summary_a, summary_b])


def test_prepare_compares_latest_two_of_a_three_period_spread(tmp_path):
    """Three single-period summary files (plus their transaction
    counterparts) must compare the latest two, not the first two."""
    summaries = []
    txns = []
    for period, revenue in [("2025-01", 100000), ("2025-02", 110000), ("2025-03", 125000)]:
        summaries.append(
            _write_csv(
                tmp_path / f"{period}.csv",
                ["period", "account_code", "account_name", "amount"],
                [[period, "4000", "Enterprise Revenue", revenue]],
            )
        )
        txns.append(
            _write_csv(
                tmp_path / f"txn-{period}.csv",
                ["period", "account_code", "amount"],
                [[period, "4000", revenue]],
            )
        )
    prepared = uploads.prepare(summaries + txns)
    try:
        assert (prepared.prior_period, prepared.period) == ("2025-02", "2025-03")
    finally:
        prepared.conn.close()
