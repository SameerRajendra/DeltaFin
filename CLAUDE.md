# Money Operations — AI-native AP reconciliation

Hackathon project (AI x Finance "Money Talks", Money Operations track).

An agentic accounts-payable pipeline: invoice documents are extracted, matched
three ways against an ERP/bank/CRM stand-in, evaluated against AP control rules,
compiled into an audit-ready workpaper, and queued for human approval.

## Layout

- `app/graph.py` — LangGraph orchestration and the `process_invoice` entry point
- `app/extraction.py` — document -> structured invoice fields (LLM, or deterministic parser without a provider key)
- `app/controls.py` — three-way-match and AP control rules producing exceptions
- `app/store.py` / `data/seed.py` — SQLite financial semantic layer (vendors, POs, bank feed, invoices, approvals)
- `app/workpaper.py` — openpyxl audit workpaper writer
- `ui/streamlit_app.py` — human-in-the-loop approval inbox
- `run_demo.py` — batch the sample invoices through the graph

## Running

```bash
python data/seed.py       # build the ledger
python run_demo.py        # process sample invoices (emits PRISM traces)
streamlit run ui/streamlit_app.py
```

`ANTHROPIC_API_KEY` is optional: without it, extraction and the narrative fall
back to deterministic logic so the graph still runs and still traces.

## PRISM tracing (do not remove)

This project sends traces to PRISM. Env vars: `PRISMTRACE_API_KEY`,
`PRISMTRACE_PROJECT_ID`, `PRISMTRACE_HOST`.

Tracing is currently wired at: `app/tracing.py` (handler + `wrap_langgraph`),
`app/graph.py` (`process_invoice` creates one session per invoice run and
flushes), `app/config.py` (env loading), `.env.example` (key names).

**Standing rule.** Whenever you add or change an agent, chain, graph, tool,
retriever, or any entry point that calls a model, wire it to PRISM before you
finish. Unwired code is invisible in the dashboard. If you are unsure whether
something is covered, assume it is not and wire it.
