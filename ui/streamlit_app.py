"""Human-in-the-loop UI: the AP approval inbox, plus the flux variance-brief viewer."""

import json
import sqlite3
import sys
import uuid
from pathlib import Path

import pandas as pd
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import config, store  # noqa: E402
from app import graph as ap_graph  # noqa: E402
from app.flux import memory as flux_memory  # noqa: E402
from app.flux import uploads as flux_uploads  # noqa: E402
from app.flux import brief as flux_brief  # noqa: E402

SEVERITY_COLOR = {
    "critical": "#c0392b",
    "high": "#d35400",
    "medium": "#b7950b",
    "low": "#5d8a3a",
}
RECOMMENDATION_COLOR = {"hold": "#c0392b", "review": "#b7950b", "approve": "#2e7d32"}

st.set_page_config(page_title="AP Approval Inbox", page_icon="", layout="wide")


@st.cache_resource
def get_conn():
    return store.connect()


@st.cache_resource
def get_flux_conn():
    return flux_memory.connect()


def money(value, currency="USD"):
    return f"{value:,.2f} {currency}" if isinstance(value, (int, float)) else "—"


def amount(value):
    return f"{value:,.2f}" if isinstance(value, (int, float)) else "—"


def build_match_table(extracted, vendor, po, bank):
    """Attribute-by-attribute diff across invoice, ERP purchase order, and bank feed."""
    total = extracted.get("total_amount")
    po_amount = po["amount"] if po else None
    within_tolerance = (
        po_amount is not None
        and isinstance(total, (int, float))
        and abs(total - po_amount)
        <= max(po_amount * config.AMOUNT_TOLERANCE_PCT, config.AMOUNT_TOLERANCE_ABS)
    )
    rows = [
        {
            "Attribute": "Counterparty",
            "Invoice": extracted.get("vendor_name"),
            "Purchase order": (vendor or {}).get("name") or "—",
            "Bank feed": (bank or {}).get("counterparty") or "—",
            "Match": "OK" if vendor else "MISMATCH",
        },
        {
            "Attribute": "Reference",
            "Invoice": extracted.get("invoice_number"),
            "Purchase order": (po or {}).get("po_number") or "NOT FOUND",
            "Bank feed": (bank or {}).get("reference") or "—",
            "Match": "OK" if po else "MISMATCH",
        },
        {
            "Attribute": "Amount",
            "Invoice": money(total, extracted.get("currency", "USD")),
            "Purchase order": money(po_amount) if po_amount is not None else "—",
            "Bank feed": money(bank["amount"]) if bank else "—",
            "Match": "OK" if within_tolerance else "MISMATCH",
        },
        {
            "Attribute": "Date",
            "Invoice": extracted.get("invoice_date"),
            "Purchase order": (po or {}).get("issued_date") or "—",
            "Bank feed": (bank or {}).get("posted_date") or "—",
            "Match": "",
        },
        {
            "Attribute": "Status",
            "Invoice": "received",
            "Purchase order": (po or {}).get("status") or "—",
            "Bank feed": "settled" if bank else "no payment found",
            "Match": "",
        },
    ]
    return pd.DataFrame(rows)


def render_detail(conn, invoice):
    extracted = json.loads(invoice["extracted_json"] or "{}")
    exceptions = store.get_exceptions(conn, invoice["id"])
    po = store.find_po(conn, invoice["po_number"])
    bank = store.find_bank_payment(conn, invoice["invoice_number"], invoice["amount"])
    vendor = store.find_vendor(conn, invoice["vendor_name"])

    rec = (invoice["recommendation"] or "").lower()
    st.markdown(
        f"### {invoice['invoice_number']} — {invoice['vendor_name']}  "
        f"<span style='background:{RECOMMENDATION_COLOR.get(rec, '#555')};color:white;"
        f"padding:3px 10px;border-radius:10px;font-size:0.6em;'>AGENT: {rec.upper()}</span>",
        unsafe_allow_html=True,
    )
    st.caption(f"Status: {invoice['status']} · run {invoice['run_id']} · source {invoice['source_file']}")

    st.markdown("#### Three-way match")
    st.dataframe(
        build_match_table(extracted, vendor, po, bank),
        use_container_width=True,
        hide_index=True,
    )

    variance = None
    if po and isinstance(extracted.get("total_amount"), (int, float)):
        variance = extracted["total_amount"] - po["amount"]
    cols = st.columns(3)
    cols[0].metric("Invoice total", amount(invoice["amount"]))
    cols[1].metric("PO amount", amount(po["amount"]) if po else "—")
    cols[2].metric("Variance", amount(variance) if variance is not None else "—",
                   delta=f"{variance:,.2f}" if variance else None, delta_color="inverse")

    st.markdown("#### Control exceptions")
    if exceptions:
        for exc in exceptions:
            color = SEVERITY_COLOR.get(exc["severity"], "#555")
            st.markdown(
                f"<div style='border-left:4px solid {color};padding:6px 12px;margin-bottom:6px;'>"
                f"<b style='color:{color};'>{exc['severity'].upper()}</b> · <code>{exc['code']}</code><br>{exc['detail']}</div>",
                unsafe_allow_html=True,
            )
    else:
        st.success("No control exceptions identified.")

    st.markdown("#### Auditor narrative")
    st.info(invoice["narrative"] or "—")

    workpaper_path = Path(invoice["workpaper_path"] or "")
    if workpaper_path.exists():
        st.download_button(
            "Download audit workpaper (.xlsx)",
            data=workpaper_path.read_bytes(),
            file_name=workpaper_path.name,
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )

    if invoice["status"] == "pending_review":
        st.markdown("#### Decision")
        note = st.text_input("Reviewer note", key=f"note_{invoice['id']}")
        approve, reject = st.columns(2)
        if approve.button("Approve for payment", key=f"ok_{invoice['id']}", type="primary"):
            store.record_decision(conn, invoice["id"], "approved", "reviewer", note)
            st.rerun()
        if reject.button("Reject / send back", key=f"no_{invoice['id']}"):
            store.record_decision(conn, invoice["id"], "rejected", "reviewer", note)
            st.rerun()
    else:
        st.caption(f"Decision recorded: {invoice['status']}")


def _handle_invoice_upload():
    uploaded = st.file_uploader(
        "Invoice document (.txt or .pdf)", type=["txt", "pdf"], key="ap_upload"
    )
    if uploaded is None:
        return
    if not st.button("Run through the reconciliation agent", key="ap_process_upload", type="primary"):
        return

    config.UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
    dest = config.UPLOADS_DIR / f"{uuid.uuid4().hex[:8]}_{uploaded.name}"
    dest.write_bytes(uploaded.getvalue())

    with st.spinner(f"Extracting, matching, and evaluating controls for {uploaded.name}..."):
        try:
            result = ap_graph.process_invoice(dest)
        except Exception as exc:  # noqa: BLE001 -- surface any failure to the uploader, don't crash the page
            st.error(f"Processing failed: {exc}")
            return

    rec = (result.get("recommendation") or "").upper()
    st.success(
        f"Processed {uploaded.name} as invoice {result.get('extracted', {}).get('invoice_number', '?')} "
        f"— agent recommendation: **{rec}**. It's now in the queue below."
    )
    st.rerun()


def ap_inbox():
    conn = get_conn()
    st.title("AP Approval Inbox")
    st.caption("Agent-reconciled invoices awaiting human approval — AI-native finance team")

    if not config.DB_PATH.exists():
        st.error("No ledger found. Run `python data/seed.py` then `python run_demo.py`.")
        return

    with st.expander("Upload a new invoice", expanded=False):
        st.caption(
            "Runs the same LangGraph pipeline as `run_demo.py` — extraction, three-way match, "
            "AP controls, workpaper — live, on this one document."
        )
        _handle_invoice_upload()

    status = st.sidebar.selectbox(
        "Queue", ["pending_review", "approved", "rejected", "all"], index=0
    )
    invoices = store.list_invoices(conn, None if status == "all" else status)
    st.sidebar.metric("In queue", len(invoices))

    if not invoices:
        st.info(f"Nothing in '{status}'.")
        return

    labels = {
        f"{inv['invoice_number']} · {inv['vendor_name']} · {money(inv['amount'])} · {(inv['recommendation'] or '').upper()}": inv
        for inv in invoices
    }
    choice = st.sidebar.radio("Invoices", list(labels.keys()))
    render_detail(conn, labels[choice])


def _flux_briefs():
    """Latest brief per (prior, current) period pair -- re-running the same
    comparison (e.g. while iterating on the agent) leaves older files behind,
    and those should never shadow the current one in the dropdown."""
    briefs_dir = config.FLUX_OUT_DIR
    if not briefs_dir.exists() or not list(briefs_dir.glob("*.md")):
        return []
    latest = {}
    for md_path in briefs_dir.glob("*.md"):
        # filename: flux_<prior>_to_<current>_<run_id>.md
        stem = md_path.stem
        parts = stem.split("_")
        prior_period, current_period = parts[1], parts[3]
        xlsx_path = md_path.with_suffix(".xlsx")
        analysis_path = md_path.with_name(f"{stem}_analysis.txt")
        actions_path = md_path.with_name(f"{stem}_actions.txt")
        entry = {
            "prior_period": prior_period,
            "current_period": current_period,
            "md_path": md_path,
            "xlsx_path": xlsx_path if xlsx_path.exists() else None,
            "analysis_path": analysis_path if analysis_path.exists() else None,
            "actions_path": actions_path if actions_path.exists() else None,
            "mtime": md_path.stat().st_mtime,
        }
        key = (prior_period, current_period)
        if key not in latest or entry["mtime"] > latest[key]["mtime"]:
            latest[key] = entry
    briefs = sorted(latest.values(), key=lambda b: (b["current_period"], b["mtime"]), reverse=True)
    return briefs


def _flux_recurring_drivers(limit=10):
    """Drivers the memory graph has seen fire in more than one period — the
    concrete evidence that the agent's intuition compounds across runs rather
    than resetting on every invocation."""
    if not config.FLUX_GRAPH_PATH.exists():
        return pd.DataFrame()
    graph = json.loads(config.FLUX_GRAPH_PATH.read_text(encoding="utf-8"))
    rows = []
    for edge in graph.get("edges", {}).values():
        periods = edge.get("periods", {})
        if len(periods) < 2:
            continue
        latest = max(periods)
        rows.append(
            {
                "Account": edge["account_code"],
                "Driver": edge.get("driver_name") or edge["driver_key"],
                "Periods seen": len(periods),
                "Latest period": latest,
                "Latest share": f"{periods[latest]['share'] * 100:.0f}%",
                "Analyst verdict": edge.get("last_verdict") or "—",
            }
        )
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows).sort_values("Periods seen", ascending=False)
    return df.head(limit)


def _flux_feedback_section(brief):
    """Analyst feedback control for the currently-displayed period comparison.

    Confirm / off-base a specific finding by its real SQLite id
    (`memory.findings_for_run`) -> `memory.record_feedback` -> stamps
    `last_verdict`/`last_note` on that account's graph edges -> already picked
    up by `memory.recall()` in the next `run_flux.py` invocation. This is the
    write side of institutional memory; everything else on this page is the
    read side.
    """
    conn = get_flux_conn()
    findings = flux_memory.findings_for_run(conn, brief["current_period"], brief["prior_period"])
    if not findings:
        return
    with st.expander(f"Analyst feedback ({len(findings)} findings)", expanded=False):
        st.caption(
            "Confirm a finding or flag it as off-base. This updates the memory graph's "
            "`last_verdict`/`last_note` for the account and is folded into the narrative "
            "prompt next time this account shows up — it does not change this run's "
            "priority or owner assignment."
        )
        for finding in findings:
            finding_id = finding["id"]
            existing = conn.execute(
                "SELECT verdict, note FROM feedback WHERE finding_id = ? ORDER BY id DESC LIMIT 1",
                (finding_id,),
            ).fetchone()
            st.markdown(f"**{finding['account_name']}** — {finding['headline']}")
            if existing:
                note_suffix = f' — "{existing["note"]}"' if existing["note"] else ""
                st.caption(f"You marked this: **{existing['verdict']}**{note_suffix}")
            else:
                cols = st.columns([3, 1, 1])
                note = cols[0].text_input(
                    "Note",
                    key=f"flux_note_{finding_id}",
                    label_visibility="collapsed",
                    placeholder="Optional note",
                )
                if cols[1].button("Confirm", key=f"flux_confirm_{finding_id}"):
                    graph = flux_memory.load_graph()
                    flux_memory.record_feedback(conn, graph, finding_id, "confirmed", note)
                    st.rerun()
                if cols[2].button("Off-base", key=f"flux_offbase_{finding_id}"):
                    graph = flux_memory.load_graph()
                    flux_memory.record_feedback(conn, graph, finding_id, "off-base", note)
                    st.rerun()
            st.divider()


_SAMPLE_UPLOADS = (
    ("Sample summary (.csv)", "upload_sample_summary.csv", "flux_sample_summary"),
    ("Sample transactions (.csv)", "upload_sample_transactions.csv", "flux_sample_txn"),
)


def _offer_sample_uploads():
    """Download buttons for a known-good pair, so the panel is demoable without
    hunting for correctly-shaped data first.

    These are 2026-07 + 2026-08 of the seeded ledger concatenated into one file
    each -- the same comparison the committed example brief covers.
    """
    root = Path(__file__).resolve().parent.parent / "examples"
    available = [(label, root / name, key) for label, name, key in _SAMPLE_UPLOADS if (root / name).exists()]
    if len(available) != len(_SAMPLE_UPLOADS):
        return
    st.caption("No data handy? Download this pair and upload them straight back:")
    cols = st.columns(len(available))
    for col, (label, path, key) in zip(cols, available):
        col.download_button(
            label,
            data=path.read_bytes(),
            file_name=path.name,
            mime="text/csv",
            key=key,
        )


def _handle_flux_upload():
    st.caption(
        "Two files: a period summary (`period, account_code, account_name, amount`) and a "
        "transaction detail file (`period, account_code, amount`, plus `customer_id` or "
        "`vendor`). **Each file needs at least two `YYYY-MM` periods in it** -- the latest two "
        "present are compared. `resolve_roles` sniffs which file is which by its columns, so "
        "File 1 / File 2 order genuinely doesn't matter."
    )

    # The seeded dataset under data/financials/ is deliberately one period per
    # file, so those files are NOT valid uploads on their own -- a single-period
    # summary is rejected up front. Hand the user a working pair instead of
    # pointing them at files that will bounce.
    _offer_sample_uploads()

    file_a = st.file_uploader("File 1", type=["csv", "xlsx"], key="flux_upload_a")
    file_b = st.file_uploader("File 2", type=["csv", "xlsx"], key="flux_upload_b")
    if file_a is None or file_b is None:
        return
    if not st.button("Run the variance agent on these files", key="flux_run_upload", type="primary"):
        return

    config.UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
    dest_a = config.UPLOADS_DIR / f"{uuid.uuid4().hex[:8]}_{file_a.name}"
    dest_a.write_bytes(file_a.getvalue())
    dest_b = config.UPLOADS_DIR / f"{uuid.uuid4().hex[:8]}_{file_b.name}"
    dest_b.write_bytes(file_b.getvalue())

    with st.spinner("Comparing periods, slicing drivers, and drafting the action plan..."):
        try:
            state, prepared = flux_uploads.run(dest_a, dest_b)
        except flux_uploads.UploadError as exc:
            st.error(str(exc))
            return
        except Exception as exc:  # noqa: BLE001 -- surface any failure to the uploader, don't crash the page
            st.error(f"Upload run failed: {exc}")
            return

    # Render once, here, and stash everything the render needs in session state.
    # st.download_button triggers a rerun when clicked; a result held only in a
    # local inside this `if st.button(...)` block would vanish on the first
    # download click, mid-demo. No st.rerun() here -- the block below picks up
    # this session key in the same pass, and a rerun would throw away the frame
    # we just computed (unlike the AP handler, which reruns to re-read SQLite).
    st.session_state["flux_upload"] = {
        "state": state,
        "prior_period": prepared.prior_period,
        "period": prepared.period,
        "warnings": prepared.warnings,
        "summary_rows": prepared.summary_rows,
        "txn_rows": prepared.txn_rows,
        "names": (file_a.name, file_b.name),
        "md": flux_brief.markdown(state),
        "analysis": flux_brief.analysis_text(state),
        "actions": flux_brief.actions_text(state),
        "xlsx": flux_brief.workbook_bytes(state),
        "stem": f"flux_{prepared.prior_period}_to_{prepared.period}_{state.get('run_id')}",
    }


def _render_upload_result(entry):
    state = entry["state"]
    prior, period = entry["prior_period"], entry["period"]
    name_a, name_b = entry["names"]

    st.success(f"Compared {prior} → {period} from {name_a} + {name_b}.")
    st.caption("Isolated run — nothing was written to the agent's institutional memory or to out/flux/.")

    for warning in entry.get("warnings") or []:
        st.warning(warning)

    metric_cols = st.columns(3)
    metric_cols[0].metric("Accounts analyzed", len(state.get("variances", [])))
    metric_cols[1].metric("Material findings", len(state.get("findings", [])))
    metric_cols[2].metric("Actions", len(state.get("action_plan", [])))

    if not state.get("findings"):
        st.info(
            f"No account moved enough between {prior} and {period} to clear the materiality gate — "
            f"±${config.MATERIALITY_ABS:,.0f}, or ±{config.MATERIALITY_PCT:.0%} on a balance over "
            f"${config.MATERIALITY_FLOOR:,.0f}, or a {config.ANOMALY_Z}σ swing versus the account's own "
            f"trailing history. That's a clean close, not a failed run."
        )
        variances = state.get("variances") or []
        if variances:
            top = sorted(variances, key=lambda v: abs(v["delta"]), reverse=True)[:5]
            rows = [
                {
                    "Account": v["account_name"],
                    "Delta": v["delta"],
                    "Pct": "new" if v["pct"] == float("inf") else f"{v['pct'] * 100:+.1f}%",
                }
                for v in top
            ]
            st.caption("Largest movements below the threshold")
            st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)

    col_analysis, col_actions = st.columns(2)
    with col_analysis:
        st.subheader("Analysis — what changed, why")
        st.text(entry["analysis"])
    with col_actions:
        st.subheader("Recommended actions — what to do next")
        st.text(entry["actions"])

    stem = entry["stem"]
    dl_cols = st.columns(4)
    dl_cols[0].download_button(
        "Brief (.md)",
        data=entry["md"],
        file_name=f"{stem}.md",
        mime="text/markdown",
        key="flux_dl_md",
    )
    dl_cols[1].download_button(
        "Workpaper (.xlsx)",
        data=entry["xlsx"],
        file_name=f"{stem}.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        key="flux_dl_xlsx",
    )
    dl_cols[2].download_button(
        "Analysis (.txt)",
        data=entry["analysis"],
        file_name=f"{stem}_analysis.txt",
        mime="text/plain",
        key="flux_dl_analysis",
    )
    dl_cols[3].download_button(
        "Actions (.txt)",
        data=entry["actions"],
        file_name=f"{stem}_actions.txt",
        mime="text/plain",
        key="flux_dl_actions",
    )

    with st.expander("Full brief (driver tables, tie-out gaps)"):
        st.markdown(entry["md"])

    if st.button("Clear result", key="flux_clear_upload"):
        st.session_state.pop("flux_upload", None)
        st.rerun()


def flux_page():
    st.title("Variance Explanation Agent")
    st.caption("What changed, why, and what's driving it — with intuition that compounds across runs")

    briefs = _flux_briefs()

    with st.expander("Analyze your own financials (upload two CSVs)", expanded=not briefs):
        _handle_flux_upload()

    if "flux_upload" in st.session_state:
        _render_upload_result(st.session_state["flux_upload"])
        st.divider()

    if not briefs:
        st.info(
            "No pre-computed briefs yet. Upload two CSVs above, or run "
            "`python data/seed_flux.py` then `python run_flux.py --replay`."
        )
        return

    labels = {f"{b['prior_period']} -> {b['current_period']}": b for b in briefs}
    choice = st.sidebar.selectbox("Period comparison", list(labels.keys()))
    brief = labels[choice]
    st.sidebar.caption("Institutional memory persists across every run in this list.")

    recurring = _flux_recurring_drivers()
    if not recurring.empty:
        with st.sidebar.expander(f"Recurring drivers ({len(recurring)})", expanded=False):
            st.dataframe(recurring, hide_index=True, use_container_width=True)

    # Both model outputs, front and center -- the whole point of this page.
    col_analysis, col_actions = st.columns(2)
    with col_analysis:
        st.subheader("Analysis — what changed, why")
        if brief["analysis_path"]:
            st.text(brief["analysis_path"].read_text(encoding="utf-8"))
        else:
            st.info("No analysis output for this run.")
    with col_actions:
        st.subheader("Recommended actions — what to do next")
        if brief["actions_path"]:
            st.text(brief["actions_path"].read_text(encoding="utf-8"))
        else:
            st.info("No action-plan output for this run.")

    dl_cols = st.columns(3)
    if brief["xlsx_path"]:
        dl_cols[0].download_button(
            "Workpaper (.xlsx)",
            data=brief["xlsx_path"].read_bytes(),
            file_name=brief["xlsx_path"].name,
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
    if brief["analysis_path"]:
        dl_cols[1].download_button(
            "Analysis (.txt)",
            data=brief["analysis_path"].read_bytes(),
            file_name=brief["analysis_path"].name,
            mime="text/plain",
        )
    if brief["actions_path"]:
        dl_cols[2].download_button(
            "Actions (.txt)",
            data=brief["actions_path"].read_bytes(),
            file_name=brief["actions_path"].name,
            mime="text/plain",
        )

    with st.expander("Full brief (driver tables, tie-out gaps)", expanded=False):
        st.markdown(brief["md_path"].read_text(encoding="utf-8"))

    if config.FLUX_DB_PATH.exists():
        _flux_feedback_section(brief)

        with st.expander("Run history (institutional memory, SQLite)"):
            hist_conn = sqlite3.connect(config.FLUX_DB_PATH)
            hist_conn.row_factory = sqlite3.Row
            rows = hist_conn.execute(
                "SELECT period, account_name, headline, confidence, recurring_count "
                "FROM findings ORDER BY id DESC LIMIT 25"
            ).fetchall()
            hist_conn.close()
            if rows:
                st.dataframe(pd.DataFrame([dict(r) for r in rows]), hide_index=True, use_container_width=True)


def main():
    page = st.sidebar.radio("View", ["Variance Explanation Agent", "AP Approval Inbox"])
    st.sidebar.divider()
    if page == "AP Approval Inbox":
        ap_inbox()
    else:
        flux_page()


main()
