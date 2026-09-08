"""Render the variance-explanation agent's output: a markdown executive brief
plus an evidence workpaper (.xlsx) with the full drill-down trail."""

import io
from datetime import datetime, timezone

from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill
from openpyxl.utils import get_column_letter

from app import config

HEADER_FILL = PatternFill("solid", fgColor="1F3864")
HEADER_FONT = Font(color="FFFFFF", bold=True)
TITLE_FONT = Font(bold=True, size=14)


def _fmt_pct(pct):
    if pct is None:
        return "—"
    if pct == float("inf"):
        return "new"
    return f"{pct * 100:+.1f}%"


def _gate_sentence(variances) -> str:
    """How the materiality gate was configured for this run, naming only the
    checks that actually ran. compute_variances leaves z_score at 0.0 when an
    account has fewer than two trailing month-over-month deltas to compare
    against -- a two-period upload never clears that -- so claiming the
    anomaly check would describe a test that never happened."""
    text = (
        f"+/-{config.MATERIALITY_ABS:,.0f}, or +/-{config.MATERIALITY_PCT:.0%} on a move over "
        f"{config.MATERIALITY_FLOOR:,.0f}"
    )
    if any(abs(v.get("z_score") or 0) > 0 for v in variances or []):
        text += f", or a {config.ANOMALY_Z} sigma swing versus the account's own trailing history"
    return text


def _split_findings(state):
    """(material, informational). A quiet period still produces findings -- the
    largest movements, drilled anyway -- and counting those as material would
    quietly undo the gate they failed to clear."""
    findings = state.get("findings") or []
    material = [f for f in findings if not f.get("informational")]
    informational = [f for f in findings if f.get("informational")]
    return material, informational


def _movement_rows(variances, limit=10):
    """Largest absolute movements first. Used to give a period where nothing
    cleared the gate something to actually read: 'no material variances' plus
    an empty section is indistinguishable from a run that failed."""
    return sorted(variances or [], key=lambda v: abs(v["delta"]), reverse=True)[:limit]


def _write_header(sheet, headers, row=1):
    for col, name in enumerate(headers, start=1):
        cell = sheet.cell(row=row, column=col, value=name)
        cell.fill = HEADER_FILL
        cell.font = HEADER_FONT


def _autosize(sheet, max_width=60):
    for column in sheet.columns:
        width = max((len(str(c.value)) for c in column if c.value is not None), default=0)
        sheet.column_dimensions[get_column_letter(column[0].column)].width = min(width + 3, max_width)


def markdown(state) -> str:
    lines = [
        f"# Variance Explanation Brief: {state['prior_period']} -> {state['period']}",
        "",
        f"_Generated {datetime.now(timezone.utc).isoformat(timespec='seconds')} UTC · run {state.get('run_id')}_",
        "",
        "## Executive summary",
        "",
        state.get("brief_text", ""),
        "",
    ]
    plan = state.get("action_plan") or []
    if plan:
        lines += ["## Recommended actions", ""]
        lines.append("| Priority | Account | Task | Owner |")
        lines.append("|---|---|---|---|")
        for item in plan:
            lines.append(
                f"| {item['priority']} · {item['priority_label']} | {item['account_name']} "
                f"| {item['task']} | {item['owner']} |"
            )
        lines.append("")
    lines += [
        "## Material variances",
        "",
    ]
    material, informational = _split_findings(state)
    if not material:
        variances = state.get("variances") or []
        lines.append(
            f"No account cleared the materiality gate ({_gate_sentence(variances)}). "
            "That is a clean close, not a failed run. Every movement in the period is below."
        )
        lines.append("")
        rows = _movement_rows(variances)
        if rows:
            lines.append("| Account | Prior | Current | Delta | Pct |")
            lines.append("|---|---|---|---|---|")
            for v in rows:
                lines.append(
                    f"| {v['account_name']} ({v['account_code']}) | {v['prior_amt']:,.2f} "
                    f"| {v['current_amt']:,.2f} | {v['delta']:+,.2f} | {_fmt_pct(v['pct'])} |"
                )
            lines.append("")
    for f in material:
        lines.append(f"### {f['account_name']} ({f['account_code']})")
        lines.append("")
        lines.append(f"**{f['headline']}**")
        lines.append("")
        lines.append(f"Delta: ${f['delta']:+,.2f} ({_fmt_pct(f['pct'])}) · materiality: {f['materiality_reason']} · confidence: {f['confidence']}")
        lines.append("")
        lines.append(f["why"])
        lines.append("")
        if f.get("drivers"):
            lines.append("| Driver | Delta | Cohort | Share of account delta |")
            lines.append("|---|---|---|---|")
            for d in f["drivers"]:
                lines.append(
                    f"| {d['member_name']} | {d['delta']:+,.2f} | {d['cohort']} | {d['contribution_share'] * 100:.0f}% |"
                )
            lines.append("")
        if f.get("action"):
            lines.append(f"**Recommended action** ({f.get('priority', 'P2')} · {f.get('owner', 'FP&A')}): {f['action']}")
            lines.append("")
    if informational:
        lines.append(f"## Largest movements reviewed ({len(informational)}, below materiality)")
        lines.append("")
        lines.append(
            "Drilled for context only: none of these cleared the gate, all are P3, and none "
            "requires action this close."
        )
        lines.append("")
        for f in informational:
            lines.append(f"### {f['account_name']} ({f['account_code']})")
            lines.append("")
            lines.append(f"**{f['headline']}**")
            lines.append("")
            lines.append(f"Delta: ${f['delta']:+,.2f} ({_fmt_pct(f['pct'])}) · confidence: {f['confidence']}")
            lines.append("")
            lines.append(f["why"])
            lines.append("")
            if f.get("drivers"):
                lines.append("| Driver | Delta | Cohort | Share of account delta |")
                lines.append("|---|---|---|---|")
                for d in f["drivers"]:
                    lines.append(
                        f"| {d['member_name']} | {d['delta']:+,.2f} | {d['cohort']} "
                        f"| {d['contribution_share'] * 100:.0f}% |"
                    )
                lines.append("")

    tie_out = state.get("tie_out") or []
    gaps = [row for row in tie_out if row["coverage_pct"] < 99.9]
    if gaps:
        lines.append("## Subledger tie-out gaps")
        lines.append("")
        # State the denominator. One gap row on its own reads as if that account
        # were the whole report, when it is the one exception out of nine.
        subject = "account does" if len(gaps) == 1 else "accounts do"
        lines.append(
            f"{len(tie_out) - len(gaps)} of {len(tie_out)} accounts trace fully to subledger "
            f"detail; the following {subject} not."
        )
        lines.append("")
        lines.append("| Account | Summary | Subledger | Coverage |")
        lines.append("|---|---|---|---|")
        for row in gaps:
            lines.append(
                f"| {row['account_name']} | {row['summary_amt']:,.2f} | {row['txn_amt']:,.2f} | {row['coverage_pct']}% |"
            )
        lines.append("")
    return "\n".join(lines)


def _write_xlsx(state, path):
    wb = Workbook()
    summary = wb.active
    summary.title = "Summary"
    summary["A1"] = "Variance Explanation Workpaper"
    summary["A1"].font = TITLE_FONT
    rows = [
        ("Prepared by", "AI-native variance-explanation agent (LangGraph)"),
        ("Prepared at (UTC)", datetime.now(timezone.utc).isoformat(timespec="seconds")),
        ("Run ID", state.get("run_id")),
        ("Prior period", state.get("prior_period")),
        ("Current period", state.get("period")),
        ("Material findings", len(_split_findings(state)[0])),
        ("Reviewed below materiality", len(_split_findings(state)[1])),
    ]
    for offset, (label, value) in enumerate(rows, start=3):
        summary.cell(row=offset, column=1, value=label).font = Font(bold=True)
        summary.cell(row=offset, column=2, value=value)
    narrative_row = len(rows) + 5
    summary.cell(row=narrative_row, column=1, value="Executive summary").font = Font(bold=True)
    from openpyxl.styles import Alignment

    cell = summary.cell(row=narrative_row + 1, column=1, value=state.get("brief_text") or "")
    cell.alignment = Alignment(wrap_text=True, vertical="top")
    summary.merge_cells(start_row=narrative_row + 1, start_column=1, end_row=narrative_row + 8, end_column=6)
    _autosize(summary)

    actions_sheet = wb.create_sheet("Actions")
    _write_header(actions_sheet, ["Priority", "Account", "Task", "Owner", "Context"])
    for item in state.get("action_plan") or []:
        actions_sheet.append(
            [
                f"{item['priority']} · {item['priority_label']}",
                f"{item['account_name']} ({item['account_code']})",
                item["task"],
                item["owner"],
                item["context"],
            ]
        )
    _autosize(actions_sheet)

    findings_sheet = wb.create_sheet("Findings")
    _write_header(
        findings_sheet,
        ["Account", "Delta", "Pct", "Materiality", "Confidence", "Priority", "Owner", "Headline", "Why", "Action"],
    )
    for f in state.get("findings") or []:
        findings_sheet.append(
            [
                f"{f['account_name']} ({f['account_code']})",
                f["delta"],
                _fmt_pct(f["pct"]),
                f["materiality_reason"],
                f["confidence"],
                f.get("priority", ""),
                f.get("owner", ""),
                f["headline"],
                f["why"],
                f["action"],
            ]
        )
    _autosize(findings_sheet)

    drivers_sheet = wb.create_sheet("Drivers")
    _write_header(drivers_sheet, ["Account", "Driver", "Delta", "Cohort", "Share of account delta"])
    for f in state.get("findings") or []:
        for d in f.get("drivers") or []:
            drivers_sheet.append(
                [
                    f"{f['account_name']} ({f['account_code']})",
                    d["member_name"],
                    d["delta"],
                    d["cohort"],
                    d["contribution_share"],
                ]
            )
    _autosize(drivers_sheet)

    tie_sheet = wb.create_sheet("Tie-Out")
    _write_header(tie_sheet, ["Account", "Summary amount", "Subledger amount", "Gap", "Coverage %"])
    for row in state.get("tie_out") or []:
        tie_sheet.append(
            [row["account_name"], row["summary_amt"], row["txn_amt"], row["gap"], row["coverage_pct"]]
        )
    _autosize(tie_sheet)

    wb.save(path)


def workbook_bytes(state) -> bytes:
    """The same .xlsx `build()` writes, as bytes.

    For a caller with no directory to write to -- an isolated upload run
    streams the workbook straight to the browser instead of landing it in
    out/flux/. openpyxl's Workbook.save() accepts any file-like object.
    """
    buffer = io.BytesIO()
    _write_xlsx(state, buffer)
    return buffer.getvalue()


def analysis_text(state) -> str:
    """Plain-text rendering of just the LLM's what-changed/why analysis --
    no action items, no driver tables. Meant to be readable on its own."""
    lines = [
        f"VARIANCE ANALYSIS: {state['prior_period']} -> {state['period']}",
        f"Generated {datetime.now(timezone.utc).isoformat(timespec='seconds')} UTC | run {state.get('run_id')}",
        "",
        "EXECUTIVE SUMMARY",
        "=================",
        state.get("brief_text", "").strip() or "No material variances this period.",
        "",
    ]
    material, informational = _split_findings(state)
    if not material:
        variances = state.get("variances") or []
        lines.append("ALL MOVEMENTS (none cleared the materiality gate)")
        lines.append("=================================================")
        lines.append(f"Gate: {_gate_sentence(variances)}.")
        lines.append("")
        for v in _movement_rows(variances):
            lines.append(
                f"{v['account_name']} ({v['account_code']}): {v['prior_amt']:,.2f} -> "
                f"{v['current_amt']:,.2f} | {v['delta']:+,.2f} ({_fmt_pct(v['pct'])})"
            )
        lines.append("")
    for section, group in (("PER-ACCOUNT ANALYSIS", material),
                           ("LARGEST MOVEMENTS REVIEWED (below materiality, P3)", informational)):
        if not group:
            continue
        lines.append(section)
        lines.append("=" * len(section))
        lines.append("")
        for f in group:
            lines.append(f"{f['account_name']} ({f['account_code']})")
            lines.append("-" * len(f"{f['account_name']} ({f['account_code']})"))
            lines.append(f["headline"])
            lines.append(
                f"Delta: ${f['delta']:+,.2f} ({_fmt_pct(f['pct'])}) | "
                f"materiality: {f['materiality_reason']} | confidence: {f['confidence']}"
            )
            lines.append("")
            lines.append(f["why"])
            lines.append("")
    return "\n".join(lines)


def actions_text(state) -> str:
    """Plain-text rendering of just the next-task output -- what to do about
    the analysis above, one prioritized, owned item at a time."""
    plan = state.get("action_plan") or []
    lines = [
        f"RECOMMENDED ACTIONS: {state['prior_period']} -> {state['period']}",
        f"Generated {datetime.now(timezone.utc).isoformat(timespec='seconds')} UTC | run {state.get('run_id')}",
        "",
    ]
    if not plan:
        lines.append("No actions generated this period.")
        return "\n".join(lines)
    for i, item in enumerate(plan, start=1):
        lines.append(f"{i}. [{item['priority']} - {item['priority_label']}] {item['account_name']}")
        lines.append(f"   Owner: {item['owner']}")
        lines.append(f"   Task:  {item['task']}")
        lines.append("")
    return "\n".join(lines)


def build(state) -> dict:
    config.FLUX_OUT_DIR.mkdir(parents=True, exist_ok=True)
    stem = f"flux_{state['prior_period']}_to_{state['period']}_{state.get('run_id')}"
    md_path = config.FLUX_OUT_DIR / f"{stem}.md"
    xlsx_path = config.FLUX_OUT_DIR / f"{stem}.xlsx"
    analysis_path = config.FLUX_OUT_DIR / f"{stem}_analysis.txt"
    actions_path = config.FLUX_OUT_DIR / f"{stem}_actions.txt"

    md_path.write_text(markdown(state), encoding="utf-8")
    _write_xlsx(state, xlsx_path)
    analysis_path.write_text(analysis_text(state), encoding="utf-8")
    actions_path.write_text(actions_text(state), encoding="utf-8")

    return {
        "brief_path": str(md_path),
        "workpaper_path": str(xlsx_path),
        "analysis_path": str(analysis_path),
        "actions_path": str(actions_path),
    }
