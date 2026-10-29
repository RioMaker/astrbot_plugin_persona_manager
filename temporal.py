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
    start = end = None
    precision = "day"
    try:
        anchor = datetime.fromtimestamp(float(anchor_ts), BEIJING)
        day = anchor.replace(hour=0, minute=0, second=0, microsecond=0)
        # Parse one complete expression. Alternatives, ranges and qualifiers such
        # as "昨天以前" must not silently become an exact day.
        clock_match = re.search(
            r"(上午|中午|下午|晚上|傍晚|凌晨)?\s*(?<!\d)(\d{1,2})(?:[:：](\d{2})|点(?:(\d{1,2})分?|半)?)$",
            raw,
        )
        date = raw[: clock_match.start()].strip() if clock_match else raw
        explicit = re.fullmatch(r"(\d{4})[-/.年](\d{1,2})[-/.月](\d{1,2})[日号]?", date)
        short = re.fullmatch(
            r"(去年|今年|明年|前年|后年)?(\d{1,2})月(\d{1,2})[日号]?", date
        )
        days = {
            "大前天": -3,
            "前天": -2,
            "昨天": -1,
            "今天": 0,
            "明天": 1,
            "后天": 2,
            "大后天": 3,
        }
        years = {"前年": -2, "去年": -1, "今年": 0, "明年": 1, "后年": 2}
        week = re.fullmatch(r"(上|下|本|这)(?:周|星期)([一二三四五六日天末])?", date)
        if explicit:
            start = datetime(*(int(x) for x in explicit.groups()), tzinfo=BEIJING)
        elif short:
            start = datetime(
                anchor.year + years.get(short[1], 0),
                int(short[2]),
                int(short[3]),
                tzinfo=BEIJING,
            )
        elif date in days:
            start = day + timedelta(days=days[date])
        elif week:
            offset = {"上": -1, "下": 1, "本": 0, "这": 0}[week[1]]
            start = day - timedelta(days=day.weekday()) + timedelta(weeks=offset)
            if week[2] == "末":
                start += timedelta(days=5)
                end, precision = start + timedelta(days=2), "weekend"
            elif week[2]:
                start += timedelta(
                    days="一二三四五六日".index(week[2].replace("天", "日"))
                )
            else:
                end, precision = start + timedelta(days=7), "week"
        elif re.fullmatch(r"上(?:个)?月", date):
            start = _month_start(anchor.year, anchor.month) - timedelta(days=1)
            start = _month_start(start.year, start.month)
            end, precision = _next_month(start), "month"
        elif re.fullmatch(r"下(?:个)?月", date):
            start = _next_month(_month_start(anchor.year, anchor.month))
            end, precision = _next_month(start), "month"
        elif re.fullmatch(r"(?:本|这(?:个)?)月", date):
            start = _month_start(anchor.year, anchor.month)
            end, precision = _next_month(start), "month"
        elif date in years or re.fullmatch(r"\d{4}年", date):
            year = anchor.year + years[date] if date in years else int(date[:-1])
            start, end = _month_start(year, 1), _month_start(year + 1, 1)
            precision = "year"
        if start is None or (clock_match and precision != "day"):
            return result
        if clock_match:
            period, hour = clock_match[1], int(clock_match[2])
            minute = int(
                clock_match[3]
                or clock_match[4]
                or (30 if "半" in clock_match[0] else 0)
            )
            if period and not 1 <= hour <= 12:
                return result
            if period in {"下午", "晚上", "傍晚"} and hour < 12:
                hour += 12
            elif period == "凌晨" and hour == 12:
                hour = 0
            start = start.replace(hour=hour, minute=minute)
            precision = (
                "minute"
                if clock_match[3] or clock_match[4] or "半" in clock_match[0]
                else "hour"
            )
            end = start + (
                timedelta(minutes=1) if precision == "minute" else timedelta(hours=1)
            )
        if end is None:
            end = start + timedelta(days=1)
        result.update(start=start.timestamp(), end=end.timestamp(), precision=precision)
    except (ValueError, TypeError, OverflowError, OSError):
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


def is_past_reference(text: str) -> bool:
    """Unknown historical references must not be promoted to current attributes."""
    return bool(
        re.search(
            r"以前|之前|从前|曾经|过去|去年|前年|上(?:个)?月|上(?:周|星期)|前天|昨天",
            text or "",
        )
    )
