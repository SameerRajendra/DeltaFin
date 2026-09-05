"""LangGraph orchestration for the variance-explanation agent.

ingest_periods -> compute_variances -> rank_materiality -> slice_drivers
    -> recall_memory -> explain_drivers -> compile_action_plan -> synthesize_brief
    -> persist_memory -> render_artifacts

One trajectory per period comparison, traced to PRISM exactly like
app/graph.py's invoice pipeline (same get_handler/instrument/flush pattern).
"""

import json
import re
import uuid
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from app import llm as llm_module, tracing
from app.flux import actions as actions_module, brief as brief_module, ingest, memory, slicer, variance

EXPLAIN_PROMPT = """You are a senior FP&A analyst explaining a month-over-month variance
to an executive. Be specific and evidence-based; do not restate numbers you weren't given.
Each driver's cohort label (new / churned / expansion / contraction) already tells you its
business status -- only call a driver "new" if its cohort is literally "new". The separate
"institutional memory" section below is about whether OUR analysis has tracked this driver
in a past run, which is a different fact; don't conflate the two.

Account: {account_name} ({account_code})
Prior ({prior_period}): {prior_amt:,.2f}
Current ({current_period}): {current_amt:,.2f}
Delta: {delta:+,.2f} ({pct})

Top drivers (member, delta, share of this account's total delta):
{driver_lines}

Institutional memory on these drivers:
{memory_lines}

Subledger tie-out: {tie_out_note}

Write a JSON object with keys:
"headline" -- one sentence: what changed, by how much, and its single biggest named driver
(e.g. "Enterprise Revenue increased 32%, primarily driven by growth at 3 customers accounting
for 64% of the increase."). Use the concentration figure given above verbatim if present --
never invent your own count or percentage of drivers.
"why" -- 2-3 sentences naming the specific top drivers by name, their individual deltas, and
any recurrence/seasonality context from memory.
"action" -- one concrete, specific next step for a named team to take (not "monitor" or "keep
an eye on it" -- e.g. "Ask Sales Ops to confirm whether the Globex/Initech/Stark expansion is
contracted run-rate or a one-time upsell before it's forecast forward").
"priority" -- one of "P1" (material dollar impact or audit/compliance risk, act this week),
"P2" (review this close cycle), "P3" (monitor only, nothing actionable yet).
"owner" -- the specific team best placed to act, e.g. "Sales Ops", "Vendor Management",
"Marketing", "Controller / Accounting", "FP&A".
Return ONLY the JSON object."""

SYNTHESIS_PROMPT = """You are compiling the executive summary section of a variance report
covering the change from {prior_period} to {current_period} (in that direction -- {prior_period}
is the earlier, baseline period; {current_period} is the later period being explained). Given
these per-account findings (most material first), write 3-5 sentences covering: what changed
overall, the single biggest driver (name it, with its concentration figure if one is given,
e.g. "3 customers accounting for 64% of the increase"), and one thing that's recurring or
worth watching next period. Be concrete and cite the numbers given -- never invent a
concentration count or percentage that isn't in the findings below.

Findings:
{findings_text}"""


class FluxState(TypedDict, total=False):
    run_id: str
    period: str
    prior_period: str
    tie_out: list[dict]
    variances: list[dict]
    ranked: list[dict]
    drilldowns: list[dict]
    findings: list[dict]
    action_plan: list[dict]
    brief_text: str
    brief_path: str
    workpaper_path: str


def _fmt_pct(pct):
    if pct == float("inf"):
        return "new (no prior balance)"
    return f"{pct * 100:+.1f}%"


def _concentration_note(slice_) -> str:
    """How many of the top drivers it takes to explain most of the delta.

    This is the number that turns "revenue increased 32%" into "...with 3
    customers accounting for 64% of the increase" -- the concentration stat
    the narrative prompt is required to surface, not bury in a driver table.
    """
    if not slice_.get("available") or not slice_["drivers"]:
        return ""
    drivers = slice_["drivers"]
    # Smallest prefix of (same-signed) drivers that clears 60% cumulative share,
    # capped at 3 -- concentration stories lose force past a handful of names.
    for n in range(1, min(3, len(drivers)) + 1):
        if drivers[n - 1]["cumulative_share"] >= 0.6 or n == len(drivers):
            vendor_like = slice_.get("dimension") == "vendor"
            noun = ("vendor" if vendor_like else "customer") + ("" if n == 1 else "s")
            return f"{n} {noun} account for {drivers[n - 1]['cumulative_share'] * 100:.0f}% of this account's delta"
    return ""


def _driver_lines(drilldown) -> str:
    slice_ = drilldown["slice"]
    if not slice_.get("available"):
        return f"(none -- {slice_.get('reason', 'no detail')})"
    lines = []
    for d in slice_["drivers"]:
        lines.append(
            f"- {d['member_name']}: {d['delta']:+,.2f} ({d['cohort']}, "
            f"{d['contribution_share'] * 100:.0f}% of this account's delta)"
        )
    note = _concentration_note(slice_)
    if note:
        lines.append(f"Concentration: {note}.")
    return "\n".join(lines) or "(no material drivers)"


def _memory_lines(drilldown) -> str:
    ctx = drilldown.get("memory_context") or {}
    if not ctx:
        return "(no memory recorded yet for these drivers)"
    names = {}
    if drilldown["slice"].get("available"):
        names = {d["member_key"]: d["member_name"] for d in drilldown["slice"]["drivers"]}
    lines = []
    for driver_key, info in ctx.items():
        label = names.get(driver_key, driver_key)
        if not info.get("seen_before"):
            # This is about OUR analysis history, not the customer/vendor relationship
            # itself -- the driver's own new/churned/expansion/contraction status is
            # already reported separately in the driver table. Don't conflate the two.
            lines.append(f"- {label}: not tracked as a driver in any prior run of this agent")
            continue
        streak_note = f"{info['streak']} consecutive prior period(s)" if info["streak"] else "seen before, not consecutively (e.g. same period last year)"
        verdict_note = f", analyst previously marked '{info['last_verdict']}'" if info.get("last_verdict") else ""
        lines.append(f"- {label}: tracked as a driver before, {streak_note}{verdict_note}")
    return "\n".join(lines)


def _tie_out_note(state, account_code) -> str:
    for row in state.get("tie_out") or []:
        if row["account_code"] == account_code:
            if row["coverage_pct"] >= 99.9:
                return "fully reconciled to subledger detail"
            return f"only {row['coverage_pct']}% of the summary traced to subledger detail (gap ${row['gap']:,.2f})"
    return "not checked"


def _template_finding(drilldown, state) -> dict:
    account_code = drilldown["account_code"]
    pct = _fmt_pct(drilldown["pct"])
    slice_ = drilldown["slice"]
    headline = (
        f"{drilldown['account_name']} {'increased' if drilldown['delta'] >= 0 else 'decreased'} "
        f"{pct} (${drilldown['delta']:+,.2f}) versus {state['prior_period']}."
    )
    concentration = _concentration_note(slice_)
    if concentration:
        headline = headline[:-1] + f", with {concentration}."
    if slice_.get("available") and slice_["drivers"]:
        top = slice_["drivers"][:3]
        driver_desc = "; ".join(f"{d['member_name']} ({d['delta']:+,.2f}, {d['cohort']})" for d in top)
        cum_share = top[-1]["cumulative_share"] * 100
        why = (
            f"Top driver(s): {driver_desc}. Together the top {len(top)} account for "
            f"{cum_share:.0f}% of this account's ${slice_['total_delta']:+,.2f} delta."
        )
    else:
        why = f"No transaction-level detail is available for this account ({slice_.get('reason', 'n/a')})."
    recurring_notes = [
        f"{key} has recurred for {info['streak']} consecutive prior period(s)"
        for key, info in (drilldown.get("memory_context") or {}).items()
        if info.get("seen_before") and info.get("streak")
    ]
    if recurring_notes:
        why += " " + "; ".join(recurring_notes) + "."
    tie_note = _tie_out_note(state, account_code)
    action = "Review and confirm before close." if "gap" not in tie_note else f"Confirm this accrual: {tie_note}."
    return {"headline": headline, "why": why, "action": action}


def build_graph(llm=None, conn=None):
    """`conn` is a single DuckDB connection shared by every node in one graph run.

    Opening a fresh connection per node (each one re-globbing and re-registering
    views over all period CSVs) is what a `--replay` walk over 19+ periods needs
    to avoid: this sandbox's DuckDB memory accounting doesn't reliably release
    between many short-lived connections in one long-running process, and it
    OOMs partway through. One connection per `process_period()` call fixes that
    and is simply less I/O.
    """
    conn = conn or ingest.connect()

    def ingest_periods(state: FluxState) -> dict[str, Any]:
        periods = ingest.list_periods(conn)
        period = state.get("period") or periods[-1]
        prior_period = state.get("prior_period") or ingest.shift_period(period, -1)
        tie_out = ingest.tie_out(conn, period)
        return {"period": period, "prior_period": prior_period, "tie_out": tie_out}

    def compute_variances(state: FluxState) -> dict[str, Any]:
        variances = variance.compute_variances(conn, state["period"], state["prior_period"])
        return {"variances": variances}

    def rank_materiality(state: FluxState) -> dict[str, Any]:
        from app import config

        ranked = variance.rank_materiality(state["variances"])[: config.MAX_DRILLDOWNS]
        return {"ranked": ranked}

    def slice_drivers_node(state: FluxState) -> dict[str, Any]:
        drilldowns = []
        for v in state["ranked"]:
            slice_ = slicer.slice_drivers(conn, v["account_code"], state["period"], state["prior_period"])
            drilldowns.append({**v, "slice": slice_})
        return {"drilldowns": drilldowns}

    def recall_memory_node(state: FluxState) -> dict[str, Any]:
        graph = memory.load_graph()
        drilldowns = []
        for d in state["drilldowns"]:
            slice_ = d["slice"]
            driver_keys = [m["member_key"] for m in slice_["drivers"]] if slice_.get("available") else []
            context = memory.recall(graph, d["account_code"], driver_keys, state["period"])
            drilldowns.append({**d, "memory_context": context})
        return {"drilldowns": drilldowns}

    def explain_drivers(state: FluxState, config=None) -> dict[str, Any]:
        findings = []
        for d in state["drilldowns"]:
            if llm is not None:
                prompt = EXPLAIN_PROMPT.format(
                    account_name=d["account_name"],
                    account_code=d["account_code"],
                    prior_period=state["prior_period"],
                    current_period=state["period"],
                    prior_amt=d["prior_amt"],
                    current_amt=d["current_amt"],
                    delta=d["delta"],
                    pct=_fmt_pct(d["pct"]),
                    driver_lines=_driver_lines(d),
                    memory_lines=_memory_lines(d),
                    tie_out_note=_tie_out_note(state, d["account_code"]),
                )
                response = llm.invoke(prompt, config=config)
                text = response.content if hasattr(response, "content") else str(response)
                match = re.search(r"\{.*\}", text, re.DOTALL)
                parsed = None
                if match:
                    try:
                        parsed = json.loads(match.group(0))
                    except json.JSONDecodeError:
                        parsed = None
                if not parsed:
                    parsed = _template_finding(d, state)
            else:
                parsed = _template_finding(d, state)

            recurring_count = 1
            for info in (d.get("memory_context") or {}).values():
                if info.get("seen_before") and info.get("streak"):
                    recurring_count = max(recurring_count, info["streak"] + 1)

            priority = parsed.get("priority")
            if priority not in ("P1", "P2", "P3"):
                priority = actions_module.default_priority(d)
            owner = parsed.get("owner") or actions_module.owner_for(d["account_code"], d.get("account_type", ""))

            findings.append(
                {
                    "account_code": d["account_code"],
                    "account_name": d["account_name"],
                    "delta": d["delta"],
                    "pct": d["pct"],
                    "materiality_reason": d["materiality_reason"],
                    "headline": parsed.get("headline", ""),
                    "why": parsed.get("why", ""),
                    "action": parsed.get("action", ""),
                    "priority": priority,
                    "owner": owner,
                    "confidence": "high" if d["slice"].get("available") else "low",
                    "recurring_count": recurring_count,
                    "drivers": d["slice"].get("drivers", []) if d["slice"].get("available") else [],
                }
            )
        return {"findings": findings}

    def compile_action_plan(state: FluxState) -> dict[str, Any]:
        plan = actions_module.build_plan(state["findings"], state.get("tie_out") or [])
        return {"action_plan": plan}

    def synthesize_brief(state: FluxState, config=None) -> dict[str, Any]:
        findings_text = "\n".join(
            f"- {f['account_name']}: {f['headline']} {f['why']}" for f in state["findings"]
        ) or "No material variances this period."
        if llm is not None and state["findings"]:
            prompt = SYNTHESIS_PROMPT.format(
                current_period=state["period"], prior_period=state["prior_period"], findings_text=findings_text
            )
            response = llm.invoke(prompt, config=config)
            text = response.content if hasattr(response, "content") else str(response)
            return {"brief_text": text.strip()}
        header = f"Variance summary, {state['prior_period']} -> {state['period']}:\n"
        return {"brief_text": header + findings_text}

    def persist_memory_node(state: FluxState) -> dict[str, Any]:
        conn = memory.connect()
        graph = memory.load_graph()
        try:
            memory.persist_findings(conn, graph, state["period"], state["prior_period"], state["run_id"], state["findings"])
        finally:
            conn.close()
        return {}

    def render_artifacts(state: FluxState) -> dict[str, Any]:
        paths = brief_module.build(state)
        return paths

    builder = StateGraph(FluxState)
    builder.add_node("ingest_periods", ingest_periods)
    builder.add_node("compute_variances", compute_variances)
    builder.add_node("rank_materiality", rank_materiality)
    builder.add_node("slice_drivers", slice_drivers_node)
    builder.add_node("recall_memory", recall_memory_node)
    builder.add_node("explain_drivers", explain_drivers)
    builder.add_node("compile_action_plan", compile_action_plan)
    builder.add_node("synthesize_brief", synthesize_brief)
    builder.add_node("persist_memory", persist_memory_node)
    builder.add_node("render_artifacts", render_artifacts)

    builder.add_edge(START, "ingest_periods")
    builder.add_edge("ingest_periods", "compute_variances")
    builder.add_edge("compute_variances", "rank_materiality")
    builder.add_edge("rank_materiality", "slice_drivers")
    builder.add_edge("slice_drivers", "recall_memory")
    builder.add_edge("recall_memory", "explain_drivers")
    builder.add_edge("explain_drivers", "compile_action_plan")
    builder.add_edge("compile_action_plan", "synthesize_brief")
    builder.add_edge("synthesize_brief", "persist_memory")
    builder.add_edge("persist_memory", "render_artifacts")
    builder.add_edge("render_artifacts", END)
    return builder.compile()


def process_period(period=None, prior_period=None, session_id=None, conn=None):
    """Run one period comparison through the traced graph. One comparison = one PRISM trajectory.

    Pass `conn` (a shared `ingest.connect()`) when running many comparisons back
    to back (see run_flux.py --replay) -- see build_graph's docstring for why
    that matters in this sandbox.
    """
    run_id = uuid.uuid4().hex[:8]
    session_id = session_id or f"flux-{run_id}"
    owns_conn = conn is None
    conn = conn or ingest.connect()

    handler = tracing.get_handler(session_id, agent_name="flux-variance-graph")
    graph = tracing.instrument(build_graph(llm=llm_module.get_llm(), conn=conn), handler)
    try:
        return graph.invoke({"run_id": run_id, "period": period, "prior_period": prior_period})
    finally:
        if owns_conn:
            conn.close()
        tracing.flush(handler)
