"""Tier 0: app/flux/actions.py plan assembly. No LLM, no DB, no seeded data.

Does not cover priority/owner heuristics themselves (default_priority /
owner_for) beyond what plan assembly needs -- only that build_plan emits one
row per account and keeps the more urgent priority when an account is both a
material finding and a subledger tie-out gap.
"""

from app.flux import actions


def _finding(account_code="7000", account_name="Insurance", priority="P3", delta=25_000.0):
    return {
        "account_code": account_code,
        "account_name": account_name,
        "priority": priority,
        "owner": "FP&A",
        "action": "Review this variance.",
        "headline": f"{account_name} moved.",
        "delta": delta,
    }


def _gap(account_code="7000", account_name="Insurance", coverage_pct=0.0, gap=5_000.0):
    return {
        "account_code": account_code,
        "account_name": account_name,
        "coverage_pct": coverage_pct,
        "gap": gap,
    }


def test_account_that_is_both_a_finding_and_a_gap_gets_one_row():
    """Insurance in the premium-true-up month is materially up AND has 0%
    subledger coverage. Emitting a task for each produced two near-identical
    rows at different priorities for one underlying problem."""
    plan = actions.build_plan([_finding()], [_gap()])
    assert len(plan) == 1
    assert plan[0]["account_code"] == "7000"


def test_the_more_urgent_priority_wins_when_merging():
    plan = actions.build_plan([_finding(priority="P3")], [_gap(coverage_pct=0.0)])
    assert plan[0]["priority"] == actions.gap_priority(0.0)
    assert "subledger detail" in plan[0]["task"]


def test_merged_row_records_both_reasons_in_context():
    plan = actions.build_plan([_finding()], [_gap()])
    assert "Subledger tie-out gap" in plan[0]["context"]


def test_a_gap_on_an_account_with_no_finding_still_gets_its_own_row():
    plan = actions.build_plan([_finding(account_code="4000", account_name="Revenue")], [_gap()])
    assert {i["account_code"] for i in plan} == {"4000", "7000"}


def test_a_finding_with_no_gap_is_untouched():
    plan = actions.build_plan([_finding(account_code="4000", account_name="Revenue")], [])
    assert len(plan) == 1
    assert plan[0]["task"] == "Review this variance."
