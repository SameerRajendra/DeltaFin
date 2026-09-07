"""System-level evals for the variance-explanation agent, deterministic path only.

Walks every consecutive period comparison in the seeded 20-month dataset with
the LLM forcibly disabled, then scores the run against `evals/ground_truth.yaml`
(a hand-transcribed copy of the stories `data/seed_flux.py` plants -- see the
header of that file for why it is transcribed rather than imported).

What it scores:

  1. Detection recall        -- did each planted story's account clear the
                                materiality gate in its expected comparison,
                                and did it survive the MAX_DRILLDOWNS cap?
  2. Detection precision     -- what fraction of all findings sit in a
                                comparison/account with no planted story.
                                Reported, never asserted (see the
                                `false_positive_policy` note in the YAML).
  3. Driver attribution      -- top-1 and top-3 accuracy against the planted
                                per-customer / per-vendor deltas.
  4. Concentration stat      -- `graph._concentration_note` re-derived
                                independently from the CSVs with plain Python
                                (no DuckDB, no slicer) and compared, plus the
                                documented ">3 drivers never clear 60%" edge
                                case, which must yield an empty note.
  5. Memory after replay     -- the seasonal edge reads seen_before/streak=0,
                                the hosting edge is recorded, and a churned
                                customer stops appearing as a driver.
  6. Tie-out                 -- the summary-only insurance accrual reports 0%
                                subledger coverage every period and lands a P1
                                Controller/Accounting action every period.

Run it:

    python data/seed_flux.py        # once, if data/financials is not built
    python evals/run_evals.py

Writes `evals/results/latest.md` and exits non-zero if any assertion failed.

Isolation: this script redirects `config.FLUX_DB_PATH`, `config.FLUX_GRAPH_PATH`
and `config.FLUX_OUT_DIR` into `evals/results/`, and wipes them first, so the
replay always starts from empty memory and never touches the repo's real
institutional memory or the `out/` demo artifacts.
"""

import argparse
import csv
import shutil
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import config  # noqa: E402

EVAL_DIR = Path(__file__).resolve().parent
RESULTS_DIR = EVAL_DIR / "results"
MEMORY_DIR = RESULTS_DIR / "_memory"
ARTIFACT_DIR = RESULTS_DIR / "_artifacts"

PASS = "PASS"
FAIL = "FAIL"
INFO = "INFO"
SKIP = "SKIP"


# --------------------------------------------------------------------------
# result collection
# --------------------------------------------------------------------------


class Scorecard:
    def __init__(self):
        self.rows = []

    def record(self, group, name, status, detail=""):
        self.rows.append({"group": group, "name": name, "status": status, "detail": detail})
        return status

    def check(self, group, name, ok, detail=""):
        return self.record(group, name, PASS if ok else FAIL, detail)

    def info(self, group, name, detail=""):
        return self.record(group, name, INFO, detail)

    def skip(self, group, name, detail=""):
        return self.record(group, name, SKIP, detail)

    @property
    def failures(self):
        return [r for r in self.rows if r["status"] == FAIL]

    def counts(self):
        out = defaultdict(int)
        for r in self.rows:
            out[r["status"]] += 1
        return dict(out)


# --------------------------------------------------------------------------
# independent recomputation straight off the CSVs
# --------------------------------------------------------------------------


def _txn_rows(period):
    path = config.FINANCIALS_DIR / "transactions" / f"{period}.csv"
    with path.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def _summary_rows(period):
    path = config.FINANCIALS_DIR / "summaries" / f"{period}.csv"
    with path.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def _dimension_key(account_code):
    """Mirror of slicer._dimension_for, re-stated here on purpose.

    The whole value of this recomputation is that it does not call into
    app/flux/slicer.py -- if the two implementations disagree, one of them is
    wrong and the eval says so.
    """
    if account_code.startswith("4"):
        return "customer_id"
    if account_code[:1] in ("5", "6"):
        return "vendor"
    return None


def independent_slice(account_code, period, prior_period, max_drivers):
    """Recompute driver deltas, shares and the concentration note in plain Python."""
    key = _dimension_key(account_code)
    if key is None:
        return None

    totals = {period: defaultdict(float), prior_period: defaultdict(float)}
    for p in (period, prior_period):
        for row in _txn_rows(p):
            if row["account_code"] != account_code:
                continue
            member = row.get(key) or "(unattributed)"
            totals[p][member] += float(row["amount"])

    members = []
    for member in set(totals[period]) | set(totals[prior_period]):
        cur = totals[period].get(member, 0.0)
        pri = totals[prior_period].get(member, 0.0)
        if cur == 0 and pri == 0:
            continue
        members.append({"member_key": member, "current_amt": cur, "prior_amt": pri,
                        "delta": round(cur - pri, 2)})
    if not members:
        return None

    members.sort(key=lambda m: abs(m["delta"]), reverse=True)
    total_delta = sum(m["delta"] for m in members)
    running = 0.0
    for m in members:
        share = (m["delta"] / total_delta) if total_delta else 0.0
        m["contribution_share"] = round(share, 4)
        running += share
        m["cumulative_share"] = round(running, 4)

    drivers = members[:max_drivers]
    return {
        "dimension": key,
        "total_delta": round(total_delta, 2),
        "all_members": len(members),
        "drivers": drivers,
        "note": independent_concentration_note(drivers, key),
    }


def independent_concentration_note(drivers, dimension):
    """Mirror of graph._concentration_note, re-stated for the same reason."""
    if not drivers:
        return ""
    for n in range(1, min(3, len(drivers)) + 1):
        if drivers[n - 1]["cumulative_share"] >= 0.6 or n == len(drivers):
            noun = ("vendor" if dimension == "vendor" else "customer") + ("" if n == 1 else "s")
            pct = drivers[n - 1]["cumulative_share"] * 100
            return f"{n} {noun} account for {pct:.0f}% of this account's delta"
    return ""


# --------------------------------------------------------------------------
# the replay
# --------------------------------------------------------------------------


def _isolate_state():
    for path in (MEMORY_DIR, ARTIFACT_DIR):
        if path.exists():
            shutil.rmtree(path)
        path.mkdir(parents=True, exist_ok=True)
    config.FLUX_DB_PATH = MEMORY_DIR / "flux_memory.db"
    config.FLUX_GRAPH_PATH = MEMORY_DIR / "flux_memory_graph.json"
    config.FLUX_OUT_DIR = ARTIFACT_DIR


def replay():
    """Every consecutive comparison, oldest first, with the LLM disabled.

    Oldest-first matters: institutional memory is written by each run and read
    by the next, so the streak/seasonality expectations are only meaningful
    after a full ordered walk.
    """
    from app import llm as llm_module

    # Deterministic path only. Even if MODAL_QWEN_URL is set in the
    # environment, this eval never calls a model -- it scores the arithmetic
    # and the memory, not narrative quality.
    llm_module.get_llm = lambda: None

    from app.flux import graph as flux_graph, ingest

    conn = ingest.connect()
    try:
        periods = ingest.list_periods(conn)
        results = []
        for prior_period, period in zip(periods, periods[1:]):
            state = flux_graph.process_period(period=period, prior_period=prior_period, conn=conn)
            results.append(
                {
                    "period": period,
                    "prior_period": prior_period,
                    "variances": state.get("variances") or [],
                    "ranked": state.get("ranked") or [],
                    "drilldowns": state.get("drilldowns") or [],
                    "findings": state.get("findings") or [],
                    "action_plan": state.get("action_plan") or [],
                    "tie_out": state.get("tie_out") or [],
                }
            )
        return periods, results
    finally:
        conn.close()


def _by_comparison(results):
    return {(r["period"], r["prior_period"]): r for r in results}


def _variance_row(result, account_code):
    for v in result["variances"]:
        if v["account_code"] == account_code:
            return v
    return None


def _drilldown(result, account_code):
    for d in result["drilldowns"]:
        if d["account_code"] == account_code:
            return d
    return None


# --------------------------------------------------------------------------
# scoring
# --------------------------------------------------------------------------


def score_config(card, gt):
    expected = gt["meta"]["config"]
    for name, value in expected.items():
        actual = getattr(config, name)
        card.check(
            "Config guard",
            f"app.config.{name} == {value}",
            actual == value,
            f"actual {actual}",
        )


def score_story(card, story, comparisons, tol_abs):
    from app.flux import variance

    sid = story["id"]
    account = story["account_code"]
    key = (story["period"], story["prior_period"])
    result = comparisons.get(key)
    if result is None:
        card.check("Detection recall", f"{sid}: comparison {key[1]} -> {key[0]} was walked", False)
        return
    label = f"{sid} ({account}, {key[1]} -> {key[0]})"

    # --- materiality gate, independent of the MAX_DRILLDOWNS cap -----------
    v = _variance_row(result, account)
    if v is None:
        card.check("Detection recall", f"{label}: account present in variances", False)
        return
    material, reason = variance.is_material(v)
    card.check(
        "Detection recall",
        f"{label}: clears the materiality gate",
        material is bool(story["material"]),
        f"delta {v['delta']:+,.2f}, pct {v['pct']:.4f}, z {v['z_score']}, reason '{reason}'",
    )
    if "materiality_reason" in story:
        card.check(
            "Detection recall",
            f"{label}: materiality reason is '{story['materiality_reason']}'",
            reason == story["materiality_reason"],
            f"actual '{reason}'",
        )
    elif "materiality_reason_any_of" in story:
        allowed = story["materiality_reason_any_of"]
        card.check(
            "Detection recall",
            f"{label}: materiality reason in {allowed}",
            reason in allowed,
            f"actual '{reason}'",
        )

    # --- survived the drill-down cap ---------------------------------------
    surfaced = account in {f["account_code"] for f in result["findings"]}
    card.check(
        "Detection recall",
        f"{label}: surfaced as a finding (past the MAX_DRILLDOWNS cap)",
        surfaced is bool(story["material"]),
        f"{len(result['findings'])} findings this comparison",
    )

    # --- exact planted amounts ---------------------------------------------
    for field, expected in (story.get("exact") or {}).items():
        actual = v[field]
        ok = abs(actual - expected) <= (tol_abs if field != "pct" else 1e-9)
        card.check("Planted amounts", f"{label}: {field} == {expected}", ok, f"actual {actual}")

    # --- driver attribution -------------------------------------------------
    d = _drilldown(result, account)
    if d is None:
        if story["material"]:
            card.check("Driver attribution", f"{label}: drill-down present", False)
        return
    slice_ = d["slice"]
    if not slice_.get("available"):
        if story.get("slice_available") is False:
            card.check("Driver attribution", f"{label}: slice correctly unavailable", True,
                       slice_.get("reason", ""))
        else:
            card.check("Driver attribution", f"{label}: slice available", False,
                       slice_.get("reason", ""))
        return

    drivers = slice_["drivers"]
    keys = [m["member_key"] for m in drivers]
    deltas = {m["member_key"]: m["delta"] for m in drivers}
    cohorts = {m["member_key"]: m["cohort"] for m in drivers}

    if "top_1" in story:
        card.check(
            "Driver attribution",
            f"{label}: top-1 driver is {story['top_1']}",
            keys[:1] == [story["top_1"]],
            f"actual top-3 {keys[:3]}",
        )
    if "top_3" in story:
        card.check(
            "Driver attribution",
            f"{label}: top-3 drivers are {sorted(story['top_3'])}",
            sorted(keys[:3]) == sorted(story["top_3"]),
            f"actual {keys[:3]}",
        )
    for member, expected in (story.get("driver_deltas") or {}).items():
        actual = deltas.get(member)
        card.check(
            "Driver attribution",
            f"{label}: {member} delta == {expected:+,.2f}",
            actual is not None and abs(actual - expected) <= tol_abs,
            f"actual {actual}",
        )
    for member, expected in (story.get("driver_cohorts") or {}).items():
        card.check(
            "Driver attribution",
            f"{label}: {member} cohort is '{expected}'",
            cohorts.get(member) == expected,
            f"actual '{cohorts.get(member)}'",
        )
    if story.get("all_driver_cohorts"):
        expected = story["all_driver_cohorts"]
        card.check(
            "Driver attribution",
            f"{label}: every driver is '{expected}'",
            all(c == expected for c in cohorts.values()),
            f"actual {cohorts}",
        )
    for member, sign in (story.get("driver_delta_sign") or {}).items():
        actual = deltas.get(member)
        ok = actual is not None and ((actual < 0) if sign == "negative" else (actual > 0))
        card.check("Driver attribution", f"{label}: {member} delta is {sign}", ok, f"actual {actual}")

    if "concentration_note" in story:
        from app.flux import graph as flux_graph

        note = flux_graph._concentration_note(slice_)
        card.check(
            "Concentration stat",
            f"{label}: concentration note",
            note == story["concentration_note"],
            f"actual '{note}'",
        )


def score_absent_after(card, story, results, group="Memory correctness"):
    """A churned / one-off driver must stop appearing once it has no money on either side."""
    account = story["account_code"]
    after = story["absent_as_driver_after"]
    members = set(story.get("driver_deltas") or {}) | {story.get("top_1")}
    members.discard(None)
    offenders = []
    for r in results:
        if r["period"] <= after:
            continue
        d = _drilldown(r, account)
        if d is None or not d["slice"].get("available"):
            continue
        present = {m["member_key"] for m in d["slice"]["drivers"]} & members
        if present:
            offenders.append(f"{r['period']}: {sorted(present)}")
    card.check(
        group,
        f"{story['id']}: {sorted(members)} never a driver of {account} after {after}",
        not offenders,
        "; ".join(offenders) or "clean across every later comparison",
    )


def score_memory(card, gt, results):
    from app.flux import memory

    graph = memory.load_graph()
    edges = graph.get("edges", {})

    for story in gt["stories"]:
        spec = story.get("memory")
        if not spec:
            continue
        edge_key = spec["edge"]
        account, _, driver = edge_key.partition("::")
        ctx = memory.recall(graph, account, [driver], spec["before_period"]).get(driver, {})
        card.check(
            "Memory correctness",
            f"{story['id']}: {edge_key} seen_before is {spec['seen_before']} at {spec['before_period']}",
            ctx.get("seen_before") is spec["seen_before"],
            f"actual {ctx}",
        )
        card.check(
            "Memory correctness",
            f"{story['id']}: {edge_key} streak is {spec['streak']} at {spec['before_period']}",
            ctx.get("streak") == spec["streak"],
            f"actual {ctx.get('streak')}",
        )

    # The hosting overrun's first month is the only one whose materiality is
    # predictable by reading the generator, so that is the only part asserted;
    # the resulting streak is reported, not asserted (see ground_truth.yaml).
    hosting_key = "5000::CloudBeam Compute"
    edge = edges.get(hosting_key)
    card.check(
        "Memory correctness",
        f"{hosting_key} recorded, including 2026-06",
        bool(edge) and "2026-06" in (edge or {}).get("periods", {}),
        f"periods {sorted((edge or {}).get('periods', {}))}",
    )
    if edge:
        ctx = memory.recall(graph, "5000", ["CloudBeam Compute"], "2026-08")["CloudBeam Compute"]
        card.info(
            "Memory correctness",
            f"{hosting_key} streak at 2026-08 (observed, not asserted)",
            f"seen_before={ctx.get('seen_before')} streak={ctx.get('streak')} "
            f"periods={sorted(edge['periods'])}",
        )

    for story in gt["stories"]:
        if "absent_as_driver_after" in story:
            score_absent_after(card, story, results)


def score_tie_out(card, gt, results):
    story = next(s for s in gt["stories"] if s["id"] == "insurance_accrual")
    account = story["account_code"]
    spec = story["tie_out_every_period"]

    bad_coverage, missing_action = [], []
    for r in results:
        row = next((t for t in r["tie_out"] if t["account_code"] == account), None)
        if row is None or row["coverage_pct"] != spec["coverage_pct"]:
            bad_coverage.append(f"{r['period']}: {row and row['coverage_pct']}")
        items = [
            i
            for i in r["action_plan"]
            if i["account_code"] == account
            and i["priority"] == spec["action_priority"]
            and i["owner"] == spec["action_owner"]
        ]
        if not items:
            missing_action.append(r["period"])

    card.check(
        "Tie-out",
        f"account {account} reports {spec['coverage_pct']}% subledger coverage in all "
        f"{len(results)} periods",
        not bad_coverage,
        "; ".join(bad_coverage) or "0.0% every period",
    )
    card.check(
        "Tie-out",
        f"account {account} produces a {spec['action_priority']} "
        f"'{spec['action_owner']}' action in all {len(results)} periods",
        not missing_action,
        "; ".join(missing_action) or "present every period",
    )

    # The planted summary amounts themselves, straight off the CSVs.
    baseline = story["exact_amounts"]["baseline"]
    spike_period = story["spike"]["period"]
    spike_amt = story["exact_amounts"][spike_period]
    wrong = []
    for r in results:
        rows = {row["account_code"]: float(row["amount"]) for row in _summary_rows(r["period"])}
        expected = spike_amt if r["period"] == spike_period else baseline
        if rows.get(account) != expected:
            wrong.append(f"{r['period']}: {rows.get(account)} != {expected}")
    card.check("Tie-out", f"account {account} summary amounts match the planted series",
               not wrong, "; ".join(wrong) or "every period matches")

    # The true-up and its reversal are ordinary account variances, so they are
    # handed back to be scored by the same path every other story uses.
    sub_stories = []
    for key in ("spike", "reversal"):
        sub = story[key]
        sub_stories.append(
            {
                "id": f"insurance_accrual.{key}",
                "account_code": account,
                "period": sub["period"],
                "prior_period": sub["prior_period"],
                "material": sub["material"],
                "materiality_reason": sub["materiality_reason"],
                "exact": {"delta": sub["delta"]},
                "slice_available": sub.get("slice_available", False),
            }
        )
    return sub_stories


def score_concentration_everywhere(card, results, max_drivers, tol_abs):
    """Re-derive every concentration note from the CSVs and compare."""
    from app.flux import graph as flux_graph

    compared = mismatched = 0
    mismatches = []
    delta_mismatches = []
    empty_note_cases = []
    for r in results:
        for d in r["drilldowns"]:
            slice_ = d["slice"]
            if not slice_.get("available"):
                continue
            independent = independent_slice(d["account_code"], r["period"], r["prior_period"], max_drivers)
            if independent is None:
                continue
            compared += 1
            pipeline_note = flux_graph._concentration_note(slice_)
            if pipeline_note != independent["note"]:
                mismatched += 1
                mismatches.append(
                    f"{r['period']} {d['account_code']}: pipeline '{pipeline_note}' "
                    f"vs recomputed '{independent['note']}'"
                )
            # The n and the percentage both come out of the driver table, so
            # the deltas backing them are checked too.
            pipe_deltas = {m["member_key"]: m["delta"] for m in slice_["drivers"]}
            for m in independent["drivers"]:
                actual = pipe_deltas.get(m["member_key"])
                if actual is None or abs(actual - m["delta"]) > tol_abs:
                    delta_mismatches.append(
                        f"{r['period']} {d['account_code']} {m['member_key']}: "
                        f"pipeline {actual} vs recomputed {m['delta']}"
                    )
            # Documented edge case: more than 3 drivers, none of the first
            # three clearing 60% cumulative -> an EMPTY note, not a wrong one.
            drivers = slice_["drivers"]
            if len(drivers) > 3 and drivers[2]["cumulative_share"] < 0.6:
                empty_note_cases.append(
                    (f"{r['period']} {d['account_code']} "
                     f"(cum3={drivers[2]['cumulative_share']})", pipeline_note)
                )

    card.check(
        "Concentration stat",
        f"all {compared} concentration notes match an independent CSV recomputation",
        mismatched == 0,
        "; ".join(mismatches[:5]) or "no mismatches",
    )
    card.check(
        "Concentration stat",
        "every driver delta matches an independent CSV recomputation",
        not delta_mismatches,
        "; ".join(delta_mismatches[:5]) or "no mismatches",
    )
    if empty_note_cases:
        bad = [case for case, note in empty_note_cases if note != ""]
        card.check(
            "Concentration stat",
            f">3 drivers never clearing 60% yields an empty note "
            f"({len(empty_note_cases)} instance(s))",
            not bad,
            "; ".join(f"{c} -> '{n}'" for c, n in empty_note_cases[:5] if n != "") or "all empty",
        )
    else:
        card.info(
            "Concentration stat",
            ">3 drivers never clearing 60% (empty-note edge case)",
            "0 instances in this dataset -- the branch is exercised by "
            "tests only, not by the seeded data",
        )


def score_precision(card, gt, results):
    """False-positive rate: findings in comparisons with no planted story."""
    policy = gt["false_positive_policy"]
    story_set = {(s["period"], s["account_code"]) for s in policy["story_comparisons"]}

    total = 0
    explained = 0
    unexplained = []
    per_account = defaultdict(int)
    for r in results:
        for f in r["findings"]:
            total += 1
            if (r["period"], f["account_code"]) in story_set:
                explained += 1
            else:
                unexplained.append(f"{r['period']} {f['account_code']} {f['account_name']} "
                                   f"({f['delta']:+,.2f}, {f['materiality_reason']})")
                per_account[f"{f['account_code']} {f['account_name']}"] += 1

    rate = (len(unexplained) / total) if total else 0.0
    precision = (explained / total) if total else 0.0
    card.info(
        "Detection precision",
        "findings per comparison",
        f"{total} findings across {len(results)} comparisons "
        f"({total / len(results):.2f} per comparison)",
    )
    card.info(
        "Detection precision",
        "precision against the planted set",
        f"{explained}/{total} findings sit on a planted story = {precision * 100:.1f}%",
    )
    if unexplained:
        breakdown = ", ".join(
            f"{k} x{v}" for k, v in sorted(per_account.items(), key=lambda kv: -kv[1])
        )
        detail = (
            f"{len(unexplained)}/{total} = {rate * 100:.1f}%. These are not necessarily "
            f"wrong -- the base series are genuinely noised, so a real 12% swing in R&D "
            f"is a real variance -- but nothing in the dataset planted them. "
            f"By account: {breakdown}"
        )
    else:
        detail = "no findings outside the planted set"
    card.info("Detection precision", "unexplained (candidate false-positive) rate", detail)

    if policy.get("assert_max_rate") is None:
        card.skip(
            "Detection precision",
            "assert a ceiling on the unexplained rate",
            "ground_truth.yaml sets false_positive_policy.assert_max_rate: null. "
            "Fill it in once the number above has been reviewed by a human; "
            "inventing a threshold before measuring it would be meaningless.",
        )
    else:
        card.check(
            "Detection precision",
            f"unexplained rate <= {policy['assert_max_rate']}",
            rate <= policy["assert_max_rate"],
            f"actual {rate:.4f}",
        )

    return unexplained


def score_report_only(card, gt, comparisons):
    from app.flux import variance

    for story in gt["stories"]:
        if not story.get("report_only"):
            continue
        account = story["account_code"]
        for period in story["periods"]:
            result = next((r for k, r in comparisons.items() if k[0] == period), None)
            if result is None:
                continue
            v = _variance_row(result, account)
            material, reason = variance.is_material(v) if v else (None, "")
            d = _drilldown(result, account)
            drivers = ""
            if d and d["slice"].get("available"):
                drivers = ", ".join(
                    f"{m['member_key']} {m['delta']:+,.2f}" for m in d["slice"]["drivers"]
                )
            card.info(
                "Report-only",
                f"{story['id']}: {account} at {period}",
                f"delta {v['delta']:+,.2f} z {v['z_score']} -> material={material} "
                f"('{reason}'); drivers: {drivers or 'not drilled into'}",
            )


# --------------------------------------------------------------------------
# scorecard
# --------------------------------------------------------------------------


def write_scorecard(card, path, periods, results, unexplained):
    counts = card.counts()
    lines = [
        "# Flux eval scorecard",
        "",
        f"_Run {datetime.now(timezone.utc).isoformat(timespec='seconds')} UTC · "
        f"{len(results)} period comparisons ({periods[1]} through {periods[-1]}) · "
        f"deterministic path, no model configured_",
        "",
        f"**{counts.get(PASS, 0)} passed · {counts.get(FAIL, 0)} failed · "
        f"{counts.get(INFO, 0)} reported · {counts.get(SKIP, 0)} skipped**",
        "",
    ]

    groups = []
    for row in card.rows:
        if row["group"] not in groups:
            groups.append(row["group"])

    for group in groups:
        lines += [f"## {group}", "", "| Status | Check | Detail |", "|---|---|---|"]
        for row in card.rows:
            if row["group"] != group:
                continue
            detail = str(row["detail"]).replace("|", "\\|").replace("\n", " ")
            lines.append(f"| {row['status']} | {row['name']} | {detail} |")
        lines.append("")

    if unexplained:
        lines += [
            "## Unexplained findings (full list)",
            "",
            "Findings in a comparison/account with no planted story. Reviewing this "
            "list is how the false-positive number stops being a guess.",
            "",
        ]
        lines += [f"- {u}" for u in unexplained]
        lines.append("")

    lines += [
        "## What this scorecard does not cover",
        "",
        "- **LLM narrative quality.** This run disables the model entirely; every "
        "`headline`/`why`/`action` scored here comes from `graph._template_finding`.",
        "- **The AP pipeline.** `app/graph.py`, `app/controls.py` and `app/extraction.py` "
        "are covered by `tests/`, not here.",
        "- **Rendering.** The markdown brief and `.xlsx` workpaper are produced during the "
        "replay but nothing asserts their contents.",
        "",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


# --------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ground-truth", default=str(EVAL_DIR / "ground_truth.yaml"))
    parser.add_argument("--out", default=str(RESULTS_DIR / "latest.md"))
    args = parser.parse_args()

    summaries = config.FINANCIALS_DIR / "summaries"
    if not summaries.exists() or not any(summaries.glob("*.csv")):
        print(
            f"No seeded financials at {config.FINANCIALS_DIR}. Run `python data/seed_flux.py` first.",
            file=sys.stderr,
        )
        return 2

    gt = yaml.safe_load(Path(args.ground_truth).read_text(encoding="utf-8"))
    tol_abs = gt["meta"]["recompute_tolerance_abs"]
    max_drivers = gt["meta"]["config"]["MAX_DRIVERS"]

    _isolate_state()
    card = Scorecard()
    score_config(card, gt)

    periods, results = replay()
    card.check(
        "Dataset",
        f"the generator still produces {gt['meta']['period_count']} periods "
        f"({gt['meta']['first_period']}..{gt['meta']['last_period']})",
        len(periods) == gt["meta"]["period_count"]
        and periods[0] == gt["meta"]["first_period"]
        and periods[-1] == gt["meta"]["last_period"],
        f"actual {len(periods)}: {periods[0]}..{periods[-1]}",
    )

    comparisons = _by_comparison(results)
    for story in gt["stories"]:
        if story.get("report_only") or "period" not in story:
            continue
        score_story(card, story, comparisons, tol_abs)

    for sub_story in score_tie_out(card, gt, results):
        score_story(card, sub_story, comparisons, tol_abs)

    score_concentration_everywhere(card, results, max_drivers, tol_abs)
    score_memory(card, gt, results)
    score_report_only(card, gt, comparisons)
    unexplained = score_precision(card, gt, results)

    out_path = Path(args.out)
    write_scorecard(card, out_path, periods, results, unexplained)

    counts = card.counts()
    print(
        f"{counts.get(PASS, 0)} passed, {counts.get(FAIL, 0)} failed, "
        f"{counts.get(INFO, 0)} reported, {counts.get(SKIP, 0)} skipped -> {out_path}"
    )
    for row in card.failures:
        print(f"  FAIL [{row['group']}] {row['name']} -- {row['detail']}")
    return 1 if card.failures else 0


if __name__ == "__main__":
    sys.exit(main())
