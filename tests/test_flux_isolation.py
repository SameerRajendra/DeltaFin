"""Requirement-1 regression suite: `isolated=True` must not read from or
write into the seeded institutional memory (SQLite run/finding history +
the JSON account<->driver graph) or `out/flux/`, for an ad-hoc upload
comparison.

Runs the real LangGraph pipeline end-to-end
(`graph.build_graph(...).invoke(...)`) against a small hand-built DuckDB
connection, but stays offline throughout (`llm=None`, so `explain_drivers`
takes the deterministic `_template_finding` path) -- no network, no LLM, no
seeded `data/` files. Every test in this file redirects
`config.FLUX_DB_PATH` / `FLUX_GRAPH_PATH` / `FLUX_OUT_DIR` into `tmp_path` as
a safety net, even where `isolated=True` should make that redirection moot.

Does not cover: the LLM-narrated branch of `explain_drivers` /
`synthesize_brief` (needs a live model), or `app/flux/uploads.py`'s CSV
normalization (see `tests/test_uploads.py`).
"""

import json

import duckdb
import pytest

from app import config
from app.flux import graph

PRIOR = "2026-06"
CURRENT = "2026-07"


def _build_isolation_conn():
    """summary + txn across two periods with one revenue swing well past
    config.MATERIALITY_ABS (25,000), on account 4000 (revenue dimension, so
    slicer._dimension_for applies) with a single named customer driving the
    whole delta."""
    conn = duckdb.connect()
    conn.execute(
        "CREATE TABLE summary (period VARCHAR, account_code VARCHAR, account_name VARCHAR, "
        "account_type VARCHAR, amount DOUBLE)"
    )
    conn.execute(
        "CREATE TABLE txn (txn_id VARCHAR, period VARCHAR, txn_date VARCHAR, account_code VARCHAR, "
        "entity VARCHAR, department VARCHAR, region VARCHAR, segment VARCHAR, customer_id VARCHAR, "
        "customer_name VARCHAR, vendor VARCHAR, amount DOUBLE, memo VARCHAR)"
    )
    conn.execute(
        "INSERT INTO summary VALUES (?,?,?,?,?)",
        [PRIOR, "4000", "Enterprise Revenue", "revenue", 100_000.0],
    )
    conn.execute(
        "INSERT INTO summary VALUES (?,?,?,?,?)",
        [CURRENT, "4000", "Enterprise Revenue", "revenue", 170_000.0],
    )
    conn.execute(
        "INSERT INTO txn VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ["TXN-0001", PRIOR, f"{PRIOR}-15", "4000", "", "", "", "", "CUST-1", "Acme Corp", "", 100_000.0, "memo"],
    )
    conn.execute(
        "INSERT INTO txn VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ["TXN-0002", CURRENT, f"{CURRENT}-15", "4000", "", "", "", "", "CUST-1", "Acme Corp", "", 170_000.0, "memo"],
    )
    return conn


@pytest.fixture(autouse=True)
def _redirect_flux_paths(tmp_path, monkeypatch):
    """Safety net for every test in this file: even though isolated=True
    should make these paths untouched, redirect them into tmp_path so a
    regression in the isolation flag can never write into this repo's real
    data/ or out/ directories."""
    monkeypatch.setattr(config, "FLUX_DB_PATH", tmp_path / "flux_memory.db")
    monkeypatch.setattr(config, "FLUX_GRAPH_PATH", tmp_path / "flux_memory_graph.json")
    monkeypatch.setattr(config, "FLUX_OUT_DIR", tmp_path / "out_flux")


@pytest.fixture
def flux_conn():
    conn = _build_isolation_conn()
    yield conn
    conn.close()


def test_isolated_run_produces_findings_and_writes_nothing(flux_conn):
    state = graph.build_graph(llm=None, conn=flux_conn, isolated=True).invoke(
        {"run_id": "test1234", "period": CURRENT, "prior_period": PRIOR}
    )
    assert state["findings"]
    # The redirection is a safety net; the actual assertion is that nothing
    # was written even to the safe, redirected location.
    assert not config.FLUX_DB_PATH.exists()
    assert not config.FLUX_GRAPH_PATH.exists()
    assert not config.FLUX_OUT_DIR.exists()


def test_non_isolated_run_writes_all_three_redirected_paths(flux_conn):
    """Contrast to the test above: proves the isolated flag -- not some
    other accident -- is what suppresses the writes, and guards against
    silently breaking run_flux.py's normal (isolated=False) path."""
    state = graph.build_graph(llm=None, conn=flux_conn, isolated=False).invoke(
        {"run_id": "test1234", "period": CURRENT, "prior_period": PRIOR}
    )
    assert state["findings"]
    assert config.FLUX_DB_PATH.exists()
    assert config.FLUX_GRAPH_PATH.exists()
    assert config.FLUX_OUT_DIR.exists()
    assert list(config.FLUX_OUT_DIR.iterdir())
    assert "brief_path" in state


def test_isolated_run_ignores_seeded_memory_graph_history(flux_conn):
    """Regression: don't inject unrelated seeded history into an upload.
    `recall_memory_node` substitutes an empty graph ({"edges": {}}) rather
    than calling memory.load_graph() when isolated=True, so a genuine
    multi-period streak sitting in the redirected FLUX_GRAPH_PATH file must
    have zero effect on this run's recurring_count or narrative."""
    seeded_graph = {
        "edges": {
            "4000::CUST-1": {
                "account_code": "4000",
                "driver_key": "CUST-1",
                "driver_name": "Acme Corp",
                "periods": {
                    "2026-04": {"share": 1.0, "delta": 1000.0},
                    "2026-05": {"share": 1.0, "delta": 1000.0},
                    "2026-06": {"share": 1.0, "delta": 1000.0},
                },
            }
        }
    }
    config.FLUX_GRAPH_PATH.write_text(json.dumps(seeded_graph), encoding="utf-8")

    state = graph.build_graph(llm=None, conn=flux_conn, isolated=True).invoke(
        {"run_id": "test1234", "period": CURRENT, "prior_period": PRIOR}
    )

    assert state["findings"][0]["recurring_count"] == 1
    why = state["findings"][0]["why"]
    assert "consecutive" not in why
    assert "recurred" not in why


def test_process_period_forwards_isolated_flag(flux_conn, monkeypatch):
    """process_period hardcodes llm=llm_module.get_llm(); monkeypatch that to
    return None so the run stays offline while still proving isolated=True
    reaches build_graph and produces the same no-write behavior."""
    monkeypatch.setattr(graph.llm_module, "get_llm", lambda: None)
    state = graph.process_period(period=CURRENT, prior_period=PRIOR, conn=flux_conn, isolated=True)

    assert state["findings"]
    assert not config.FLUX_DB_PATH.exists()
    assert not config.FLUX_GRAPH_PATH.exists()
    assert not config.FLUX_OUT_DIR.exists()


def test_isolated_state_has_no_artifact_path_keys(flux_conn):
    """FluxState is `total=False`; render_artifacts/persist_memory return {}
    when isolated, so these keys are simply absent, not None."""
    state = graph.build_graph(llm=None, conn=flux_conn, isolated=True).invoke(
        {"run_id": "test1234", "period": CURRENT, "prior_period": PRIOR}
    )
    for key in ("brief_path", "workpaper_path", "analysis_path", "actions_path"):
        assert key not in state
