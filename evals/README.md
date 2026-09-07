# Evals — scoring the variance agent against a labelled set

`data/seed_flux.py` has always planted specific, known stories in the synthetic
20-month ledger — a flagship enterprise-revenue jump, a hosting overrun, a
seasonal sponsorship, a churned customer, a one-off legal fee, an accrual with
no subledger. Until now nothing asserted against them. This directory turns
that generator docstring into a scored, reproducible test of the whole
pipeline.

## Running

```bash
pip install -r requirements.txt -r requirements-dev.txt
python data/seed_flux.py          # generates data/financials/, required
python evals/run_evals.py         # -> evals/results/latest.md
```

Exit code is `1` if any assertion failed, `0` otherwise, `2` if the seeded
dataset is missing. Everything is deterministic: the eval forces
`app.llm.get_llm()` to return `None`, so `explain_drivers` and
`synthesize_brief` take their template paths even if `MODAL_QWEN_URL` is set.

The run replays all 19 consecutive period comparisons oldest-first (the same
order `run_flux.py --replay` uses) so institutional memory accumulates the way
it does in a real close cycle. It redirects `config.FLUX_DB_PATH`,
`config.FLUX_GRAPH_PATH` and `config.FLUX_OUT_DIR` into `evals/results/` and
wipes them first, so it always starts from empty memory and never touches
`data/flux_memory.db`, `data/flux_memory_graph.json` or the `out/` demo
artifacts.

## The ground truth is transcribed, not imported

`ground_truth.yaml` contains hand-copied literals read off `data/seed_flux.py`.
Nothing in `evals/` imports that module. If the eval derived its expectations
from the generator, a drifting generator (new story, changed amount, different
RNG seed) would silently redefine truth and keep reporting green. Transcribing
means drift breaks the eval loudly. The file's header states the rule and each
entry cites the generator line it came from.

Two kinds of number live in there, asserted differently:

- **Planted amounts** overwrite the RNG series (`ENTERPRISE_FLAGSHIP`, the
  `25_000` sponsorship, the `40_000` legal fee, the `8/11/15k` hosting
  overruns, the `5_000 -> 30_000` insurance true-up). Exact, asserted exactly.
- **Base series** come from `smooth_series` with ±3–5% uniform noise. Anything
  derived from them carries a tolerance, and where the noise genuinely decides
  an outcome the entry is marked `report_only` rather than guessed at.

## The metrics

| Metric | What it answers | Asserted? |
|---|---|---|
| **Detection recall** | For each planted story: did the account clear `variance.is_material` in its expected comparison, with the expected gate — and did it survive the `MAX_DRILLDOWNS` cap to become a finding? | Yes |
| **Planted amounts** | Do the account-level `prior_amt` / `current_amt` / `delta` / `pct` still equal the planted figures? | Yes, exactly |
| **Driver attribution** | Top-1 and top-3 driver accuracy against the planted per-customer / per-vendor deltas, plus each driver's `new`/`churned`/`expansion`/`contraction` cohort. | Yes |
| **Concentration stat** | Every `graph._concentration_note` produced during the replay is re-derived from the transaction CSVs in plain Python — no DuckDB, no `slicer` — and compared. The driver deltas backing it are compared the same way. | Yes |
| **Concentration edge case** | The documented case where more than 3 drivers never clear 60% cumulative must yield an *empty* note, not a wrong one. | Yes, when the dataset produces an instance; otherwise reported as 0 instances |
| **Memory after replay** | The seasonal `6000::TechConf Expo` edge reads `seen_before=True, streak=0` at 2026-08 (a gap year, not a run). The `5000::CloudBeam Compute` edge is recorded for 2026-06. A churned customer (`MID-04`) and a one-off vendor (`Latham Legal Advisors`) never reappear as drivers after their last real period. | Yes |
| **Tie-out** | Account 7000 reports 0% subledger coverage in every period and produces a `P1` / `Controller / Accounting` action every period; its summary amounts match the planted `5_000` baseline and `30_000` true-up. | Yes |
| **Detection precision / false-positive rate** | What fraction of all findings sit in a comparison/account with no planted story. | **No — reported only** |

### Why precision is reported and not asserted

Nobody has ever measured this number, and the base series are genuinely noised:
a real 12% swing in R&D is a real variance, not a hallucination. Asserting a
ceiling before a human has looked at the list would be inventing a threshold.
`ground_truth.yaml` has `false_positive_policy.assert_max_rate: null`; the
scorecard prints the rate, the per-account breakdown, and the full list of
unexplained findings. Fill the field in once that list has been reviewed and
the check turns into a real gate.

Two entries are `report_only` for the same reason:

- **`hosting_overrun_continuation`** — the month-over-month *change* in the
  overrun is only `+3,000` (July) and `+4,000` (August), inside the swing the
  ±3% noise on the ~41k hosting base produces on its own. Whether account 5000
  clears the gate in those two months cannot be determined by reading the
  generator, so the eval reports the outcome instead of asserting one. The
  streak on the hosting memory edge depends on the same thing and is likewise
  reported.

## What this does **not** cover

- **LLM narrative quality.** The model is disabled. Every `headline`, `why` and
  `action` scored here comes from `graph._template_finding`. Whether Qwen writes
  a good executive sentence is not measured anywhere in this repo.
- **The AP pipeline.** `app/controls.py` is covered by `tests/test_controls.py`;
  `app/extraction.py`, `app/graph.py`, `app/store.py` and `app/workpaper.py` are
  not covered at all.
- **Rendering.** The markdown brief and `.xlsx` workpaper are written during the
  replay (into `evals/results/_artifacts/`), but nothing asserts their contents.
- **Ranking quality beyond membership.** The eval checks that a story's account
  is material and surfaces as a finding; it does not assert its *rank* among the
  other findings.
- **Anything about real financial data.** This is a seeded synthetic ledger with
  nine accounts and a handful of customers and vendors.

## Relationship to `tests/`

`tests/` is Tier 0 — pure functions and in-memory fixtures, no data files, no
model, runs in CI on every push (`.github/workflows/tests.yml`). This directory
is Tier 1 — system-level, needs the seeded dataset, and is deliberately not
wired into CI.
