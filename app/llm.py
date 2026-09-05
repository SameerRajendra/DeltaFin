"""Model access. Absent a provider key the pipeline still runs, deterministically."""

from app import config


def get_llm():
    """Return a chat model, or None when no provider is configured.

    Prefers the serverless Qwen endpoint on Modal (scale-to-zero, so the first
    call after idle time eats a cold start) over Anthropic when both are set.
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
        )
    if not config.ANTHROPIC_API_KEY:
        return None
    from langchain_anthropic import ChatAnthropic

    return ChatAnthropic(
        model=config.MODEL,
        api_key=config.ANTHROPIC_API_KEY,
        max_tokens=1500,
        temperature=0,
    )
