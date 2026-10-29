"""One schema for the Dashboard editor, with strict writes and stable revisions."""

import hashlib
import json
import re
from dataclasses import asdict
from pathlib import Path

from .scheduling import parse_window
from .settings import INT_LIMITS, Settings

SCHEMA = json.loads(
    (Path(__file__).parent / "_conf_schema.json").read_text(encoding="utf-8")
)
SECTIONS = {
    "基础与范围": {
        "enabled",
        "persona_id",
        "observation_groups",
        "memory_enabled",
        "memory_disabled_groups",
        "ignored_qq",
    },
    "学习与模型": {
        "learning_enabled",
        "learning_provider_id",
        "batch_size",
        "model_timeout_seconds",
    },
    "Token 预算": {
        "daily_calls",
        "daily_group_calls",
        "daily_token_limit",
        "daily_group_token_limit",
        "max_output_tokens",
    },
    "价格时段与积累": {
        "price_aware_learning",
        "cheap_learning_windows",
        "learning_windows",
        "regular_interaction_batch",
        "cheap_interaction_batch",
        "cheap_observation_batch",
        "min_observation_batch",
        "learning_interval_seconds",
        "cheap_learning_interval_seconds",
        "max_learning_wait_seconds",
    },
    "进化方向": {"evolution_guidance"},
}


def values(config) -> dict:
    return {
        key: sorted(value) if isinstance(value, frozenset) else value
        for key, value in asdict(Settings.read(config)).items()
    }


def revision(config) -> str:
    encoded = json.dumps(
        values(config), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(encoded.encode()).hexdigest()


def page_config(config) -> dict:
    schema = {}
    for key, value in SCHEMA.items():
        field = dict(value)
        field["section"] = next(
            (name for name, keys in SECTIONS.items() if key in keys), "保留与高级"
        )
        if key in INT_LIMITS:
            _, field["minimum"], field["maximum"] = INT_LIMITS[key]
        if key == "evolution_guidance":
            field["maxLength"] = 4000
        schema[key] = field
    return {"values": values(config), "revision": revision(config), "schema": schema}


def validate_changes(current, changes) -> dict:
    if not isinstance(changes, dict) or not changes:
        raise ValueError("没有需要保存的配置")
    if set(changes) - set(SCHEMA):
        raise ValueError("包含未知配置项")
    result = values(current)
    for key, value in changes.items():
        field = SCHEMA[key]
        label, kind = field.get("description", key), field["type"]
        if kind == "bool":
            if not isinstance(value, bool):
                raise ValueError(f"{label}必须是开关值")
        elif kind == "int":
            _, minimum, maximum = INT_LIMITS[key]
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or not minimum <= value <= maximum
            ):
                raise ValueError(f"{label}须为 {minimum}～{maximum} 的整数")
        elif kind == "list":
            if (
                not isinstance(value, list)
                or len(value) > 500
                or any(not isinstance(item, str) for item in value)
            ):
                raise ValueError(f"{label}须为文本列表，最多500项")
            value = sorted(set(item.strip() for item in value if item.strip()))
            if key.endswith("windows"):
                if len(value) > 24:
                    raise ValueError("时间窗最多24项")
                for item in value:
                    parse_window(item)
            else:
                pattern = (
                    r"[1-9]\d{3,19}"
                    if key == "ignored_qq"
                    else r"(?:[^\s:]{1,100}:)?[1-9]\d{3,19}"
                )
                if any(not re.fullmatch(pattern, item) for item in value):
                    raise ValueError(f"{label}含无效QQ号或群号")
        elif kind in {"string", "text"}:
            limit = 4000 if key == "evolution_guidance" else 200
            if not isinstance(value, str) or len(value) > limit:
                raise ValueError(f"{label}须为不超过 {limit} 字的文本")
            value = value.strip()
            if key == "persona_id" and not value:
                raise ValueError("目标人格不能为空")
        result[key] = value
    thresholds = []
    if result["observation_groups"]:
        thresholds.append("min_observation_batch")
        if result["price_aware_learning"] and result["cheap_learning_windows"]:
            thresholds.append("cheap_observation_batch")
    if result["memory_enabled"] and result["price_aware_learning"]:
        thresholds.append("regular_interaction_batch")
        if result["cheap_learning_windows"]:
            thresholds.append("cheap_interaction_batch")
    for key in thresholds:
        if result[key] > result["batch_size"]:
            raise ValueError("单批最多消息数须不小于各类积累门槛")
    if result["max_batch_chars"] < result["max_source_chars"]:
        raise ValueError("单批原文总长度上限须不小于每条采集正文长度上限")
    return result


def usage_for_page(report: dict, settings: Settings) -> dict:
    """Expose numbers only; never leak provider settings or chat/source bodies."""

    def summary(row):
        return {
            **row,
            "input_tokens": row.get("reported_input_tokens", 0),
            "output_tokens": row.get("reported_output_tokens", 0),
            "charged_tokens": row.get("budget_tokens", 0),
            "unknown_calls": row.get(
                "unknown_calls", row.get("missing_usage_calls", 0)
            ),
        }

    def with_group(row):
        result = dict(row)
        try:
            scope = json.loads(row.get("scope_key", ""))
            if isinstance(scope, list) and len(scope) == 4:
                result.update(group_id=str(scope[3]), platform_id=str(scope[1]))
        except (ValueError, TypeError):
            pass
        return result

    return {
        "day": report.get("day", ""),
        "summary": summary(report.get("today", {})),
        "daily": [summary(row) for row in report.get("by_day", [])],
        "groups": [with_group(summary(row)) for row in report.get("by_group", [])],
        "recent": [with_group(row) for row in report.get("recent", [])],
        "limits": {
            "daily_tokens": settings.daily_token_limit,
            "group_tokens": settings.daily_group_token_limit,
            "daily_calls": settings.daily_calls,
            "group_calls": settings.daily_group_calls,
        },
    }
