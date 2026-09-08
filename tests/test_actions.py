"""Tier 0: app/flux/actions.py plan assembly. No LLM, no DB, no seeded data.

Does not cover priority/owner heuristics themselves (default_priority /
owner_for) beyond what plan assembly needs -- only that build_plan emits one
row per account and keeps the more urgent priority when an account is both a
material finding and a subledger tie-out gap.
"""

from app.flux import actions


def _finding(account_code="7000", account_name="Accrued Liabilities", priority="P3", delta=25_000.0):
    return {
        "account_code": account_code,
        "account_name": account_name,
        "priority": priority,
        "owner": "FP&A",
        "action": "Review this variance.",
        "headline": f"{account_name} moved.",
        "delta": delta,
    }


def _gap(account_code="7000", account_name="Accrued Liabilities", coverage_pct=0.0, gap=5_000.0):
    return {
        "account_code": account_code,
        "account_name": account_name,
        "coverage_pct": coverage_pct,
        "gap": gap,
    }


def test_account_that_is_both_a_finding_and_a_gap_gets_one_row():
    """An accrual trued up in a month it does not reconcile is materially up
    AND has 0% subledger coverage. Emitting a task for each produced two
    near-identical rows at different priorities for one underlying problem."""
    plan = actions.build_plan([_finding()], [_gap()])
    assert len(plan) == 1
    assert plan[0]["account_code"] == "7000"


def test_the_more_urgent_priority_wins_when_merging():
    plan = actions.build_plan([_finding(priority="P3")], [_gap(coverage_pct=0.0)])
    assert plan[0]["priority"] == actions.gap_priority(0.0, 5_000.0)
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


# --- gap priority scales with the money behind the gap ----------------------


def test_a_small_uncovered_gap_does_not_outrank_a_large_variance():
    """Coverage alone made a $5,000 accrual that never moves outrank a $96,000
    revenue swing, and put the identical row at the top of every period."""
    assert actions.gap_priority(0.0, 5_000.0) == "P2"


def test_a_large_uncovered_gap_is_still_p1():
    assert actions.gap_priority(0.0, 30_000.0) == "P1"


def test_a_trivial_gap_is_p3():
    assert actions.gap_priority(0.0, 100.0) == "P3"


def test_full_coverage_produces_no_action_at_all():
    assert actions.gap_priority(100.0, 50_000.0) is None


def test_a_large_gap_that_is_mostly_covered_is_not_p1():
    """P1 needs both a material amount and genuinely poor coverage."""
    assert actions.gap_priority(80.0, 30_000.0) == "P2"


# --- a gap that did not move is monitoring, not this month's action ---------


def test_an_unchanged_gap_drops_to_monitoring():
    """A permanent summary-only accrual is 0%-covered in every period. Judged on
    coverage alone it reprinted the identical P2 'confirm this accrual' task
    every month until the reader learned to skip the row."""
    plan = actions.build_plan(
        [],
        [{**_gap(), "summary_amt": 5_000.0}],
        [{"account_code": "7000", "delta": 0.0}],
    )
    assert plan[0]["priority"] == "P3"
    assert "unchanged" in plan[0]["task"]


def test_a_gap_that_moved_keeps_its_coverage_priority():
    plan = actions.build_plan(
        [],
        [{**_gap(), "summary_amt": 30_000.0, "gap": 30_000.0}],
        [{"account_code": "7000", "delta": 25_000.0}],
    )
    assert plan[0]["priority"] == actions.gap_priority(0.0, 30_000.0)
    assert "Confirm the Accrued Liabilities accrual" in plan[0]["task"]


def test_without_variances_the_old_two_argument_behavior_is_unchanged():
    plan = actions.build_plan([], [_gap()])
    assert plan[0]["priority"] == "P2"
    assert "Confirm the Accrued Liabilities accrual" in plan[0]["task"]
