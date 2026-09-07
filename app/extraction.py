"""Document extraction: invoice text -> structured fields.

Uses the LLM when a provider key is present, otherwise a deterministic parser
so the graph is demoable without provider credentials.
"""

import json
import re
from pathlib import Path

LINE_ITEM_RE = re.compile(
    r"^(?P<sku>[A-Z0-9\-]+)\s{2,}(?P<desc>.+?)\s{2,}(?P<qty>\d+)\s+(?P<unit>[\d,]+\.\d{2})\s+(?P<amount>[\d,]+\.\d{2})\s*$"
)

EXTRACTION_PROMPT = """You are an accounts-payable document extraction engine.
Return ONLY a JSON object with these keys:
invoice_number, vendor_name, tax_id, invoice_date (YYYY-MM-DD), due_date (YYYY-MM-DD),
po_number (null if absent), currency, total_amount (number),
line_items (list of {{sku, description, qty, unit_price, amount}}).

Invoice text:
---
{text}
---"""


def load_document(path):
    path = Path(path)
    if path.suffix.lower() == ".pdf":
        from pypdf import PdfReader

        return "\n".join((page.extract_text() or "") for page in PdfReader(str(path)).pages)
    return path.read_text(encoding="utf-8")


def _money(value):
    return float(value.replace(",", "")) if value else None


def parse_deterministic(text):
    def grab(pattern):
        match = re.search(pattern, text, re.IGNORECASE)
        return match.group(1).strip() if match else None

    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    total_match = re.search(r"Total Due:\s*([A-Z]{3})?\s*([\d,]+\.\d{2})", text, re.IGNORECASE)

    line_items = []
    for line in text.splitlines():
        match = LINE_ITEM_RE.match(line.rstrip())
        if match:
            line_items.append(
                {
                    "sku": match.group("sku"),
                    "description": match.group("desc").strip(),
                    "qty": int(match.group("qty")),
                    "unit_price": _money(match.group("unit")),
                    "amount": _money(match.group("amount")),
                }
            )

    return {
        "invoice_number": grab(r"Invoice Number:\s*(\S+)"),
        "vendor_name": lines[0].title() if lines else None,
        "tax_id": grab(r"Tax ID:\s*(\S+)"),
        "invoice_date": grab(r"Invoice Date:\s*(\S+)"),
        "due_date": grab(r"Due Date:\s*(\S+)"),
        "po_number": grab(r"PO Number:\s*(\S+)"),
        "currency": (total_match.group(1) if total_match and total_match.group(1) else "USD"),
        "total_amount": _money(total_match.group(2)) if total_match else None,
        "line_items": line_items,
        "extraction_method": "deterministic-parser",
    }


def extract(text, llm=None, config=None):
    if llm is None:
        return parse_deterministic(text)

    response = llm.invoke(EXTRACTION_PROMPT.format(text=text), config=config)
    payload = response.content if hasattr(response, "content") else str(response)
    match = re.search(r"\{.*\}", payload, re.DOTALL)
    if not match:
        return parse_deterministic(text)
    try:
        extracted = json.loads(match.group(0))
    except json.JSONDecodeError:
        return parse_deterministic(text)
    extracted["extraction_method"] = "llm"
    return extracted
