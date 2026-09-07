# What this is and how it's built

An agentic accounts-payable reconciliation system. You hand it an invoice
document; it extracts the fields, matches them three ways against an ERP, a bank
feed, and a vendor master, runs AP control rules over the result, writes an
audit-ready Excel workpaper, and puts the whole thing in front of a human to
approve or reject. Every run is traced to PRISM.

Built for the AI x Finance "Money Talks" hackathon, Money Operations track.

---

## 1. The problem

Accounts payable is reconciliation work. A finance team receives an invoice and
has to answer four questions before paying it:

1. Is this vendor real and onboarded?
2. Did we actually order this, at this price? (the purchase order)
3. Have we already paid it? (the bank feed)
4. Does the arithmetic hold?

Done by hand this is slow and error-prone, and the failure modes are expensive:
duplicate invoices paid twice, vendors quietly billing over their PO, payments
to entities that were never onboarded. These are exactly the checks that are
mechanical enough to automate but judgement-laden enough that you don't want a
model silently approving payments.

So the design goal is **an agent that does the reconciliation and assembles the
evidence, but never makes the payment decision.** A human approves, with the
agent's findings and a complete workpaper in front of them.

---

## 2. What it does, concretely

Given `samples/invoices/INV-2001_northwind.txt`:

```
NORTHWIND LOGISTICS
Invoice Number: INV-2001
PO Number: PO-5002
...
Total Due: USD 5190.00
```

The system produces:

- **Structured fields** — vendor, invoice number, dates, PO reference, line items, total
- **A three-way match grid** — invoice vs. purchase order vs. bank feed, attribute by attribute
- **Control exceptions** — `[HIGH] amount_over_po: Invoice 5,190.00 exceeds PO 4,200.00 by 990.00 (tolerance 84.00)`
- **A recommendation** — `HOLD` (never an action, only a recommendation)
- **An audit workpaper** — `out/workpaper_INV-2001_38e95103.xlsx`, five sheets
- **A review queue entry** — surfaced in the Streamlit approval inbox
- **A PRISM trajectory** — one session per invoice, spans for every node

---

## 3. Architecture

```
                      ┌─────────────────────────────────────────┐
   invoice document   │           LangGraph  (app/graph.py)     │
   (.txt / .pdf)  ───►│                                         │
                      │  ingest_document                        │
                      │        ↓                                │
                      │  extract_fields ──────► LLM or parser   │
                      │        ↓                                │
                      │  match_records ───────► semantic layer  │
                      │        ↓                    (SQLite)    │
                      │  evaluate_controls ───► control rules   │
                      │        ↓                                │
                      │  draft_narrative ─────► LLM or template │
                      │        ↓                                │
                      │  compile_workpaper ───► openpyxl .xlsx  │
                      │        ↓                                │
                      │  queue_for_review ────► invoices table  │
                      └─────────────────┬───────────────────────┘
                                        │ callbacks on every node
                                        ▼
                              PRISM  (app/tracing.py)
                                        │
                                        ▼
                        Streamlit approval inbox (human decides)
```

Seven nodes, linear, deterministic. No conditional edges and no agent loop —
this is deliberate, see §8.

---

## 4. Components

### 4.1 Financial semantic layer — `app/store.py`, `data/seed.py`

A SQLite database standing in for the three systems a real finance team would
query. Using one local store keeps the demo self-contained and reproducible.

| Table | Stands in for | Contents |
|---|---|---|
| `vendors` | Vendor master / CRM | name, tax ID, payment terms, active status |
| `purchase_orders` | ERP | PO number, vendor, amount, status, issue date |
| `bank_transactions` | Bank feed | posted date, amount, counterparty, reference |
| `invoices` | AP subledger | extracted fields, recommendation, narrative, workpaper path, status |
| `invoice_exceptions` | — | one row per control failure, with severity |
| `approvals` | Audit trail | who decided what, when, with what note |

Lookups are intentionally forgiving in the way a real matcher must be:
`find_vendor` falls back from exact name match to a prefix match;
`find_bank_payment` matches on invoice reference first, then on amount.

`python data/seed.py` drops and rebuilds the database with four vendors, four
POs, and three settled bank transactions.

### 4.2 Document extraction — `app/extraction.py`

Turns document text into a structured invoice dict. Two paths:

- **With `ANTHROPIC_API_KEY`** — the text goes to the model with a prompt
  demanding a strict JSON object. The response is JSON-extracted and parsed.
- **Without a key** — a deterministic regex parser pulls the same fields, and a
  column-aware regex reads the line-item table.

The LLM path degrades to the parser on any JSON failure, so extraction never
hard-fails. The resulting dict carries `extraction_method` so the workpaper
records how each field was obtained — an audit trail requirement, not a nicety.

PDFs are read via `pypdf`; `.txt` is read directly.

### 4.3 Control rules — `app/controls.py`

Pure functions over `(extracted, vendor, po, bank_txn, prior_invoice)`. No model
involvement — these are the checks that decide whether money moves, so they are
ordinary deterministic code that can be read and audited.

| Code | Severity | Fires when |
|---|---|---|
| `unknown_vendor` | critical | Payee absent from the vendor master |
| `duplicate_invoice` | critical | Invoice number already processed |
| `inactive_vendor` | high | Vendor exists but isn't active |
| `missing_po` | high | No PO referenced — three-way match impossible |
| `po_not_found` | high | PO referenced but absent from the ERP |
| `amount_over_po` | high | Invoice exceeds PO beyond tolerance |
| `unreadable_total` | high | No total could be extracted |
| `po_closed` | medium | PO already closed |
| `possible_prior_payment` | medium | Bank feed shows a matching settlement |
| `invalid_dates` | medium | Due date precedes invoice date |
| `terms_mismatch` | low | Stated terms disagree with the vendor master |

Tolerance is `max(2% of PO, $50.00)` (`app/config.py`), so small invoices aren't
flagged over rounding and large ones aren't waved through on a percentage.

The recommendation is a pure function of the worst severity present:
critical/high → `hold`, medium → `review`, otherwise → `approve`.

### 4.4 Audit workpaper — `app/workpaper.py`

An `openpyxl` workbook, one per invoice, with five sheets:

1. **Summary** — preparer, run ID, source document, extraction method, the key fields, exception count, recommendation, and the narrative
2. **Three-Way Match** — attribute grid across invoice / PO / bank with an "Agrees?" column
3. **Exceptions** — every exception, severity-shaded (red → green)
4. **Line Items** — the parsed invoice table
5. **Source Document** — the original text, verbatim, as evidence

The point is that a reviewer can hand this file to an auditor without rebuilding
the reasoning: findings and the underlying evidence live in one artifact.

### 4.5 Orchestration — `app/graph.py`

A LangGraph `StateGraph` over a `ReconState` TypedDict. Each node returns a
partial state update:

| Node | Reads | Writes |
|---|---|---|
| `ingest_document` | `source_file` | `raw_text` |
| `extract_fields` | `raw_text` | `extracted` |
| `match_records` | `extracted` | `vendor`, `po`, `bank_txn`, `prior_invoice` |
| `evaluate_controls` | all matches | `exceptions`, `recommendation` |
| `draft_narrative` | findings | `narrative` |
| `compile_workpaper` | everything | `workpaper_path` |
| `queue_for_review` | everything | `invoice_id` |

`process_invoice(source_file)` is the single entry point: it mints a run ID,
builds the handler, wraps the graph, invokes, and flushes traces in a `finally`.
Both the CLI and the UI go through it, so there is exactly one traced path.

### 4.6 Observability — `app/tracing.py`

PRISM is wired with `PRISMtraceLangGraphHandler` + `wrap_langgraph`, which
injects callbacks at `invoke`/`stream` so no call site repeats
`config={"callbacks": [...]}`.

**One invoice = one `session_id` = one trajectory.** That grouping is what turns
individual spans into a reviewable agent run in the dashboard.

Every PRISM call is wrapped so that a missing SDK, absent credentials, or an
unreachable collector degrades to a warning on stderr. Instrumentation that can
take down an AP pipeline is worse than no instrumentation.

### 4.7 Review UI — `ui/streamlit_app.py`

The human-in-the-loop surface, and the reason the agent's output is a
*recommendation*:

- Sidebar queue filtered by status, with a count
- Three-way match rendered as an attribute table with an explicit `OK` / `MISMATCH` column
- Invoice / PO / variance metrics
- Exceptions as severity-coloured cards
- The auditor narrative
- One-click workpaper download
- Approve / reject with a reviewer note, written to `approvals` as an audit trail

---

## 5. Running it

```bash
pip install -r requirements.txt
cp .env.example .env          # PRISM keys; ANTHROPIC_API_KEY optional
python data/seed.py           # build the ledger
python run_demo.py            # process all sample invoices
streamlit run ui/streamlit_app.py
```

---

## 6. The demo cases

The five sample invoices are built to trip a different control each:

| Invoice | Scenario | Verdict |
|---|---|---|
| INV-1001 | Clean match, but the bank feed shows it settled | REVIEW — possible prior payment |
| INV-2001 | Freight surcharges push it 990.00 over PO-5002 | HOLD — amount over PO |
| INV-1001 (dup) | Same invoice number resubmitted weeks later | HOLD — duplicate invoice |
| INV-7788 | Globex: not in the vendor master, PO doesn't exist | HOLD — unknown vendor |
| INV-4102 | Emergency HVAC repair, no PO raised | HOLD — three-way match impossible |

Order matters for the duplicate case: `run_demo.py` fixes the sequence so the
original is processed before its duplicate.

---

## 7. Design decisions

**The model doesn't decide payments.** Extraction and narrative use the LLM;
matching and control evaluation are deterministic code. Anything that determines
whether money moves is auditable and reproducible.

**It runs without a provider key.** No `ANTHROPIC_API_KEY` and the pipeline still
completes via the regex parser and a templated narrative. A demo that depends on
a live provider is a demo that can fail in front of judges. Adding a key upgrades
extraction quality and produces genuine model spans in PRISM.

**One trajectory per invoice.** Traces without a shared `session_id` stay raw and
never assemble into a reviewable run.

**Tracing fails open.** Instrumentation degrades to stderr warnings rather than
raising into the pipeline.

**The workpaper carries its own evidence.** The source text ships inside the
workbook, so findings can be checked without going back to the system.

---

## 8. The variance-explanation agent ("flux")

A second, independent pipeline (`app/flux/`, `run_flux.py`) built for the same
"AI-native finance team" brief but for a different job: not reconciling one
invoice, but explaining a month-over-month change across a whole P&L. The
target bar is going from *"Revenue increased 18%"* to *"Revenue increased 18%,
primarily driven by a 32% increase in enterprise accounts, with three
customers accounting for 64% of the increase"* — and doing it in a way that
gets sharper the more months it's run against, not just once. It doesn't stop
at the explanation either: every finding is turned into a prioritized, owned
next step (§8.5), so the output reads as a task list a finance team can act
on, not just a report they read and set aside.

### 8.1 What changed / why / what's driving it

Three questions, three stages of the graph:

| Question | Node(s) | How |
|---|---|---|
| **What changed?** | `compute_variances`, `rank_materiality` | Delta, %, and a z-score against a 6-month trailing baseline per account; ranked by a materiality gate (`app/config.py`: `$25k` absolute, `10%` with a `$5k` floor, or a `2.0` z-score anomaly) |
| **What's driving it?** | `slice_drivers` | The account's subledger is grouped by customer (revenue) or vendor (cost), each member classified `new` / `churned` / `expansion` / `contraction`, ranked by contribution, with a cumulative-share concentration stat (`app/flux/graph.py: _concentration_note`) — the "3 customers account for 64%" figure |
| **Why did it change?** | `recall_memory`, `explain_drivers` | Institutional memory (§8.2) is folded into the same prompt/template that writes the narrative, so a driver that has fired before reads as "the third consecutive month" rather than a fresh surprise |
| **What should we do about it?** | `compile_action_plan` | Every finding (plus every unreconciled tie-out gap) becomes one prioritized, owned task (§8.5) |

### 8.2 Institutional memory — learning across runs, not within one

This is the part that turns a single-shot summary into a system that builds
intuition: `app/flux/memory.py` keeps two stores, both surviving across every
`run_flux.py` invocation (and untouched by `data/seed.py`, which only rebuilds
the AP ledger):

- **A JSON driver graph** (`data/flux_memory_graph.json`) — `account × driver`
  edges keyed by period. Answers "has this exact customer/vendor fired as a
  driver before, and for how many consecutive periods" in O(1), which is what
  lets a headline say *"the third consecutive month CloudBeam Compute has
  driven Hosting COGS"* instead of re-deriving it from scratch every run.
- **An append-only SQLite history** (`data/flux_memory.db`) — every run, every
  finding, and any analyst feedback (`record_feedback`), the system of record
  for "what did we say, and when."

`python run_flux.py --replay` walks every period in the synthetic 20-month
dataset oldest-first specifically to exercise this: by the last period, a
recurring driver (the TechConf Expo sponsorship every August, the hosting
overrun running June–August 2026) is reported with its streak and — once fed
back through `record_feedback` — a remembered analyst verdict, rather than as
an unexplained one-off.

### 8.3 Data and components

| Piece | File | Role |
|---|---|---|
| Synthetic ledger | `data/seed_flux.py` → `data/financials/{summaries,transactions}/YYYY-MM.csv` | 20 months, one summary + subledger CSV pair per period, deterministically seeded with planted stories (a flagship revenue jump, a recurring cost overrun, a seasonal sponsorship, a churned customer, a one-off legal fee, a subledger-less accrual) |
| Ingestion | `app/flux/ingest.py` | DuckDB reads both CSV globs into in-memory tables per run; also computes the subledger tie-out (does the transaction detail reconcile to the summary total, honestly reporting 0% for accrual-only lines) |
| Variance math | `app/flux/variance.py` | Delta / % / z-score computation and the materiality gate |
| Driver decomposition | `app/flux/slicer.py` | Cohort slicing and the concentration statistic |
| Memory | `app/flux/memory.py` | Described above |
| Action planning | `app/flux/actions.py` | Priority and owner assignment, described in §8.5 |
| Orchestration | `app/flux/graph.py` | LangGraph `StateGraph`, one trajectory per period comparison, traced to PRISM with the same `get_handler` / `instrument` / `flush` pattern as `app/graph.py` |
| Output | `app/flux/brief.py` | A markdown executive brief plus an `.xlsx` workpaper (findings, drivers, actions, tie-out) per comparison, written to `out/flux/` |

### 8.4 Model: serverless Qwen on Modal, Anthropic as fallback

`app/llm.py` prefers a self-hosted, scale-to-zero Qwen2.5-7B-Instruct endpoint
on Modal (`modal_app/qwen_reasoner.py`, deployed separately with
`modal deploy`) over `ANTHROPIC_API_KEY` when `MODAL_QWEN_URL` is set — a
cost/control tradeoff for a pipeline that calls the model twice per period
(per-account explanation, then executive synthesis) across a 20-period replay.
The tradeoff is a cold start on the first call after ~3 minutes idle
(`scaledown_window=180`). Either way, `explain_drivers` and `synthesize_brief`
degrade to a deterministic, driver-table-derived template if no LLM is
configured or a response fails to parse as JSON — same "never hard-fail"
posture as the AP pipeline's extraction.

### 8.5 From finding to task list — `app/flux/actions.py`

A finding explains a variance; it doesn't tell anyone what to do about it. The
`compile_action_plan` node closes that gap deterministically, independent of
whether an LLM is configured:

- **Priority** (`default_priority`) comes from the same signals `variance.py`
  already used to decide materiality in the first place — twice the absolute
  threshold or a sharp z-score anomaly is `P1` ("act this week"); a material
  finding with driver detail is `P2` ("review this close cycle"); a material
  finding with no subledger detail to point to is `P3` ("monitor, nothing
  concrete to act on yet").
- **Owner** (`owner_for`) maps the account to the team actually positioned to
  act on it — Sales Ops for revenue accounts, Vendor Management for hosting
  COGS, Marketing for the S&M line, Controller/Accounting for accruals —
  rather than routing everything to a generic "Finance" bucket.
- **Subledger tie-out gaps fold into the same list.** An account can clear
  every materiality check and still hide a real problem — the Insurance
  accrual with 0% subledger coverage never shows up as a "variance" (nothing
  changed month over month) but is still a `P1` for Controller/Accounting,
  because an unreconciled accrual is an audit risk regardless of whether the
  number moved.
- **The LLM is asked for the same fields** (`priority`, `owner`, and a
  concrete `action`) when one is configured, and told explicitly to name a
  specific team and a specific next step ("ask Sales Ops to confirm whether
  this is contracted run-rate or a one-time upsell") rather than "monitor" —
  `actions.py`'s deterministic assignment is the fallback when the LLM omits
  or mis-shapes those fields, not the primary path.

The result ships in three places: a "Recommended actions" table at the top of
the markdown brief (before the per-account detail), an "Actions" sheet in the
workpaper, and — since the Streamlit flux page just renders the brief file —
the same table there, with no separate UI code needed.

---

## 9. Sharing it beyond this machine

`streamlit run ui/streamlit_app.py` only ever serves `localhost` — usable for
a live demo on this laptop, useless for a judge or teammate opening a link on
their own machine. `modal_app/streamlit_host.py` solves that by deploying the
same Streamlit app to Modal as a public, unauthenticated web endpoint:

```bash
modal deploy modal_app/streamlit_host.py
# -> https://<workspace>--deltafin-ui-serve.modal.run
```

It bakes the **current local `data/` and `out/` into the image at deploy
time** (`Image.add_local_dir(..., copy=True)`) rather than running the
pipelines live — deliberately, for two reasons: a public endpoint with no
login has no business holding `PRISMTRACE_API_KEY` / `MODAL_KEY` /
`ANTHROPIC_API_KEY` (`.env` is excluded from the image outright), and a
snapshot means the page has real content the instant it's opened instead of
depending on a cold LLM call succeeding for a first-time visitor. The
tradeoff: it's a snapshot, not a live view — approve/reject clicks write to
that container's own copy of the ledger, and new invoice or period runs don't
appear there until the next `modal deploy`.

---

## 10. Limits and what's next

Honest about what this is — a hackathon slice:

- **The graph is linear.** No retries, no conditional routing, no re-extraction
  when confidence is low. Deterministic and demoable, but a production version
  would branch on extraction confidence and retry failed parses.
- **The semantic layer is synthetic.** SQLite with seeded rows, not a real ERP
  connector. The store interface is narrow enough to swap.
- **No vector retrieval yet.** The original design called for pgvector; matching
  is currently exact-then-prefix. Fuzzy vendor resolution over embeddings is the
  obvious next step for messy real-world vendor names.
- **PDF handling is text-only.** `pypdf` extracts text; scanned invoices would
  need a multimodal pass.
- **Single-user approvals.** `decided_by` is hardcoded to `reviewer`; no auth,
  no segregation of duties, no approval thresholds by amount.
- **Line-item matching is not implemented.** The amount check is invoice-total
  against PO-total. True three-way matching compares receipts line by line.
- **Flux's memory has no decay or contradiction handling.** A driver that
  stops mattering just stops appearing in findings; the graph still remembers
  it forever. And `record_feedback` overwrites the account's last verdict
  globally rather than per-driver.
- **Flux's cohort dimension is fixed per account prefix** (customers for `4xxx`
  revenue, vendors for `5xxx`/`6xxx` cost) rather than configurable or inferred.
- **Action-plan priority/owner are per-account heuristics**, not learned from
  outcomes — there's no feedback loop from "was this action actually taken"
  back into how future findings get prioritized.
- **The hosted Modal UI is a manual snapshot**, not a deployment that tracks
  the pipelines live — it only updates when someone re-runs `modal deploy`
  after generating new local demo data.
