"""Run invoices through the traced reconciliation graph.

    python run_demo.py                # all sample invoices
    python run_demo.py path/to/x.txt  # one document
"""

import sys

from app import config
from app.graph import process_invoice

# Ordered so the original invoice is processed before its duplicate.
DEMO_ORDER = [
    "INV-1001_acme.txt",
    "INV-2001_northwind.txt",
    "INV-1001-DUP_acme.txt",
    "INV-7788_globex.txt",
    "INV-4102_fabrikam.txt",
]


def main():
    if len(sys.argv) > 1:
        targets = [sys.argv[1]]
    else:
        targets = [str(config.SAMPLES_DIR / name) for name in DEMO_ORDER]

    for target in targets:
        result = process_invoice(target)
        extracted = result.get("extracted") or {}
        exceptions = result.get("exceptions") or []
        print(f"\n=== {extracted.get('invoice_number')} | {extracted.get('vendor_name')} ===")
        print(f"  source        : {target}")
        print(f"  total         : {extracted.get('total_amount')} {extracted.get('currency')}")
        print(f"  po            : {extracted.get('po_number') or '(none)'}")
        print(f"  recommendation: {(result.get('recommendation') or '').upper()}")
        print(f"  workpaper     : {result.get('workpaper_path')}")
        if exceptions:
            for exc in exceptions:
                print(f"    - [{exc['severity']:<8}] {exc['code']}: {exc['detail']}")
        else:
            print("    - no exceptions")


if __name__ == "__main__":
    main()
