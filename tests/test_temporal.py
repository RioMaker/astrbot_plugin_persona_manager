from datetime import datetime

import pytest
from astrbot_plugin_persona_manager.temporal import BEIJING, infer_state, resolve_time


def stamp(value):
    return datetime.fromisoformat(value).replace(tzinfo=BEIJING).timestamp()


@pytest.mark.parametrize(
    "phrase,start,end,precision",
    [
        ("昨天", "2026-10-28", "2026-10-29", "day"),
        ("前天", "2026-10-27", "2026-10-28", "day"),
        ("今天", "2026-10-29", "2026-10-30", "day"),
        ("明天", "2026-10-30", "2026-10-31", "day"),
        ("下周", "2026-11-02", "2026-11-09", "week"),
        ("下周日", "2026-11-08", "2026-11-09", "day"),
        ("下周末", "2026-11-07", "2026-11-09", "weekend"),
        ("上月", "2026-09-01", "2026-10-01", "month"),
        ("下个月", "2026-11-01", "2026-12-01", "month"),
        ("2026年10月20日", "2026-10-20", "2026-10-21", "day"),
        ("10月20日", "2026-10-20", "2026-10-21", "day"),
        ("明天下午3点半", "2026-10-30T15:30", "2026-10-30T15:31", "minute"),
    ],
)
def test_source_anchored_intervals(phrase, start, end, precision):
    resolved = resolve_time(phrase, stamp("2026-10-29T08:00"))
    assert resolved == {
        "start": stamp(start),
        "end": stamp(end),
        "precision": precision,
        "text": phrase,
    }


def test_calendar_rollovers_and_leap_day():
    assert resolve_time("上个月", stamp("2026-01-02"))["start"] == stamp("2025-12-01")
    assert resolve_time("昨天", stamp("2024-03-01T00:05"))["start"] == stamp(
        "2024-02-29"
    )


@pytest.mark.parametrize(
    "phrase", ["最近", "以前", "下次", "2026-02-30", "明天25点", ""]
)
def test_unknown_and_invalid_times_do_not_invent_dates(phrase):
    assert resolve_time(phrase, stamp("2026-10-29"))["start"] is None


def test_timezone_uses_source_day_not_utc_day():
    source = datetime.fromisoformat("2026-10-28T16:05:00+00:00").timestamp()
    assert resolve_time("今天", source)["start"] == stamp("2026-10-29")


def test_plans_are_not_completion_and_negation_is_preserved():
    assert infer_state("我打算明天去机厅") == "planned"
    assert infer_state("我已经去了") == "completed"
    assert infer_state("还没完成") == "in_progress"
    assert infer_state("计划取消了") == "cancelled"
