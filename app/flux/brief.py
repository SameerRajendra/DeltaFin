"""Render the variance-explanation agent's output: a markdown executive brief
plus an evidence workpaper (.xlsx) with the full drill-down trail."""

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


def _write_header(sheet, headers, row=1):
    for col, name in enumerate(headers, start=1):
        cell = sheet.cell(row=row, column=col, value=name)
        cell.fill = HEADER_FILL
        cell.font = HEADER_FONT


def _autosize(sheet, max_width=60):
    for column in sheet.columns:
        width = max((len(str(c.value)) for c in column if c.value is not None), default=0)
        sheet.column_dimensions[get_column_letter(column[0].column)].width = min(width + 3, max_width)


def _markdown(state) -> str:
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
    findings = state.get("findings") or []
    if not findings:
        lines.append("No material variances this period.")
    for f in findings:
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
    tie_out = state.get("tie_out") or []
    gaps = [row for row in tie_out if row["coverage_pct"] < 99.9]
    if gaps:
        lines.append("## Subledger tie-out gaps")
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
        ("Material findings", len(state.get("findings") or [])),
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


def build(state) -> dict:
    config.FLUX_OUT_DIR.mkdir(parents=True, exist_ok=True)
    stem = f"flux_{state['prior_period']}_to_{state['period']}_{state.get('run_id')}"
    md_path = config.FLUX_OUT_DIR / f"{stem}.md"
    xlsx_path = config.FLUX_OUT_DIR / f"{stem}.xlsx"

    md_path.write_text(_markdown(state), encoding="utf-8")
    _write_xlsx(state, xlsx_path)

    return {"brief_path": str(md_path), "workpaper_path": str(xlsx_path)}
