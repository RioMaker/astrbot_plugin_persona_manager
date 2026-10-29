"""Dashboard settings share the native config and never silently lose drafts."""

import asyncio
import json
from types import SimpleNamespace

import pytest
from astrbot_plugin_persona_manager.configuration import (
    SCHEMA,
    page_config,
    revision,
    usage_for_page,
    validate_changes,
)
from astrbot_plugin_persona_manager.settings import Settings
from astrbot_plugin_persona_manager.tests import test_integration as integration

entry = integration.entry
route = integration.route


class SavedConfig(dict):
    def __init__(self, path, *, fail=False, superseded=False, **values):
        super().__init__(enabled=True, **values)
        self.path = path
        self.fail = fail
        self.superseded = superseded

    async def save_config_async(self, replacement):
        self.update(replacement)
        if self.fail:
            raise OSError("simulated atomic write failure")
        if self.superseded:
            self["evolution_guidance"] = "另一个编辑器保存的方向"
            return False
        self.path.write_text(json.dumps(self, ensure_ascii=False), encoding="utf-8")
        return True


def test_every_native_setting_has_a_dashboard_control():
    result = page_config({})
    assert set(result["values"]) == set(result["schema"]) == set(SCHEMA)
    assert len(SCHEMA) == 31
    assert all(field["section"] for field in result["schema"].values())
    assert "api_key" not in result["values"]
    assert result["schema"]["daily_token_limit"]["minimum"] == 0
    assert revision({"observation_groups": ["456789", "234567"]}) == revision(
        {"observation_groups": ["234567", "456789"]}
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"private_chat": True},
        {"enabled": "false"},
        {"daily_calls": True},
        {"daily_token_limit": -1},
        {"max_output_tokens": 999999},
        {"evolution_guidance": "长" * 4001},
        {"persona_id": ""},
        {"observation_groups": ["任意群"]},
        {"ignored_qq": ["qq:12345"]},
        {"learning_windows": ["23:00-99:00"]},
        {"cheap_learning_windows": ["12:00-12:00"]},
        {"learning_windows": "23:00-07:00"},
        {"observation_groups": ["12345"], "batch_size": 5, "min_observation_batch": 20},
        {"max_source_chars": 2000, "max_batch_chars": 1000},
    ],
)
def test_invalid_setting_edits_are_rejected(changes):
    with pytest.raises(ValueError):
        validate_changes({}, changes)


def test_cross_midnight_windows_and_partial_edits_preserve_other_values():
    result = validate_changes(
        {"daily_calls": 8},
        {
            "cheap_learning_windows": ["23:00-07:00", "23:00-07:00"],
            "evolution_guidance": "  简短自然，记住练习进展  ",
        },
    )
    assert result["cheap_learning_windows"] == ["23:00-07:00"]
    assert result["daily_calls"] == 8
    assert result["evolution_guidance"] == "简短自然，记住练习进展"


def test_page_save_persists_and_invalidates_inflight_work_without_touching_core(
    entry, tmp_path
):
    async def run():
        cfg = SavedConfig(tmp_path / "native.json", custom_future_field="keep")
        async with entry.running(config=cfg) as env:
            epoch = env.plugin.store.epoch()
            env.request.payload = {
                "action": "config_save",
                "revision": revision(cfg),
                "values": {
                    "evolution_guidance": "少复读，保留有证据的练习目标",
                    "daily_token_limit": 80000,
                    "cheap_learning_windows": ["23:00-07:00"],
                },
            }
            result = await route(env.context, "action")()
            assert result["saved"] is True
            persisted = json.loads(cfg.path.read_text(encoding="utf-8"))
            assert persisted["daily_token_limit"] == 80000
            assert persisted["custom_future_field"] == "keep"
            assert env.plugin.settings().evolution_guidance.startswith("少复读")
            assert env.plugin.store.epoch() > epoch
            assert env.context.persona_manager.updates == []
            assert Settings.read(persisted) == env.plugin.settings()

    asyncio.run(run())


@pytest.mark.parametrize(
    "mode",
    ["stale", "invalid", "unauthenticated", "unknown_provider", "unknown_persona"],
)
def test_page_save_rejection_leaves_config_untouched(entry, tmp_path, mode):
    async def run():
        cfg = SavedConfig(tmp_path / "native.json")
        async with entry.running(config=cfg) as env:
            before = dict(cfg)
            env.request.payload = {
                "action": "config_save",
                "revision": revision(cfg),
                "values": {"daily_calls": 8},
            }
            if mode == "stale":
                env.request.payload["revision"] = "old"
            elif mode == "invalid":
                env.request.payload["values"] = {"daily_calls": -4}
            elif mode == "unauthenticated":
                env.request.username = None
            elif mode == "unknown_provider":
                env.request.payload["values"] = {"learning_provider_id": "missing"}
            else:
                env.request.payload["values"] = {"persona_id": "missing"}
            result = await route(env.context, "action")()
            assert result.status_code in {400, 403, 409}
            assert dict(cfg) == before
            assert not cfg.path.exists()

    asyncio.run(run())


def test_failed_atomic_native_save_restores_runtime_config(entry, tmp_path):
    async def run():
        cfg = SavedConfig(tmp_path / "native.json", fail=True, daily_calls=9)
        async with entry.running(config=cfg) as env:
            before = dict(cfg)
            env.request.payload = {
                "action": "config_save",
                "revision": revision(cfg),
                "values": {"daily_calls": 7},
            }
            result = await route(env.context, "action")()
            assert result.status_code == 500
            assert dict(cfg) == before

    asyncio.run(run())


def test_superseded_save_does_not_overwrite_the_newer_native_config(entry, tmp_path):
    async def run():
        cfg = SavedConfig(tmp_path / "native.json", superseded=True)
        async with entry.running(config=cfg) as env:
            env.request.payload = {
                "action": "config_save",
                "revision": revision(cfg),
                "values": {"evolution_guidance": "旧草稿"},
            }
            result = await route(env.context, "action")()
            assert result.status_code == 409
            assert cfg["evolution_guidance"] == "另一个编辑器保存的方向"

    asyncio.run(run())


def test_external_change_during_invalidation_is_not_overwritten(entry, tmp_path):
    async def run():
        cfg = SavedConfig(tmp_path / "native.json")
        async with entry.running(config=cfg) as env:
            original_db = env.plugin.runtime.db

            async def concurrent_db(method, *args, **kwargs):
                result = await original_db(method, *args, **kwargs)
                if method == "invalidate":
                    cfg["daily_calls"] = 6
                return result

            env.plugin.runtime.db = concurrent_db
            env.request.payload = {
                "action": "config_save",
                "revision": revision(cfg),
                "values": {"evolution_guidance": "待保存草稿"},
            }
            result = await route(env.context, "action")()
            assert result.status_code == 409
            assert cfg["daily_calls"] == 6
            assert "evolution_guidance" not in cfg
            assert not cfg.path.exists()

    asyncio.run(run())


def test_state_exposes_settings_and_safe_selector_labels_only(entry):
    async def run():
        async with entry.running() as env:
            env.context.get_all_providers = lambda: [
                SimpleNamespace(
                    provider_config={
                        "id": "existing-model",
                        "api_key": "NEVER_EXPOSE_THIS",
                        "api_base": "private-url",
                    }
                )
            ]
            result = await route(env.context, "state")()
            assert result.status_code == 200
            assert len(result["config"]["schema"]) == 31
            assert result["choices"]["providers"] == [
                {"id": "existing-model", "label": "existing-model"}
            ]
            assert "NEVER_EXPOSE_THIS" not in json.dumps(result)
            assert "usage" in result and "schedule" in result

    asyncio.run(run())


def test_usage_mapping_keeps_legacy_unknowns_and_human_readable_group():
    result = usage_for_page(
        {
            "today": {"calls": 3, "unknown_calls": 3, "missing_usage_calls": 1},
            "by_group": [
                {"scope_key": '["可可子","qq1","12345","23456"]', "budget_tokens": 1000}
            ],
        },
        Settings(),
    )
    assert result["summary"]["unknown_calls"] == 3
    assert result["groups"][0]["group_id"] == "23456"
    assert result["groups"][0]["charged_tokens"] == 1000
