"""Keep neutral counters authoritative while feeding existing task accounting."""


def neutral_counters(usage):
    known = all(value is not None for value in
        (usage.input_tokens, usage.cache_read_tokens, usage.cache_write_tokens))
    return {"input": usage.input_tokens,
        "cacheRead": usage.cache_read_tokens, "cacheCreation": usage.cache_write_tokens,
        "uncachedInput": max(0, usage.input_tokens - usage.cache_read_tokens - usage.cache_write_tokens)
            if known else None, "output": usage.output_tokens}


def record_usage(logger, result, source: str, *, conversation_id=None):
    usage = result.usage
    counters = neutral_counters(usage)
    logger.write(source + ".model_usage", {**counters, "diagnostics": result.diagnostics})
    logger.record_llm_usage(source=source, provider=result.message.provider or "unknown",
        model=result.message.model or "unknown", usage={**usage.legacy(), **result.diagnostics,
            "counterAvailability": {key: value is not None for key, value in counters.items()}},
        conversation_id=conversation_id)
