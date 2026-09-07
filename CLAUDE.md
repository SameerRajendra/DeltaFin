# DeltaFin — agentic variance explanation, plus AP reconciliation

Hackathon project (AI x Finance "Money Talks", Money Operations track).

The lead pipeline is a **variance-explanation agent** (`app/flux/`, "flux"):
monthly account summaries and transaction-level CSVs are ingested, variances
are computed against a trailing baseline and materiality-gated, drivers are
sliced out of the subledger with cohort classification, institutional memory
from prior runs is recalled, an LLM narrates the already-computed facts, and
every finding is compiled into a prioritized, owned action plan and rendered as
a markdown brief plus an `.xlsx` workpaper.

The **AP reconciliation pipeline** (`app/graph.py`) applies the same shape to a
second domain: invoice documents are extracted, matched three ways against an
ERP/bank/CRM stand-in, evaluated against AP control rules, compiled into an
audit-ready workpaper, and queued for human approval.

## Layout

Variance agent:

- `app/flux/graph.py` — LangGraph orchestration and the `process_period` entry point
- `app/flux/ingest.py` — DuckDB ingestion of the summary/transaction CSV globs, plus the subledger tie-out
- `app/flux/variance.py` — delta / % / z-score and the materiality gate
- `app/flux/slicer.py` — cohort driver decomposition and the concentration statistic
- `app/flux/memory.py` — cross-run memory: JSON `account::driver` graph + append-only SQLite history
- `app/flux/actions.py` — priority and owner assignment
- `app/flux/brief.py` — markdown brief, `.xlsx` workpaper, and the two `.txt` exports
- `app/flux/uploads.py` — normalizes and validates two arbitrary uploaded CSVs into the
  `summary`/`txn` tables the pipeline expects, then runs one **isolated** comparison
  (`graph.build_graph(..., isolated=True)`): no memory read, no memory write, no `out/flux/`
  artifacts. Uploaded data must never enter the institutional history.
- `data/seed_flux.py` — the deterministic 20-month synthetic ledger
- `run_flux.py` — one period comparison, or `--replay` over all of them

AP pipeline:

- `app/graph.py` — LangGraph orchestration and the `process_invoice` entry point
- `app/extraction.py` — document -> structured invoice fields (LLM, or deterministic parser without a provider key)
- `app/controls.py` — three-way-match and AP control rules producing exceptions
- `app/store.py` / `data/seed.py` — SQLite financial semantic layer (vendors, POs, bank feed, invoices, approvals)
- `app/workpaper.py` — openpyxl audit workpaper writer
- `run_demo.py` — batch the sample invoices through the graph

Shared:

- `ui/streamlit_app.py` — the AP approval inbox and the flux page (including the analyst Confirm / Off-base feedback control)
- `examples/` — committed copies of real output; `out/` is gitignored

## Running

```bash
python data/seed_flux.py     # synthetic financials
python run_flux.py --replay  # walk every period so memory accumulates

python data/seed.py          # build the AP ledger
python run_demo.py           # process sample invoices

streamlit run ui/streamlit_app.py
```

`MODAL_QWEN_URL` is optional: without it, extraction, driver explanation, and
the narrative fall back to deterministic logic so both graphs still run.

## Model access

Qwen on Modal (`modal_app/qwen_reasoner.py`, serverless vLLM behind an
OpenAI-compatible endpoint) is the only LLM provider, reached through
`app/llm.py`'s `get_llm()`. Env vars: `MODAL_QWEN_URL`, `MODAL_KEY`,
`MODAL_SECRET`. When `get_llm()` returns `None`, every caller has a
deterministic fallback path — that property is load-bearing, keep it.

There is no observability layer. An earlier PRISM tracing integration was
removed; do not re-add tracing wiring unless asked for it explicitly.
