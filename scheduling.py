"""Price-window batching rules; never infer a provider's tariff from chat activity."""

import re
from datetime import datetime, timedelta

from .temporal import BEIJING


def parse_window(value: str) -> tuple[int, int]:
    """Parse an explicit Beijing-time half-open daily window, including midnight."""
    if not isinstance(value, str):
        raise ValueError("时间窗必须为 HH:MM-HH:MM")
    match = re.fullmatch(
        r"([01]\d|2[0-3]):([0-5]\d)-([01]\d|2[0-3]):([0-5]\d)", value.strip()
    )
    if not match:
        raise ValueError("时间窗必须为 HH:MM-HH:MM")
    start = int(match[1]) * 60 + int(match[2])
    end = int(match[3]) * 60 + int(match[4])
    if start == end:
        raise ValueError("时间窗起止不能相同；全天请留空")
    return start, end


def in_windows(values, now: float, *, empty_allowed: bool) -> bool:
    """Invalid configuration is closed, not silently converted to all day."""
    if not values:
        return empty_allowed
    clock = datetime.fromtimestamp(now, BEIJING)
    minute = clock.hour * 60 + clock.minute
    try:
        windows = [parse_window(value) for value in values]
    except (TypeError, ValueError):
        return False
    return any(
        start <= minute < end if start < end else minute >= start or minute < end
        for start, end in windows
    )


def next_window_start(values, now: float) -> float | None:
    if not values:
        return None
    clock = datetime.fromtimestamp(now, BEIJING)
    midnight = clock.replace(hour=0, minute=0, second=0, microsecond=0)
    try:
        starts = [
            midnight + timedelta(minutes=parse_window(value)[0]) for value in values
        ]
    except (TypeError, ValueError):
        return None
    return min(
        (start if start.timestamp() > now else start + timedelta(days=1)).timestamp()
        for start in starts
    )


def batch_policy(settings, purpose: str, now: float) -> dict:
    cheap = settings.price_aware_learning and in_windows(
        settings.cheap_learning_windows, now, empty_allowed=False
    )
    if purpose == "observation":
        minimum = (
            settings.cheap_observation_batch
            if cheap
            else settings.min_observation_batch
        )
    elif settings.price_aware_learning:
        minimum = (
            settings.cheap_interaction_batch
            if cheap
            else settings.regular_interaction_batch
        )
    else:
        minimum = 1
    allowed = in_windows(settings.learning_windows, now, empty_allowed=True)
    return {
        "allowed": allowed,
        "phase": "cheap" if cheap else "regular",
        "minimum": minimum,
        "interval_seconds": settings.cheap_learning_interval_seconds
        if cheap
        else settings.learning_interval_seconds,
        "next_window_at": None
        if allowed
        else next_window_start(settings.learning_windows, now),
    }


def batch_due(settings, rows: list[dict], minimum: int, now: float) -> bool:
    if len(rows) >= minimum:
        return True
    return (
        bool(rows)
        and now - min(row["sent_at"] for row in rows)
        >= settings.max_learning_wait_seconds
    )
