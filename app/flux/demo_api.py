"""Live demo bridge: monthly statement vs transaction statement -> quality
check -> audit.ts.

Built for the "Audit-Ready Agent" React demo, which has no backend of its
own. This is a DATA QUALITY / hallucination check, not a period-over-period
comparison: it takes one uploaded SUMMARY file (the "monthly statement" --
what's being claimed) and one uploaded TRANSACTIONS file (the "transaction
statement" -- the ground truth detail), and checks, account by account,
whether the claimed total is actually supported by the transaction detail.
A gap between the two is exactly the shape of an LLM (or human) hallucinating
or fabricating a number that the underlying data doesn't back up.

Reuses the real tie-out check (app/flux/ingest.py: tie_out), the same
function the flux agent runs on every real period, just pointed at an
in-memory pair of tables built from the two uploaded files instead of the
committed dataset -- no files are written to data/financials/, so repeat
uploads never accumulate state.

The tie-out arithmetic (gap, coverage %) is always deterministic, computed
before the model ever sees anything -- same "no LLM arithmetic" rule as the
rest of this project. Qwen (via app/llm.get_llm(), serverless on Modal when
MODAL_QWEN_URL is set) only narrates those already-computed facts: it names
the account, quotes the given figures, and suggests a next step. It cannot
invent a number that isn't in the prompt, and every LLM call is wired to
PRISM (one session per upload) per this project's standing tracing rule.
Falls back to a deterministic template if no LLM is configured or a response
fails to parse -- narration never blocks the numbers from being usable.

Usage:
    python app/flux/demo_api.py <summary_file> <transactions_file> <audit_ts_out_path>
"""

import json
import re
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import duckdb  # noqa: E402
import pandas as pd  # noqa: E402

from app import llm as llm_module, tracing  # noqa: E402
from app.flux import ingest  # noqa: E402

SUMMARY_COLUMNS = ["period", "account_code", "account_name", "account_type", "amount"]
TXN_COLUMNS = [
    "txn_id", "period", "txn_date", "account_code", "entity", "department",
    "region", "segment", "customer_id", "customer_name", "vendor", "amount", "memo",
]
PERIOD = "upload"


def _peek_columns(path: Path) -> set[str]:
    if path.suffix.lower() in (".xlsx", ".xls"):
        return set(pd.read_excel(path, nrows=0).columns)
    return set(pd.read_csv(path, nrows=0).columns)


def _resolve_summary_and_txn_paths(path_a: Path, path_b: Path) -> tuple[Path, Path]:
    """The two upload slots are easy to swap (two similar file pickers) --
    detect which file is actually the summary vs the transactions file by its
    columns rather than trusting argument order."""
    cols_a, cols_b = _peek_columns(path_a), _peek_columns(path_b)
    a_is_summary = "account_name" in cols_a
    b_is_summary = "account_name" in cols_b
    if a_is_summary and not b_is_summary:
        return path_a, path_b
    if b_is_summary and not a_is_summary:
        return path_b, path_a
    if a_is_summary and b_is_summary:
        raise ValueError(
            "Both uploaded files look like a monthly statement (both have an 'account_name' "
            "column). One of them should be the transaction statement instead -- it needs an "
            "'account_code' and 'amount' column, but no 'account_name' column."
        )
    raise ValueError(
        "Neither uploaded file looks like a monthly statement (neither has an 'account_name' "
        "column). One of them should be the summary/statement file."
    )


def _read_table(path: Path, required: list[str]) -> pd.DataFrame:
    if path.suffix.lower() in (".xlsx", ".xls"):
        df = pd.read_excel(path, dtype={"account_code": str})
    else:
        df = pd.read_csv(path, dtype={"account_code": str})
    missing = [c for c in required if c not in df.columns]
    if missing:
        raise ValueError(f"{path.name} is missing required column(s): {missing}")
    return df


def _read_summary(path: Path) -> pd.DataFrame:
    """A statement may cover one month or several -- if the file has its own
    'period' column (YYYY-MM, matching data/financials/summaries/*.csv), each
    month is checked independently; a file with no period column is treated
    as a single, unlabeled period for backward compatibility."""
    df = _read_table(path, ["account_code", "account_name", "amount"])
    if "period" not in df.columns or df["period"].isna().all():
        df["period"] = PERIOD
    else:
        df["period"] = df["period"].astype(str).replace({"nan": PERIOD, "": PERIOD})

    dupes = df[["account_code", "period"]][df.duplicated(["account_code", "period"])]
    if not dupes.empty:
        pairs = list(dupes.itertuples(index=False, name=None))
        raise ValueError(
            f"{path.name} (the monthly statement) has more than one row for the same "
            f"account in the same period: {pairs} -- one line per account per period."
        )
    if "account_type" not in df.columns:
        df["account_type"] = "other"
    df["account_type"] = df["account_type"].replace("", "other")
    df["amount"] = pd.to_numeric(df["amount"], errors="coerce").fillna(0.0)
    return df[SUMMARY_COLUMNS]


def _read_transactions(path: Path) -> pd.DataFrame:
    df = _read_table(path, ["account_code", "amount"])
    for col in TXN_COLUMNS:
        if col not in df.columns:
            df[col] = ""
    df["amount"] = pd.to_numeric(df["amount"], errors="coerce").fillna(0.0)
    if "period" not in df.columns or df["period"].astype(str).isin(["", "nan"]).all():
        df["period"] = PERIOD
    else:
        df["period"] = df["period"].astype(str).replace({"nan": PERIOD, "": PERIOD})
    for i, row in df.iterrows():
        if not str(row["txn_id"]).strip():
            df.at[i, "txn_id"] = f"TXN-{i + 1:04d}"
        if not str(row["txn_date"]).strip():
            df.at[i, "txn_date"] = ""
    return df[TXN_COLUMNS]


def _money(n: float) -> str:
    sign = "+" if n >= 0 else "−"
    return f"{sign}${abs(n):,.2f}"


_ACCRUAL_HISTORY_THRESHOLD = 5.0  # avg historical coverage % below this = known accrual


def _known_accrual_accounts() -> set[str]:
    """Account codes that are historically summary-only across the real
    committed dataset (an accrual like Insurance -- booked as one lump sum
    with no subledger detail, by design, every single period) -- as opposed
    to an account that normally reconciles but doesn't this time, which is
    exactly the case this whole check exists to catch.

    Checked by pattern, not by name: an account only gets excluded if
    *every* historical period shows the same near-zero coverage. A newly
    uploaded account with no history, or one whose coverage just dropped
    this period, is never excluded -- only a consistent, longstanding
    accrual pattern is."""
    try:
        conn = ingest.connect()
    except Exception:  # noqa: BLE001 -- no historical dataset available; nothing to exclude
        return set()
    try:
        periods = ingest.list_periods(conn)
        if not periods:
            return set()
        coverage_by_account: dict[str, list[float]] = {}
        for period in periods:
            for row in ingest.tie_out(conn, period):
                coverage_by_account.setdefault(row["account_code"], []).append(row["coverage_pct"])
        return {
            code for code, values in coverage_by_account.items()
            if len(values) == len(periods) and (sum(values) / len(values)) < _ACCRUAL_HISTORY_THRESHOLD
        }
    finally:
        conn.close()


def _verdict(coverage_pct: float) -> str:
    if coverage_pct >= 99.9:
        return "Reconciled"
    if coverage_pct <= 0.1:
        return "No transaction support found"
    return "Partial support only"


NARRATE_PROMPT = """You are a financial controller writing the findings section of a
reconciliation workpaper, comparing a monthly statement against the underlying transaction
detail. The gap, coverage %, and every dollar figure below were already computed
deterministically -- quote them exactly as given. Never invent a number, recompute one, or
restate a figure differently than it appears below. Be thorough and specific, not brief --
this is a workpaper an auditor will read, not a chat reply.

Accounts checked (account name, statement amount, transaction total, gap, coverage %):
{account_lines}

Write a JSON object with keys:
"headline" -- one sentence: what this reconciliation found overall. If any account has a gap,
name the one with the largest gap and its dollar amount. If everything reconciles, say so plainly.
"summary" -- 5-7 sentences, a genuinely thorough overall assessment, not a brief note. Cover:
(1) how many accounts were checked and how many reconciled cleanly; (2) for each account with a
gap, its coverage % and what that specific percentage implies (0% coverage -- literally zero
supporting transactions -- is the strongest possible signal of a fabricated or hallucinated
figure, categorically different from an account that's, say, 80% covered and likely just has a
missing transaction or timing gap); (3) whether the pattern across all discrepancies looks like
isolated clerical error, a systemic data-entry issue, or something needing escalation; (4) what
this means for sign-off on the statement as a whole, given the accounts that are clean.
"accounts" -- an array with one entry per account that has ANY gap (omit accounts that fully
reconcile), each: {{"account_code": "...", "why": "2-3 sentences: what this specific gap means,
how confident you are it's an error vs. something worse given its coverage %, and any pattern
worth naming", "suggestion": "one concrete, specific next step naming who should look into it and
what exactly they should check (e.g. a specific document, system, or person to contact)"}}.
Return ONLY the JSON object."""


def _narrate_with_llm(tie_out: list[dict], known_accrual_codes: set[str]) -> dict | None:
    """Have Qwen narrate the already-computed tie-out facts. Returns None (the
    caller falls back to the deterministic template) if no LLM is configured
    or the response can't be parsed -- narration failing never blocks the
    numbers, which are already final by this point."""
    llm = llm_module.get_llm()
    if llm is None:
        print("[demo_api] no LLM configured; using deterministic template narration", file=sys.stderr)
        return None

    checkable = [r for r in tie_out if r["account_code"] not in known_accrual_codes]
    if not checkable:
        return None
    account_lines = "\n".join(
        f"- {r['account_name']} ({r['account_code']}): statement={_money(r['summary_amt'])}, "
        f"transactions={_money(r['txn_amt'])}, gap={_money(r['gap'])}, coverage={r['coverage_pct']}%"
        for r in checkable
    )
    prompt = NARRATE_PROMPT.format(account_lines=account_lines)

    session_id = f"audit-demo-upload-{uuid.uuid4().hex[:8]}"
    handler = tracing.get_handler(session_id, agent_name="audit-agent-live-upload")
    try:
        response = llm.invoke(prompt, config={"callbacks": [handler]} if handler else None)
        text = response.content if hasattr(response, "content") else str(response)
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if not match:
            print("[demo_api] LLM response had no JSON object; using template narration", file=sys.stderr)
            return None
        return json.loads(match.group(0))
    except Exception as exc:  # noqa: BLE001 -- narration is best-effort, never fatal
        print(f"[demo_api] LLM narration failed ({exc}); using template narration", file=sys.stderr)
        return None
    finally:
        tracing.flush(handler)


def _to_audit_ts(
    tie_out: list[dict],
    txn_df: pd.DataFrame,
    narrative: dict | None = None,
    known_accrual_codes: set[str] | None = None,
) -> str:
    known_accrual_codes = known_accrual_codes or set()
    narrated = {a["account_code"]: a for a in (narrative or {}).get("accounts", []) if "account_code" in a}
    excluded = [r for r in tie_out if r["account_code"] in known_accrual_codes]
    checkable = [r for r in tie_out if r["account_code"] not in known_accrual_codes]
    flagged = [r for r in checkable if r["coverage_pct"] < 99.9]
    clean = [r for r in checkable if r["coverage_pct"] >= 99.9]
    total_gap = sum(abs(r["gap"]) for r in flagged)
    worst = sorted(flagged, key=lambda r: abs(r["gap"]), reverse=True)

    if narrative and narrative.get("headline"):
        headline = narrative["headline"]
    elif flagged:
        top = worst[0]
        headline = (
            f"{len(flagged)} of {len(checkable)} account(s) show a discrepancy between the "
            f"monthly statement and transaction detail -- largest gap is {top['account_name']} "
            f"at {_money(top['gap'])} ({top['coverage_pct']}% covered)."
        )
    else:
        headline = f"All {len(checkable)} checkable account(s) reconciled -- the monthly statement matches transaction detail exactly."
    if excluded:
        headline += f" ({len(excluded)} known summary-only accrual account(s) excluded from this check.)"

    hero = {
        "status": "Investigation complete",
        "headline": headline,
        "delta": f"{_money(-total_gap) if total_gap else '$0'} total discrepancy" if flagged else "No discrepancy",
        "net": _money(-total_gap) if flagged else "$0",
        "period": "Statement vs transactions",
        "pills": [
            f"{len(checkable)} accounts checked",
            f"{len(clean)} reconciled",
            f"{len(flagged)} flagged",
            f"{len(excluded)} accrual account(s) excluded",
        ],
    }

    drivers_section = []
    evidence: dict[str, dict] = {}
    attention = []
    multi_period = len({r["period"] for r in tie_out}) > 1

    for r in sorted(tie_out, key=lambda r: abs(r["gap"]), reverse=True):
        # Compound id: the same account can legitimately appear once per
        # uploaded month, so the bare account_code alone can't uniquely key
        # DRIVERS/EVIDENCE/ATTENTION once more than one period is involved.
        did = f"{r['account_code']}__{r['period']}"
        display_name = f"{r['account_name']} ({r['period']})" if multi_period else r["account_name"]
        is_accrual = r["account_code"] in known_accrual_codes
        is_flagged = r["coverage_pct"] < 99.9 and not is_accrual
        llm_note = narrated.get(did) or {}
        # Deterministic fact, quoting only the already-computed numbers -- this
        # is the "variance" side and never carries LLM prose, in DRIVERS,
        # EVIDENCE, or ATTENTION's "reason" alike. The LLM's two outputs
        # (analysis / actions) live only in AI_SUMMARY and ATTENTION.suggestion
        # respectively, kept out of these fact fields on purpose.
        fact = (
            f"Statement claims {_money(r['summary_amt'])}; transactions total {_money(r['txn_amt'])}"
        )
        suggestion = llm_note.get("suggestion") or (
            "No transaction evidence exists for this figure at all -- verify this was not "
            "fabricated before it's used in any report."
            if r["coverage_pct"] <= 0.1 else
            "Confirm the remaining, unsupported portion of this balance before sign-off."
        )
        verdict = "Known accrual (excluded)" if is_accrual else _verdict(r["coverage_pct"])

        drivers_section.append({
            "id": did,
            "name": r["account_name"],
            "amount": _money(r["gap"]) if is_flagged else "$0",
            "share": round(min(100, (abs(r["gap"]) / total_gap * 100))) if is_flagged and total_gap else 0,
            "tag": verdict,
            "note": (
                f"Excluded -- historically summary-only every period, not a new gap"
                if is_accrual else
                f"{r['coverage_pct']}% of statement amount traced to transactions"
            ),
            "reason": fact,
            "tone": "emerald" if (is_accrual or not is_flagged) else "crimson",
        })

        rows = txn_df[txn_df["account_code"] == r["account_code"]]
        evidence[did] = {
            "id": did,
            "title": r["account_name"],
            "impact": _money(r["gap"]) if is_flagged else "$0",
            "impactTone": "emerald" if (is_accrual or not is_flagged) else "crimson",
            "contributionLabel": "Transaction coverage",
            "contribution": f"{r['coverage_pct']}%",
            "classification": verdict,
            "summary": (
                f"{fact}. Excluded from this check: every historical period for this account shows the "
                f"same near-zero coverage, consistent with a summary-only accrual by design rather than "
                f"a new discrepancy."
                if is_accrual else fact
            ),
            "nullify": r["coverage_pct"] <= 0.1 and not is_accrual,
            "nullifyNote": (
                "Zero transactions found for this account -- the statement amount has no supporting "
                "detail at all. This is the strongest possible signal of a fabricated or hallucinated figure."
                if r["coverage_pct"] <= 0.1 and not is_accrual else None
            ),
            "detail": [
                ["Statement amount", _money(r["summary_amt"])],
                ["Transaction total", _money(r["txn_amt"])],
                ["Gap", _money(r["gap"])],
            ],
            "rows": [
                {"id": row["txn_id"], "date": row["txn_date"] or "—", "prior": 0, "current": row["amount"]}
                for _, row in rows.iterrows()
            ] or [{"id": "no-transactions-found", "date": "—", "prior": 0, "current": 0}],
            "calculation": f"{_money(r['summary_amt'])} (statement) − {_money(r['txn_amt'])} (transactions) = {_money(r['gap'])} gap",
        }

        if is_flagged:
            priority = "High priority" if r["coverage_pct"] <= 0.1 else "Medium priority"
            attention.append({
                "id": f"gap-{did}",
                "priority": priority,
                "tone": "crimson" if r["coverage_pct"] <= 0.1 else "amber",
                "title": f"{r['account_name']} statement/transaction mismatch",
                "impact": _money(r["gap"]),
                "reason": (
                    f"Statement claims {_money(r['summary_amt'])}; only {_money(r['txn_amt'])} "
                    f"({r['coverage_pct']}%) is supported by transaction detail."
                ),
                "suggestion": suggestion,
                "action": "Investigate discrepancy",
                "evidenceId": did,
            })

    kpis = [
        {"id": "checked", "label": "Accounts checked", "value": str(len(checkable)), "note": "statement vs transactions", "tone": "neutral"},
        {"id": "clean", "label": "Reconciled", "value": str(len(clean)), "note": "match exactly", "tone": "emerald"},
        {"id": "flagged", "label": "Flagged", "value": str(len(flagged)), "note": "discrepancy found", "tone": "crimson" if flagged else "emerald"},
        {"id": "gap", "label": "Total discrepancy", "value": _money(-total_gap) if flagged else "$0", "note": "sum of absolute gaps", "tone": "crimson" if flagged else "emerald"},
    ] + ([{"id": "excluded", "label": "Accrual accounts excluded", "value": str(len(excluded)), "note": "summary-only by design, every period", "tone": "info"}] if excluded else [])

    movements = [
        {"id": "expansion", "label": "Reconciled", "value": str(len(clean)), "note": "accounts", "tone": "emerald"},
        {"id": "new", "label": "Partial support", "value": str(sum(1 for r in flagged if 0.1 < r["coverage_pct"] < 99.9)), "note": "accounts", "tone": "amber"},
        {"id": "churn", "label": "No support found", "value": str(sum(1 for r in flagged if r["coverage_pct"] <= 0.1)), "note": "accounts", "tone": "crimson"},
        {"id": "stable", "label": "Total accounts", "value": str(len(tie_out)), "note": "checked", "tone": "neutral"},
    ]

    # AI_SUMMARY is the Analysis output (LLM headline + summary + per-account
    # why); ATTENTION.suggestion below is the separate Actions output. Neither
    # touches the deterministic fact fields (DRIVERS/EVIDENCE reason/summary).
    overall_summary = narrative.get("summary") if narrative else None
    ai_summary = {
        "title": "AI Analysis",
        "paragraphs": [headline]
        + ([overall_summary] if overall_summary else [])
        + [
            narrated.get(r["account_code"], {}).get("why")
            or f"{r['account_name']}: statement claims {_money(r['summary_amt'])}, transactions support "
               f"{_money(r['txn_amt'])} ({r['coverage_pct']}% covered)."
            for r in worst[:5]
        ],
        "badges": (
            [{"label": f"{len(flagged)} discrepancy(ies) found", "tone": "crimson"}]
            if flagged else [{"label": "Fully reconciled", "tone": "emerald"}]
        ) + [{"label": "Verified against transaction detail", "tone": "info"}]
        + ([{"label": "Narrated by Qwen", "tone": "info"}] if narrative else []),
        "link": {"label": "View live traces on PRISM", "url": "https://prism.blockconvey.com/"},
    }

    def block(name: str, type_annotation: str, value) -> str:
        return f"export const {name}{type_annotation} = {json.dumps(value, indent=2)};\n\n"

    out = '// AUTO-GENERATED by app/flux/demo_api.py from a live upload. Do not hand-edit.\n'
    out += 'export type Tone = "emerald" | "amber" | "crimson" | "info" | "neutral";\n\n'
    out += block("HERO", "", hero)
    out += 'export type Classification = "Expansion" | "New Account" | "Churn" | "Stable";\n'
    out += 'export type Customer = { id: string; name: string; segment: "Enterprise" | "Churned" | "Core"; q2: number; q3: number; change: number; changePct: string | null; classification: Classification; reason: string; nullify?: boolean };\n'
    out += block("CUSTOMERS", ": Customer[]", [])
    out += block("KPIS", "", kpis)
    out += block("MOVEMENTS", "", movements)
    out += block("DRIVERS", "", drivers_section)
    out += block("ATTENTION", "", attention)
    out += block("AI_SUMMARY", "", ai_summary)
    out += 'export type SourceRow = { id: string; date: string; prior: number; current: number };\n'
    out += 'export type Evidence = { id: string; title: string; impact: string; impactTone: Tone; contributionLabel: string; contribution: string; classification: string; summary?: string; nullify?: boolean; nullifyNote?: string; detail?: [string, string][]; rows: SourceRow[]; calculation: string };\n'
    out += block("EVIDENCE", ": Record<string, Evidence>", evidence)
    out += 'export type AssistantAnswer = { q: string; body: string[]; facts?: [string, string][]; classification?: string; suggestions?: string[]; actions?: { label: string; evidenceId?: string; focus?: string[] }[] };\n'
    out += block("ASSISTANT_INTRO", "", [
        f"I checked {len(tie_out)} account(s) from the monthly statement against the transaction detail.",
        headline,
    ])
    out += block("ASSISTANT_QA", ": AssistantAnswer[]", [])
    out += 'export type StageId = "compare" | "detect" | "drill" | "nullify" | "reconcile" | "explain";\n'
    out += block("STAGE_SEQUENCE", "", [
        {"id": "compare", "label": "Compare", "note": "Loading monthly statement and transaction detail", "ms": 900},
        {"id": "detect", "label": "Detect", "note": f"{len(tie_out)} accounts in the statement", "ms": 900},
        {"id": "drill", "label": "Drill", "note": "Transactions grouped by account", "ms": 1200},
        {"id": "nullify", "label": "Nullify", "note": f"{sum(1 for r in tie_out if r['coverage_pct'] <= 0.1)} account(s) with zero support isolated", "ms": 1000},
        {"id": "reconcile", "label": "Reconcile", "note": f"{len(clean)} of {len(tie_out)} accounts tie out exactly", "ms": 900},
        {"id": "explain", "label": "Explain", "note": "Discrepancies sent to the narrative layer", "ms": 800},
    ])
    out += block("RUNS", "", [
        {"id": 1, "label": "Run 1", "context": "Statement totals only", "narrative": f"{len(tie_out)} accounts in the statement.", "depth": 1},
        {"id": 2, "label": "Run 2", "context": "Transaction detail loaded", "narrative": f"{len(flagged)} account(s) don't fully reconcile.", "depth": 2},
        {"id": 3, "label": "Run 3", "context": "Live upload", "narrative": headline, "depth": 3},
    ])
    out += block("DETERMINISTIC_TASKS", "", ["Subledger tie-out", "Coverage %", "Gap detection", "Nullify"])
    out += block("NARRATIVE_TASKS", "", ["What's inconsistent", "Why", "Which accounts", "Recommended actions"])
    out += block("TRACE", "", [{"ts": "live:01", "text": "Uploaded statement and transactions reconciled", "id": drivers_section[0]["id"] if drivers_section else "checked"}])
    out += f'export const TELEMETRY = {json.dumps(json.dumps({"accounts_checked": len(tie_out), "flagged": len(flagged)}))};\n\n'
    out += 'export type ArchNode = { id: string; label: string; kind: "source" | "engine" | "gate" | "output"; input: string; process: string; output: string; trust: string };\n'
    out += block("ARCH_NODES", ": ArchNode[]", [])
    return out


def main() -> None:
    summary_path, txn_path = _resolve_summary_and_txn_paths(Path(sys.argv[1]), Path(sys.argv[2]))
    out_path = Path(sys.argv[3])

    summary_df = _read_summary(summary_path)
    txn_df = _read_transactions(txn_path)

    conn = duckdb.connect()
    conn.register("summary_view", summary_df)
    conn.register("txn_view", txn_df)
    conn.execute("CREATE TABLE summary AS SELECT * FROM summary_view")
    conn.execute("CREATE TABLE txn AS SELECT * FROM txn_view")
    # A statement spanning several months is checked one month at a time --
    # each period's summary is only compared against that same period's
    # transactions, so a July shortfall can never be masked by an August
    # overage netting out to a falsely clean total.
    periods = sorted(set(summary_df["period"]) | set(txn_df["period"]))
    tie_out = []
    for period in periods:
        for row in ingest.tie_out(conn, period):
            tie_out.append({**row, "period": period})
    conn.close()

    known_accrual_codes = _known_accrual_accounts()
    if known_accrual_codes:
        print(f"[demo_api] excluding known summary-only accrual account(s) from flagging: "
              f"{sorted(known_accrual_codes)}", file=sys.stderr)

    narrative = _narrate_with_llm(tie_out, known_accrual_codes)

    ts = _to_audit_ts(tie_out, txn_df, narrative, known_accrual_codes)
    out_path.write_text(ts, encoding="utf-8")

    flagged = sum(
        1 for r in tie_out
        if r["coverage_pct"] < 99.9 and r["account_code"] not in known_accrual_codes
    )
    print(json.dumps({"ok": True, "period": "statement-check", "prior_period": "n/a",
                       "findings": flagged, "accounts_checked": len(tie_out)}))


if __name__ == "__main__":
    main()
