"""Bounded, fail-closed settings. Private chat is deliberately not configurable."""

from dataclasses import dataclass, field
from typing import Any, Mapping


def integer(value: Any, default: int, low: int, high: int) -> int:
    try:
        if isinstance(value, bool):
            return default
        return max(low, min(high, int(value)))
    except (TypeError, ValueError, OverflowError):
        return default


def entries(value: Any) -> frozenset[str]:
    if not isinstance(value, list):
        return frozenset()
    return frozenset(str(item).strip() for item in value if str(item).strip())


@dataclass(frozen=True)
class Settings:
    enabled: bool = True
    persona_id: str = "可可子"
    observation_groups: frozenset[str] = field(default_factory=frozenset)
    memory_enabled: bool = True
    memory_disabled_groups: frozenset[str] = field(default_factory=frozenset)
    ignored_qq: frozenset[str] = field(default_factory=frozenset)
    learning_enabled: bool = True
    learning_provider_id: str = ""
    batch_size: int = 30
    min_observation_batch: int = 20
    learning_interval_seconds: int = 1800
    daily_calls: int = 12
    daily_group_calls: int = 4
    model_timeout_seconds: int = 45
    max_source_chars: int = 600
    max_batch_chars: int = 12000
    max_context_chars: int = 1800
    observation_retention_hours: int = 48
    interaction_retention_days: int = 7

    @classmethod
    def read(cls, config: Mapping[str, Any]) -> "Settings":
        values: dict[str, Any] = {}
        for name in ("enabled", "memory_enabled", "learning_enabled"):
            values[name] = config.get(name, True) is True
        values["persona_id"] = str(config.get("persona_id", "可可子") or "").strip()
        values["learning_provider_id"] = str(
            config.get("learning_provider_id", "") or ""
        ).strip()
        for name in ("observation_groups", "memory_disabled_groups", "ignored_qq"):
            values[name] = entries(config.get(name, []))
        for name, default, low, high in (
            ("batch_size", 30, 1, 100),
            ("min_observation_batch", 20, 2, 100),
            ("learning_interval_seconds", 1800, 60, 86400),
            ("daily_calls", 12, 0, 100),
            ("daily_group_calls", 4, 0, 20),
            ("model_timeout_seconds", 45, 5, 120),
            ("max_source_chars", 600, 100, 2000),
            ("max_batch_chars", 12000, 1000, 24000),
            ("max_context_chars", 1800, 300, 4000),
            ("observation_retention_hours", 48, 1, 168),
            ("interaction_retention_days", 7, 1, 30),
        ):
            values[name] = integer(config.get(name, default), default, low, high)
        return cls(**values)

    def group_in(self, group_id: str, platform_id: str, values: frozenset[str]) -> bool:
        return group_id in values or f"{platform_id}:{group_id}" in values

    def observes(self, group_id: str, platform_id: str) -> bool:
        return self.group_in(group_id, platform_id, self.observation_groups)

    def remembers(self, group_id: str, platform_id: str) -> bool:
        return self.memory_enabled and not self.group_in(
            group_id, platform_id, self.memory_disabled_groups
        )
