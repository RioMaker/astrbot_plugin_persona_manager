"""Conservative Chinese time resolution, anchored to the source message in UTC+8."""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

BEIJING = timezone(timedelta(hours=8))


def _month_start(year: int, month: int) -> datetime:
    return datetime(year, month, 1, tzinfo=BEIJING)


def _next_month(value: datetime) -> datetime:
    return _month_start(value.year + (value.month == 12), value.month % 12 + 1)


def resolve_time(text: str | None, anchor_ts: float) -> dict:
    """Return an inclusive start/exclusive end UTC interval; unknown stays null.

    This does not infer that a scheduled event happened. The caller stores the
    event state separately. Missing years use the source message's year, never
    the ingestion date. Ambiguous or invalid calendar values remain unknown.
    """
    raw = str(text or "").strip()
    result = {"start": None, "end": None, "precision": "unknown", "text": raw}
    if not raw:
        return result
    anchor = datetime.fromtimestamp(float(anchor_ts), BEIJING)
    day = anchor.replace(hour=0, minute=0, second=0, microsecond=0)
    start = end = None
    precision = "day"
    try:
        explicit = re.search(r"(?<!\d)(\d{4})[-/.年](\d{1,2})[-/.月](\d{1,2})日?", raw)
        short = re.search(r"(?<!\d)(\d{1,2})月(\d{1,2})[日号]?", raw)
        if explicit:
            start = datetime(*(int(x) for x in explicit.groups()), tzinfo=BEIJING)
        elif short:
            start = datetime(
                anchor.year, *(int(x) for x in short.groups()), tzinfo=BEIJING
            )
        else:
            for word, offset in (
                ("大前天", -3),
                ("前天", -2),
                ("昨天", -1),
                ("今天", 0),
                ("明天", 1),
                ("后天", 2),
            ):
                if word in raw:
                    start = day + timedelta(days=offset)
                    break
            if start is None:
                week = re.search(
                    r"(上|下|本|这)(?:周|星期)([一二三四五六日天末])?", raw
                )
                if week:
                    offset = {"上": -1, "下": 1, "本": 0, "这": 0}[week[1]]
                    start = (
                        day - timedelta(days=day.weekday()) + timedelta(weeks=offset)
                    )
                    if week[2] == "末":
                        start += timedelta(days=5)
                        end, precision = start + timedelta(days=2), "weekend"
                    elif week[2]:
                        start += timedelta(
                            days="一二三四五六日".index(week[2].replace("天", "日"))
                        )
                    else:
                        end, precision = start + timedelta(days=7), "week"
                elif re.search(r"上(?:个)?月", raw):
                    start = _month_start(anchor.year, anchor.month) - timedelta(days=1)
                    start = _month_start(start.year, start.month)
                    end, precision = _next_month(start), "month"
                elif re.search(r"下(?:个)?月", raw):
                    start = _next_month(_month_start(anchor.year, anchor.month))
                    end, precision = _next_month(start), "month"
                elif re.search(r"(?:本|这(?:个)?)月", raw):
                    start = _month_start(anchor.year, anchor.month)
                    end, precision = _next_month(start), "month"
        if start is None:
            return result
        # Only attach a clock time to a concrete day, not a whole week/month.
        clock_match = re.search(
            r"(?<!\d)(\d{1,2})(?:[:：](\d{2})|点(?:(\d{1,2})分?|半)?)", raw
        )
        if precision == "day" and clock_match:
            hour = int(clock_match[1])
            minute = int(
                clock_match[2]
                or clock_match[3]
                or (30 if "半" in clock_match[0] else 0)
            )
            if any(word in raw for word in ("下午", "晚上", "傍晚")) and hour < 12:
                hour += 12
            elif "凌晨" in raw and hour == 12:
                hour = 0
            start = start.replace(hour=hour, minute=minute)
            precision = "minute" if minute or clock_match[2] else "hour"
            end = (
                start + timedelta(minutes=1)
                if precision == "minute"
                else start + timedelta(hours=1)
            )
        if end is None:
            end = start + timedelta(days=1)
        result.update(start=start.timestamp(), end=end.timestamp(), precision=precision)
    except (ValueError, OverflowError, OSError):
        return result
    return result


parse_time = resolve_time


def infer_state(text: str) -> str:
    """Conservative fallback for explicit plans, cancellations and completion."""
    if any(x in text for x in ("取消", "不去了", "没去成", "计划作废")):
        return "cancelled"
    if any(x in text for x in ("没完成", "没全连", "还没", "正在", "在练")):
        return "in_progress"
    if any(x in text for x in ("已经完成", "完成了", "全连了", "练完了", "已经去了")):
        return "completed"
    if any(x in text for x in ("打算", "计划", "准备", "想要")):
        return "planned"
    return "asserted"
