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
    """A period where nothing cleared the gate has to say so in terms of the
    gate. A bare "no material variances" line above an empty section reads as a
    run that failed rather than a real result, which is why markdown() now
    quotes the configured thresholds back."""
    state = _full_state()
    state["findings"] = []
    state["action_plan"] = []
    state["tie_out"] = []
    md = brief.markdown(state)
    assert "No account cleared the materiality gate" in md
    assert "clean close, not a failed run" in md
    assert f"{config.MATERIALITY_ABS:,.0f}" in md
    # No variances means compute_variances never produced a z-score, so the
    # brief must not claim an anomaly check that never ran (brief._gate_sentence).
    assert "sigma" not in md


def test_markdown_lists_the_largest_movements_when_nothing_cleared_the_gate():
    """The quiet-period fallback: the reader's question is "how close was
    anything to mattering?", so the movements are tabulated rather than just
    declared absent."""
    state = _full_state()
    state["findings"] = []
    state["action_plan"] = []
    state["tie_out"] = []
    state["variances"] = [
        {
            "account_code": "4000",
            "account_name": "Enterprise Revenue",
            "prior_amt": 100_000.0,
            "current_amt": 109_351.0,
            "delta": 9_351.0,
            "pct": 0.09351,
            "z_score": 0.0,
        },
        {
            "account_code": "6000",
            "account_name": "Sales & Marketing",
            "prior_amt": 70_000.0,
            "current_amt": 67_384.0,
            "delta": -2_616.0,
            "pct": -0.0374,
            "z_score": 0.0,
        },
    ]
    md = brief.markdown(state)
    assert "Enterprise Revenue (4000)" in md
    assert "+9,351.00" in md
    # Largest absolute movement first, regardless of sign.
    assert md.index("Enterprise Revenue (4000)") < md.index("Sales & Marketing (6000)")
