"""PRISM tracing wiring for the reconciliation graph.

Instrumentation must never take down the pipeline, so every PRISM call here
degrades to a no-op if the SDK is missing or the collector is unreachable.
"""

import sys

from app import config


def get_handler(session_id: str, agent_name: str = "ap-reconciliation-graph"):
    if not (config.PRISM_API_KEY and config.PRISM_PROJECT_ID):
        print("[prism] no credentials in env; tracing disabled", file=sys.stderr)
        return None
    try:
        from prismtrace import PRISMtraceLangGraphHandler

        return PRISMtraceLangGraphHandler(
            api_key=config.PRISM_API_KEY,
            project_id=config.PRISM_PROJECT_ID,
            host=config.PRISM_HOST,
            session_id=session_id,
            agent_name=agent_name,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"[prism] handler init failed, tracing disabled: {exc}", file=sys.stderr)
        return None


def instrument(compiled_graph, handler):
    """Inject PRISM callbacks into invoke/stream so call sites stay clean."""
    if handler is None:
        return compiled_graph
    try:
        from prismtrace import wrap_langgraph

        return wrap_langgraph(compiled_graph, handler)
    except Exception as exc:  # noqa: BLE001
        print(f"[prism] wrap_langgraph failed, running untraced: {exc}", file=sys.stderr)
        return compiled_graph


def flush(handler):
    if handler is None:
        return
    try:
        handler.flush()
    except Exception as exc:  # noqa: BLE001
        print(f"[prism] flush failed: {exc}", file=sys.stderr)
