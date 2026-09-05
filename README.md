# AI-native AP reconciliation

Agentic accounts-payable close for a finance team of one: drop in an invoice,
get back a three-way match, a flagged exception list, an audit-ready workpaper,
and an approval decision a human actually makes.

## The problem

AP teams reconcile invoices against purchase orders and bank activity by hand.
The failure modes are boring and expensive: duplicate invoices paid twice,
vendors billing over their PO, payments to entities that were never onboarded.

## Pipeline

LangGraph runs a deterministic seven-node graph, one trajectory per invoice:

```
ingest_document -> extract_fields -> match_records -> evaluate_controls
    -> draft_narrative -> compile_workpaper -> queue_for_review
```

- **Semantic layer** (`app/store.py`) — SQLite stand-in for ERP, bank feed, and CRM
- **Extraction** (`app/extraction.py`) — LLM extraction, with a deterministic parser fallback
- **Controls** (`app/controls.py`) — duplicate detection, PO variance, unknown vendor, closed PO, terms mismatch, prior-payment detection
- **Workpaper** (`app/workpaper.py`) — five-sheet .xlsx with evidence and the match grid
- **Review UI** (`ui/streamlit_app.py`) — approval inbox with side-by-side diffs

Every run is traced to PRISM (`app/tracing.py`), one session per invoice.

## Quickstart

```bash
pip install -r requirements.txt
cp .env.example .env      # fill in PRISM keys; ANTHROPIC_API_KEY optional
python data/seed.py
python run_demo.py
streamlit run ui/streamlit_app.py
```

## Sample cases

| Invoice | Scenario | Outcome |
|---|---|---|
| INV-1001 | Clean three-way match, already settled | REVIEW — possible prior payment |
| INV-2001 | Billed 990.00 over PO-5002 | HOLD — amount over PO |
| INV-1001 (dup) | Same invoice number resubmitted | HOLD — duplicate invoice |
| INV-7788 | Vendor not in master, PO doesn't exist | HOLD — unknown vendor |
| INV-4102 | No PO referenced | HOLD — three-way match impossible |
