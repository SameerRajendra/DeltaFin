"""LangGraph orchestration for AP invoice reconciliation.

ingest -> extract -> match -> controls -> narrate -> workpaper -> queue for human review
"""

import uuid
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from app import controls, extraction, llm as llm_module, store, tracing, workpaper

NARRATIVE_PROMPT = """You are a senior accounts-payable auditor writing a workpaper note.
Given the reconciliation findings below, write 3-5 sentences stating what was checked,
what failed, and what the human approver should do next. Be specific and factual.

Invoice: {invoice}
Purchase order: {po}
Bank activity: {bank}
Exceptions: {exceptions}
Recommendation: {recommendation}"""


class ReconState(TypedDict, total=False):
    run_id: str
    source_file: str
    raw_text: str
    extracted: dict
    vendor: dict | None
    po: dict | None
    bank_txn: dict | None
    prior_invoice: dict | None
    exceptions: list[dict]
    recommendation: str
    narrative: str
    workpaper_path: str
    invoice_id: int


def _template_narrative(state):
    extracted = state.get("extracted") or {}
    exceptions = state.get("exceptions") or []
    header = (
        f"Invoice {extracted.get('invoice_number')} from {extracted.get('vendor_name')} "
        f"for {extracted.get('total_amount')} {extracted.get('currency')} was extracted from "
        f"{state.get('source_file')} and matched against the vendor master, open purchase orders, "
        f"and the bank feed."
    )
    if not exceptions:
        return header + " All three-way match attributes agree and no control exceptions were raised. Recommend approval."
    detail = " ".join(f"[{e['severity'].upper()}] {e['detail']}" for e in exceptions)
    return (
        header
        + f" {len(exceptions)} control exception(s) were raised: {detail} "
        + f"Agent recommendation is to {state.get('recommendation')} pending human approval."
    )


def build_graph(llm=None):
    def ingest(state: ReconState) -> dict[str, Any]:
        return {"raw_text": extraction.load_document(state["source_file"])}

    def extract_fields(state: ReconState, config=None) -> dict[str, Any]:
        return {"extracted": extraction.extract(state["raw_text"], llm=llm, config=config)}

    def match_records(state: ReconState) -> dict[str, Any]:
        extracted = state["extracted"]
        conn = store.connect()
        try:
            vendor = store.find_vendor(conn, extracted.get("vendor_name"))
            return {
                "vendor": vendor,
                "po": store.find_po(conn, extracted.get("po_number")),
                "bank_txn": store.find_bank_payment(
                    conn, extracted.get("invoice_number"), extracted.get("total_amount")
                ),
                "prior_invoice": store.find_prior_invoice(conn, extracted.get("invoice_number")),
                "extracted": {**extracted, "vendor_id": (vendor or {}).get("id")},
            }
        finally:
            conn.close()

    def evaluate_controls(state: ReconState) -> dict[str, Any]:
        exceptions, recommendation = controls.evaluate(
            state["extracted"],
            state.get("vendor"),
            state.get("po"),
            state.get("bank_txn"),
            state.get("prior_invoice"),
        )
        return {"exceptions": exceptions, "recommendation": recommendation}

    def draft_narrative(state: ReconState, config=None) -> dict[str, Any]:
        if llm is None:
            return {"narrative": _template_narrative(state)}
        prompt = NARRATIVE_PROMPT.format(
            invoice=state.get("extracted"),
            po=state.get("po"),
            bank=state.get("bank_txn"),
            exceptions=state.get("exceptions"),
            recommendation=state.get("recommendation"),
        )
        response = llm.invoke(prompt, config=config)
        text = response.content if hasattr(response, "content") else str(response)
        return {"narrative": text.strip()}

    def compile_workpaper(state: ReconState) -> dict[str, Any]:
        return {"workpaper_path": workpaper.build(state)}

    def queue_for_review(state: ReconState) -> dict[str, Any]:
        conn = store.connect()
        try:
            invoice_id = store.save_invoice(
                conn,
                state["extracted"],
                state.get("exceptions") or [],
                state.get("recommendation"),
                state.get("narrative"),
                state.get("workpaper_path"),
                state.get("run_id"),
                state.get("source_file"),
            )
        finally:
            conn.close()
        return {"invoice_id": invoice_id}

    builder = StateGraph(ReconState)
    builder.add_node("ingest_document", ingest)
    builder.add_node("extract_fields", extract_fields)
    builder.add_node("match_records", match_records)
    builder.add_node("evaluate_controls", evaluate_controls)
    builder.add_node("draft_narrative", draft_narrative)
    builder.add_node("compile_workpaper", compile_workpaper)
    builder.add_node("queue_for_review", queue_for_review)

    builder.add_edge(START, "ingest_document")
    builder.add_edge("ingest_document", "extract_fields")
    builder.add_edge("extract_fields", "match_records")
    builder.add_edge("match_records", "evaluate_controls")
    builder.add_edge("evaluate_controls", "draft_narrative")
    builder.add_edge("draft_narrative", "compile_workpaper")
    builder.add_edge("compile_workpaper", "queue_for_review")
    builder.add_edge("queue_for_review", END)
    return builder.compile()


def process_invoice(source_file, session_id=None):
    """Run one invoice through the traced graph. One invoice = one PRISM trajectory."""
    run_id = uuid.uuid4().hex[:8]
    session_id = session_id or f"ap-recon-{run_id}"

    handler = tracing.get_handler(session_id)
    graph = tracing.instrument(build_graph(llm=llm_module.get_llm()), handler)
    try:
        return graph.invoke({"run_id": run_id, "source_file": str(source_file)})
    finally:
        tracing.flush(handler)
