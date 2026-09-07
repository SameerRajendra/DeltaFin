# DeltaFin — an agentic variance-explanation system

Monthly account summaries and transaction-level CSVs go in; a prioritized,
owned variance brief comes out. The bar it is built to clear is the move from
*"Revenue increased 18%"* to *"Revenue increased 18%, driven by a 32% increase
in enterprise accounts, with three customers accounting for 64% of the
increase"* — and to get sharper the more months it has been run against, not
just once.

## What it actually produces

Real excerpt from [`examples/flux_2026-07_to_2026-08_e3e64975.md`](examples/flux_2026-07_to_2026-08_e3e64975.md),
unedited:

```markdown
## Recommended actions

| Priority | Account | Task | Owner |
|---|---|---|---|
| P1 · Act this week | Insurance | Confirm the Insurance accrual: only 0.0% traced to subledger detail (gap $5,000.00). | Controller / Accounting |
| P2 · Review this close cycle | Enterprise Revenue | Ask Sales Ops to confirm whether the expansion at Globex Corp, Initech Holdings, and Stark Industries is a contracted run-rate or a one-time upsell before it's forecast forward. | Sales Ops |
| P2 · Review this close cycle | Hosting COGS | Ask Vendor Management to review the cost structure of CloudBeam Compute to identify any potential inefficiencies or negotiate better pricing terms. | Vendor Management |

### Enterprise Revenue (4000)

**Enterprise Revenue increased 32%, primarily driven by growth at 3 customers
accounting for 71% of the increase, with expansion at Globex Corp, Initech
Holdings, and Stark Industries.**

Delta: $+96,000.00 (+32.0%) · materiality: absolute-threshold · confidence: high

| Driver | Delta | Cohort | Share of account delta |
|---|---|---|---|
| Globex Corp | +30,000.00 | expansion | 31% |
| Initech Holdings | +20,000.00 | expansion | 21% |
| Stark Industries | +18,000.00 | expansion | 19% |
| Wayne Enterprises | +15,000.00 | expansion | 16% |
| Umbrella Group | +13,000.00 | expansion | 14% |
```

The P1 row is the one worth pausing on: **the Insurance accrual is never a
variance at all.** Nothing moved month over month, so no materiality gate fires
on it. It surfaces because ingestion also ties the subledger out against the
summary, and an accrual with 0% traced detail is an audit risk whether or not
the number changed. `compile_action_plan` folds tie-out gaps into the same
prioritized list as findings.

`examples/` also holds the matching `.xlsx` workpaper (`Summary` / `Actions` /
`Findings` / `Drivers` / `Tie-Out` sheets) and a trimmed excerpt of the
cross-run memory graph.

## What this demonstrates

- **A hard deterministic/LLM boundary, drawn where finance needs it.**
  `app/flux/variance.py` computes every delta, percentage, z-score against a
  6-month trailing baseline, and materiality decision; `app/flux/slicer.py`
  computes every driver, cohort label, and concentration share;
  `app/flux/actions.py` assigns every priority and owner. The model is handed
  those already-computed facts and asked only to write prose over them. Nothing
  it emits changes a number — and `explain_drivers` / `synthesize_brief` both
  fall back to a driver-table-derived template when no endpoint is configured
  or a response fails to parse, so the pipeline has no hard dependency on the
  model at all.
- **Cross-run memory shaped like the task, not like a chat log.**
  `app/flux/memory.py` keeps `account_code::driver_key` edges (`5000::CloudBeam
  Compute`) in a JSON graph, so "has this exact vendor driven this exact account
  before, and for how many consecutive periods" is an O(1) lookup on an exact
  key — not a similarity search over past narratives. That is what lets a
  headline say *recurring* instead of re-deriving the streak from history every
  run. An append-only SQLite table alongside it is the system of record for what
  was said and when.
- **A closed analyst-feedback loop.** The Streamlit flux page
  (`ui/streamlit_app.py`) resolves the displayed findings to their real SQLite
  ids via `memory.findings_for_run`, renders a Confirm / Off-base control per
  finding, and calls `memory.record_feedback` — which writes a `feedback` row
  and stamps `last_verdict` / `last_note` onto that account's graph edges.
  `memory.recall()` reads those two fields, so the next run's `explain_drivers`
  prompt carries the analyst's prior verdict. (`ARCHITECTURE.md` documents
  where this loop is currently too coarse.)
- **Self-hosted serving, not a hosted API key.** `modal_app/qwen_reasoner.py`
  runs Qwen2.5-7B-Instruct under vLLM behind an OpenAI-compatible endpoint on
  Modal, on an A10G, with no `min_containers` — it scales to zero between runs
  and pays a cold start on the first call after `scaledown_window=180`. Weights
  live in a Modal Volume so a cold start re-downloads nothing. `app/llm.py`
  speaks to it as the only provider.
- **Columnar ingestion under an explicit memory ceiling.** `app/flux/ingest.py`
  uses DuckDB to `read_csv_auto` two file globs — 20 periods of summaries and
  subledgers — straight into typed in-memory tables, with a `_MEMORY_LADDER`
  probe because DuckDB's auto-detected limit OOMs on the multi-file glob. All
  the driver decomposition is then SQL against those tables. The AP pipeline's
  semantic layer (`app/store.py`) is a separate SQLite database standing in for
  ERP / bank feed / vendor master.

## Architecture

```
  data/financials/            ┌──────────────────────────────────────────────┐
    summaries/*.csv           │      LangGraph  (app/flux/graph.py)          │
    transactions/*.csv  ─────►│                                              │
                              │  ingest_periods ──────► DuckDB (ingest.py)   │
                              │        ↓                 + subledger tie-out │
                              │  compute_variances ────► variance.py         │
                              │        ↓                 delta / % / z-score │
                              │  rank_materiality ─────► materiality gate    │
                              │        ↓                                     │
                              │  slice_drivers ────────► slicer.py           │
                              │        ↓                 cohorts + concentr. │
                              │  recall_memory  ◄──────────────────┐         │
                              │        ↓                           │         │
                              │  explain_drivers ─────► LLM        │         │
                              │        ↓                           │         │
                              │  compile_action_plan ─► actions.py │         │
                              │        ↓                           │         │
                              │  synthesize_brief ────► LLM        │         │
                              │        ↓                           │         │
                              │  persist_memory ───────────────────┤         │
                              │        ↓                           │         │
                              │  render_artifacts ────► brief.py   │         │
                              └─────────────────┬──────────────────┼─────────┘
                                                │                  │
                                                ▼                  ▼
                                     out/flux/*.md, *.xlsx   institutional memory
                                                             data/flux_memory_graph.json
                                                             data/flux_memory.db
                                                                   ▲
                                                                   │ Confirm / Off-base
                                                       Streamlit flux page (analyst)
```

Ten nodes, linear, no conditional edges. The memory store is read by
`recall_memory` and written by `persist_memory`, so what one run learns is
available to the next. The LLM touches exactly two nodes — `explain_drivers`
and `synthesize_brief` — both of which write prose only, and both of which have
a deterministic fallback.

## Quickstart

```bash
pip install -r requirements.txt
cp .env.example .env          # MODAL_QWEN_URL optional; unset runs deterministic

python data/seed_flux.py      # 20 months of synthetic summaries + subledgers
python run_flux.py --replay   # every period oldest-first, so memory accumulates
streamlit run ui/streamlit_app.py
```

**Or bring your own numbers.** The flux page has an "Analyze your own financials"
panel: drop in a period summary and a transaction subledger (each covering at
least two `YYYY-MM` periods) and it runs the same graph over them, returning the
analysis, the prioritized action plan, and downloadable artifacts. Those runs are
**isolated** — an uploaded company's data is never read into, or written into, the
institutional memory built from the seeded ledger (`app/flux/uploads.py`).

`python run_flux.py 2026-08` runs a single comparison instead. The seeded
dataset has planted stories to find (`data/seed_flux.py` names them in its
docstring): a 32% enterprise-revenue jump concentrated in three customers, a
hosting overrun recurring three months running, an August-only conference
sponsorship, a churned mid-market customer, a one-off legal fee, and the
subledger-less Insurance accrual.

## Evaluation and benchmarks — in progress

Placeholders, deliberately empty. **No number below is estimated or projected.**
The harness is being built; these tables get filled in with measured results or
they get removed.

**Accuracy scorecard** — against the seeded dataset, where the planted stories
give a ground-truth answer key: material-variance detection recall and
precision, top-1 and top-3 driver-attribution accuracy, and the rate at which an
LLM response fails to parse as JSON and falls through to the deterministic
template.

**Serving benchmark** — for the Modal vLLM endpoint: cold-start latency, warm
p50/p95 per call, serial versus batched throughput across a 20-period replay,
and the effect of prefix caching on the shared system prompt.

## The same pattern, applied to AP

`app/graph.py` + `run_demo.py` apply the same shape — deterministic core, LLM
narrates, human approves — to accounts-payable reconciliation. An invoice
document goes in; it is extracted (LLM, with a regex parser fallback), matched
three ways against a SQLite ERP / bank-feed / vendor-master stand-in, run
through eleven deterministic control rules in `app/controls.py` (duplicate
invoice, amount over PO, unknown vendor, closed PO, possible prior payment, …),
written to a five-sheet audit workpaper, and queued to a Streamlit approval
inbox. The agent's output is a *recommendation* — `hold` / `review` /
`approve` — and a human makes the decision with the evidence in front of them.
A sample workpaper is in
[`examples/`](examples/workpaper_INV-2001_38e95103.xlsx).

```bash
python data/seed.py
python run_demo.py
```

## Further reading

[`ARCHITECTURE.md`](ARCHITECTURE.md) — the full design, and a closing section
of eleven stated limitations, one of which is a bug I found in my own feedback
loop and wrote up rather than quietly fixed.

Built for the AI x Finance "Money Talks" hackathon, Money Operations track.
