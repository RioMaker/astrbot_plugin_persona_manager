"""Provider usage normalization and explicitly estimated learning reservations."""

from collections.abc import Mapping


def _field(value, name, default=None):
    return (
        value.get(name, default)
        if isinstance(value, Mapping)
        else getattr(value, name, default)
    )


def token_count(value):
    """Unknown, malformed and negative counters are not measured zeroes."""
    return value if type(value) is int and 0 <= value <= 10**12 else None


def _count(value, *names):
    for name in names:
        result = token_count(_field(value, name))
        if result is not None:
            return result
    return None


def usage_values(input_tokens=None, output_tokens=None, total_tokens=None):
    input_tokens, output_tokens, total_tokens = (
        token_count(input_tokens),
        token_count(output_tokens),
        token_count(total_tokens),
    )
    known_sum = (input_tokens or 0) + (output_tokens or 0)
    if input_tokens is not None and output_tokens is not None:
        total_tokens = max(total_tokens or 0, known_sum)
    elif total_tokens is not None and total_tokens < known_sum:
        # Inconsistent upstream totals must never lower the known consumption.
        total_tokens = None
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
        "reported": total_tokens is not None,
    }


def _standard(value):
    input_tokens = _count(value, "prompt_tokens", "input_tokens")
    output_tokens = _count(value, "completion_tokens", "output_tokens")
    if (
        _field(value, "cache_read_input_tokens") is not None
        or _field(value, "cache_creation_input_tokens") is not None
    ):
        # Anthropic input excludes reads and writes; OpenAI already includes cache.
        reads = _count(value, "cache_read_input_tokens")
        writes = _count(value, "cache_creation_input_tokens")
        malformed_cache = any(
            _field(value, key) is not None and _count(value, key) is None
            for key in ("cache_read_input_tokens", "cache_creation_input_tokens")
        )
        input_tokens = (
            None
            if input_tokens is None or malformed_cache
            else input_tokens + (reads or 0) + (writes or 0)
        )
    return usage_values(input_tokens, output_tokens, _count(value, "total_tokens"))


def normalize_usage(response):
    """Read reported counters without saving prompts or provider response bodies."""
    native = _field(response, "usage")
    raw = _field(response, "raw_completion")
    # Some AstrBot adapters create TokenUsage() when the server omitted usage.
    zero_sentinel = (
        raw is not None
        and _field(raw, "usage") is None
        and all(
            _count(native, key) == 0
            for key in ("input_other", "input_cached", "output")
        )
    )
    if native is not None and not zero_sentinel:
        other, cached, output = (
            _count(native, key) for key in ("input_other", "input_cached", "output")
        )
        if other is not None and cached is not None:
            return usage_values(other + cached, output)
        if output is not None:
            return usage_values(None, output)
        result = _standard(native)
        if any(
            result[key] is not None
            for key in ("input_tokens", "output_tokens", "total_tokens")
        ):
            return result
    upstream = _field(raw, "usage")
    if upstream is not None:
        return _standard(upstream)
    google = _field(raw, "usage_metadata", _field(raw, "usageMetadata"))
    if google is not None:
        input_tokens = _count(google, "prompt_token_count", "promptTokenCount")
        output_tokens = _count(google, "candidates_token_count", "candidatesTokenCount")
        thoughts = _count(google, "thoughts_token_count", "thoughtsTokenCount")
        if output_tokens is not None:
            output_tokens += thoughts or 0
        return usage_values(
            input_tokens,
            output_tokens,
            _count(google, "total_token_count", "totalTokenCount"),
        )
    return usage_values()


def estimate_request_tokens(prompt, system_prompt, output_limit):
    """Conservative UTF-8-byte estimate plus envelope margin, never a bill.

    The margin covers ordinary message framing. A provider can add its own hidden
    content or ignore the output cap, so this is not a universal tokenizer bound.
    """
    output_limit = token_count(output_limit)
    if (
        output_limit is None
        or not isinstance(prompt, str)
        or not isinstance(system_prompt, str)
    ):
        raise ValueError("Invalid learning request estimate")
    input_estimate = (
        len(prompt.encode("utf-8")) + len(system_prompt.encode("utf-8")) + 256
    )
    return {
        "estimated_input": input_estimate,
        "reserved_tokens": input_estimate + output_limit,
    }
