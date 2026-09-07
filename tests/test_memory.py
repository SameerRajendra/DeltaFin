"""Tier 0 tests for `app/flux/memory.py`.

`_shift` and `recall` are pure functions over a graph dict, so most of this
file needs nothing on disk. The few tests that exercise `persist_findings`,
`record_feedback` and `findings_for_run` redirect `config.FLUX_DB_PATH` and
`config.FLUX_GRAPH_PATH` into `tmp_path` -- the repo's real institutional
memory (`data/flux_memory.db`, `data/flux_memory_graph.json`) is never
touched by the test suite.
"""

import pytest

from app import config
from app.flux import memory


# --- _shift -----------------------------------------------------------------


def test_shift_back_across_a_year_boundary():
    assert memory._shift("2026-01", -1) == "2025-12"


def test_shift_forward_across_a_year_boundary():
    assert memory._shift("2025-12", 1) == "2026-01"


def test_shift_a_full_year_back_is_the_yoy_period():
    assert memory._shift("2026-08", -12) == "2025-08"


def test_shift_by_zero_is_identity():
    assert memory._shift("2026-08", 0) == "2026-08"


def test_shift_multiple_years_back():
    assert memory._shift("2026-03", -15) == "2024-12"


# --- recall -----------------------------------------------------------------

HOSTING_EDGE = "5000::CloudBeam Compute"
SEASONAL_EDGE = "6000::TechConf Expo"


def _graph(**edges):
    """A memory graph with the edges given as {driver_key: [periods]}."""
    return {
        "edges": {
            key: {
                "account_code": key.split("::")[0],
                "driver_key": key.split("::")[1],
                "driver_name": key.split("::")[1],
                "periods": {p: {"share": 0.5, "delta": 1.0} for p in periods},
            }
            for key, periods in edges.items()
        }
    }


def test_recall_reports_an_unknown_driver_as_never_seen():
    context = memory.recall(_graph(), "4000", ["ENT-01"], "2026-08")
    assert context["ENT-01"] == {"seen_before": False}


def test_recall_counts_only_immediately_consecutive_prior_periods():
    # The hosting overrun: June and July immediately precede August.
    graph = _graph(**{HOSTING_EDGE: ["2026-06", "2026-07"]})
    context = memory.recall(graph, "5000", ["CloudBeam Compute"], "2026-08")
    assert context["CloudBeam Compute"]["seen_before"] is True
    assert context["CloudBeam Compute"]["streak"] == 2


def test_recall_streak_stops_at_the_first_gap():
    # 2026-04 is separated from the 06/07 run by an empty May, so it does not
    # extend the streak even though it is a prior period.
    graph = _graph(**{HOSTING_EDGE: ["2026-04", "2026-06", "2026-07"]})
    context = memory.recall(graph, "5000", ["CloudBeam Compute"], "2026-08")
    assert context["CloudBeam Compute"]["seen_before"] is True
    assert context["CloudBeam Compute"]["streak"] == 2


def test_recall_seasonal_driver_is_seen_before_with_a_zero_streak():
    # The TechConf Expo sponsorship: last seen a full year ago. This is the
    # distinction the narrative depends on -- "seen before, but not
    # consecutively (e.g. same period last year)" rather than a fresh driver.
    graph = _graph(**{SEASONAL_EDGE: ["2025-08"]})
    context = memory.recall(graph, "6000", ["TechConf Expo"], "2026-08")
    assert context["TechConf Expo"]["seen_before"] is True
    assert context["TechConf Expo"]["streak"] == 0


def test_recall_ignores_periods_at_or_after_the_before_period():
    # An edge whose only period is the period being analysed is not history.
    graph = _graph(**{SEASONAL_EDGE: ["2026-08", "2026-09"]})
    context = memory.recall(graph, "6000", ["TechConf Expo"], "2026-08")
    assert context["TechConf Expo"]["seen_before"] is False
    assert context["TechConf Expo"]["streak"] == 0


def test_recall_surfaces_the_last_analyst_verdict():
    graph = _graph(**{HOSTING_EDGE: ["2026-06", "2026-07"]})
    graph["edges"][HOSTING_EDGE]["last_verdict"] = "confirmed"
    graph["edges"][HOSTING_EDGE]["last_note"] = "contracted burst capacity"
    context = memory.recall(graph, "5000", ["CloudBeam Compute"], "2026-08")
    assert context["CloudBeam Compute"]["last_verdict"] == "confirmed"
    assert context["CloudBeam Compute"]["last_note"] == "contracted burst capacity"


def test_recall_scopes_edges_by_account():
    # The same driver key under a different account is a different edge.
    graph = _graph(**{HOSTING_EDGE: ["2026-06", "2026-07"]})
    context = memory.recall(graph, "6000", ["CloudBeam Compute"], "2026-08")
    assert context["CloudBeam Compute"] == {"seen_before": False}


# --- persistence (tmp_path-scoped) ------------------------------------------


@pytest.fixture
def flux_memory(tmp_path, monkeypatch):
    """SQLite + graph JSON redirected into tmp_path, plus an open connection."""
    monkeypatch.setattr(config, "FLUX_DB_PATH", tmp_path / "flux_memory.db")
    monkeypatch.setattr(config, "FLUX_GRAPH_PATH", tmp_path / "flux_memory_graph.json")
    conn = memory.connect()
    try:
        yield conn
    finally:
        conn.close()


def _finding(account_code="4000", driver_keys=("ENT-01",), headline="headline"):
    return {
        "account_code": account_code,
        "account_name": "Enterprise Revenue",
        "headline": headline,
        "why": "why",
        "confidence": "high",
        "recurring_count": 1,
        "drivers": [
            {"member_key": k, "member_name": k, "contribution_share": 0.5, "delta": 1_000.0}
            for k in driver_keys
        ],
    }


def test_persist_findings_writes_an_edge_per_driver_keyed_by_period(flux_memory):
    graph = memory.load_graph()
    memory.persist_findings(
        flux_memory, graph, "2026-08", "2026-07", "runabc", [_finding(driver_keys=("ENT-01", "ENT-02"))]
    )
    assert set(graph["edges"]) == {"4000::ENT-01", "4000::ENT-02"}
    assert list(graph["edges"]["4000::ENT-01"]["periods"]) == ["2026-08"]
    assert graph["edges"]["4000::ENT-01"]["periods"]["2026-08"]["share"] == 0.5
    # And the graph was flushed to the redirected path.
    assert config.FLUX_GRAPH_PATH.exists()


def test_findings_for_run_returns_the_latest_run_of_that_exact_comparison(flux_memory):
    graph = memory.load_graph()
    memory.persist_findings(flux_memory, graph, "2026-08", "2026-07", "run1", [_finding(headline="first")])
    memory.persist_findings(flux_memory, graph, "2026-08", "2026-07", "run2", [_finding(headline="second")])
    rows = memory.findings_for_run(flux_memory, "2026-08", "2026-07")
    assert [r["headline"] for r in rows] == ["second"]
    assert isinstance(rows[0]["id"], int)


def test_findings_for_run_is_empty_for_an_unseen_comparison(flux_memory):
    assert memory.findings_for_run(flux_memory, "2099-01", "2098-12") == []


def test_record_feedback_stamps_the_finding_s_own_driver_edge(flux_memory):
    graph = memory.load_graph()
    memory.persist_findings(flux_memory, graph, "2026-08", "2026-07", "run1", [_finding(driver_keys=("ENT-01",))])
    finding_id = memory.findings_for_run(flux_memory, "2026-08", "2026-07")[0]["id"]
    memory.record_feedback(flux_memory, graph, finding_id, "confirmed", "checked with Sales Ops")
    assert graph["edges"]["4000::ENT-01"]["last_verdict"] == "confirmed"
    assert graph["edges"]["4000::ENT-01"]["last_note"] == "checked with Sales Ops"


def test_record_feedback_on_an_unknown_finding_id_is_a_no_op(flux_memory):
    graph = memory.load_graph()
    memory.persist_findings(flux_memory, graph, "2026-08", "2026-07", "run1", [_finding()])
    memory.record_feedback(flux_memory, graph, 9999, "confirmed")
    assert "last_verdict" not in graph["edges"]["4000::ENT-01"]


@pytest.mark.xfail(
    strict=True,
    reason=(
        "Known bug, documented in ARCHITECTURE.md section 10: record_feedback stamps "
        "last_verdict/last_note onto EVERY driver edge for the finding's account, not "
        "just the drivers that finding actually cited. The UI's feedback control only "
        "carries a finding_id, and a finding can bundle several drivers, so the write "
        "side loses the per-driver association the read side (recall) assumes. "
        "This test pins the bug in executable form -- do not 'fix' the test; when the "
        "verdict is scoped per driver, this xfail flips to a pass and the strict flag "
        "will fail the suite until the marker is removed."
    ),
)
def test_record_feedback_should_not_stamp_unrelated_driver_edges(flux_memory):
    graph = memory.load_graph()
    # An earlier run recorded ENT-99 as a driver of the same account.
    memory.persist_findings(
        flux_memory, graph, "2026-07", "2026-06", "run1", [_finding(driver_keys=("ENT-99",))]
    )
    # This period's finding cites only ENT-01.
    memory.persist_findings(
        flux_memory, graph, "2026-08", "2026-07", "run2", [_finding(driver_keys=("ENT-01",))]
    )
    finding_id = memory.findings_for_run(flux_memory, "2026-08", "2026-07")[0]["id"]
    memory.record_feedback(flux_memory, graph, finding_id, "confirmed")

    assert graph["edges"]["4000::ENT-01"]["last_verdict"] == "confirmed"
    # The verdict was about ENT-01. ENT-99 was not in that finding.
    assert "last_verdict" not in graph["edges"]["4000::ENT-99"]
