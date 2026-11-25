"""Dashboard settings share the native config and never silently lose drafts."""

import asyncio
import json
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from astrbot_plugin_persona_manager import simulation_bailian
from astrbot_plugin_persona_manager.configuration import (
    SCHEMA,
    page_config,
    revision,
    usage_for_page,
    validate_changes,
)
from astrbot_plugin_persona_manager.ingress import sample
from astrbot_plugin_persona_manager.settings import (
    BAILIAN_GROUP_CHAT_MODEL,
    BAILIAN_LIUYAO_MODEL,
    Settings,
)
from astrbot_plugin_persona_manager.tests import test_integration as integration
from astrbot_plugin_persona_manager.tests.astrbot_stub import MockEvent

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
    assert len(SCHEMA) == 56
    assert all(field["section"] for field in result["schema"].values())
    assert "api_key" not in result["values"]
    assert result["schema"]["daily_token_limit"]["minimum"] == 0
    assert result["schema"]["thinking_notice_text"]["maxLength"] == 1000
    assert (
        result["values"]["thinking_notice_text"]
        == SCHEMA["thinking_notice_text"]["default"]
    )
    assert revision({"observation_groups": ["456789", "234567"]}) == revision(
        {"observation_groups": ["234567", "456789"]}
    )


def test_quart_dashboard_reads_group_settings_under_the_authenticated_identity(
    entry, monkeypatch, tmp_path
):
    quart = pytest.importorskip("quart", reason="旧版 AstrBot 管理页使用 Quart")
    from astrbot_plugin_persona_manager import legacy_web

    for name in ("request", "json_response", "error_response"):
        monkeypatch.setattr(entry.module, name, getattr(legacy_web, name))
    app = quart.Quart(__name__)
    binding = f"qq-main:234567=bailian:{Settings.bailian_workspace_id}:{BAILIAN_GROUP_CHAT_MODEL}"

    async def run():
        cfg = SavedConfig(tmp_path / "settings.json", bailian_api_key="test-key")
        async with entry.running(cfg) as env:
            body = {
                "action": "config_save",
                "revision": revision(cfg),
                "values": {"bailian_group_models": [binding]},
            }
            async with app.test_request_context("/action", method="POST", json=body):
                assert (await env.plugin.page_action()).status_code == 403
                quart.g.username = "dashboard-admin"
                saved = await env.plugin.page_action()
                assert saved.status_code == 200 and (await saved.get_json())["saved"]
            async with app.test_request_context(
                "/state", query_string={"persona_id": "可可子"}
            ):
                quart.g.username = "dashboard-admin"
                state = await (await env.plugin.page_state()).get_json()
                assert state["config"]["values"]["bailian_group_models"] == [binding]

    asyncio.run(run())


@pytest.mark.parametrize(
    "changes",
    [
        {"private_chat": True},
        {"enabled": "false"},
        {"daily_calls": True},
        {"daily_token_limit": -1},
        {"max_output_tokens": 999999},
        {"evolution_guidance": "长" * 4001},
        {"thinking_notice_text": "长" * 1001},
        {"thinking_notice_text": None},
        {"thinking_notice_text": True},
        {"persona_id": ""},
        {"observation_groups": ["任意群"]},
        {"ignored_qq": ["qq:12345"]},
        {"bailian_workspace_id": "https://other.example"},
        {"bailian_api_key": "test\ninvalid-header"},
        {"bailian_model_codes": ["two IDs"]},
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


def test_bailian_model_settings_accept_new_deployments_and_explicit_empty_lists():
    defaults = page_config({})["values"]
    for name in ("bailian_workspace_id", "bailian_model_codes"):
        assert defaults[name] == SCHEMA[name]["default"]
    edited = validate_changes(
        {}, {"bailian_model_codes": [" tuned-v2 ", "tuned-v1", "tuned-v2"]}
    )
    assert Settings.read(edited).bailian_model_codes == {"tuned-v1", "tuned-v2"}
    assert not Settings.read({"bailian_model_codes": []}).bailian_model_codes
    assert not Settings.read(
        {"bailian_workspace_id": "ws.example/path"}
    ).bailian_workspace_id


def test_trial_bindings_require_one_known_deployment_and_an_exact_group():
    first = f"bailian:ws-test:{BAILIAN_GROUP_CHAT_MODEL}"
    second = f"bailian:ws-test:{BAILIAN_LIUYAO_MODEL}"
    config = {"bailian_workspace_id": "ws-test"}
    for entries in (
        [f"234567={first}"],
        [f"qq-main:*={first}"],
        [f"qq-main:234567={first}", f"qq-main:234567={second}"],
        [f"qq-main:234567=bailian:ws-other:{BAILIAN_LIUYAO_MODEL}"],
        ["qq-main:234567=bailian:ws-test:custom"],
    ):
        with pytest.raises(ValueError):
            validate_changes(config, {"bailian_group_models": entries})
        assert not Settings.read(
            {**config, "bailian_group_models": entries}
        ).trial_model_for("234567", "qq-main")


def test_saving_group_trials_uses_existing_key_and_keeps_other_groups_learning(
    entry, tmp_path, monkeypatch
):
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    binding = f"qq-main:234567=bailian:{Settings.bailian_workspace_id}:{BAILIAN_GROUP_CHAT_MODEL}"

    async def run():
        path = tmp_path / "native.json"
        cfg = SavedConfig(path, batch_size=8, memory_batch_threshold=20)
        async with entry.running(config=cfg) as env:
            epoch = env.plugin.store.epoch()
            baseline = env.plugin.settings()

            async def save(values):
                env.request.payload = {
                    "action": "config_save",
                    "revision": revision(cfg),
                    "values": values,
                }
                return await env.plugin.page_action()

            assert (await save({"bailian_group_models": [binding]})).status_code == 400
            assert not path.exists()
            assert (
                await save(
                    {"bailian_group_models": [binding], "bailian_api_key": "test-key"}
                )
            )["saved"]
            assert json.loads(path.read_text(encoding="utf-8"))[
                "bailian_group_models"
            ] == [binding]
            assert (
                env.plugin.settings().trial_model_for("234567", "qq-main")
                == BAILIAN_GROUP_CHAT_MODEL
            )
            assert env.plugin.settings() == baseline
            state = await env.plugin.page_state()
            assert "test-key" not in json.dumps(state)
            assert {
                m["group_trial_label"]
                for m in state["simulation_models"]
                if m.get("group_trial_label")
            } == {"六爻 Qwen3-4B", "群聊微调 Qwen3-8B v2"}
            assert (await save({"bailian_workspace_id": "ws-new"}))["saved"]
            assert not env.plugin.settings().trial_model_for("234567", "qq-main")
            assert (await save({"bailian_group_models": []}))["saved"]
            assert cfg["bailian_api_key"] == "test-key"
            assert env.plugin.store.epoch() == epoch

    asyncio.run(run())


@pytest.mark.parametrize(
    ("workspace", "models", "upgrade"),
    [
        (
            Settings.bailian_workspace_id,
            ["qwen3-4b-instruct-2507-438f534673d5-v1"],
            True,
        ),
        (Settings.bailian_workspace_id, [], False),
        (Settings.bailian_workspace_id, ["custom-model"], False),
        ("ws-other", ["qwen3-4b-instruct-2507-438f534673d5-v1"], False),
    ],
)
def test_bailian_preset_upgrade_respects_saved_choices(
    entry, tmp_path, workspace, models, upgrade
):
    async def run():
        path = tmp_path / "native.json"
        cfg = SavedConfig(
            path,
            bailian_workspace_id=workspace,
            bailian_model_codes=models,
            bailian_api_key="existing-test-key",
        )
        async with entry.running(config=cfg) as env:
            if not upgrade:
                assert cfg["bailian_model_codes"] == models and not path.exists()
                return
            saved = json.loads(path.read_text(encoding="utf-8"))
            assert saved["bailian_model_codes"] == [*models, "qwen3-8b-d39bed90dfab"]
            assert saved["bailian_api_key"] == "existing-test-key"
            env.request.payload = {
                "action": "config_save",
                "revision": revision(cfg),
                "values": {"bailian_model_codes": models},
            }
            assert (await env.plugin.page_action())["saved"]
        async with entry.running(config=cfg):
            assert (
                json.loads(path.read_text(encoding="utf-8"))["bailian_model_codes"]
                == models
            )
            assert cfg["bailian_model_codes"] == models

    asyncio.run(run())


def test_failed_bailian_preset_upgrade_retries_on_reload(entry, tmp_path):
    async def run():
        legacy = ["qwen3-4b-instruct-2507-438f534673d5-v1"]
        cfg = SavedConfig(
            tmp_path / "native.json", fail=True, bailian_model_codes=legacy
        )
        async with entry.running(config=cfg):
            assert cfg["bailian_model_codes"] == legacy and not cfg.path.exists()
        cfg.fail = False
        async with entry.running(config=cfg):
            assert json.loads(cfg.path.read_text(encoding="utf-8"))[
                "bailian_model_codes"
            ] == [*legacy, "qwen3-8b-d39bed90dfab"]

    asyncio.run(run())


def test_saving_bailian_deployments_updates_simulator_without_interrupting_learning(
    entry, tmp_path
):
    async def run():
        config = SavedConfig(
            tmp_path / "native.json",
            local_chat_provider_id="existing-model",
            memory_batch_threshold=20,
            batch_size=8,
        )
        async with entry.running(config=config) as env:
            epoch = env.plugin.store.epoch()
            env.request.payload = {
                "action": "config_save",
                "revision": revision(config),
                "values": {
                    "bailian_workspace_id": "ws-other",
                    "bailian_model_codes": ["next-model"],
                },
            }
            assert (await env.plugin.page_action())["saved"]
            persisted = json.loads(config.path.read_text(encoding="utf-8"))
            assert persisted["bailian_model_codes"] == ["next-model"]
            assert persisted["local_chat_provider_id"] == "existing-model"
            assert (
                persisted["memory_batch_threshold"] == 20
                and persisted["batch_size"] == 8
            )
            state = await env.plugin.page_state()
            assert [
                m["id"] for m in state["simulation_models"] if m["kind"] == "bailian"
            ] == ["bailian:ws-other:next-model"]
            assert env.plugin.store.epoch() == epoch

    asyncio.run(run())


def test_plugin_key_is_private_and_live_without_changing_other_settings(
    entry, tmp_path, monkeypatch
):
    monkeypatch.setenv("DASHSCOPE_API_KEY", "shared-environment-key")
    generate = AsyncMock(return_value="试聊回答")
    monkeypatch.setattr(simulation_bailian, "generate", generate)

    async def run():
        config = SavedConfig(
            tmp_path / "native.json",
            local_chat_provider_id="existing-model",
            learning_provider_id="business-model",
            evolution_guidance="  原有设置  ",
        )
        async with entry.running(config=config) as env:
            before = dict(config)
            epoch = env.plugin.store.epoch()
            initial_revision = revision(config)

            async def save(changes, *, version=None):
                env.request.payload = {
                    "action": "config_save",
                    "revision": version or revision(config),
                    "values": changes,
                }
                return await env.plugin.page_action()

            assert (await save({"bailian_api_key": "plugin-only-key"}))["saved"]
            assert dict(config) == {**before, "bailian_api_key": "plugin-only-key"}
            assert json.loads(config.path.read_text(encoding="utf-8")) == dict(config)
            state = await env.plugin.page_state()
            assert "plugin-only-key" not in json.dumps(state)
            assert "shared-environment-key" not in json.dumps(state)
            field = state["config"]["schema"]["bailian_api_key"]
            assert field["secret"] is True and field["configured"] is True
            assert state["config"]["values"]["bailian_api_key"] == ""
            assert (
                await save({"bailian_api_key": "stale-key"}, version=initial_revision)
            ).status_code == 409
            cloud = next(
                row for row in state["simulation_models"] if row["kind"] == "bailian"
            )

            async def check_request(expected):
                env.request.payload = {
                    "action": "send",
                    "model": cloud["id"],
                    "message": "试聊",
                }
                assert (await env.plugin.page_simulation())["status"] == "running"
                await env.plugin.simulation.task
                assert generate.await_args.args[-1] == expected
                assert os.environ["DASHSCOPE_API_KEY"] == "shared-environment-key"

            await check_request("plugin-only-key")
            assert (await save({"bailian_api_key": "replacement-key"}))["saved"]
            await check_request("replacement-key")
            assert env.plugin.store.epoch() == epoch
            assert (await save({"daily_calls": 8}))["saved"]
            assert config["bailian_api_key"] == "replacement-key"
            assert (await save({"bailian_api_key": ""}))["saved"]
            await check_request("shared-environment-key")
            env.context.llm_generate.assert_not_awaited()
            env.context.send_message.assert_not_awaited()

    asyncio.run(run())


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
                    "thinking_notice_text": "让我想一想～\n整理一下刚才的聊天。",
                    "daily_token_limit": 80000,
                    "cheap_learning_windows": ["23:00-07:00"],
                    "observation_groups": ["qq-main:234567"],
                    "memory_groups": ["qq-main:456789"],
                    "global_style_groups": ["qq-main:234567"],
                    "group_tags": ["qq-main:234567=太鼓达人,轻松"],
                },
            }
            result = await route(env.context, "action")()
            assert result["saved"] is True
            persisted = json.loads(cfg.path.read_text(encoding="utf-8"))
            assert persisted["daily_token_limit"] == 80000
            assert persisted["custom_future_field"] == "keep"
            assert persisted["observation_groups"] == ["qq-main:234567"]
            assert persisted["memory_groups"] == ["qq-main:456789"]
            assert persisted["global_style_groups"] == ["qq-main:234567"]
            assert env.plugin.settings().tags_for("234567", "qq-main") == (
                "太鼓达人",
                "轻松",
            )
            assert env.plugin.settings().evolution_guidance.startswith("少复读")
            text = "让我想一想～\n整理一下刚才的聊天。"
            assert persisted["thinking_notice_text"] == text
            source = sample(
                MockEvent(group_id="456789"), env.plugin.settings(), "interaction"
            )
            await env.plugin._thinking_notice(source)
            env.context.send_message.assert_awaited_once()
            umo, chain = env.context.send_message.await_args.args
            assert umo == "qq-main:GroupMessage:456789"
            assert [item.text for item in chain.chain] == [text]
            assert env.plugin.store.epoch() > epoch
            assert env.context.persona_manager.updates == []
            assert Settings.read(persisted) == env.plugin.settings()

    asyncio.run(run())


@pytest.mark.parametrize(
    "mode",
    [
        "stale",
        "invalid",
        "unauthenticated",
        "learning_provider_id",
        "local_chat_provider_id",
        "unknown_persona",
    ],
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
            elif mode in {"learning_provider_id", "local_chat_provider_id"}:
                env.request.payload["values"] = {mode: "missing"}
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
            assert len(result["config"]["schema"]) == len(SCHEMA)
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
