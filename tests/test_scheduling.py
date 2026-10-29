from dataclasses import replace
from datetime import datetime

import pytest
from astrbot_plugin_persona_manager.scheduling import (
    batch_due,
    batch_policy,
    in_windows,
    next_window_start,
    parse_window,
)
from astrbot_plugin_persona_manager.settings import Settings
from astrbot_plugin_persona_manager.temporal import BEIJING


def stamp(value):
    return datetime.fromisoformat(value).replace(tzinfo=BEIJING).timestamp()


@pytest.mark.parametrize(
    "value", ["1:00-07:00", "24:00-01:00", "00:00-00:00", "12:60-13:00", "anytime", 2]
)
def test_invalid_windows_are_rejected(value):
    with pytest.raises(ValueError):
        parse_window(value)
    assert not in_windows([value], stamp("2026-10-29T02:00"), empty_allowed=True)


@pytest.mark.parametrize(
    "time,allowed",
    [
        ("2026-10-29T22:59", False),
        ("2026-10-29T23:00", True),
        ("2026-10-30T00:00", True),
        ("2026-10-30T06:59", True),
        ("2026-10-30T07:00", False),
    ],
)
def test_daily_price_window_spans_midnight_without_server_timezone(time, allowed):
    assert in_windows(["23:00-07:00"], stamp(time), empty_allowed=False) == allowed


def test_empty_cheap_windows_never_invent_provider_discount():
    policy = batch_policy(Settings(), "interaction", stamp("2026-10-29T03:00"))
    assert policy["phase"] == "regular"
    assert policy["minimum"] == 3
    assert policy["allowed"]
    assert policy["interval_seconds"] == 1800


def test_price_phase_controls_sample_size_and_interval():
    settings = Settings(cheap_learning_windows=frozenset({"23:00-07:00"}))
    now = stamp("2026-10-29T03:00")
    assert batch_policy(settings, "interaction", now)["minimum"] == 1
    assert batch_policy(settings, "observation", now)["minimum"] == 5
    assert batch_policy(settings, "observation", now)["interval_seconds"] == 300
    regular = batch_policy(settings, "observation", stamp("2026-10-29T12:00"))
    assert regular["minimum"] == 20 and regular["interval_seconds"] == 1800


def test_price_awareness_off_preserves_old_batching_but_obeys_allowed_window():
    settings = Settings(
        price_aware_learning=False,
        cheap_learning_windows=frozenset({"23:00-07:00"}),
        learning_windows=frozenset({"01:00-02:00"}),
    )
    policy = batch_policy(settings, "interaction", stamp("2026-10-29T03:00"))
    assert policy["phase"] == "regular" and policy["minimum"] == 1
    assert not policy["allowed"]
    assert policy["next_window_at"] == stamp("2026-10-30T01:00")


def test_next_daily_window_and_max_wait_small_batch_fallback():
    now = stamp("2026-10-29T12:00")
    assert next_window_start(["13:00-15:00", "23:00-07:00"], now) == stamp(
        "2026-10-29T13:00"
    )
    settings = replace(Settings(), max_learning_wait_seconds=600)
    assert not batch_due(settings, [], 3, now)
    assert not batch_due(settings, [{"sent_at": now - 599}], 3, now)
    assert batch_due(settings, [{"sent_at": now - 600}], 3, now)
