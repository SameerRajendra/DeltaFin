"""Human-in-the-loop UI: the AP approval inbox, plus the flux variance-brief viewer."""

import base64
import json
import re
import sqlite3
import sys
import uuid
from pathlib import Path

import pandas as pd
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import config, store  # noqa: E402
from app import controls as ap_controls  # noqa: E402
from app import graph as ap_graph  # noqa: E402
from app.flux import memory as flux_memory  # noqa: E402
from app.flux import uploads as flux_uploads  # noqa: E402
from app.flux import brief as flux_brief  # noqa: E402
from app.flux import variance as flux_variance  # noqa: E402
from ui import charts  # noqa: E402

# Colors (severity, recommendation, match) live in ui/charts.py — single home
# for the app's palette, see SEVERITY_COLORS / RECOMMENDATION_COLORS / MATCH_COLORS.

st.set_page_config(page_title="DeltaFin — variance & AP agents", page_icon="📊", layout="wide")


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


def _download_row(items):
    """One row of download buttons. `items` is a list of
    (label, data, file_name, mime, key) tuples; entries whose `data` is None
    are dropped BEFORE the columns are laid out, so a missing artifact costs
    a button rather than leaving an empty column in a fixed-index grid."""
    available = [item for item in items if item[1] is not None]
    if not available:
        return
    cols = st.columns(len(available))
    for col, (label, data, file_name, mime, key) in zip(cols, available):
        col.download_button(label, data=data, file_name=file_name, mime=mime, key=key)


def _gate_sentence(anomaly_ran):
    """The single canonical statement of the materiality gate for the whole UI.

    `variance.compute_variances` leaves `z_score` at 0.0 unless it found at
    least two month-over-month deltas strictly before the prior period (four
    periods of history), so the sigma clause is only named when the check
    actually ran -- naming it on a two-period upload would describe a test
    that never ran.
    """
    # Dollar signs are escaped because every surface this lands on -- st.caption,
    # st.info -- renders markdown, where a matched pair of `$` opens and closes a
    # LaTeX math span. Unescaped, "±$25,000 ... over $5,000" swallowed everything
    # between the two amounts into a math block on screen.
    sentence = (
        f"±\\${config.MATERIALITY_ABS:,.0f}, or ±{config.MATERIALITY_PCT:.0%} on a move over "
        f"\\${config.MATERIALITY_FLOOR:,.0f}"
    )
    if anomaly_ran:
        sentence += f", or a {config.ANOMALY_Z}σ swing versus the account's own trailing history"
    return sentence


def _anomaly_caveat(anomaly_ran):
    """Companion note to `_gate_sentence`: empty when the sigma check ran,
    otherwise explains why it didn't (needs four periods of history; a
    two-period upload only carries two) and how to switch it on."""
    if anomaly_ran:
        return ""
    return (
        f" The {config.ANOMALY_Z}σ anomaly check did not run: it needs at least four periods of "
        "history, and this upload carries two. Upload more periods to switch it on."
    )


def _md(text):
    """Escape `$` so Streamlit's markdown doesn't eat currency as LaTeX math.

    Every free-text string the pipeline produces -- headlines, narratives,
    action tasks -- quotes amounts as `$27,550.00`. Streamlit renders markdown,
    where a second `$` closes the math span the first one opened, so a sentence
    naming two amounts silently lost both dollar signs and typeset everything
    between them. Apply this to pipeline text on its way to any markdown
    surface (`st.markdown`, `st.caption`, `st.info`, an expander label);
    `st.text` renders literally and needs no escaping.
    """
    return text.replace("$", "\\$") if isinstance(text, str) else text


def _section(title, caption=None):
    """Identical heading spacing for every section in the app, so nobody
    re-invents the double-heading bug."""
    st.subheader(title)
    if caption:
        st.caption(caption)


def _po_tolerance(po_amount):
    """Symmetric tolerance band around a PO amount -- the single place this is
    computed.

    Returns `None` when there's no PO amount to band around.
    """
    if po_amount is None:
        return None
    return max(po_amount * config.AMOUNT_TOLERANCE_PCT, config.AMOUNT_TOLERANCE_ABS)


def _po_match(total, po_amount):
    """`(tolerance, within_tolerance, verdict)` for one invoice against its PO.

    Every surface that judges the invoice amount -- the match table's cell, the
    match grid, and the invoice-vs-PO chart -- reads this one function, so they
    cannot drift into disagreeing on screen.

    `verdict` distinguishes the two directions rather than collapsing both into
    "MISMATCH". `controls.py` only raises `amount_over_po` when the invoice is
    OVER its PO, so an invoice materially under used to render a bare MISMATCH
    with no exception beside it, sending the reviewer looking for a control
    failure that was never raised. Naming the direction makes the table agree
    with the exception list: OVER is the exception, UNDER is a variance the
    controls deliberately don't flag.
    """
    tolerance = _po_tolerance(po_amount)
    if po_amount is None or not isinstance(total, (int, float)) or tolerance is None:
        return tolerance, False, "NOT FOUND"
    difference = total - po_amount
    if abs(difference) <= tolerance:
        return tolerance, True, "OK"
    return tolerance, False, "OVER" if difference > 0 else "UNDER"


def _match_rows(attribute, invoice_val, po_val, bank_val, ok):
    """One row per source (Invoice / Purchase order / Bank feed) for a single
    three-way-match attribute, shaped for `charts.match_status_chart`.

    A source with no value for this attribute is "not available"; a source
    that does carry a value gets the row's overall match verdict `ok` (the
    same OK/MISMATCH boolean `build_match_table` computes for that
    attribute) -- this does not re-derive its own per-source comparison.
    """
    rows = []
    for source, value in (("Invoice", invoice_val), ("Purchase order", po_val), ("Bank feed", bank_val)):
        status = "not available" if value in (None, "") else ("match" if ok else "mismatch")
        rows.append({"attribute": attribute, "source": source, "status": status})
    return rows


def build_match_table(extracted, vendor, po, bank):
    """Attribute-by-attribute diff across invoice, ERP purchase order, and bank feed."""
    total = extracted.get("total_amount")
    po_amount = po["amount"] if po else None
    _, _, amount_verdict = _po_match(total, po_amount)
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
            "Match": amount_verdict,
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

    total = extracted.get("total_amount")
    po_amount = po["amount"] if po else None
    tolerance, within_tolerance, amount_verdict = _po_match(total, po_amount)

    rec = (invoice["recommendation"] or "").lower()
    st.markdown(
        f"### {invoice['invoice_number']} — {invoice['vendor_name']}  "
        f"<span style='background:{charts.RECOMMENDATION_COLORS.get(rec, '#555')};color:white;"
        f"padding:3px 10px;border-radius:10px;font-size:0.6em;'>AGENT: {rec.upper()}</span>",
        unsafe_allow_html=True,
    )
    st.caption(f"{invoice['status']} · run {invoice['run_id']}")

    left, right = st.columns([3, 2])

    with left:
        tab_match, tab_exceptions, tab_narrative = st.tabs(["Match", "Exceptions", "Narrative"])

        with tab_match:
            match_rows = (
                _match_rows(
                    "Counterparty",
                    extracted.get("vendor_name"),
                    (vendor or {}).get("name"),
                    (bank or {}).get("counterparty"),
                    bool(vendor),
                )
                + _match_rows(
                    "Reference",
                    extracted.get("invoice_number"),
                    (po or {}).get("po_number"),
                    (bank or {}).get("reference"),
                    bool(po),
                )
                + _match_rows("Amount", total, po_amount, bank["amount"] if bank else None, within_tolerance)
            )
            match_chart = charts.match_status_chart(
                pd.DataFrame(match_rows), attribute_order=["Counterparty", "Reference", "Amount"]
            )
            if match_chart is not None:
                st.altair_chart(match_chart, use_container_width=True)

            po_chart = charts.invoice_vs_po_chart(
                total, po_amount, tolerance, "within tolerance" if within_tolerance else "outside tolerance"
            )
            if po_chart is not None:
                st.altair_chart(po_chart, use_container_width=True)

            with st.expander("Attribute-by-attribute detail"):
                st.dataframe(
                    build_match_table(extracted, vendor, po, bank),
                    use_container_width=True,
                    hide_index=True,
                )

        with tab_exceptions:
            if exceptions:
                exc_chart = charts.exception_severity_chart(
                    pd.DataFrame(
                        [
                            {
                                "code": exc["code"],
                                "severity": exc["severity"],
                                "rank": ap_controls.SEVERITY_RANK.get(exc["severity"], 0),
                                "detail": exc["detail"],
                            }
                            for exc in exceptions
                        ]
                    )
                )
                if exc_chart is not None:
                    st.altair_chart(exc_chart, use_container_width=True)
                for exc in exceptions:
                    color = charts.SEVERITY_COLORS.get(exc["severity"], "#555")
                    st.markdown(
                        f"<div style='border-left:4px solid {color};padding:6px 12px;margin-bottom:6px;'>"
                        f"<b style='color:{color};'>{exc['severity'].upper()}</b> · <code>{exc['code']}</code><br>{_md(exc['detail'])}</div>",
                        unsafe_allow_html=True,
                    )
            else:
                st.success("No control exceptions identified.")

        with tab_narrative:
            st.info(_md(invoice["narrative"]) or "—")

    with right:
        variance = None
        if po and isinstance(total, (int, float)):
            variance = total - po_amount
        st.metric("Invoice total", amount(invoice["amount"]))
        st.metric("PO amount", amount(po_amount) if po_amount is not None else "—")
        st.metric(
            "Variance",
            amount(variance) if variance is not None else "—",
            delta=f"{variance:,.2f}" if variance is not None else None,
            delta_color="inverse",
        )

        workpaper_path = Path(invoice["workpaper_path"] or "")
        if workpaper_path.exists():
            _download_row(
                [
                    (
                        "Download audit workpaper (.xlsx)",
                        workpaper_path.read_bytes(),
                        workpaper_path.name,
                        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        f"ap_wp_{invoice['id']}",
                    )
                ]
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


def _queue_composition(conn):
    """Recommendation counts across the WHOLE queue, not the current filter --
    the sidebar chart's job is to show the shape of the whole inbox.

    Direct SQL is the house style on this page rather than a new app/store.py
    function. Guards the query itself (e.g. no table yet) and drops any
    NULL/empty recommendation before the caller ever hands this to the chart
    builder, which separately guards an all-empty frame.
    """
    try:
        rows = conn.execute(
            "SELECT recommendation, COUNT(*) AS count FROM invoices GROUP BY recommendation"
        ).fetchall()
    except sqlite3.Error:
        return pd.DataFrame()
    counts = pd.DataFrame([dict(r) for r in rows])
    if counts.empty:
        return counts
    return counts[counts["recommendation"].fillna("").str.strip() != ""]


def ap_inbox():
    conn = get_conn()
    st.title("AP Approval Inbox")
    st.caption("Agent-reconciled invoices awaiting human approval — AI-native finance team")

    if not config.DB_PATH.exists():
        st.error("No ledger found. Run `python data/seed.py` then `python run_demo.py`.")
        return

    with st.expander("Upload a new invoice", expanded=False):
        st.caption("Runs the full reconciliation pipeline live, on this one document.")
        _handle_invoice_upload()

    status = st.sidebar.selectbox(
        "Queue", ["pending_review", "approved", "rejected", "all"], index=0
    )
    invoices = store.list_invoices(conn, None if status == "all" else status)
    st.sidebar.metric("In queue", len(invoices))

    queue_chart = charts.queue_composition_chart(_queue_composition(conn))
    if queue_chart is not None:
        st.sidebar.altair_chart(queue_chart, use_container_width=True)

    if not invoices:
        st.info(f"Nothing in '{status}'.")
        return

    # Keyed on the row id, not on the rendered label. Duplicate detection is a
    # headline control here, and the sample set deliberately carries two copies
    # of INV-1001 with the same vendor and amount -- a label-keyed dict collapses
    # them into one entry the moment their recommendations also agree, silently
    # hiding exactly the case the agent is meant to catch.
    by_id = {inv["id"]: inv for inv in invoices}

    def _invoice_label(invoice_id):
        inv = by_id[invoice_id]
        return (
            f"{inv['invoice_number']} · {inv['vendor_name']} · "
            f"{money(inv['amount'])} · {(inv['recommendation'] or '').upper()}"
        )

    choice = st.sidebar.radio("Invoices", list(by_id.keys()), format_func=_invoice_label)
    render_detail(conn, by_id[choice])


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
        # Skip anything that isn't a brief rather than indexing blindly. A stray
        # README.md or a hand-saved note dropped in out/flux/ used to raise
        # IndexError here -- before a single element rendered, so one unrelated
        # file took down the whole page.
        if len(parts) < 4 or parts[0] != "flux" or parts[2] != "to":
            continue
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


@st.cache_data(show_spinner=False)
def _load_flux_graph(path_str: str, mtime: float):
    """Cached read of the memory graph JSON that both _flux_recurring_drivers
    and _flux_recurring_driver_events parse. Keyed on (path, mtime) rather
    than just the path -- same pattern _read_brief_workbook uses -- so a
    regenerated graph file busts the cache. Returns {} for a missing,
    truncated, or malformed file so callers degrade to "no recurring
    drivers" instead of crashing the page.
    """
    try:
        graph = json.loads(Path(path_str).read_text(encoding="utf-8"))
        return graph if isinstance(graph, dict) else {}
    except Exception:  # noqa: BLE001 -- malformed graph degrades to "no data" for callers
        return {}


def _flux_recurring_drivers(limit=10):
    """Drivers the memory graph has seen fire in more than one period — the
    concrete evidence that the agent's intuition compounds across runs rather
    than resetting on every invocation."""
    if not config.FLUX_GRAPH_PATH.exists():
        return pd.DataFrame()
    graph = _load_flux_graph(str(config.FLUX_GRAPH_PATH), config.FLUX_GRAPH_PATH.stat().st_mtime)
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


def _flux_recurring_driver_events(limit=10):
    """Long-form (driver, period) rows for every memory-graph edge seen in
    more than one period -- the shape charts.recurring_drivers_chart expects
    (one row per driver per period it fired in), rather than the one-row-per-
    driver summary _flux_recurring_drivers computes for the sidebar count.

    `limit` caps the number of drivers kept, not the number of rows: the
    drivers seen in the most periods are kept, then every period row for
    each of them is emitted, so the chart stays legible without truncating
    any one driver's history.
    """
    if not config.FLUX_GRAPH_PATH.exists():
        return pd.DataFrame()
    try:
        graph = _load_flux_graph(str(config.FLUX_GRAPH_PATH), config.FLUX_GRAPH_PATH.stat().st_mtime)
        recurring_edges = []
        for edge in (graph.get("edges") or {}).values():
            periods = edge.get("periods") or {}
            if len(periods) < 2:
                continue
            recurring_edges.append((edge, periods))
        recurring_edges.sort(key=lambda pair: len(pair[1]), reverse=True)
        rows = []
        for edge, periods in recurring_edges[:limit]:
            driver = edge.get("driver_name") or edge.get("driver_key")
            account = edge.get("account_code")
            for period, values in periods.items():
                values = values or {}
                rows.append(
                    {
                        "driver": driver,
                        "account": account,
                        "period": period,
                        "delta": values.get("delta", 0.0),
                        "share": values.get("share", 0.0),
                    }
                )
        return pd.DataFrame(rows)
    except Exception:  # noqa: BLE001 -- malformed graph degrades to "no recurring drivers"
        return pd.DataFrame()


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
            "Confirm a finding or flag it as off-base to update the memory graph for future "
            "runs — it does not change this run's priority or owner assignment."
        )
        for finding in findings:
            finding_id = finding["id"]
            existing = conn.execute(
                "SELECT verdict, note FROM feedback WHERE finding_id = ? ORDER BY id DESC LIMIT 1",
                (finding_id,),
            ).fetchone()
            st.markdown(f"**{finding['account_name']}** — {_md(finding['headline'])}")
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


def _parse_account_cell(cell):
    """'Enterprise Revenue (4000)' -> ('Enterprise Revenue', '4000'). The
    workbook writes every Account cell in this shape (see brief.py's
    findings/drivers/actions sheets); Tie-Out is the one sheet that doesn't."""
    if not isinstance(cell, str) or " (" not in cell:
        return cell, None
    name, _, rest = cell.rpartition(" (")
    return name, rest.rstrip(")")


def _parse_priority_code(cell):
    """'P1 · Act this week' -> 'P1' -- take the first whitespace-delimited
    token so this doesn't depend on the exact separator character."""
    if not isinstance(cell, str) or not cell.strip():
        return "P2"
    return cell.split()[0]


def _reconstruct_prior_current(delta, pct_cell):
    """Recover an account's prior/current balance from the Findings sheet's
    Delta and Pct columns for the seeded-brief view's bridge chart.

    The workbook doesn't carry the raw balances, only the already-computed
    delta and a 1-decimal-rounded percentage string, so this is a best-effort
    reconstruction (worst for a pct that rounds hard). 'new' means no prior
    balance at all (pct was +inf) -- prior is exactly 0 there, no division
    needed. Returns (None, None) when it can't be recovered (e.g. pct is
    "0.0%", which would divide by zero), and callers must treat that as "no
    bridge for this account" rather than guessing.
    """
    if pct_cell == "new":
        return 0.0, float(delta)
    if not isinstance(pct_cell, str) or not pct_cell.endswith("%"):
        return None, None
    try:
        pct = float(pct_cell.rstrip("%")) / 100.0
    except ValueError:
        return None, None
    if pct == 0:
        return None, None
    prior = float(delta) / pct
    return prior, prior + float(delta)


@st.cache_data(show_spinner=False)
def _read_brief_workbook(path_str: str, mtime: float):
    """Cached read of the evidence sheets a seeded brief's .xlsx carries.

    Keyed on (path, mtime) rather than just the path: st.cache_data hashes
    its arguments, so passing mtime explicitly busts the cache if a brief is
    ever regenerated at the same path -- the same reasoning _flux_briefs()
    already applies when picking the latest file per period pair. Returns
    empty DataFrames for a sheet (or all sheets) that can't be read, so a
    missing/corrupt .xlsx degrades to "nothing to chart" rather than crashing
    the page.
    """
    sheets = {}
    for name in ("Findings", "Drivers", "Tie-Out", "Actions"):
        try:
            sheets[name] = pd.read_excel(path_str, sheet_name=name)
        except Exception:  # noqa: BLE001 -- any read failure degrades to "no data" for this sheet
            sheets[name] = pd.DataFrame()
    return sheets


def _render_variance_visuals(
    *,
    prior_period,
    current_period,
    source_note,
    metrics,
    findings,
    action_plan_df,
    tie_out_df,
    exports,
    below_threshold_df=None,
    movements_df=None,
    anomaly_ran=True,
    largest_move=None,
):
    """Shared chart-first rendering for both the upload-result view and the
    seeded-brief view, so the two look like the same product.

    `findings` is a list of dicts, each with: account_name, account_code,
    headline, why, pct_display (already-formatted string), delta, priority,
    owner, confidence, drivers (a DataFrame of member_name/delta/cohort, or
    empty), prior_amt, current_amt (either may be None if the caller
    couldn't determine them). An empty `findings` list renders the
    below-threshold table and materiality-gate explanation instead of any
    chart -- there is nothing material to show a bridge or movement bar for.

    `source_note` is one short provenance line rendered once, as a caption
    under the metric row (isolated upload run vs. seeded demo ledger).

    `anomaly_ran` and `largest_move` feed `_gate_sentence`/`_anomaly_caveat`
    to compose the below-threshold message here rather than have each caller
    restate the materiality gate in its own words.

    `exports` is a dict with keys `downloads` (a list shaped for
    `_download_row`), `analysis` (str|None), `actions` (str|None), and
    `brief_md` (str|None); rendered at the end of this function so both
    callers share one download row and one pair of full-output expanders.
    """
    if metrics:
        metric_cols = st.columns([2] + [1] * (len(metrics) - 1))
        for col, (label, value) in zip(metric_cols, metrics.items()):
            col.metric(label, value)
    st.caption(source_note)
    st.caption(f"Materiality gate: {_gate_sentence(anomaly_ran)}")

    material = [f for f in findings if not f.get("informational")]
    informational = [f for f in findings if f.get("informational")]

    tab_overview, tab_findings, tab_actions, tab_tie_out, tab_exports = st.tabs(
        ["Overview", "Findings", "Actions", "Tie-out", "Exports"]
    )

    with tab_overview:
        if material:
            movement_df = pd.DataFrame(
                [
                    {"account_name": f["account_name"], "delta": f["delta"], "priority": f["priority"]}
                    for f in material
                ]
            )
            movement_chart = charts.account_movement_chart(movement_df)
            if movement_chart is not None:
                st.altair_chart(movement_chart, use_container_width=True)
        else:
            # No early return. A period with nothing material still carries the
            # action plan and the tie-out, and now the drilled top movers too.
            # Returning here hid the single actionable item on the page and made a
            # legitimately quiet close look like a broken run.
            message = (
                # The gate itself is already stated in the page-level caption a
                # few lines above this, so name it here rather than spell it out
                # twice on one screen.
                f"No account moved enough between {prior_period} and {current_period} to clear "
                "the materiality gate above. That's a clean close, not a failed run."
            )
            if largest_move is not None:
                message += f" The largest move was \\${largest_move:,.0f}."
            message += _anomaly_caveat(anomaly_ran)
            st.info(message)
            # Chart first, table second. "Nothing cleared the gate" is a real
            # answer, but as prose plus a five-row table it reads like the agent
            # came back empty; against the gate line, how far short every account
            # fell is the finding.
            gate_chart = charts.movement_vs_gate_chart(movements_df, config.MATERIALITY_ABS)
            if gate_chart is not None:
                st.altair_chart(gate_chart, use_container_width=True)
            if below_threshold_df is not None and not below_threshold_df.empty:
                with st.expander("Largest movements below the threshold"):
                    st.dataframe(below_threshold_df, hide_index=True, use_container_width=True)

    with tab_findings:
        if material:
            _render_findings_section(material, prior_period, current_period)

        if informational:
            # After the gate explanation, never in place of it: these were drilled
            # precisely because nothing was material, and putting them under the
            # "Material findings" heading would launder them into findings.
            _render_findings_section(
                informational,
                prior_period,
                current_period,
                heading="Largest movements reviewed",
                caption=(
                    "Below the gate — drilled for context, marked P3, no action needed this close."
                ),
            )

    with tab_actions:
        if action_plan_df is not None and not action_plan_df.empty:
            has_priority = "Priority" in action_plan_df.columns
            has_owner = "Owner" in action_plan_df.columns
            has_account = "Account" in action_plan_df.columns
            has_task = "Task" in action_plan_df.columns

            if has_priority and has_owner:
                owner_chart = charts.action_owner_chart(
                    pd.DataFrame(
                        {
                            "owner": action_plan_df["Owner"],
                            "priority": action_plan_df["Priority"].apply(_parse_priority_code),
                        }
                    )
                )
                if owner_chart is not None:
                    st.altair_chart(owner_chart, use_container_width=True)

            grouped = {"P1": [], "P2": [], "P3": []}
            # The workbook writes Priority as "P1 · Act this week"; the bare code
            # sorts the groups, the full string labels them, so the heading keeps
            # saying what the priority actually means.
            labels = {}
            for _, row in action_plan_df.iterrows():
                code = _parse_priority_code(row["Priority"]) if has_priority else "P2"
                grouped.setdefault(code, []).append(row)
                if has_priority and code not in labels:
                    labels[code] = str(row["Priority"]).strip() or code
            # Known codes first, in order; then anything unexpected, so a new
            # priority from actions.py surfaces instead of vanishing.
            ordered = [c for c in ("P1", "P2", "P3") if grouped.get(c)]
            ordered += [c for c in grouped if c not in ("P1", "P2", "P3") and grouped.get(c)]
            for code in ordered:
                rows = grouped[code]
                st.markdown(f"**{labels.get(code, code)}**")
                for row in rows:
                    account = row["Account"] if has_account else "—"
                    owner = row["Owner"] if has_owner else "—"
                    task = _md(row["Task"]) if has_task else "—"
                    st.markdown(f"- **{account}** — *{owner}* — {task}")
        else:
            st.caption("No actions generated this period.")

    with tab_tie_out:
        tie_chart = charts.tie_out_chart(tie_out_df)
        if tie_chart is not None:
            if "coverage_pct" in tie_out_df:
                total = len(tie_out_df)
                gaps = total - int((tie_out_df["coverage_pct"] >= 99.9).sum())
                if not gaps:
                    st.caption(f"All {total} accounts trace fully to subledger detail.")
                else:
                    subject = "account does" if gaps == 1 else "accounts do"
                    st.caption(
                        f"{total - gaps} of {total} accounts trace fully to subledger detail; "
                        f"{gaps} {subject} not."
                    )
            st.altair_chart(tie_chart, use_container_width=True)
        else:
            st.caption("No tie-out data available for this run.")

    with tab_exports:
        downloads = exports.get("downloads") or []
        if downloads:
            _download_row(downloads)

        col_analysis, col_actions = st.columns(2)
        with col_analysis:
            st.subheader("Analysis — what changed, why")
            if exports.get("analysis") is not None:
                st.text(exports["analysis"])
            else:
                st.info("No analysis output for this run.")
        with col_actions:
            st.subheader("Recommended actions — what to do next")
            if exports.get("actions") is not None:
                st.text(exports["actions"])
            else:
                st.info("No action-plan output for this run.")

        if exports.get("brief_md") is not None:
            with st.expander("Full brief (driver tables, tie-out gaps)"):
                st.markdown(exports["brief_md"])


def _render_findings_section(findings, prior_period, current_period, heading="Material findings", caption=None):
    """Per-account expander with a concentration strip, bridge, driver bars,
    and the model's narrative.

    Takes the two periods explicitly: this body was extracted out of
    _render_variance_visuals, where they were in scope as parameters, and
    reading them as globals raised NameError at render time -- invisible to
    py_compile and to a test suite that never exercises the UI.
    """
    # The caption describes these expanders, not the movement chart -- that moved
    # to the Overview tab, and a caption still pointing at its delta/priority
    # encoding would be explaining something no longer on screen.
    _section(heading, caption or "Open an account for its concentration, bridge, drivers, and narrative.")
    for f in findings:
        with st.expander(f"{f['account_name']} — {_md(f['headline'])}", expanded=False):
            st.caption(
                # Escaped for the same reason as _gate_sentence: st.caption is
                # markdown, and a bare `$` pairs with any later one to open math.
                f"{f['priority']} · {f['owner']} · confidence: {f['confidence']} · "
                f"delta \\${f['delta']:+,.2f} ({f['pct_display']})"
            )
            share_chart = charts.driver_share_chart(f.get("drivers"), f.get("delta"))
            if share_chart is not None:
                st.altair_chart(share_chart, use_container_width=True)
            bridge = charts.bridge_chart(
                f["account_name"], prior_period, current_period,
                f.get("prior_amt"), f.get("current_amt"), f.get("drivers"),
            )
            driver_chart = charts.driver_contribution_chart(f.get("drivers"))
            if bridge is None and driver_chart is None:
                st.caption("No bridge or subledger driver detail available for this account.")
            else:
                col_bridge, col_driver = st.columns(2)
                with col_bridge:
                    if bridge is not None:
                        st.altair_chart(bridge, use_container_width=True)
                with col_driver:
                    if driver_chart is not None:
                        st.altair_chart(driver_chart, use_container_width=True)
            st.markdown(_md(f["why"]))


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
    st.caption(
        "No data handy? Download this pair and upload them straight back -- they cover "
        "2026-07 to 2026-08, the comparison with the most to find. (Several period pairs "
        "in the seeded data have nothing above the materiality gate; that is a real "
        "result, but it makes for a quiet demo.)"
    )
    _download_row([(label, path.read_bytes(), path.name, "text/csv", key) for label, path, key in available])


# An upload result lives in st.session_state, which dies with the browser
# socket: reload the tab, lose the connection for a moment, or let Streamlit
# start a fresh session for any reason, and the result silently vanishes --
# leaving the seeded demo brief as the only thing on screen, which reads as
# "the agent answered my CSV with someone else's accounts". Writing the
# rendered result out under its run id, and putting that id in the URL, makes
# a reload reproduce the same page instead.
#
# Container-local and disposable by design: this holds one visitor's uploaded
# figures, it is never read by the agent itself (an upload is an isolated run),
# and it goes away with the container.
FLUX_RUN_CACHE = config.UPLOADS_DIR / "runs"
_RUN_ID_RE = re.compile(r"^[0-9a-f]{4,40}$")
_MAX_CACHED_RUNS = 20


def _save_upload_result(entry):
    """Persist one rendered upload result. Best-effort: a read-only or full
    disk costs the reload-survives-this property, not the result on screen."""
    try:
        FLUX_RUN_CACHE.mkdir(parents=True, exist_ok=True)
        payload = {
            **entry,
            "names": list(entry["names"]),
            # The workbook is the one binary in here; everything else the
            # renderer needs is already plain dicts, lists, and strings.
            "xlsx": base64.b64encode(entry["xlsx"]).decode("ascii"),
        }
        (FLUX_RUN_CACHE / f"{entry['run_id']}.json").write_text(json.dumps(payload), encoding="utf-8")
        cached = sorted(FLUX_RUN_CACHE.glob("*.json"), key=lambda f: f.stat().st_mtime, reverse=True)
        for stale in cached[_MAX_CACHED_RUNS:]:
            stale.unlink(missing_ok=True)
    except Exception:  # noqa: BLE001 -- persistence is a convenience, never the run
        pass


def _load_upload_result(run_id):
    """The saved result for `run_id`, or None. The id comes from the URL, so it
    is pattern-checked before it is ever joined onto a path."""
    if not run_id or not _RUN_ID_RE.match(run_id):
        return None
    path = FLUX_RUN_CACHE / f"{run_id}.json"
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["names"] = tuple(payload["names"])
        payload["xlsx"] = base64.b64decode(payload["xlsx"])
    except Exception:  # noqa: BLE001 -- a truncated or stale file degrades to "no saved result"
        return None
    return payload


def _forget_upload_result():
    st.session_state.pop("flux_upload", None)
    if "run" in st.query_params:
        del st.query_params["run"]


def _handle_flux_upload():
    st.caption(
        "Add one file per period, or files that already span multiple periods -- e.g. drop "
        "`2025-01.csv` and `2025-02.csv` summaries plus their two transaction files, four files "
        "total. Each file needs a period summary shape (`period, account_code, account_name, "
        "amount`) or a transaction detail shape (`period, account_code, amount`, plus "
        "`customer_id` or `vendor`); `group_by_role` sorts out which is which from the columns, "
        "not from upload order. The latest two periods across all the summary files combined are "
        "compared."
    )

    # Also offer a known-good pair for a no-hunting demo path.
    _offer_sample_uploads()

    # One multi-file widget rather than fixed slots: group_by_role identifies
    # summary vs. transaction files by their columns, and any number of files
    # can be dropped in -- numbered slots would imply an order and a count
    # that never mattered.
    uploaded = st.file_uploader(
        "Summary + transaction detail (add as many files as you need)",
        type=["csv", "xlsx"],
        accept_multiple_files=True,
        key="flux_upload_files",
    ) or []

    if len(uploaded) < 2:
        if uploaded:
            # Say so rather than sitting inert -- one file looks like a hang.
            st.info(f"Got **{uploaded[0].name}**. Add at least one more file to run the agent.")
        return

    if not st.button("Run the variance agent on these files", key="flux_run_upload", type="primary"):
        return

    config.UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
    saved = []
    for item in uploaded:
        dest = config.UPLOADS_DIR / f"{uuid.uuid4().hex[:8]}_{item.name}"
        dest.write_bytes(item.getvalue())
        saved.append(dest)

    with st.spinner(
        "Comparing periods, slicing drivers, and drafting the action plan... "
        "(the first run after an idle period also cold-starts the model, so give it ~30s)"
    ):
        try:
            state, prepared = flux_uploads.run(saved)
        except flux_uploads.UploadError as exc:
            st.error(str(exc))
            return
        except Exception as exc:  # noqa: BLE001 -- surface any failure to the uploader, don't crash the page
            st.error(f"Upload run failed: {exc}")
            return

    # Stash everything the render needs in session state rather than holding it
    # in a local: clicking a download button triggers a rerun, and a result
    # that only existed inside this `if st.button(...)` block would vanish on the
    # first download click, mid-demo.
    entry = {
        "state": state,
        "run_id": state.get("run_id"),
        "prior_period": prepared.prior_period,
        "period": prepared.period,
        "warnings": prepared.warnings,
        "summary_rows": prepared.summary_rows,
        "txn_rows": prepared.txn_rows,
        "names": tuple(item.name for item in uploaded),
        "md": flux_brief.markdown(state),
        "analysis": flux_brief.analysis_text(state),
        "actions": flux_brief.actions_text(state),
        "xlsx": flux_brief.workbook_bytes(state),
        "stem": f"flux_{prepared.prior_period}_to_{prepared.period}_{state.get('run_id')}",
    }
    st.session_state["flux_upload"] = entry
    # Both, deliberately: session state renders this pass, the file plus the
    # ?run= id in the URL is what a reload comes back to.
    _save_upload_result(entry)
    if entry["run_id"]:
        st.query_params["run"] = entry["run_id"]

    # Rerun rather than rendering the result in the tail of this pass. This pass
    # began as the seeded-brief page, so the frontend is holding that page's
    # charts and expanders; finishing it with a different, shorter element tree
    # left stale seeded findings interleaved with the uploaded ones on screen.
    # A rerun renders the result page from the top, and costs nothing: the graph
    # run is already finished and in session state, so nothing is recomputed.
    st.rerun()


def _render_upload_result(entry):
    state = entry["state"]
    prior, period = entry["prior_period"], entry["period"]
    names = entry["names"]
    if len(names) <= 3:
        files_desc = " + ".join(names)
    else:
        files_desc = f"{len(names)} files ({', '.join(names)})"

    st.success(f"Compared {prior} → {period} from {files_desc}.")

    for warning in entry.get("warnings") or []:
        st.warning(warning)

    variances_by_code = {v["account_code"]: v for v in state.get("variances") or []}
    findings = []
    for f in state.get("findings") or []:
        v = variances_by_code.get(f["account_code"])
        drivers = f.get("drivers") or []
        findings.append(
            {
                "account_name": f["account_name"],
                "account_code": f["account_code"],
                "headline": f["headline"],
                "why": f["why"],
                "delta": f["delta"],
                "pct_display": flux_brief._fmt_pct(f["pct"]),
                "priority": f.get("priority") or "P2",
                "owner": f.get("owner") or "FP&A",
                "confidence": f.get("confidence") or "—",
                "informational": bool(f.get("informational")),
                "drivers": pd.DataFrame(
                    [{"member_name": d["member_name"], "delta": d["delta"], "cohort": d["cohort"]} for d in drivers]
                ),
                "prior_amt": v["prior_amt"] if v else None,
                "current_amt": v["current_amt"] if v else None,
            }
        )

    action_plan_df = pd.DataFrame(
        [
            {
                "Priority": f"{item['priority']} · {item['priority_label']}",
                "Account": f"{item['account_name']} ({item['account_code']})",
                "Owner": item["owner"],
                "Task": item["task"],
            }
            for item in state.get("action_plan") or []
        ]
    )
    tie_out_df = pd.DataFrame(
        [{"account_name": t["account_name"], "coverage_pct": t["coverage_pct"]} for t in state.get("tie_out") or []]
    )

    below_threshold_df = pd.DataFrame()
    movements_df = pd.DataFrame()
    variances = state.get("variances") or []
    material_findings = [f for f in findings if not f.get("informational")]
    if not material_findings and variances:
        top = sorted(variances, key=lambda v: abs(v["delta"]), reverse=True)[:5]
        below_threshold_df = pd.DataFrame(
            [
                {
                    "Account": v["account_name"],
                    "Delta": v["delta"],
                    "Pct": flux_brief._fmt_pct(v["pct"]),
                }
                for v in top
            ]
        )
        # Every account, not just the top five: the chart's job is to show that
        # the whole period is short of the gate, which a truncated list can't.
        movements_df = pd.DataFrame(
            [{"account_name": v["account_name"], "delta": v["delta"]} for v in variances]
        )

    # variance.compute_variances leaves z_score at 0.0 unless it found at least
    # two month-over-month deltas strictly before the prior period -- four
    # periods of history. A two-period upload never has that, so the anomaly
    # gate silently does not run, and saying it did would be describing a check
    # that never happened.
    anomaly_ran = any(abs(v.get("z_score") or 0) > 0 for v in variances)

    stem = entry["stem"]
    exports = {
        "downloads": [
            ("Brief (.md)", entry["md"], f"{stem}.md", "text/markdown", "flux_dl_md"),
            (
                "Workpaper (.xlsx)",
                entry["xlsx"],
                f"{stem}.xlsx",
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                "flux_dl_xlsx",
            ),
            ("Analysis (.txt)", entry["analysis"], f"{stem}_analysis.txt", "text/plain", "flux_dl_analysis"),
            ("Actions (.txt)", entry["actions"], f"{stem}_actions.txt", "text/plain", "flux_dl_actions"),
        ],
        "analysis": entry["analysis"],
        "actions": entry["actions"],
        "brief_md": entry["md"],
    }

    _render_variance_visuals(
        prior_period=prior,
        current_period=period,
        source_note="Isolated run — nothing was written to the agent's institutional memory or to out/flux/.",
        metrics={
            "Accounts analyzed": len(state.get("variances", [])),
            "Material findings": len(material_findings),
            "Reviewed below gate": len(findings) - len(material_findings),
            "Actions": len(state.get("action_plan", [])),
        },
        findings=findings,
        action_plan_df=action_plan_df,
        tie_out_df=tie_out_df,
        exports=exports,
        below_threshold_df=below_threshold_df,
        movements_df=movements_df,
        anomaly_ran=anomaly_ran,
        largest_move=max((abs(v["delta"]) for v in variances), default=0),
    )

    if st.button("Clear result", key="flux_clear_upload"):
        _forget_upload_result()
        st.rerun()


def flux_page():
    st.title("Variance Explanation Agent")
    st.caption("What changed, why, and what's driving it — with intuition that compounds across runs")

    briefs = _flux_briefs()

    with st.expander("Analyze your own financials (upload two CSVs)", expanded=not briefs):
        _handle_flux_upload()

    # A fresh session (reload, reconnect) has empty session state; the ?run= id
    # in the URL is what carries the result across that boundary.
    if "flux_upload" not in st.session_state and st.query_params.get("run"):
        restored = _load_upload_result(st.query_params.get("run"))
        if restored is not None:
            st.session_state["flux_upload"] = restored
        else:
            del st.query_params["run"]
            st.warning(
                "That upload result is no longer available — the demo server restarts when idle "
                "and only keeps recent runs. Upload the files again to re-run the agent."
            )

    if "flux_upload" in st.session_state:
        # Return, don't fall through. The seeded-brief view below renders the
        # same headings (Account movement overview / Material findings /
        # Subledger tie-out) from the committed synthetic ledger, so stacking
        # it under a live result put two near-identical reports on one page --
        # and the second one, always the same seeded accounts, read as the
        # answer to whatever CSV had just been uploaded.
        _render_upload_result(st.session_state["flux_upload"])
        st.caption(
            "Showing your uploaded run. Use **Clear result** above to go back to the "
            "seeded demo briefs."
        )
        return

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
        st.sidebar.caption(f"{len(recurring)} recurring drivers in memory")

    # The workbook is the one artifact with structured, chartable sheets --
    # the .md/.txt outputs are prose. Read it (cached on path+mtime) and
    # normalize its sheets into the same shapes _render_variance_visuals
    # expects from the live-upload view's `state` dict, so both views produce
    # the same charts from different sources.
    if brief["xlsx_path"] and brief["xlsx_path"].exists():
        sheets = _read_brief_workbook(str(brief["xlsx_path"]), brief["xlsx_path"].stat().st_mtime)
    else:
        sheets = {name: pd.DataFrame() for name in ("Findings", "Drivers", "Tie-Out", "Actions")}

    findings_sheet = sheets["Findings"]
    drivers_sheet = sheets["Drivers"]
    tie_out_sheet = sheets["Tie-Out"]
    actions_sheet = sheets["Actions"]

    # _read_brief_workbook already degrades an unreadable SHEET to an empty
    # frame, but a sheet that reads fine with unexpected columns -- an older
    # brief, or a hand-edited .xlsx -- used to raise KeyError here and take the
    # page down. Require the columns this loop indexes; a workbook missing them
    # is not one this view can normalize.
    _REQUIRED_FINDINGS_COLS = {
        "Account", "Headline", "Why", "Delta", "Pct", "Priority", "Owner", "Confidence",
    }
    if not findings_sheet.empty and not _REQUIRED_FINDINGS_COLS.issubset(findings_sheet.columns):
        missing = ", ".join(sorted(_REQUIRED_FINDINGS_COLS - set(findings_sheet.columns)))
        st.warning(
            f"This brief's workbook is missing expected Findings columns ({missing}), so its "
            "findings can't be charted. Re-run `python run_flux.py --replay` to regenerate it."
        )
        findings_sheet = pd.DataFrame()

    findings = []
    for _, row in findings_sheet.iterrows():
        account_name, account_code = _parse_account_cell(row["Account"])
        has_driver_account = not drivers_sheet.empty and "Account" in drivers_sheet.columns
        account_drivers = drivers_sheet[drivers_sheet["Account"] == row["Account"]] if has_driver_account else pd.DataFrame()
        drivers_df = pd.DataFrame(
            {
                "member_name": account_drivers.get("Driver", pd.Series(dtype=object)),
                "delta": account_drivers.get("Delta", pd.Series(dtype=float)),
                "cohort": account_drivers.get("Cohort", pd.Series(dtype=object)),
            }
        )
        prior_amt, current_amt = _reconstruct_prior_current(row["Delta"], row["Pct"])
        findings.append(
            {
                "account_name": account_name,
                "account_code": account_code,
                "headline": row["Headline"],
                "why": row["Why"],
                "delta": row["Delta"],
                "pct_display": row["Pct"],
                "priority": _parse_priority_code(row["Priority"]),
                "owner": row["Owner"],
                "confidence": row["Confidence"],
                # The workbook carries the reason string, which is the only
                # record on disk of whether a row cleared the gate.
                "informational": row.get("Materiality") == flux_variance.BELOW_THRESHOLD_REASON,
                "drivers": drivers_df,
                "prior_amt": prior_amt,
                "current_amt": current_amt,
            }
        )

    # Same reasoning as the Findings guard above: select these columns only when
    # the sheet actually carries them, so an older or hand-edited workbook costs
    # one panel rather than the whole page.
    def _select(sheet, columns):
        if sheet.empty or not set(columns).issubset(sheet.columns):
            return pd.DataFrame()
        return sheet[list(columns)]

    action_plan_df = _select(actions_sheet, ("Priority", "Account", "Owner", "Task"))
    renamed_tie_out = (
        tie_out_sheet.rename(columns={"Account": "account_name", "Coverage %": "coverage_pct"})
        if not tie_out_sheet.empty
        else tie_out_sheet
    )
    tie_out_df = _select(renamed_tie_out, ("account_name", "coverage_pct"))

    exports = {
        "downloads": [
            (
                "Workpaper (.xlsx)",
                brief["xlsx_path"].read_bytes() if brief["xlsx_path"] else None,
                brief["xlsx_path"].name if brief["xlsx_path"] else None,
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                "seeded_dl_xlsx",
            ),
            (
                "Analysis (.txt)",
                brief["analysis_path"].read_bytes() if brief["analysis_path"] else None,
                brief["analysis_path"].name if brief["analysis_path"] else None,
                "text/plain",
                "seeded_dl_analysis",
            ),
            (
                "Actions (.txt)",
                brief["actions_path"].read_bytes() if brief["actions_path"] else None,
                brief["actions_path"].name if brief["actions_path"] else None,
                "text/plain",
                "seeded_dl_actions",
            ),
        ],
        "analysis": brief["analysis_path"].read_text(encoding="utf-8") if brief["analysis_path"] else None,
        "actions": brief["actions_path"].read_text(encoding="utf-8") if brief["actions_path"] else None,
        "brief_md": brief["md_path"].read_text(encoding="utf-8"),
    }

    _render_variance_visuals(
        prior_period=brief["prior_period"],
        current_period=brief["current_period"],
        source_note=(
            "Seeded demo ledger — synthetic financials committed with the repo, not uploaded "
            "data. Upload your own files above to run the agent against them."
        ),
        metrics={
            "Accounts analyzed": len(tie_out_sheet),
            "Material findings": sum(1 for f in findings if not f["informational"]),
            "Reviewed below gate": sum(1 for f in findings if f["informational"]),
            "Actions": len(actions_sheet),
        },
        findings=findings,
        action_plan_df=action_plan_df,
        tie_out_df=tie_out_df,
        exports=exports,
        anomaly_ran=True,
        largest_move=None,
    )

    _section(
        "Institutional memory",
        caption="Recurring drivers, analyst feedback, and run history — how the agent's read compounds across runs.",
    )
    driver_events_chart = charts.recurring_drivers_chart(_flux_recurring_driver_events())
    if driver_events_chart is not None:
        st.altair_chart(driver_events_chart, use_container_width=True)
    else:
        st.caption(
            "No driver has fired in more than one period yet — recurrence builds up as more "
            "runs are recorded, this is not an error."
        )

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
