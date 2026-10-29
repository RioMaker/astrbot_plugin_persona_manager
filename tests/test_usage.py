from types import SimpleNamespace

import pytest
from astrbot_plugin_persona_manager.usage import (
    estimate_request_tokens,
    normalize_usage,
)


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        (
            {"usage": {"input_other": 100, "input_cached": 50, "output": 20}},
            (150, 20, 170),
        ),
        (
            SimpleNamespace(
                usage=SimpleNamespace(input_other=0, input_cached=0, output=0)
            ),
            (0, 0, 0),
        ),
        (
            {
                "usage": {
                    "prompt_tokens": 150,
                    "completion_tokens": 20,
                    "prompt_tokens_details": {"cached_tokens": 50},
                }
            },
            (150, 20, 170),
        ),
        (
            {
                "raw_completion": {
                    "usage": {
                        "input_tokens": 10,
                        "cache_read_input_tokens": 20,
                        "cache_creation_input_tokens": 30,
                        "output_tokens": 5,
                    }
                }
            },
            (60, 5, 65),
        ),
        (
            {
                "raw_completion": {
                    "usageMetadata": {
                        "promptTokenCount": 10,
                        "candidatesTokenCount": 3,
                        "thoughtsTokenCount": 2,
                        "totalTokenCount": 15,
                    }
                }
            },
            (10, 5, 15),
        ),
        (
            {"usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 2}},
            (10, 5, 15),
        ),
        ({"usage": {"output": 8}}, (None, 8, None)),
        (
            {"usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}},
            (0, 0, 0),
        ),
        ({"usage": {"total_tokens": 12}}, (None, None, 12)),
        ({}, (None, None, None)),
        ({"usage": None}, (None, None, None)),
        (
            {
                "usage": {"input_other": 0, "input_cached": 0, "output": 0},
                "raw_completion": {"usage": None},
            },
            (None, None, None),
        ),
        (
            {
                "usage": {"input_other": 0, "input_cached": 0, "output": 0},
                "raw_completion": {},
            },
            (None, None, None),
        ),
        (
            {"usage": {"prompt_tokens": True, "completion_tokens": -2}},
            (None, None, None),
        ),
        (
            {"usage": {"prompt_tokens": "20", "completion_tokens": 4.5}},
            (None, None, None),
        ),
        (
            {
                "raw_completion": {
                    "usage": {
                        "input_tokens": 10,
                        "cache_read_input_tokens": -1,
                        "output_tokens": 4,
                    }
                }
            },
            (None, 4, None),
        ),
    ],
)
def test_reported_usage_is_normalized_without_cache_duplication(response, expected):
    actual = normalize_usage(response)
    assert (
        actual["input_tokens"],
        actual["output_tokens"],
        actual["total_tokens"],
    ) == expected
    assert actual["reported"] is (expected[2] is not None)


def test_estimate_uses_utf8_bytes_and_does_not_claim_reported_usage():
    result = estimate_request_tokens("你好", "规则", 1200)
    assert result == {"estimated_input": 268, "reserved_tokens": 1468}
    assert "reported" not in result


@pytest.mark.parametrize("cap", [-1, True, "1200", None])
def test_estimate_rejects_invalid_output_cap(cap):
    with pytest.raises(ValueError):
        estimate_request_tokens("text", "system", cap)
