"""Pure Altair chart builders for the flux (variance-explanation) UI.

Every function here takes plain pandas DataFrames and returns an Altair
chart object, or `None` when there is nothing sensible to draw -- no
`st.*` calls anywhere below, so these are testable without a running
Streamlit process and are shared by both the live-upload view (fed from
the in-memory `state` dict) and the seeded-briefs view (fed from the
already-written `.xlsx` workpaper's sheets). `ui/streamlit_app.py` owns
adapting each view's data into the DataFrame shapes documented per
function; this module only draws.

Cohort, priority, severity, recommendation, and match status each get one
fixed color scale, defined once here, so the same color means the same
thing on every chart in the app. These same dicts are also the source for
the inline-HTML badges in `ui/streamlit_app.py`, so the palette has exactly
one home. Nothing below sets an explicit background or text color --
Streamlit re-themes Altair charts for light/dark automatically, and
hardcoding either would break that.
"""

import altair as alt
import pandas as pd

# Colorblind-safe categorical colors (Okabe-Ito-derived). One fixed
# domain/range pair per field, reused by every chart that encodes it.
COHORT_COLORS = {
    "new": "#2980b9",  # blue
    "expansion": "#27ae60",  # green
    "contraction": "#e67e22",  # orange
    "churned": "#c0392b",  # red
}
PRIORITY_COLORS = {
    "P1": "#c0392b",  # red -- act this week
    "P2": "#e67e22",  # orange -- review this close cycle
    "P3": "#7f8c8d",  # gray -- monitor only, nothing actionable yet
}
# Moved here from ui/streamlit_app.py (lines 24-30), which held a second,
# competing palette. Two hex values changed in the move:
#   - "low" severity: #5d8a3a -> #7f8c8d, the same gray as
#     PRIORITY_COLORS["P3"]. "low severity" and "monitor only" say the same
#     thing to the reader, so they now render as the same color.
#   - "approve": #2e7d32 -> #27ae60, i.e. INCREASE_COLOR (below), so the app
#     has exactly one green instead of three.
SEVERITY_COLORS = {"critical": "#c0392b", "high": "#d35400", "medium": "#b7950b", "low": "#7f8c8d"}
RECOMMENDATION_COLORS = {"hold": "#c0392b", "review": "#b7950b", "approve": "#27ae60"}
MATCH_COLORS = {"match": "#27ae60", "mismatch": "#c0392b", "not available": "#95a5a6"}
INCREASE_COLOR = "#27ae60"
DECREASE_COLOR = "#c0392b"
TOTAL_COLOR = "#34495e"

_COHORT_SCALE = alt.Scale(domain=list(COHORT_COLORS), range=list(COHORT_COLORS.values()))
_PRIORITY_SCALE = alt.Scale(domain=list(PRIORITY_COLORS), range=list(PRIORITY_COLORS.values()))
_SEVERITY_SCALE = alt.Scale(domain=list(SEVERITY_COLORS), range=list(SEVERITY_COLORS.values()))
_RECOMMENDATION_SCALE = alt.Scale(
    domain=list(RECOMMENDATION_COLORS), range=list(RECOMMENDATION_COLORS.values())
)
_MATCH_SCALE = alt.Scale(domain=list(MATCH_COLORS), range=list(MATCH_COLORS.values()))
_BRIDGE_SCALE = alt.Scale(
    domain=["total", "increase", "decrease"],
    range=[TOTAL_COLOR, INCREASE_COLOR, DECREASE_COLOR],
)


def _signed_value_labels(df: pd.DataFrame, category_field: str, value_field: str, order: list):
    """Bar-end text labels for a diverging (positive/negative) bar chart.

    Left-aligned past the bar for positive values, right-aligned before it
    for negative ones, so the label never sits on top of the zero axis. A
    single conditional encoding would need a Vega-Lite expression for
    `align`/`dx`; two data-filtered layers are simpler and just as cheap
    for the handful of rows these charts ever carry.
    """
    layers = []
    pos = df[df[value_field] >= 0]
    neg = df[df[value_field] < 0]
    if not pos.empty:
        layers.append(
            alt.Chart(pos)
            .mark_text(align="left", dx=3)
            .encode(
                y=alt.Y(f"{category_field}:N", sort=order),
                x=alt.X(f"{value_field}:Q"),
                text=alt.Text(f"{value_field}:Q", format="+,.0f"),
            )
        )
    if not neg.empty:
        layers.append(
            alt.Chart(neg)
            .mark_text(align="right", dx=-3)
            .encode(
                y=alt.Y(f"{category_field}:N", sort=order),
                x=alt.X(f"{value_field}:Q"),
                text=alt.Text(f"{value_field}:Q", format="+,.0f"),
            )
        )
    return layers


def bridge_chart(account_name, prior_period, current_period, prior_amt, current_amt, drivers):
    """Waterfall from the prior balance to the current balance.

    `drivers` is a DataFrame of the drivers to plot as floating steps in
    between, columns `member_name` (str) and `delta` (float) -- already the
    subset the caller wants shown; this does not re-rank or truncate. Since
    only the top few drivers are ever passed in, their deltas almost never
    sum to the full account delta -- the gap is folded into one "Other
    drivers" step so the bars always land exactly on the current balance.

    Returns `None` if `prior_amt` or `current_amt` is missing (the
    seeded-brief view can't always reconstruct them for an account).
    """
    if prior_amt is None or current_amt is None:
        return None
    prior_amt = float(prior_amt)
    current_amt = float(current_amt)

    steps = [{"label": "Prior", "start": 0.0, "end": prior_amt, "kind": "total"}]
    running = prior_amt
    if drivers is not None and not drivers.empty:
        for _, row in drivers.iterrows():
            d = float(row["delta"])
            steps.append(
                {
                    "label": str(row["member_name"]),
                    "start": running,
                    "end": running + d,
                    "kind": "increase" if d >= 0 else "decrease",
                }
            )
            running += d
        residual = current_amt - running
        if abs(residual) > 0.5:
            steps.append(
                {
                    "label": "Other drivers",
                    "start": running,
                    "end": running + residual,
                    "kind": "increase" if residual >= 0 else "decrease",
                }
            )
            running += residual
    steps.append({"label": "Current", "start": 0.0, "end": current_amt, "kind": "total"})

    df = pd.DataFrame(steps)
    df["delta"] = df["end"] - df["start"]
    df["label_y"] = df[["start", "end"]].max(axis=1)
    df["value_label"] = df.apply(
        lambda r: f"{r['end']:,.0f}" if r["kind"] == "total" else f"{r['delta']:+,.0f}", axis=1
    )
    order = df["label"].tolist()

    bars = (
        alt.Chart(df)
        .mark_bar(size=32)
        .encode(
            x=alt.X("label:N", sort=order, title=None, axis=alt.Axis(labelAngle=-30, labelLimit=140)),
            y=alt.Y("start:Q", title=f"Amount ({prior_period} -> {current_period})"),
            y2="end:Q",
            color=alt.Color("kind:N", scale=_BRIDGE_SCALE, legend=alt.Legend(title=None)),
            tooltip=[
                alt.Tooltip("label:N", title="Step"),
                alt.Tooltip("delta:Q", title="Change", format="+,.2f"),
            ],
        )
    )
    labels = (
        alt.Chart(df)
        .mark_text(dy=-6, fontWeight="bold")
        .encode(
            x=alt.X("label:N", sort=order),
            y=alt.Y("label_y:Q"),
            text=alt.Text("value_label:N"),
        )
    )
    return (bars + labels).properties(title=f"{account_name}: bridge from prior to current balance")


def driver_contribution_chart(drivers: pd.DataFrame):
    """Horizontal bars of each driver's delta, sorted by magnitude, colored
    by cohort, with the delta value labeled on the bar.

    `drivers` needs columns `member_name`, `delta`, `cohort`. Returns `None`
    when there's nothing to plot -- a finding whose account has no
    subledger detail to slice (a summary-only account).
    """
    if drivers is None or drivers.empty:
        return None
    df = drivers.copy()
    df["abs_delta"] = df["delta"].abs()
    order = df.sort_values("abs_delta", ascending=False)["member_name"].tolist()

    bars = alt.Chart(df).mark_bar().encode(
        y=alt.Y("member_name:N", sort=order, title=None),
        x=alt.X("delta:Q", title="Delta"),
        color=alt.Color("cohort:N", scale=_COHORT_SCALE, legend=alt.Legend(title="Cohort")),
        tooltip=[
            alt.Tooltip("member_name:N", title="Driver"),
            alt.Tooltip("delta:Q", title="Delta", format="+,.2f"),
            alt.Tooltip("cohort:N", title="Cohort"),
        ],
    )
    layers = [bars] + _signed_value_labels(df, "member_name", "delta", order)
    return alt.layer(*layers).properties(title="Driver contribution")


def account_movement_chart(findings: pd.DataFrame, title: str = "Account movement overview"):
    """Horizontal bars of every material finding's delta, colored by
    priority, sorted so the biggest increases and decreases anchor the ends.

    `findings` needs columns `account_name`, `delta`, `priority`. Returns
    `None` when there are no findings (a clean close, nothing cleared the
    materiality gate).
    """
    if findings is None or findings.empty:
        return None
    df = findings.copy()
    order = df.sort_values("delta", ascending=False)["account_name"].tolist()

    bars = alt.Chart(df).mark_bar().encode(
        y=alt.Y("account_name:N", sort=order, title=None),
        x=alt.X("delta:Q", title="Delta vs. prior period"),
        color=alt.Color("priority:N", scale=_PRIORITY_SCALE, legend=alt.Legend(title="Priority")),
        tooltip=[
            alt.Tooltip("account_name:N", title="Account"),
            alt.Tooltip("delta:Q", title="Delta", format="+,.2f"),
            alt.Tooltip("priority:N", title="Priority"),
        ],
    )
    layers = [bars] + _signed_value_labels(df, "account_name", "delta", order)
    return alt.layer(*layers).properties(title=title)


def movement_vs_gate_chart(movements: pd.DataFrame, threshold: float):
    """Every account's delta against the materiality gate, for a period where
    nothing cleared it.

    A quiet close used to render as a five-row table under a paragraph of
    explanation, which reads like the agent found nothing to say. Charting the
    movements against a dashed rule at +/- `threshold` answers the reader's
    actual question -- "how close was anything to mattering?" -- and the answer
    is visible in one glance instead of arithmetic across two columns.

    `movements` needs columns `account_name` and `delta`. Returns `None` when
    there is nothing to plot.
    """
    if movements is None or movements.empty:
        return None
    df = movements.copy()
    df["direction"] = df["delta"].apply(lambda d: "increase" if d >= 0 else "decrease")
    order = df.sort_values("delta", ascending=False)["account_name"].tolist()
    direction_scale = alt.Scale(domain=["increase", "decrease"], range=[INCREASE_COLOR, DECREASE_COLOR])

    bars = alt.Chart(df).mark_bar(opacity=0.85).encode(
        y=alt.Y("account_name:N", sort=order, title=None),
        x=alt.X("delta:Q", title="Delta vs. prior period"),
        color=alt.Color("direction:N", scale=direction_scale, legend=None),
        tooltip=[
            alt.Tooltip("account_name:N", title="Account"),
            alt.Tooltip("delta:Q", title="Delta", format="+,.2f"),
        ],
    )
    # Both rules, always: the gate is symmetric, and showing only the side the
    # data happens to sit on would imply a one-directional threshold.
    gate = alt.Chart(pd.DataFrame({"gate": [threshold, -threshold]})).mark_rule(
        strokeDash=[6, 4], color=TOTAL_COLOR
    ).encode(x=alt.X("gate:Q"))
    layers = [bars, gate] + _signed_value_labels(df, "account_name", "delta", order)
    return alt.layer(*layers).properties(
        title=f"Account movement vs. the ±{threshold:,.0f} materiality gate (dashed)"
    )


def tie_out_chart(tie_out: pd.DataFrame):
    """Subledger coverage % per account, worst first, with a dashed
    reference rule at 100% -- an under-covered account (a summary-only
    accrual with no matching subledger detail) should visibly pop.

    `tie_out` needs columns `account_name`, `coverage_pct`. Returns `None`
    when there's nothing to plot (no tie-out data for this run).
    """
    if tie_out is None or tie_out.empty:
        return None
    df = tie_out.copy()
    # Chart the UNCOVERED share, not coverage. On a coverage axis the account
    # that needs attention is the one with no bar -- a summary-only accrual at
    # 0% renders as nothing while every reconciled account is a full bar, so
    # the eye is drawn to everything except the problem. Inverted, the worst
    # account is the longest bar and a clean tie-out is an empty chart.
    df["uncovered_pct"] = (100.0 - df["coverage_pct"]).clip(lower=0.0)
    df["status"] = df["coverage_pct"].apply(lambda p: "ok" if p >= 99.9 else "gap")
    order = df.sort_values("uncovered_pct", ascending=False)["account_name"].tolist()
    status_scale = alt.Scale(domain=["ok", "gap"], range=[INCREASE_COLOR, DECREASE_COLOR])

    bars = alt.Chart(df).mark_bar().encode(
        y=alt.Y("account_name:N", sort=order, title=None),
        x=alt.X(
            "uncovered_pct:Q",
            title="Unreconciled share of the summary balance (%)",
            scale=alt.Scale(domain=[0, 100]),
        ),
        color=alt.Color("status:N", scale=status_scale, legend=None),
        tooltip=[
            alt.Tooltip("account_name:N", title="Account"),
            alt.Tooltip("coverage_pct:Q", title="Traced to subledger %", format=".1f"),
            alt.Tooltip("uncovered_pct:Q", title="Unreconciled %", format=".1f"),
        ],
    )
    labels = alt.Chart(df[df["uncovered_pct"] > 0]).mark_text(align="left", dx=3).encode(
        y=alt.Y("account_name:N", sort=order),
        x=alt.X("uncovered_pct:Q"),
        text=alt.Text("uncovered_pct:Q", format=".0f"),
    )
    return (bars + labels).properties(title="Subledger tie-out — unreconciled share")


def queue_composition_chart(counts: pd.DataFrame):
    """Compact stacked horizontal bar of the whole AP queue, split by agent
    recommendation. This renders in the sidebar next to the full queue
    table, so it stays small and skips axes rather than repeating detail
    the table already shows.

    `counts` needs columns `recommendation` ("hold"/"review"/"approve") and
    `count` (int), one row per recommendation. Returns `None` when the
    frame is empty/None or every count is zero (nothing queued).
    """
    if counts is None or counts.empty or counts["count"].sum() == 0:
        return None
    return (
        alt.Chart(counts)
        .mark_bar()
        .encode(
            x=alt.X("count:Q", stack="zero", title=None, axis=None),
            color=alt.Color(
                "recommendation:N",
                scale=_RECOMMENDATION_SCALE,
                legend=alt.Legend(title=None, orient="bottom", columns=3),
            ),
            tooltip=[
                alt.Tooltip("recommendation:N", title="Recommendation"),
                alt.Tooltip("count:Q", title="Count"),
            ],
        )
        .properties(height=40, title="Queue by agent recommendation")
    )


def invoice_vs_po_chart(invoice_amt, po_amt, tolerance, status):
    """Two-bar comparison of an invoice's amount against its purchase
    order, with dashed rules marking the tolerance band around the PO.

    `status` is caller-supplied -- either "within tolerance" or "outside
    tolerance" -- and this function does not recompute that boolean. The
    caller already owns the tolerance logic; re-deriving it here is how the
    chart and the table it sits next to end up disagreeing. Both tolerance
    rules are always drawn, at `po_amt + tolerance` and `po_amt -
    tolerance`, because the tolerance is symmetric -- the same dashed-gate
    idiom `movement_vs_gate_chart` uses, so a dashed line means "threshold"
    everywhere in the app.

    Returns `None` if `po_amt` is `None` or `invoice_amt` isn't an
    int/float.
    """
    if po_amt is None or not isinstance(invoice_amt, (int, float)):
        return None
    df = pd.DataFrame(
        [
            {"source": "Invoice", "amount": invoice_amt, "kind": status},
            {"source": "Purchase order", "amount": po_amt, "kind": "reference"},
        ]
    )
    order = ["Invoice", "Purchase order"]
    kind_scale = alt.Scale(
        domain=["within tolerance", "outside tolerance", "reference"],
        range=[INCREASE_COLOR, DECREASE_COLOR, TOTAL_COLOR],
    )
    bars = alt.Chart(df).mark_bar(size=30).encode(
        y=alt.Y("source:N", sort=order, title=None),
        x=alt.X("amount:Q", title="Amount"),
        color=alt.Color("kind:N", scale=kind_scale, legend=None),
        tooltip=[
            alt.Tooltip("source:N", title="Source"),
            alt.Tooltip("amount:Q", title="Amount", format=",.2f"),
        ],
    )
    labels = alt.Chart(df).mark_text(align="left", dx=3).encode(
        y=alt.Y("source:N", sort=order),
        x=alt.X("amount:Q"),
        text=alt.Text("amount:Q", format=",.2f"),
    )
    gate = (
        alt.Chart(pd.DataFrame({"gate": [po_amt + tolerance, po_amt - tolerance]}))
        .mark_rule(strokeDash=[6, 4], color=TOTAL_COLOR)
        .encode(x=alt.X("gate:Q"))
    )
    return (bars + gate + labels).properties(
        title=f"Invoice vs. purchase order (±{tolerance:,.0f} tolerance, dashed)"
    )


def match_status_chart(rows: pd.DataFrame, attribute_order: list, source_order: list = None):
    """Grid of the three-way match result: one cell per (attribute, source)
    pair, colored by match status.

    `rows` needs columns `attribute` (str), `source` (str), and `status`
    ("match"/"mismatch"/"not available"). A `mark_text` layer renders the
    status as "OK" / "X" / "—" on top of the color grid -- a redundant,
    non-color encoding so the grid stays readable without relying on color
    perception. That glyph is derived into a new column in-function; its
    text color is left unset (themed), same as everywhere else in this
    module.

    `source_order` fixes the column order. A three-way match reads
    invoice -> purchase order -> bank feed, which is the order the documents
    are reconciled in; left to itself Vega sorts them alphabetically and puts
    the bank feed first, which reads as though the payment came before the
    invoice. Labels are held horizontal for the same reason -- rotated column
    headers on a three-column grid cost legibility for no space saved.

    Returns `None` when `rows` is empty or `None`.
    """
    if rows is None or rows.empty:
        return None
    df = rows.copy()
    glyph = {"match": "OK", "mismatch": "X", "not available": "—"}
    df["glyph"] = df["status"].map(glyph)
    x_sort = source_order if source_order else alt.Undefined

    cells = alt.Chart(df).mark_rect().encode(
        x=alt.X("source:N", title=None, sort=x_sort, axis=alt.Axis(labelAngle=0)),
        y=alt.Y("attribute:N", sort=attribute_order, title=None),
        color=alt.Color(
            "status:N", scale=_MATCH_SCALE, legend=alt.Legend(title=None, orient="bottom")
        ),
        tooltip=[
            alt.Tooltip("attribute:N", title="Attribute"),
            alt.Tooltip("source:N", title="Source"),
            alt.Tooltip("status:N", title="Status"),
        ],
    )
    labels = alt.Chart(df).mark_text().encode(
        x=alt.X("source:N", sort=x_sort),
        y=alt.Y("attribute:N", sort=attribute_order),
        text=alt.Text("glyph:N"),
    )
    return (cells + labels).properties(title="Three-way match")


def exception_severity_chart(exceptions: pd.DataFrame):
    """Horizontal bars of control exceptions ranked by severity.

    `exceptions` needs columns `code` (str), `severity` (str), `rank` (int,
    4=critical down to 1=low), and an optional `detail` column shown in the
    tooltip when present. The x-axis is drawn on the numeric `rank` but
    labeled with the severity names via `labelExpr`, so there is no legend
    -- the axis already names every severity the color encodes, and a
    legend next to it would just repeat that.

    Returns `None` when `exceptions` is empty or `None`.
    """
    if exceptions is None or exceptions.empty:
        return None
    tooltip = [
        alt.Tooltip("code:N", title="Code"),
        alt.Tooltip("severity:N", title="Severity"),
    ]
    if "detail" in exceptions.columns:
        tooltip.append(alt.Tooltip("detail:N", title="Detail"))
    return (
        alt.Chart(exceptions)
        .mark_bar()
        .encode(
            y=alt.Y("code:N", sort=alt.EncodingSortField("rank", order="descending"), title=None),
            x=alt.X(
                "rank:Q",
                title=None,
                scale=alt.Scale(domain=[0, 4]),
                axis=alt.Axis(
                    values=[1, 2, 3, 4],
                    labelExpr="['','low','medium','high','critical'][datum.value]",
                ),
            ),
            color=alt.Color("severity:N", scale=_SEVERITY_SCALE, legend=None),
            tooltip=tooltip,
        )
        .properties(title="Control exceptions by severity")
    )


def action_owner_chart(actions: pd.DataFrame):
    """Horizontal bars of action-plan item counts per owner, stacked by
    priority -- replaces a raw action-plan table so "who has the most P1s"
    is a glance instead of a scan down a column.

    `actions` needs one row per action, with columns `owner` (str) and
    `priority` ("P1"/"P2"/"P3"). `order=alt.Order("priority:N")` keeps P1
    stacked nearest the axis on every bar, so the most urgent load on each
    owner is always the segment closest to the axis and easiest to compare
    across owners.

    Returns `None` when `actions` is empty or `None`.
    """
    if actions is None or actions.empty:
        return None
    return (
        alt.Chart(actions)
        .mark_bar()
        .encode(
            y=alt.Y("owner:N", sort="-x", title=None),
            x=alt.X("count():Q", title="Actions", axis=alt.Axis(tickMinStep=1)),
            color=alt.Color("priority:N", scale=_PRIORITY_SCALE, legend=alt.Legend(title="Priority")),
            order=alt.Order("priority:N"),
            tooltip=[
                alt.Tooltip("owner:N", title="Owner"),
                alt.Tooltip("priority:N", title="Priority"),
                alt.Tooltip("count():Q", title="Actions"),
            ],
        )
        .properties(title="Action plan — who owns what, by priority")
    )


def recurring_drivers_chart(edges: pd.DataFrame):
    """Recurrence timeline of memory-graph driver edges: one point per
    (driver, period) the driver fired in. This is the visual evidence for
    the app's "intuition compounds across runs" claim, which a flat
    6-column sidebar table was not making.

    `edges` needs columns `driver` (str), `account` (str), `period` (str,
    "YYYY-MM"), `delta` (float), and `share` (float) -- one row per period
    the memory graph has recorded a driver firing. Drivers are sorted
    top-to-bottom by the number of distinct periods they've been seen in,
    descending (computed in-function), so the most persistent drivers
    anchor the top. Point size encodes `abs_share` (`share` magnitude,
    computed in-function since a size scale needs a non-negative field);
    color encodes `direction`, derived from `delta >= 0` -- the same idiom
    `movement_vs_gate_chart` uses for its bar direction.

    The tooltip reports that same magnitude, not the raw signed `share`.
    The memory graph stores `share` as driver delta over ACCOUNT delta, so
    a driver that rose while its account fell carries a negative share --
    and a tooltip reading "direction: increase, Delta: +1,405, Share: -2%"
    just looks broken. Magnitude is what the point size already encodes;
    direction is carried by the color and the signed delta beside it.

    Returns `None` when `edges` is empty or `None`.
    """
    if edges is None or edges.empty:
        return None
    df = edges.copy()
    df["abs_share"] = df["share"].abs()
    df["direction"] = df["delta"].apply(lambda d: "increase" if d >= 0 else "decrease")
    order = df.groupby("driver")["period"].nunique().sort_values(ascending=False).index.tolist()
    direction_scale = alt.Scale(domain=["increase", "decrease"], range=[INCREASE_COLOR, DECREASE_COLOR])

    return (
        alt.Chart(df)
        .mark_circle(opacity=0.85)
        .encode(
            x=alt.X("period:O", title=None, axis=alt.Axis(labelAngle=-45)),
            y=alt.Y("driver:N", sort=order, title=None),
            size=alt.Size("abs_share:Q", scale=alt.Scale(range=[30, 400]), legend=None),
            color=alt.Color("direction:N", scale=direction_scale, legend=alt.Legend(title=None)),
            tooltip=[
                alt.Tooltip("driver:N", title="Driver"),
                alt.Tooltip("account:N", title="Account"),
                alt.Tooltip("period:O", title="Period"),
                alt.Tooltip("delta:Q", title="Delta", format="+,.2f"),
                alt.Tooltip("abs_share:Q", title="Share of account move", format=".0%"),
            ],
        )
        .properties(title="Recurring drivers — every period the memory graph has seen them fire")
    )


def driver_share_chart(drivers: pd.DataFrame, account_delta: float):
    """Compact per-finding concentration strip: one normalized stacked bar
    showing what share of the account's move each driver accounts for.

    `drivers` needs the same columns `driver_contribution_chart` takes --
    `member_name`, `delta`, `cohort` -- one row per driver for a single
    finding. Only the top few drivers are ever passed in, so they rarely
    account for the whole move; when the unexplained remainder exceeds a
    0.01 tolerance, one `"Other"` row is appended holding it -- the same
    "fold the gap into one step" reasoning `bridge_chart` documents for its
    residual step. That row carries its own `delta` so the tooltip reads a
    real number rather than a blank.

    The bar is sized by each driver's share of the total *absolute*
    movement, not by `delta / account_delta`. Drivers routinely run in both
    directions at once (a cohort expanding while another churns), and a
    signed share fed to `stack="normalize"` splits into competing positive
    and negative stacks that no longer read as "share of the move" -- with
    offsetting drivers a single member can even exceed 100%. Magnitude
    share is the question this strip actually answers: how concentrated was
    the move, and in whom. Direction is not lost -- it stays in the cohort
    color and in the signed `delta` tooltip.

    Returns `None` if `drivers` is empty/None, or if `account_delta` is
    `None` or zero, or if every driver delta is zero (nothing to apportion).
    """
    if drivers is None or drivers.empty:
        return None
    if account_delta is None or account_delta == 0:
        return None
    df = drivers.copy()
    residual = float(account_delta) - float(df["delta"].sum())
    if abs(residual) > abs(float(account_delta)) * 0.01:
        other = pd.DataFrame(
            [{"member_name": "Other drivers", "delta": residual, "cohort": "other"}]
        )
        df = pd.concat([df, other], ignore_index=True)
    df["magnitude"] = df["delta"].abs()
    total_magnitude = df["magnitude"].sum()
    if total_magnitude == 0:
        return None
    df["share"] = df["magnitude"] / total_magnitude
    cohort_scale = alt.Scale(
        domain=list(COHORT_COLORS) + ["other"],
        range=list(COHORT_COLORS.values()) + ["#95a5a6"],
    )
    return (
        alt.Chart(df)
        .mark_bar()
        .encode(
            x=alt.X("share:Q", stack="normalize", axis=alt.Axis(format="%"), title=None),
            color=alt.Color("cohort:N", scale=cohort_scale, legend=alt.Legend(title="Cohort")),
            order=alt.Order("share:Q", sort="descending"),
            tooltip=[
                alt.Tooltip("member_name:N", title="Driver"),
                alt.Tooltip("share:Q", title="Share of movement", format=".0%"),
                alt.Tooltip("delta:Q", title="Delta", format="+,.2f"),
            ],
        )
        .properties(height=42, title="Concentration — share of the account's move")
    )
