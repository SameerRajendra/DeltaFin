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
python run_demo.py        # process sample invoices
streamlit run ui/streamlit_app.py
```

`MODAL_QWEN_URL` is optional: without it, extraction and the narrative fall
back to deterministic logic so the graph still runs.

## Model access

Qwen on Modal (`modal_app/qwen_reasoner.py`, serverless vLLM behind an
OpenAI-compatible endpoint) is the only LLM provider, reached through
`app/llm.py`'s `get_llm()`. Env vars: `MODAL_QWEN_URL`, `MODAL_KEY`,
`MODAL_SECRET`. When `get_llm()` returns `None`, every caller has a
deterministic fallback path — that property is load-bearing, keep it.

There is no observability layer. An earlier PRISM tracing integration was
removed; do not re-add tracing wiring unless asked for it explicitly.
