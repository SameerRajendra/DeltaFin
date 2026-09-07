"""Run the variance-explanation agent.

    python run_flux.py                  # latest period vs the one before it
    python run_flux.py 2026-08          # a specific period vs the one before it
    python run_flux.py --replay         # walk every period in order, oldest
                                         # first, so institutional memory
                                         # actually accumulates run over run
"""

import subprocess
import sys

from app.flux import graph as flux_graph, ingest


def _run_one(period=None, prior_period=None):
    result = flux_graph.process_period(period=period, prior_period=prior_period)
    print(f"\n=== {result['prior_period']} -> {result['period']} ===")
    findings = result.get("findings") or []
    if not findings:
        print("  no material variances")
    for f in findings:
        print(f"  [{f['confidence']:<4}] {f['account_name']}: {f['headline']}")
    print(f"  brief     : {result.get('brief_path')}")
    print(f"  workpaper : {result.get('workpaper_path')}")
    print(f"  analysis  : {result.get('analysis_path')}")
    print(f"  actions   : {result.get('actions_path')}")
    return result


def _replay():
    conn = ingest.connect()
    try:
        periods = ingest.list_periods(conn)
    finally:
        conn.close()
    # First period has no prior to compare against; start from the second.
    # Each period runs as its own subprocess: memory persists to disk (SQLite +
    # JSON) between runs anyway, and a fresh process avoids DuckDB's memory
    # accounting drifting across a long walk -- which is also how a real nightly
    # close job would run.
    for period in periods[1:]:
        print(f"\n--- launching {period} ---", flush=True)
        subprocess.run([sys.executable, __file__, period], check=True)


def main():
    args = sys.argv[1:]
    if "--replay" in args:
        _replay()
        return

    period = args[0] if args else None
    _run_one(period=period)


if __name__ == "__main__":
    main()
