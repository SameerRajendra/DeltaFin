"""Normalization and validation for a CFO's own uploaded files, feeding one
isolated flux run.

`ingest.py` reads the committed synthetic dataset: pinned CSV globs, a fixed
column typing, a memory ladder for OOM-safety across 20+ files, and periods
that are trusted to already be well-formed because `data/seed_flux.py`
produced them. None of that fits files a CFO just dragged out of Excel --
column names may be close-but-not-exact, periods may be spelled a dozen
different ways, "amount" may carry currency formatting, real exports are
commonly one period per file, and a bad file is a user-facing problem, not a
stack trace. This module owns that boundary: it buckets any number of
uploaded files into summary vs. transaction subledger by column shape,
coerces each into the exact shapes `ingest.py`'s callers already expect,
raises `UploadError` with copy meant to be read by the person who uploaded
the file, and hands back an in-memory DuckDB connection the existing graph
(`app.flux.graph`) can run against unchanged via its `isolated=True` path --
no read from, or write to, the seeded institutional memory.

This is a normalizer, not a second ingestion engine: once `prepare()` returns,
every downstream node (`variance.py`, `slicer.py`, `ingest.tie_out`) is running
the exact same SQL it runs against the seeded dataset, against a `summary` /
`txn` table pair that merely happens to live in a fresh `duckdb.connect()`
instead of the committed globs.
"""

import re
from pathlib import Path
from typing import NamedTuple, Sequence, Union

import duckdb
import pandas as pd

from app.flux import graph

SUMMARY_COLUMNS = ["period", "account_code", "account_name", "account_type", "amount"]
TXN_COLUMNS = [
    "txn_id", "period", "txn_date", "account_code", "entity", "department",
    "region", "segment", "customer_id", "customer_name", "vendor", "amount", "memo",
]

# Forced to VARCHAR downstream (see the module docstring's "coerces" and the
# per-function comments below) because slicer.slice_drivers binds a member key
# back into `WHERE {key_col} = ?` against a COALESCE'd '(unattributed)' string;
# if the column landed in DuckDB as a numeric type that bind fails at runtime.
_TXN_STRING_COLUMNS = [
    "txn_id", "txn_date", "entity", "department", "region", "segment",
    "customer_id", "customer_name", "vendor", "memo",
]

_PERIOD_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
_DATE_PREFIX_RE = re.compile(r"^\d{4}-\d{2}-\d{2}")
_PAREN_NEG_RE = re.compile(r"^\((.*)\)$")

# variance.compute_variances silently zeros an amount column that fails to
# parse (pd.to_numeric(..., errors="coerce").fillna(0.0)) if we don't gate it
# first -- a whole currency-formatted column landing as all-zero would produce
# a confident, wrong variance report instead of an error. 20% is a judgment
# call: a handful of stray non-numeric cells is a data-entry typo worth
# defaulting to 0; a fifth of the column failing means the format wasn't
# understood at all and every number downstream is suspect.
_AMOUNT_FAILURE_THRESHOLD = 0.20

PathOrPaths = Union[Path, Sequence[Path]]


class UploadError(ValueError):
    """A validation failure whose message is meant to be shown to the uploader verbatim."""


class Prepared(NamedTuple):
    conn: duckdb.DuckDBPyConnection
    prior_period: str
    period: str
    summary_rows: int
    txn_rows: int
    warnings: list[str]


def _as_list(paths: PathOrPaths) -> list[Path]:
    """Normalize the "single Path or a sequence of them" call convention that
    `read_summary` / `read_transactions` / `prepare` all accept."""
    if isinstance(paths, (str, Path)):
        return [Path(paths)]
    return list(paths)


def _peek_columns(path: Path) -> set[str]:
    """Just the header row -- group_by_role needs column names, not data, to
    tell summary files apart from transaction files."""
    try:
        if path.suffix.lower() in (".xlsx", ".xls"):
            return set(pd.read_excel(path, nrows=0).columns)
        return set(pd.read_csv(path, nrows=0).columns)
    except Exception as exc:  # noqa: BLE001 -- surfaced verbatim to the uploader
        raise UploadError(f"Could not read {path.name}: {exc}") from exc


def group_by_role(paths: Sequence[Path]) -> tuple[list[Path], list[Path]]:
    """(summary_paths, txn_paths), detected by columns rather than upload
    order or count -- any number of files can be dropped in, each one bucketed
    by whether it has an 'account_name' column."""
    summary_paths: list[Path] = []
    txn_paths: list[Path] = []
    for path in paths:
        if "account_name" in _peek_columns(path):
            summary_paths.append(path)
        else:
            txn_paths.append(path)

    if not summary_paths:
        raise UploadError(
            "None of the uploaded files look like a period summary (none has an 'account_name' "
            "column). One of them should be the summary file, with 'period', 'account_code', "
            "'account_name', and 'amount' columns."
        )
    if not txn_paths:
        raise UploadError(
            "Every uploaded file looks like a period summary (every one has an 'account_name' "
            "column). One of them should be the transaction subledger instead -- it needs "
            "'period', 'account_code', and 'amount' columns, but no 'account_name' column."
        )
    return summary_paths, txn_paths


def _read_table(path: Path, required: list[str]) -> pd.DataFrame:
    try:
        if path.suffix.lower() in (".xlsx", ".xls"):
            df = pd.read_excel(path, dtype={"account_code": str})
        else:
            df = pd.read_csv(path, dtype={"account_code": str})
    except Exception as exc:  # noqa: BLE001 -- surfaced verbatim to the uploader
        raise UploadError(f"Could not read {path.name}: {exc}") from exc
    if df.empty:
        raise UploadError(f"{path.name} has no data rows.")
    missing = [c for c in required if c not in df.columns]
    if missing:
        raise UploadError(f"{path.name} is missing required column(s): {', '.join(missing)}")
    return df


def _normalize_period_series(series: pd.Series) -> pd.Series:
    """Excel commonly stores a 'period' column as a real date, so a cell that
    should read '2025-07' round-trips through pandas as the string
    '2025-07-01 00:00:00'. Truncate anything that looks like a YYYY-MM-DD (or
    longer) prefix to its first 7 characters before validating, rather than
    rejecting an otherwise-fine period."""
    s = series.fillna("").astype(str).str.strip()
    looks_like_date = s.str.match(_DATE_PREFIX_RE)
    return s.where(~looks_like_date, s.str.slice(0, 7))


def _validate_periods(series: pd.Series, filename: str) -> None:
    """Hard requirement, not a style preference: variance.compute_variances calls
    ingest.shift_period(current_period, -12) unconditionally, and shift_period does
    `int(p) for p in period.split("-")` -- anything that isn't YYYY-MM blows up deep
    inside the pipeline instead of at the upload boundary. Per-file, not
    cross-file: this is a shape check on the one file being read."""
    bad = sorted(set(series[~series.str.match(_PERIOD_RE)]))
    if bad:
        offenders = ", ".join(repr(b) for b in bad[:5])
        more = f" (and {len(bad) - 5} more)" if len(bad) > 5 else ""
        raise UploadError(
            f"{filename} has period value(s) that aren't in YYYY-MM form: {offenders}{more}. "
            "Reformat them first, e.g. 'Jul-25' -> '2025-07'."
        )


def _parse_amount(series: pd.Series, filename: str) -> pd.Series:
    """Strip currency formatting before parsing, then gate on how much of the
    column actually came through -- see _AMOUNT_FAILURE_THRESHOLD."""
    raw = series.fillna("").astype(str).str.strip()
    cleaned = raw.str.replace(r"[\$,]", "", regex=True).str.strip()
    is_paren = cleaned.str.match(_PAREN_NEG_RE)
    cleaned = cleaned.where(~is_paren, "-" + cleaned.str.strip("()"))
    parsed = pd.to_numeric(cleaned, errors="coerce")

    non_empty = raw.str.len() > 0
    failed = non_empty & parsed.isna()
    total_non_empty = int(non_empty.sum())
    if total_non_empty and failed.sum() / total_non_empty > _AMOUNT_FAILURE_THRESHOLD:
        raise UploadError(
            f"{filename}'s amount column has too many non-numeric values "
            f"({int(failed.sum())} of {total_non_empty}) to trust as currency. "
            "Check for stray text, multiple currencies, or a shifted column."
        )
    return parsed.fillna(0.0)


def _read_summary_one(path: Path) -> pd.DataFrame:
    """A period summary from a single file: one row per account per period.
    Only per-file checks live here -- required columns, period shape, amount
    parsing. Cross-file checks (>= 2 distinct periods, duplicate
    (account_code, period) pairs) run once in `prepare`, after every uploaded
    summary file has been concatenated together."""
    df = _read_table(path, ["period", "account_code", "account_name", "amount"])

    df["period"] = _normalize_period_series(df["period"])
    _validate_periods(df["period"], path.name)

    if "account_type" not in df.columns:
        df["account_type"] = "other"
    df["account_type"] = df["account_type"].fillna("").astype(str).replace("", "other")

    # account_code must be VARCHAR: slicer._dimension_for calls
    # account_code.startswith("4"), which raises AttributeError on an int.
    df["account_code"] = df["account_code"].fillna("").astype(str)
    df["amount"] = _parse_amount(df["amount"], path.name)

    return df[SUMMARY_COLUMNS]


def _read_transactions_one(path: Path) -> pd.DataFrame:
    """The transaction-level subledger from a single file. Per-file checks
    only -- see `_read_summary_one`."""
    df = _read_table(path, ["period", "account_code", "amount"])

    df["period"] = _normalize_period_series(df["period"])
    _validate_periods(df["period"], path.name)

    for col in TXN_COLUMNS:
        if col not in df.columns:
            df[col] = ""

    # See _TXN_STRING_COLUMNS above for why: any of these landing as a
    # non-VARCHAR DuckDB type breaks slicer.slice_drivers' evidence query.
    # fillna("") before astype(str), in that order, or a real NaN becomes the
    # literal string "nan" instead of an empty value.
    for col in _TXN_STRING_COLUMNS:
        df[col] = df[col].fillna("").astype(str)
    df["account_code"] = df["account_code"].fillna("").astype(str)

    blank_id = df["txn_id"].str.strip() == ""
    if blank_id.any():
        df.loc[blank_id, "txn_id"] = [f"TXN-{i + 1:04d}" for i in df.index[blank_id]]

    df["amount"] = _parse_amount(df["amount"], path.name)

    return df[TXN_COLUMNS]


def read_summary(paths: PathOrPaths) -> pd.DataFrame:
    """Read and normalize one or more period-summary files, concatenated into
    a single frame. Accepts a single Path or a sequence of them, so existing
    single-file call sites keep working."""
    frames = [_read_summary_one(p) for p in _as_list(paths)]
    return pd.concat(frames, ignore_index=True)


def read_transactions(paths: PathOrPaths) -> pd.DataFrame:
    """Read and normalize one or more transaction-detail files, concatenated
    into a single frame. Accepts a single Path or a sequence of them, so
    existing single-file call sites keep working."""
    frames = [_read_transactions_one(p) for p in _as_list(paths)]
    return pd.concat(frames, ignore_index=True)


def _validate_combined_summary(summary_df: pd.DataFrame) -> None:
    """Cross-file checks that only make sense once every summary file the
    uploader dropped in has been concatenated together."""
    distinct_periods = sorted(set(summary_df["period"]))
    if len(distinct_periods) < 2:
        only = distinct_periods[0] if distinct_periods else "none"
        raise UploadError(
            f"The summary data covers only one period ({only}). Add the file for another "
            "period -- a variance run compares two."
        )

    # Required, not cosmetic: variance.compute_variances builds
    # `{r["account_code"]: r for r in ingest.get_summary(conn, period)}` -- a
    # duplicate (account_code, period) pair would silently vanish, with only
    # the last row surviving, and nothing would ever say so. Checking the
    # concatenated frame (rather than per-file) also catches the very likely
    # mistake of dropping the same file twice, or two files that overlap a
    # period.
    dupes = summary_df[["account_code", "period"]][
        summary_df.duplicated(["account_code", "period"], keep=False)
    ]
    if not dupes.empty:
        pairs = sorted(set(dupes.itertuples(index=False, name=None)))
        periods = sorted({p for _, p in pairs})
        # Lead with the likely cause, not the evidence. Dropping the same file
        # twice duplicates every account in it, so the raw pair list can run to
        # dozens of tuples -- enough to bury the one sentence that says what to
        # do. Show a few and count the rest.
        shown = ", ".join(f"{a} in {p}" for a, p in pairs[:3])
        more = f", and {len(pairs) - 3} more" if len(pairs) > 3 else ""
        raise UploadError(
            f"The same account appears twice in the same period ({shown}{more}). "
            f"That usually means a file was added twice, or two files overlap -- "
            f"affected period(s): {', '.join(periods)}. The summary needs one row "
            f"per account per period."
        )


def latest_two_periods(summary_df: pd.DataFrame) -> tuple[str, str]:
    """(prior_period, period): the last two periods actually present in the
    sorted distinct set. Deliberately not ingest.shift_period(period, -1) --
    a file holding 2025-03 and 2025-06 with nothing in between must compare
    2025-03 -> 2025-06, not a synthesized (and absent) 2025-05."""
    periods = sorted(set(summary_df["period"]))
    return periods[-2], periods[-1]


def build_conn(summary_df: pd.DataFrame, txn_df: pd.DataFrame) -> duckdb.DuckDBPyConnection:
    """A plain in-memory DuckDB connection with `summary` / `txn` tables, built
    from already-validated DataFrames.

    Deliberately does not use ingest._MEMORY_LADDER: that ladder exists to
    keep `read_csv_auto` over a 20-file glob from OOMing DuckDB's
    auto-detected memory limit. There's no glob here -- the data is already a
    bounded set of in-memory DataFrames from one upload -- so there's nothing
    for a memory ladder to protect against.
    """
    conn = duckdb.connect()
    conn.register("summary_view", summary_df)
    conn.register("txn_view", txn_df)
    conn.execute("CREATE TABLE summary AS SELECT * FROM summary_view")
    conn.execute("CREATE TABLE txn AS SELECT * FROM txn_view")
    conn.unregister("summary_view")
    conn.unregister("txn_view")
    return conn


def prepare(paths: Sequence[Path]) -> Prepared:
    """Validate and normalize any number of uploaded files and stage them for
    one graph run. Files are bucketed into summary vs. transaction by column
    shape (`group_by_role`), each bucket is read and concatenated
    (`read_summary` / `read_transactions`), and only then are the cross-file
    checks -- at least two distinct periods, no duplicate (account_code,
    period) pairs -- applied to the combined summary."""
    paths = _as_list(paths)
    summary_paths, txn_paths = group_by_role(paths)
    summary_df = read_summary(summary_paths)
    txn_df = read_transactions(txn_paths)

    _validate_combined_summary(summary_df)

    prior_period, period = latest_two_periods(summary_df)

    warnings: list[str] = []
    txn_periods = set(txn_df["period"])
    if period not in txn_periods and prior_period not in txn_periods:
        warnings.append(
            f"No transactions found for {prior_period} or {period}: account-level "
            "variances will still work, but there will be no customer/vendor drivers."
        )

    conn = build_conn(summary_df, txn_df)
    return Prepared(
        conn=conn,
        prior_period=prior_period,
        period=period,
        summary_rows=len(summary_df),
        txn_rows=len(txn_df),
        warnings=warnings,
    )


def run(paths: Sequence[Path]) -> tuple[dict, Prepared]:
    """The single call the UI makes: validate + stage any number of uploaded
    files, then run one isolated graph comparison over exactly the two
    latest periods present across the combined summary (never a
    shift_period(-1) guess). Isolated: no read from or write to the seeded
    institutional memory, and no artifacts written to out/flux/ -- this data
    has nothing to do with that ledger."""
    prepared = prepare(paths)
    try:
        state = graph.process_period(
            period=prepared.period,
            prior_period=prepared.prior_period,
            conn=prepared.conn,
            isolated=True,
        )
    finally:
        prepared.conn.close()
    return state, prepared
