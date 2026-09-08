"""Model access. Absent a configured model the pipeline still runs, deterministically."""

from app import config


def get_llm():
    """Return a chat model, or None when no model is configured.

    The serverless Qwen endpoint on Modal is the only provider (scale-to-zero,
    so the first call after idle time eats a cold start). With MODAL_QWEN_URL
    unset every caller falls back to its deterministic path.
    """
    if config.MODAL_QWEN_URL:
        from langchain_openai import ChatOpenAI

        # No max_tokens: langchain-openai renames it to max_completion_tokens,
        # a field vLLM 0.6.3's OpenAI-compatible server rejects as unknown.
        # vLLM falls back to its own default within --max-model-len anyway.
        return ChatOpenAI(
            model=config.QWEN_MODEL_NAME,
            base_url=config.MODAL_QWEN_URL.rstrip("/") + "/v1",
            api_key=f"{config.MODAL_KEY}.{config.MODAL_SECRET}",
            temperature=0,
            # Bounded, and no retries: the client's own default is a ten-minute
            # wait with retries on top, which turns one cold start into a hang
            # that outlives the UI session waiting on it. Fail fast into the
            # deterministic path instead -- see config.LLM_TIMEOUT_S.
            timeout=config.LLM_TIMEOUT_S,
            max_retries=0,
        )
    return None
