"""Pure Altair chart builders for the flux (variance-explanation) UI.

Every function here takes plain pandas DataFrames and returns an Altair
chart object, or `None` when there is nothing sensible to draw -- no
`st.*` calls anywhere below, so these are testable without a running
Streamlit process and are shared by both the live-upload view (fed from
the in-memory `state` dict) and the seeded-briefs view (fed from the
already-written `.xlsx` workpaper's sheets). `ui/streamlit_app.py` owns
adapting each view's data into the DataFrame shapes documented per
function; this module only draws.

Cohort and priority each get one fixed color scale, defined once here, so
the same color means the same thing on every chart in the app. Nothing
below sets an explicit background or text color -- Streamlit re-themes
Altair charts for light/dark automatically, and hardcoding either would
break that.
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
INCREASE_COLOR = "#27ae60"
DECREASE_COLOR = "#c0392b"
TOTAL_COLOR = "#34495e"

_COHORT_SCALE = alt.Scale(domain=list(COHORT_COLORS), range=list(COHORT_COLORS.values()))
_PRIORITY_SCALE = alt.Scale(domain=list(PRIORITY_COLORS), range=list(PRIORITY_COLORS.values()))
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


def account_movement_chart(findings: pd.DataFrame):
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
    return alt.layer(*layers).properties(title="Account movement overview")


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
    df["status"] = df["coverage_pct"].apply(lambda p: "ok" if p >= 99.9 else "gap")
    order = df.sort_values("coverage_pct", ascending=True)["account_name"].tolist()
    status_scale = alt.Scale(domain=["ok", "gap"], range=[INCREASE_COLOR, DECREASE_COLOR])
    max_x = max(100.0, float(df["coverage_pct"].max()))

    bars = alt.Chart(df).mark_bar().encode(
        y=alt.Y("account_name:N", sort=order, title=None),
        x=alt.X("coverage_pct:Q", title="Subledger coverage (%)", scale=alt.Scale(domain=[0, max_x])),
        color=alt.Color("status:N", scale=status_scale, legend=None),
        tooltip=[
            alt.Tooltip("account_name:N", title="Account"),
            alt.Tooltip("coverage_pct:Q", title="Coverage %", format=".1f"),
        ],
    )
    labels = alt.Chart(df).mark_text(align="left", dx=3).encode(
        y=alt.Y("account_name:N", sort=order),
        x=alt.X("coverage_pct:Q"),
        text=alt.Text("coverage_pct:Q", format=".0f"),
    )
    rule = alt.Chart(pd.DataFrame({"x": [100]})).mark_rule(strokeDash=[4, 4]).encode(x=alt.X("x:Q"))
    return (bars + rule + labels).properties(title="Subledger tie-out coverage")
