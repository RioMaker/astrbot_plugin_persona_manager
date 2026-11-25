"""Exercise the installed AstrBot agent with deterministic model responses."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from astrbot_plugin_persona_manager.simulation import Job


@pytest.mark.parametrize("backend", ["configured", "bailian", "local"])
def test_native_group_agent_calls_tool_and_reads_result(tmp_path, monkeypatch, backend):
    monkeypatch.setenv("ASTRBOT_ROOT", str(tmp_path))
    pytest.importorskip("astrbot", reason="需安装 AstrBot 才能运行真实 Agent 集成测试")
    import httpx
    from astrbot.api.star import Context
    from astrbot.core import astr_main_agent
    from astrbot.core.agent.tool import FunctionTool
    from astrbot_plugin_persona_manager import simulation_agent as agent

    for name in ("chosen-skill", "excluded-skill"):
        skill = tmp_path / "data" / "skills" / name / "SKILL.md"
        skill.parent.mkdir(parents=True)
        skill.write_text(
            f"---\nname: {name}\ndescription: {name} guide\n---\nSKILL-FILE-CONTENT",
            encoding="utf-8",
        )

    config = {
        "plugin_set": ["test-tools"],
        "provider_settings": {
            "group_name_display": True,
            "computer_use_runtime": "local" if backend == "configured" else "none",
            "proactive_capability": {"add_cron_tools": False},
        },
    }
    persona = {
        "name": "测试人格",
        "prompt": "PERSONA-PROMPT",
        "skills": ["chosen-skill"],
        "tools": ["lookup", "removed"],
        "_begin_dialogs_processed": [
            {"role": "user", "content": "开场白"},
            {"role": "assistant", "content": "你好"},
        ],
    }
    events = []

    async def lookup(event, item):
        events.append(event)
        await event.send(event.plain_result("工具直达消息"))
        return "FOUND-" + item

    tools = {
        name: FunctionTool(
            name=name,
            description="test tool",
            handler=lookup,
            parameters={"type": "object", "properties": {"item": {"type": "string"}}},
        )
        for name in persona["tools"]
    }
    context = object.__new__(Context)
    context.get_config = lambda **kwargs: config
    context.subagent_orchestrator = None
    context.persona_manager = SimpleNamespace(
        resolve_selected_persona=AsyncMock(
            return_value=("测试人格", persona, None, False)
        )
    )
    context.get_llm_tool_manager = lambda: SimpleNamespace(
        get_func=tools.get, get_builtin_tool=lambda cls: cls()
    )
    monkeypatch.setattr(
        astr_main_agent, "retrieve_knowledge_base", AsyncMock(return_value=None)
    )

    async def hook(event, kind, *args):
        assert event.is_at_or_wake_command and not event.is_admin()
        assert event.get_group_id().startswith("sim-") and not event.is_private_chat()
        assert event.plugins_name == ["test-tools"]
        if kind == agent.EventType.OnLLMRequestEvent:
            (req,) = args
            assert event.get_extra("provider_request") is req
            req.system_prompt += "\nFROM-REQUEST-HOOK"
            req.func_tool.remove_tool("removed")
        return False

    monkeypatch.setattr(agent, "call_event_hook", hook)
    requests = []

    def completion(request):
        payload = json.loads(request.content)
        requests.append(payload)
        names = [t["function"]["name"] for t in payload["tools"]]
        assert "lookup" in names and "removed" not in names
        assert ("astrbot_file_read_tool" in names) == (backend == "configured")
        if backend == "bailian":
            assert request.url.host == "ws-test.cn-beijing.maas.aliyuncs.com"
            assert request.url.path == "/compatible-mode/v1/chat/completions"
            assert payload["model"] == "tuned" and payload["enable_thinking"] is False
        text = json.dumps(payload["messages"], ensure_ascii=False)
        assert text.count("PERSONA-PROMPT") == 1 and "chosen-skill" in text
        assert "excluded-skill" not in text and "FROM-REQUEST-HOOK" in text
        assert "开场白" in text and "上一轮" in text and "Group name: 模拟群聊" in text
        if len(requests) == 1:
            call = {
                "id": "call-1",
                "type": "function",
                "function": {
                    "name": "lookup",
                    "arguments": '{"item":"42"}',
                },
            }
            message, finish = {"role": "assistant", "tool_calls": [call]}, "tool_calls"
            if backend == "configured":
                path = tmp_path / "data" / "skills" / "chosen-skill" / "SKILL.md"
                message["tool_calls"].append(
                    {
                        "id": "read-skill",
                        "type": "function",
                        "function": {
                            "name": "astrbot_file_read_tool",
                            "arguments": json.dumps({"path": str(path)}),
                        },
                    }
                )
        else:
            assert any(
                m["role"] == "tool"
                and m["tool_call_id"] == "call-1"
                and "FOUND-42" in str(m["content"])
                for m in payload["messages"]
            )
            if backend == "configured":
                assert any(
                    m["role"] == "tool"
                    and m["tool_call_id"] == "read-skill"
                    and "SKILL-FILE-CONTENT" in str(m["content"])
                    for m in payload["messages"]
                )
            message, finish = {"role": "assistant", "content": "查到了 42"}, "stop"
        result = {"id": "test", "model": payload["model"], "created": 1}
        if payload.get("stream"):
            for call in message.get("tool_calls", []):
                call["index"] = 0
            result.update(
                object="chat.completion.chunk",
                choices=[{"index": 0, "delta": message, "finish_reason": None}],
            )
            end = {
                **result,
                "choices": [{"index": 0, "delta": {}, "finish_reason": finish}],
            }
            return httpx.Response(
                200,
                text=f"data: {json.dumps(result)}\n\ndata: {json.dumps(end)}\n\ndata: [DONE]\n\n",
                headers={"Content-Type": "text/event-stream"},
            )
        result.update(
            object="chat.completion",
            choices=[{"index": 0, "message": message, "finish_reason": finish}],
        )
        return httpx.Response(200, json=result)

    transport = httpx.MockTransport(completion)
    monkeypatch.setattr(
        agent.ProviderOpenAIOfficial,
        "_create_http_client",
        lambda *a: httpx.AsyncClient(transport=transport),
    )
    monkeypatch.setattr(httpx, "AsyncHTTPTransport", lambda *, uds: transport)
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-workspace-key")
    model = {
        "local": "Qwen3-8B",
        "bailian": "bailian:ws-test:tuned",
        "configured": "configured",
    }[backend]
    job = Job(
        "dashboard-owner",
        model,
        "查询",
        "测试人格",
        "已保存的人格",
        enable_tools=True,
        session_id="a" * 32,
    )
    messages = [
        {"role": "user", "content": "上一轮"},
        {"role": "assistant", "content": "好"},
        {"role": "user", "content": job.question},
    ]
    engine = (
        SimpleNamespace(
            client=SimpleNamespace(connector=SimpleNamespace(path="test.sock")),
            context_size=16384,
        )
        if backend == "local"
        else None
    )

    async def run():
        configured = agent.ProviderOpenAIOfficial(
            {
                "id": "custom",
                "type": "openai_chat_completion",
                "model": "custom",
                "key": ["local"],
                "api_base": "http://test.invalid/v1",
            },
            {},
        )
        context.get_provider_by_id = lambda name: (
            configured if name == "custom" else None
        )
        try:
            answer = await agent.group_reply(
                context, {}, job, messages, persona["prompt"], "custom", engine
            )
            assert answer == "工具直达消息\n\n查到了 42"
        finally:
            await configured.terminate()

    asyncio.run(run())
    assert len(requests) == 2 and len(events) == 1
    assert job.tool_calls == (
        ["lookup", "astrbot_file_read_tool"] if backend == "configured" else ["lookup"]
    )
    assert (job.first_text_at is not None) == (backend == "local")
    event = events[0]
    provider = "custom" if backend == "configured" else ""
    assert (
        agent.SimulatedGroupEvent(job, provider).unified_msg_origin
        == event.unified_msg_origin
    )
    job.owner = "another-owner"
    assert (
        agent.SimulatedGroupEvent(job, provider).unified_msg_origin
        != event.unified_msg_origin
    )
