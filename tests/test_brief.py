"""Tier 0 tests for `app/flux/brief.py`'s public renderers.

Pure functions over a hand-built, complete `FluxState`-shaped dict -- no
graph run, no LLM, no seeded data/ files. `workbook_bytes` is asserted to
produce valid .xlsx bytes without ever touching disk (it writes into an
`io.BytesIO`, not a path, so `config.FLUX_OUT_DIR` is redirected purely as a
belt-and-suspenders check that it's never even referenced).

Does not cover `brief.build()`, which does write four files into
`config.FLUX_OUT_DIR` -- that path is exercised instead by
`tests/test_flux_isolation.py`'s isolated=False contrast test.
"""

from app import config
from app.flux import brief


def _full_state():
    """A minimal but complete FluxState covering every key markdown(),
    _write_xlsx(), analysis_text(), and actions_text() index into."""
    return {
        "run_id": "run1",
        "period": "2026-08",
        "prior_period": "2026-07",
        "brief_text": "Overall variance summary.",
        "tie_out": [
            {
                "account_code": "4000",
                "account_name": "Enterprise Revenue",
                "summary_amt": 170_000.0,
                "txn_amt": 170_000.0,
                "gap": 0.0,
                "coverage_pct": 100.0,
            }
        ],
        "action_plan": [
            {
                "priority": "P1",
                "priority_label": "Act this week",
                "account_code": "4000",
                "account_name": "Enterprise Revenue",
                "owner": "Sales / RevOps (Enterprise)",
                "task": "Confirm the expansion.",
                "context": "headline",
                "amount": 70_000.0,
            }
        ],
        "findings": [
            {
                "account_code": "4000",
                "account_name": "Enterprise Revenue",
                "delta": 70_000.0,
                "pct": 0.7,
                "materiality_reason": "absolute-threshold",
                "headline": "Enterprise Revenue increased 70.0% ($+70,000.00).",
                "why": "Top driver: Acme Corp.",
                "action": "Confirm the expansion.",
                "priority": "P1",
                "owner": "Sales / RevOps (Enterprise)",
                "confidence": "high",
                "recurring_count": 1,
                "drivers": [
                    {
                        "member_key": "CUST-1",
                        "member_name": "Acme Corp",
                        "delta": 70_000.0,
                        "cohort": "expansion",
                        "contribution_share": 1.0,
                        "cumulative_share": 1.0,
                    }
                ],
            }
        ],
    }


def test_workbook_bytes_is_a_valid_xlsx_written_to_no_file(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "FLUX_OUT_DIR", tmp_path / "out_flux")
    data = brief.workbook_bytes(_full_state())
    assert data[:2] == b"PK"  # a .xlsx is a zip archive
    assert not config.FLUX_OUT_DIR.exists()


def test_markdown_reports_no_material_variances_when_findings_are_empty():
    state = _full_state()
    state["findings"] = []
    state["action_plan"] = []
    state["tie_out"] = []
    md = brief.markdown(state)
    assert "No material variances this period." in md
