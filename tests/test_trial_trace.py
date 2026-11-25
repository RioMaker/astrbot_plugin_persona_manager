import asyncio
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from astrbot_plugin_persona_manager import simulation_bailian
from astrbot_plugin_persona_manager.deploy.build_trial_panel import OUTPUT, build
from astrbot_plugin_persona_manager.settings import BAILIAN_GROUP_CHAT_MODEL, Settings
from astrbot_plugin_persona_manager.tests.astrbot_stub import MockEvent
from astrbot_plugin_persona_manager.tests.test_integration import entry as entry
from astrbot_plugin_persona_manager.tests.test_integration import request, route
from astrbot_plugin_persona_manager.tests.test_local_chat import GATE_MARK, TRIAL_8B
from astrbot_plugin_persona_manager.trial_trace import (
    CLIPPED,
    FIELD_CHARS,
    MAX_TURNS,
    PER_GROUP,
    TrialHistory,
)


def test_embedded_panel_matches_the_standalone_trace_view():
    assert OUTPUT.read_text(encoding="utf-8") == build()


def test_page_tracks_an_authorized_turn_independently_of_learning(entry, monkeypatch):
    monkeypatch.setattr(
        simulation_bailian,
        "create_agent_provider",
        lambda *args, **kwargs: SimpleNamespace(terminate=AsyncMock()),
    )

    async def run():
        other = TRIAL_8B.replace("qq-main", "qq-other")
        cfg = {"bailian_group_models": [TRIAL_8B, other], "learning_enabled": False}
        async with entry.running(cfg) as env:
            plugin = env.plugin
            env.context.provider_manager = SimpleNamespace(inst_map={})
            page = route(env.context, "trials")
            data = await page()
            assert [g["scope"] for g in data["groups"]] == [
                "qq-main:234567",
                "qq-other:234567",
            ]
            assert not data["turns"] and data["detail"] is None
            for event in [
                MockEvent(group_id="654321", extras=GATE_MARK),
                MockEvent(private=True),
                MockEvent(),
            ]:
                await plugin.select_local_chat(event)
            assert not (await page())["turns"]

            event = MockEvent("今天的新曲过了吗", extras=GATE_MARK)
            await plugin.select_local_chat(event)
            req = request()
            event.set_extra("provider_request", req)
            # Auxiliary requests in the same event do not belong to this chat.
            await plugin.on_request(event, request())
            await plugin.on_request(event, req)
            turn = event.get_extra(env.module.TRIAL_KEY)
            tool = SimpleNamespace(name="lookup")
            args = {"song": "new", "api_key": "private-value"}
            with turn.model_call(
                {"messages": [{"role": "system", "content": "人格提示词"}]}
            ) as call:
                call.response(
                    SimpleNamespace(
                        role="tool",
                        tools_call_name=["lookup"],
                        tools_call_args=[args],
                        usage={"prompt_tokens": 15, "completion_tokens": 3},
                    )
                )
            await plugin.on_tool_start(event, tool, args)
            result = SimpleNamespace(
                content=[SimpleNamespace(type="text", text="已通过")],
                isError=False,
                meta={"usage": {"total_tokens": 7}},
            )
            await plugin.on_tool_delivery(event, tool, args, result)
            with turn.model_call(
                {"messages": [{"role": "tool", "content": "已通过"}]}
            ) as call:
                reply = SimpleNamespace(role="assistant", completion_text="你过了！")
                call.response(reply)
            await plugin.on_response(event, reply)
            event.get_result().chain[0].text = "你过了！"
            await plugin.after_sent(event)
            plugin.group_trials.release(event.get_extra("selected_provider"))
            data = await page()
            detail = data["detail"]
            assert detail["status"] == "delivered"
            assert [s["kind"] for s in detail["steps"]] == [
                "route",
                "prepare",
                "model",
                "tool",
                "model",
                "delivery",
            ]
            assert all(
                0 <= step["elapsed_ms"] <= detail["elapsed_ms"]
                for step in detail["steps"]
            )
            assert detail["steps"][1]["input"] == req.prompt
            assert "人格提示词" in detail["steps"][2]["prompt"]
            assert (
                "new" in detail["steps"][3]["input"]
                and detail["steps"][3]["output"] == "已通过"
            )
            assert "已通过" in detail["steps"][4]["prompt"]
            assert detail["steps"][3]["usage"]["total_tokens"] == 7
            assert detail["steps"][4]["usage"]["total_tokens"] is None
            assert detail["steps"][-1]["output"] == "你过了！"
            assert (
                detail["calls"],
                detail["reported_calls"],
                detail["reported_tokens"],
            ) == (2, 1, 18)
            assert "private-value" not in json.dumps(data, ensure_ascii=False)
            state = await route(env.context, "state")()
            assert detail["id"] not in json.dumps(state)
            assert state["usage"]["summary"]["budget_tokens"] == 0
            env.request.query.update(scope="qq-other:234567", turn=detail["id"])
            assert (await page())["detail"] is None
            cfg["bailian_group_models"] = [other]
            env.request.query["scope"] = "qq-main:234567"
            assert not (await page())["turns"]
            env.request.username = None
            assert (await page()).status_code == 403

    asyncio.run(run())


def test_failed_binding_records_reason_without_intercepting_default_model(entry):
    async def run():
        async with entry.running({"bailian_group_models": [TRIAL_8B]}) as env:
            env.plugin.group_trials.bind = Mock(
                side_effect=ValueError("private provider detail")
            )
            event = MockEvent(extras=GATE_MARK)
            await env.plugin.select_local_chat(event)
            assert event.extras == GATE_MARK
            turn = (await route(env.context, "trials")())["detail"]
            assert turn["status"] == "test_failed" and turn["calls"] == 0
            assert "ValueError" in turn["steps"][0]["output"]
            assert "private provider detail" not in str(turn)

    asyncio.run(run())


def test_retention_partial_stream_and_unreported_usage():
    history = TrialHistory()
    source = {"group_id": "234567", "platform_id": "qq-main", "persona_id": "可可子"}

    def begin(group):
        return history.begin(
            dict(source, group_id=group),
            BAILIAN_GROUP_CHAT_MODEL,
            "开场",
            time.monotonic(),
        )

    for number in range(MAX_TURNS + 1):
        begin(str(10000 + number)).close()
    for _ in range(PER_GROUP + 1):
        begin("234567").close()
    turn = begin("234567")
    turn.route.finish()
    try:
        with turn.model_call(
            {"messages": [{"role": "user", "content": "长" * FIELD_CHARS + "尾"}]}
        ) as call:
            call.chunk(SimpleNamespace(completion_text="已生成一段", usage=None))
            assert call.view()["output"] == "已生成一段"
            raise asyncio.CancelledError()
    except asyncio.CancelledError:
        pass
    turn.close("cancelled")
    detail = turn.detail()
    assert detail["reported_tokens"] is None
    assert detail["steps"][1]["status"] == "cancelled"
    assert detail["steps"][1]["output"] == "已生成一段"
    assert CLIPPED in detail["steps"][1]["prompt"]
    config = Settings.read({"bailian_group_models": [TRIAL_8B]})
    assert len(history.turns) <= MAX_TURNS
    assert len(history.page(config, [], turn.scope, "")["turns"]) == PER_GROUP
