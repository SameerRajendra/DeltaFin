"""Build the synthetic 20-month financials feeding the variance-explanation agent.

Writes data/financials/summaries/YYYY-MM.csv and .../transactions/YYYY-MM.csv,
one pair per period, Jan 2025 through Aug 2026. Every account's summary total
reconciles exactly to the sum of its transaction-level detail, so `ingest.tie_out`
reports 100% coverage for every account in every period. (There is deliberately
no summary-only account here any more; the tie-out code path still exists and is
covered by tests, it simply has nothing to flag in this dataset.)

Planted stories a variance agent should be able to find and explain:
  - Enterprise revenue jumps 32% in the final month, three customers driving
    most of the increase -> the flagship "what changed, why, who" narrative.
  - COGS-Hosting overruns for three consecutive months (a recurring driver).
  - A "TechConf Expo" sponsorship line hits Sales & Marketing every August
    (2025 and 2026) -> a seasonal, YoY-recurring driver.
  - Fabrikam Mid churns out of Mid-Market revenue in 2026-03 and never returns.
  - A one-off M&A legal fee spikes G&A once (2025-11) and never recurs.

Re-run any time; it always regenerates the full 20-month set deterministically
(seeded RNG) so the agent's institutional memory is being tested against a
fixed target, not a moving one.
"""

import csv
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import config  # noqa: E402

RNG_SEED = 20260905

PERIODS = [f"2025-{m:02d}" for m in range(1, 13)] + [f"2026-{m:02d}" for m in range(1, 9)]
N = len(PERIODS)  # 20
IDX = {p: i for i, p in enumerate(PERIODS)}

# Indices of note (see module docstring):
IDX_CONFERENCE = [IDX["2025-08"], IDX["2026-08"]]
IDX_LEGAL_FEE = IDX["2025-11"]
IDX_CHURN_START = IDX["2026-03"]
IDX_HOSTING_OVERRUN = [IDX["2026-06"], IDX["2026-07"], IDX["2026-08"]]
IDX_FLAGSHIP_PRIOR = IDX["2026-07"]
IDX_FLAGSHIP_CURRENT = IDX["2026-08"]

ACCOUNTS = {
    "4000": ("Enterprise Revenue", "revenue"),
    "4010": ("Mid-Market Revenue", "revenue"),
    "4020": ("SMB Revenue", "revenue"),
    "5000": ("Hosting COGS", "cogs"),
    "6000": ("Sales & Marketing", "opex"),
    "6010": ("G&A", "opex"),
    "6020": ("R&D", "opex"),
    "6100": ("Travel", "opex"),
}

ENTERPRISE_CUSTOMERS = [
    ("ENT-01", "Globex Corp", "AMER"),
    ("ENT-02", "Initech Holdings", "AMER"),
    ("ENT-03", "Umbrella Group", "EMEA"),
    ("ENT-04", "Stark Industries", "AMER"),
    ("ENT-05", "Wayne Enterprises", "AMER"),
]
# 2026-07 -> 2026-08: the flagship story. Three of five customers
# (Globex, Initech, Stark) drive most of the increase.
ENTERPRISE_FLAGSHIP = {
    IDX_FLAGSHIP_PRIOR: {"ENT-01": 70_000, "ENT-02": 65_000, "ENT-03": 60_000, "ENT-04": 55_000, "ENT-05": 50_000},
    IDX_FLAGSHIP_CURRENT: {"ENT-01": 100_000, "ENT-02": 85_000, "ENT-03": 73_000, "ENT-04": 73_000, "ENT-05": 65_000},
}

MIDMARKET_CUSTOMERS = [
    ("MID-01", "Acme Mid", "AMER"),
    ("MID-02", "Northwind Mid", "AMER"),
    ("MID-03", "Contoso Mid", "EMEA"),
    ("MID-04", "Fabrikam Mid", "AMER"),  # churns at IDX_CHURN_START
    ("MID-05", "Soylent Mid", "EMEA"),
]


def smooth_series(rng, base, growth=0.0, noise=0.03, n=N):
    values = []
    level = base
    for _ in range(n):
        level *= 1 + growth
        values.append(round(level * (1 + rng.uniform(-noise, noise)), 2))
    return values


def build_customer_series(rng, customers, base_range, growth=0.004, noise=0.05):
    series = {}
    for cust_id, _name, _region in customers:
        base = rng.uniform(*base_range)
        series[cust_id] = smooth_series(rng, base, growth=growth, noise=noise)
    return series


def txn_date(period, day):
    return f"{period}-{day:02d}"


def main():
    rng = random.Random(RNG_SEED)

    enterprise_series = build_customer_series(rng, ENTERPRISE_CUSTOMERS, (45_000, 72_000))
    for idx, amounts in ENTERPRISE_FLAGSHIP.items():
        for cust_id, amount in amounts.items():
            enterprise_series[cust_id][idx] = float(amount)

    midmarket_series = build_customer_series(rng, MIDMARKET_CUSTOMERS, (28_000, 36_000), growth=0.002, noise=0.03)
    for i in range(IDX_CHURN_START, N):
        midmarket_series["MID-04"][i] = 0.0

    smb_us = smooth_series(rng, 56_000, growth=0.003, noise=0.03)
    smb_eu = smooth_series(rng, 24_000, growth=0.003, noise=0.03)

    hosting_base = smooth_series(rng, 38_000, growth=0.004, noise=0.03)
    hosting_overrun = {IDX["2026-06"]: 8_000, IDX["2026-07"]: 11_000, IDX["2026-08"]: 15_000}

    sm_ads = smooth_series(rng, 47_000, growth=0.003, noise=0.04)
    sm_events = smooth_series(rng, 21_000, growth=0.003, noise=0.05)
    conference_sponsorship = 25_000

    ga_base = smooth_series(rng, 48_000, growth=0.003, noise=0.03)
    legal_fee = 40_000

    rnd_base = smooth_series(rng, 95_000, growth=0.006, noise=0.03)
    travel_base = smooth_series(rng, 15_000, growth=0.0, noise=0.05)

    summaries_dir = config.FINANCIALS_DIR / "summaries"
    txns_dir = config.FINANCIALS_DIR / "transactions"
    summaries_dir.mkdir(parents=True, exist_ok=True)
    txns_dir.mkdir(parents=True, exist_ok=True)

    for idx, period in enumerate(PERIODS):
        summary_rows = []
        txn_rows = []
        seq = 1

        def add_txn(account_code, entity, department, region, segment, cust_id, cust_name, vendor, amount, memo, day):
            nonlocal seq
            txn_rows.append(
                {
                    "txn_id": f"TXN-{period}-{seq:04d}",
                    "period": period,
                    "txn_date": txn_date(period, day),
                    "account_code": account_code,
                    "entity": entity,
                    "department": department,
                    "region": region,
                    "segment": segment,
                    "customer_id": cust_id,
                    "customer_name": cust_name,
                    "vendor": vendor,
                    "amount": round(amount, 2),
                    "memo": memo,
                }
            )
            seq += 1

        # --- Enterprise revenue ---
        ent_total = 0.0
        for cust_id, name, region in ENTERPRISE_CUSTOMERS:
            amt = enterprise_series[cust_id][idx]
            ent_total += amt
            add_txn("4000", "Money Ops US", "Sales", region, "Enterprise", cust_id, name, "", amt,
                     "Subscription revenue", 10)
        summary_rows.append(("4000", *ACCOUNTS["4000"], round(ent_total, 2)))

        # --- Mid-Market revenue ---
        mid_total = 0.0
        for cust_id, name, region in MIDMARKET_CUSTOMERS:
            amt = midmarket_series[cust_id][idx]
            if amt <= 0:
                continue
            mid_total += amt
            add_txn("4010", "Money Ops US", "Sales", region, "Mid-Market", cust_id, name, "", amt,
                     "Subscription revenue", 12)
        summary_rows.append(("4010", *ACCOUNTS["4010"], round(mid_total, 2)))

        # --- SMB revenue (pooled, split US/EU entity) ---
        smb_total = smb_us[idx] + smb_eu[idx]
        add_txn("4020", "Money Ops US", "Sales", "AMER", "SMB", "SMB-POOL", "SMB Pool (US)", "", smb_us[idx],
                 "Subscription revenue - SMB pool", 14)
        add_txn("4020", "Money Ops EU", "Sales", "EMEA", "SMB", "SMB-POOL", "SMB Pool (EU)", "", smb_eu[idx],
                 "Subscription revenue - SMB pool", 14)
        summary_rows.append(("4020", *ACCOUNTS["4020"], round(smb_total, 2)))

        # --- Hosting COGS ---
        hosting_total = hosting_base[idx]
        add_txn("5000", "Money Ops US", "Engineering", "AMER", "", "", "", "NimbusHost Cloud", hosting_base[idx],
                 "Production hosting - base usage", 5)
        if idx in hosting_overrun:
            overrun_amt = hosting_overrun[idx]
            hosting_total += overrun_amt
            add_txn("5000", "Money Ops US", "Engineering", "AMER", "", "", "", "CloudBeam Compute", overrun_amt,
                     "Elastic compute burst - usage overage", 22)
        summary_rows.append(("5000", *ACCOUNTS["5000"], round(hosting_total, 2)))

        # --- Sales & Marketing ---
        sm_total = sm_ads[idx] + sm_events[idx]
        add_txn("6000", "Money Ops US", "Sales & Marketing", "AMER", "", "", "", "Ad Platform Co", sm_ads[idx],
                 "Digital advertising spend", 8)
        add_txn("6000", "Money Ops US", "Sales & Marketing", "AMER", "", "", "", "Field Events LLC", sm_events[idx],
                 "Regional field events", 16)
        if idx in IDX_CONFERENCE:
            sm_total += conference_sponsorship
            add_txn("6000", "Money Ops US", "Sales & Marketing", "AMER", "", "", "", "TechConf Expo",
                     conference_sponsorship, "Annual sponsorship - TechConf Expo", 20)
        summary_rows.append(("6000", *ACCOUNTS["6000"], round(sm_total, 2)))

        # --- G&A ---
        ga_total = ga_base[idx]
        add_txn("6010", "Money Ops US", "G&A", "AMER", "", "", "", "Corporate Services Inc", ga_base[idx],
                 "Corporate overhead allocation", 9)
        if idx == IDX_LEGAL_FEE:
            ga_total += legal_fee
            add_txn("6010", "Money Ops US", "G&A", "AMER", "", "", "", "Latham Legal Advisors", legal_fee,
                     "M&A diligence - Project Falcon", 24)
        summary_rows.append(("6010", *ACCOUNTS["6010"], round(ga_total, 2)))

        # --- R&D ---
        add_txn("6020", "Money Ops US", "R&D", "AMER", "", "", "", "Internal Payroll Allocation", rnd_base[idx],
                 "Engineering payroll allocation", 1)
        summary_rows.append(("6020", *ACCOUNTS["6020"], round(rnd_base[idx], 2)))

        # --- Travel ---
        add_txn("6100", "Money Ops US", "Sales", "AMER", "", "", "", "Corporate Travel Partners", travel_base[idx],
                 "T&E - travel partners", 18)
        summary_rows.append(("6100", *ACCOUNTS["6100"], round(travel_base[idx], 2)))

        with (summaries_dir / f"{period}.csv").open("w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["period", "account_code", "account_name", "account_type", "amount"])
            for account_code, name, atype, amount in summary_rows:
                writer.writerow([period, account_code, name, atype, amount])

        with (txns_dir / f"{period}.csv").open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(
                f,
                fieldnames=["txn_id", "period", "txn_date", "account_code", "entity", "department", "region",
                            "segment", "customer_id", "customer_name", "vendor", "amount", "memo"],
            )
            writer.writeheader()
            for row in txn_rows:
                writer.writerow(row)

    print(f"seeded {N} periods ({PERIODS[0]}..{PERIODS[-1]}) into {config.FINANCIALS_DIR}")


if __name__ == "__main__":
    main()
