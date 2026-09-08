# Flux eval scorecard

_Run 2026-09-08T02:09:59+00:00 UTC · 19 period comparisons (2025-02 through 2026-08) · deterministic path, no model configured_

**62 passed · 1 failed · 6 reported · 1 skipped**

## Config guard

| Status | Check | Detail |
|---|---|---|
| PASS | app.config.MATERIALITY_ABS == 25000.0 | actual 25000.0 |
| PASS | app.config.MATERIALITY_PCT == 0.1 | actual 0.1 |
| PASS | app.config.MATERIALITY_FLOOR == 5000.0 | actual 5000.0 |
| PASS | app.config.ANOMALY_Z == 2.0 | actual 2.0 |
| PASS | app.config.MAX_DRILLDOWNS == 6 | actual 6 |
| PASS | app.config.MAX_DRIVERS == 5 | actual 5 |

## Dataset

| Status | Check | Detail |
|---|---|---|
| PASS | the generator still produces 20 periods (2025-01..2026-08) | actual 20: 2025-01..2026-08 |

## Detection recall

| Status | Check | Detail |
|---|---|---|
| PASS | enterprise_flagship (4000, 2026-07 -> 2026-08): clears the materiality gate | delta +96,000.00, pct 0.3200, z 14.24, reason 'absolute-threshold' |
| PASS | enterprise_flagship (4000, 2026-07 -> 2026-08): materiality reason is 'absolute-threshold' | actual 'absolute-threshold' |
| PASS | enterprise_flagship (4000, 2026-07 -> 2026-08): surfaced as a finding (past the MAX_DRILLDOWNS cap) | 3 findings this comparison |
| PASS | hosting_overrun_first_month (5000, 2026-05 -> 2026-06): clears the materiality gate | delta +7,836.58, pct 0.1882, z 5.83, reason 'percentage-threshold' |
| PASS | hosting_overrun_first_month (5000, 2026-05 -> 2026-06): materiality reason is 'percentage-threshold' | actual 'percentage-threshold' |
| PASS | hosting_overrun_first_month (5000, 2026-05 -> 2026-06): surfaced as a finding (past the MAX_DRILLDOWNS cap) | 1 findings this comparison |
| PASS | seasonal_conference_2025 (6000, 2025-07 -> 2025-08): clears the materiality gate | delta +26,800.92, pct 0.3808, z 11.98, reason 'absolute-threshold' |
| PASS | seasonal_conference_2025 (6000, 2025-07 -> 2025-08): materiality reason in ['absolute-threshold', 'percentage-threshold'] | actual 'absolute-threshold' |
| PASS | seasonal_conference_2025 (6000, 2025-07 -> 2025-08): surfaced as a finding (past the MAX_DRILLDOWNS cap) | 2 findings this comparison |
| PASS | seasonal_conference_2026 (6000, 2026-07 -> 2026-08): clears the materiality gate | delta +27,550.00, pct 0.3830, z 22.32, reason 'absolute-threshold' |
| PASS | seasonal_conference_2026 (6000, 2026-07 -> 2026-08): materiality reason in ['absolute-threshold', 'percentage-threshold'] | actual 'absolute-threshold' |
| PASS | seasonal_conference_2026 (6000, 2026-07 -> 2026-08): surfaced as a finding (past the MAX_DRILLDOWNS cap) | 3 findings this comparison |
| PASS | midmarket_churn (4010, 2026-02 -> 2026-03): clears the materiality gate | delta -35,432.69, pct -0.2213, z -29.3, reason 'absolute-threshold' |
| PASS | midmarket_churn (4010, 2026-02 -> 2026-03): materiality reason in ['absolute-threshold', 'percentage-threshold'] | actual 'absolute-threshold' |
| PASS | midmarket_churn (4010, 2026-02 -> 2026-03): surfaced as a finding (past the MAX_DRILLDOWNS cap) | 2 findings this comparison |
| PASS | legal_fee_one_off (6010, 2025-10 -> 2025-11): clears the materiality gate | delta +42,630.78, pct 0.8821, z 63.63, reason 'absolute-threshold' |
| PASS | legal_fee_one_off (6010, 2025-10 -> 2025-11): materiality reason is 'absolute-threshold' | actual 'absolute-threshold' |
| PASS | legal_fee_one_off (6010, 2025-10 -> 2025-11): surfaced as a finding (past the MAX_DRILLDOWNS cap) | 2 findings this comparison |

## Planted amounts

| Status | Check | Detail |
|---|---|---|
| PASS | enterprise_flagship (4000, 2026-07 -> 2026-08): prior_amt == 300000.0 | actual 300000.0 |
| PASS | enterprise_flagship (4000, 2026-07 -> 2026-08): current_amt == 396000.0 | actual 396000.0 |
| PASS | enterprise_flagship (4000, 2026-07 -> 2026-08): delta == 96000.0 | actual 96000.0 |
| PASS | enterprise_flagship (4000, 2026-07 -> 2026-08): pct == 0.32 | actual 0.32 |

## Driver attribution

| Status | Check | Detail |
|---|---|---|
| PASS | enterprise_flagship (4000, 2026-07 -> 2026-08): top-1 driver is ENT-01 | actual top-3 ['ENT-01', 'ENT-02', 'ENT-04'] |
| PASS | enterprise_flagship (4000, 2026-07 -> 2026-08): top-3 drivers are ['ENT-01', 'ENT-02', 'ENT-04'] | actual ['ENT-01', 'ENT-02', 'ENT-04'] |
| PASS | enterprise_flagship (4000, 2026-07 -> 2026-08): ENT-01 delta == +30,000.00 | actual 30000.0 |
| PASS | enterprise_flagship (4000, 2026-07 -> 2026-08): ENT-02 delta == +20,000.00 | actual 20000.0 |
| PASS | enterprise_flagship (4000, 2026-07 -> 2026-08): ENT-04 delta == +18,000.00 | actual 18000.0 |
| PASS | enterprise_flagship (4000, 2026-07 -> 2026-08): ENT-05 delta == +15,000.00 | actual 15000.0 |
| PASS | enterprise_flagship (4000, 2026-07 -> 2026-08): ENT-03 delta == +13,000.00 | actual 13000.0 |
| PASS | enterprise_flagship (4000, 2026-07 -> 2026-08): every driver is 'expansion' | actual {'ENT-01': 'expansion', 'ENT-02': 'expansion', 'ENT-04': 'expansion', 'ENT-05': 'expansion', 'ENT-03': 'expansion'} |
| PASS | hosting_overrun_first_month (5000, 2026-05 -> 2026-06): top-1 driver is CloudBeam Compute | actual top-3 ['CloudBeam Compute', 'NimbusHost Cloud'] |
| PASS | hosting_overrun_first_month (5000, 2026-05 -> 2026-06): CloudBeam Compute delta == +8,000.00 | actual 8000.0 |
| PASS | hosting_overrun_first_month (5000, 2026-05 -> 2026-06): CloudBeam Compute cohort is 'new' | actual 'new' |
| PASS | seasonal_conference_2025 (6000, 2025-07 -> 2025-08): top-1 driver is TechConf Expo | actual top-3 ['TechConf Expo', 'Ad Platform Co', 'Field Events LLC'] |
| PASS | seasonal_conference_2025 (6000, 2025-07 -> 2025-08): TechConf Expo delta == +25,000.00 | actual 25000.0 |
| PASS | seasonal_conference_2025 (6000, 2025-07 -> 2025-08): TechConf Expo cohort is 'new' | actual 'new' |
| PASS | seasonal_conference_2026 (6000, 2026-07 -> 2026-08): top-1 driver is TechConf Expo | actual top-3 ['TechConf Expo', 'Field Events LLC', 'Ad Platform Co'] |
| PASS | seasonal_conference_2026 (6000, 2026-07 -> 2026-08): TechConf Expo delta == +25,000.00 | actual 25000.0 |
| PASS | seasonal_conference_2026 (6000, 2026-07 -> 2026-08): TechConf Expo cohort is 'new' | actual 'new' |
| PASS | midmarket_churn (4010, 2026-02 -> 2026-03): top-1 driver is MID-04 | actual top-3 ['MID-04', 'MID-01', 'MID-05'] |
| PASS | midmarket_churn (4010, 2026-02 -> 2026-03): MID-04 cohort is 'churned' | actual 'churned' |
| PASS | midmarket_churn (4010, 2026-02 -> 2026-03): MID-04 delta is negative | actual -35592.95 |
| PASS | legal_fee_one_off (6010, 2025-10 -> 2025-11): top-1 driver is Latham Legal Advisors | actual top-3 ['Latham Legal Advisors', 'Corporate Services Inc'] |
| PASS | legal_fee_one_off (6010, 2025-10 -> 2025-11): Latham Legal Advisors delta == +40,000.00 | actual 40000.0 |
| PASS | legal_fee_one_off (6010, 2025-10 -> 2025-11): Latham Legal Advisors cohort is 'new' | actual 'new' |

## Concentration stat

| Status | Check | Detail |
|---|---|---|
| PASS | enterprise_flagship (4000, 2026-07 -> 2026-08): concentration note | actual '3 customers account for 71% of this account's delta' |
| PASS | all 43 concentration notes match an independent CSV recomputation | no mismatches |
| PASS | every driver delta matches an independent CSV recomputation | no mismatches |
| FAIL | >3 drivers never clearing 60% yields an empty note (2 instance(s)) | 2025-10 4010 (cum3=0.4865) -> '1 customer account for 62% of this account's delta' |

## Tie-out

| Status | Check | Detail |
|---|---|---|
| PASS | all 8 accounts report 100% subledger coverage in all 19 periods | 100% on every account, every period |
| PASS | no tie-out gap task is raised in any period | no gap tasks, as expected for a fully reconciled ledger |

## Memory correctness

| Status | Check | Detail |
|---|---|---|
| PASS | seasonal_conference_2026: 6000::TechConf Expo seen_before is True at 2026-08 | actual {'seen_before': True, 'streak': 0, 'last_verdict': None, 'last_note': None} |
| PASS | seasonal_conference_2026: 6000::TechConf Expo streak is 0 at 2026-08 | actual 0 |
| PASS | 5000::CloudBeam Compute recorded, including 2026-06 | periods ['2026-06', '2026-08'] |
| INFO | 5000::CloudBeam Compute streak at 2026-08 (observed, not asserted) | seen_before=True streak=0 periods=['2026-06', '2026-08'] |
| PASS | midmarket_churn: ['MID-04'] never a driver of 4010 after 2026-03 | clean across every later comparison |
| PASS | legal_fee_one_off: ['Latham Legal Advisors'] never a driver of 6010 after 2025-12 | clean across every later comparison |

## Report-only

| Status | Check | Detail |
|---|---|---|
| INFO | hosting_overrun_continuation: 5000 at 2026-07 | delta +1,588.95 z 0.97 -> material=False (''); drivers: not drilled into |
| INFO | hosting_overrun_continuation: 5000 at 2026-08 | delta +5,383.64 z 1.28 -> material=True ('percentage-threshold'); drivers: CloudBeam Compute +4,000.00, NimbusHost Cloud +1,383.64 |

## Detection precision

| Status | Check | Detail |
|---|---|---|
| INFO | findings per comparison | 25 findings across 19 comparisons (1.32 per comparison) |
| INFO | precision against the planted set | 9/25 findings sit on a planted story = 36.0% |
| INFO | unexplained (candidate false-positive) rate | 16/25 = 64.0%. These are not necessarily wrong -- the base series are genuinely noised, so a real 12% swing in R&D is a real variance -- but nothing in the dataset planted them. By account: 4000 Enterprise Revenue x4, 5000 Hosting COGS x4, 6020 R&D x3, 6010 G&A x2, 6100 Travel x2, 4010 Mid-Market Revenue x1 |
| SKIP | assert a ceiling on the unexplained rate | ground_truth.yaml sets false_positive_policy.assert_max_rate: null. Fill it in once the number above has been reviewed by a human; inventing a threshold before measuring it would be meaningless. |

## Unexplained findings (full list)

Findings in a comparison/account with no planted story. Reviewing this list is how the false-positive number stops being a guess.

- 2025-05 4000 Enterprise Revenue (-13,747.48, statistical-anomaly)
- 2025-05 6010 G&A (+1,078.63, statistical-anomaly)
- 2025-05 5000 Hosting COGS (-284.11, statistical-anomaly)
- 2025-06 6010 G&A (-607.62, statistical-anomaly)
- 2025-08 6100 Travel (-1,290.04, statistical-anomaly)
- 2025-10 4000 Enterprise Revenue (-15,331.19, statistical-anomaly)
- 2025-10 4010 Mid-Market Revenue (+2,498.43, statistical-anomaly)
- 2025-11 4000 Enterprise Revenue (+18,445.14, statistical-anomaly)
- 2025-12 5000 Hosting COGS (+1,470.25, statistical-anomaly)
- 2026-01 6020 R&D (-2,122.81, statistical-anomaly)
- 2026-01 5000 Hosting COGS (-1,879.74, statistical-anomaly)
- 2026-02 6020 R&D (+5,500.16, statistical-anomaly)
- 2026-02 5000 Hosting COGS (+2,045.66, statistical-anomaly)
- 2026-02 6100 Travel (+1,043.03, statistical-anomaly)
- 2026-03 6020 R&D (-5,013.99, statistical-anomaly)
- 2026-07 4000 Enterprise Revenue (-71,494.33, absolute-threshold)

## What this scorecard does not cover

- **LLM narrative quality.** This run disables the model entirely; every `headline`/`why`/`action` scored here comes from `graph._template_finding`.
- **The AP pipeline.** `app/graph.py`, `app/controls.py` and `app/extraction.py` are covered by `tests/`, not here.
- **Rendering.** The markdown brief and `.xlsx` workpaper are produced during the replay but nothing asserts their contents.
