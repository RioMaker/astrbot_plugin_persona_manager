"""Bounded, fail-closed settings. Private chat is deliberately not configurable."""

from dataclasses import dataclass, field
from typing import Any, Mapping

from .style_matching import parse_tag_entry

INT_LIMITS = {
    "batch_size": (30, 1, 60),
    "min_observation_batch": (20, 2, 60),
    "learning_interval_seconds": (1800, 60, 86400),
    "daily_calls": (12, 0, 100),
    "daily_group_calls": (4, 0, 20),
    "model_timeout_seconds": (45, 5, 120),
    "max_source_chars": (600, 100, 2000),
    "max_batch_chars": (12000, 1000, 24000),
    "max_context_chars": (1800, 300, 4000),
    "observation_retention_hours": (48, 1, 168),
    "interaction_retention_days": (7, 1, 30),
    "daily_token_limit": (60000, 0, 2000000),
    "daily_group_token_limit": (20000, 0, 1000000),
    "max_output_tokens": (1200, 256, 4096),
    "regular_interaction_batch": (3, 1, 60),
    "cheap_interaction_batch": (1, 1, 60),
    "cheap_observation_batch": (5, 2, 60),
    "cheap_learning_interval_seconds": (300, 60, 86400),
    "max_learning_wait_seconds": (21600, 600, 86400),
}


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
    global_style_groups: frozenset[str] = field(default_factory=frozenset)
    group_tags: frozenset[str] = field(default_factory=frozenset)
    memory_enabled: bool = True
    memory_groups: frozenset[str] = field(default_factory=frozenset)
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
    daily_token_limit: int = 60000
    daily_group_token_limit: int = 20000
    max_output_tokens: int = 1200
    price_aware_learning: bool = True
    cheap_learning_windows: frozenset[str] = field(default_factory=frozenset)
    learning_windows: frozenset[str] = field(default_factory=frozenset)
    regular_interaction_batch: int = 3
    cheap_interaction_batch: int = 1
    cheap_observation_batch: int = 5
    cheap_learning_interval_seconds: int = 300
    max_learning_wait_seconds: int = 21600
    evolution_guidance: str = ""

    @classmethod
    def read(cls, config: Mapping[str, Any]) -> "Settings":
        values: dict[str, Any] = {}
        for name in (
            "enabled",
            "memory_enabled",
            "learning_enabled",
            "price_aware_learning",
        ):
            values[name] = config.get(name, True) is True
        values["persona_id"] = str(config.get("persona_id", "可可子") or "").strip()
        values["learning_provider_id"] = str(
            config.get("learning_provider_id", "") or ""
        ).strip()
        for name in (
            "observation_groups",
            "global_style_groups",
            "group_tags",
            "memory_groups",
            "memory_disabled_groups",
            "ignored_qq",
        ):
            values[name] = entries(config.get(name, []))
        for name in ("learning_windows", "cheap_learning_windows"):
            raw = config.get(name, [])
            values[name] = (
                entries(raw) if isinstance(raw, list) else frozenset({"invalid"})
            )
        guidance = config.get("evolution_guidance", "")
        values["evolution_guidance"] = (
            guidance[:4000].strip() if isinstance(guidance, str) else ""
        )
        for name, (default, low, high) in INT_LIMITS.items():
            values[name] = integer(config.get(name, default), default, low, high)
        return cls(**values)

    def group_in(self, group_id: str, platform_id: str, values: frozenset[str]) -> bool:
        return group_id in values or f"{platform_id}:{group_id}" in values

    def observes(self, group_id: str, platform_id: str) -> bool:
        return self.group_in(group_id, platform_id, self.observation_groups)

    def remembers(self, group_id: str, platform_id: str) -> bool:
        return (
            self.memory_enabled
            and self.group_in(group_id, platform_id, self.memory_groups)
            and not self.group_in(group_id, platform_id, self.memory_disabled_groups)
        )

    def uses_global_styles(self, group_id: str, platform_id: str) -> bool:
        return self.group_in(group_id, platform_id, self.global_style_groups)

    def tags_for(self, group_id: str, platform_id: str) -> tuple[str, ...]:
        parsed = {}
        for entry in sorted(self.group_tags):
            try:
                key, tags = parse_tag_entry(entry)
            except ValueError:
                continue
            if key in parsed:
                return ()  # Ambiguous native configuration never grants a match.
            parsed[key] = tags
        return parsed.get(f"{platform_id}:{group_id}", parsed.get(group_id, ()))
