import asyncio
import hashlib
import json
import select
import subprocess
import sys
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import aiohttp
import pytest
from aiohttp import web
from astrbot_plugin_persona_manager import simulation
from astrbot_plugin_persona_manager.settings import Settings
from astrbot_plugin_persona_manager.simulation_models import Asset, ModelFiles
from astrbot_plugin_persona_manager.tests.test_integration import entry as entry


@pytest.mark.skipif(sys.platform != "linux", reason="内置引擎使用 Linux 进程生命周期")
def test_resident_launcher_exits_if_the_host_is_killed():
    launcher = Path(simulation.__file__).with_name("simulation_exec.py")
    owner_program = """
import os, subprocess, sys
child = "import sys; print('ready', flush=True); sys.stdin.buffer.read()"
subprocess.run([sys.executable, "-I", sys.argv[1], str(os.getpid()), sys.executable, "-c", child], check=True)
"""
    with subprocess.Popen(
        [sys.executable, "-c", owner_program, str(launcher)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
    ) as owner:
        try:
            assert select.select([owner.stdout], [], [], 5)[0]
            assert owner.stdout.readline() == b"ready\n"
            owner.kill()
            owner.wait(timeout=5)
            # Only the resident child still owns the write side after its parent dies.
            assert select.select([owner.stdout], [], [], 5)[0]
            assert owner.stdout.read(1) == b""
        finally:
            owner.stdin.close()
            if owner.poll() is None:
                owner.kill()


@pytest.fixture
def native_engines(monkeypatch):
    engines = []

    async def open_engine(model, binary, weights, context_size):
        assert all(engine.process.returncode is not None for engine in engines)
        process = SimpleNamespace(returncode=None)
        stack = AsyncExitStack()
        stack.callback(setattr, process, "returncode", 0)
        engine = simulation.ResidentEngine(
            model, process, object(), stack, context_size
        )
        engines.append(engine)
        return engine

    monkeypatch.setattr(simulation, "_open_engine", open_engine)
    return engines


def test_timing_tracks_visible_stream_text_and_freezes_at_completion(
    tmp_path, monkeypatch
):
    clock = Mock(return_value=100.0)
    monkeypatch.setattr(simulation, "time", SimpleNamespace(monotonic=clock))
    sim = simulation.Simulation(tmp_path, None)
    model = next(iter(simulation.MODELS))

    async def prepare(*args):
        clock.return_value += 2.0
        assert sim.active.timing()["prepare_ms"] == 2000
        return "engine", "weights"

    async def events():
        clock.return_value += 0.25
        yield b'data: {"choices":[{"delta":{"role":"assistant"}}]}'
        yield b'data: {"choices":[{"delta":{"reasoning_content":"thinking"}}]}'
        yield b'data: {"choices":[{"delta":{"content":" "}}]}'
        assert sim.active.timing()["first_text_ms"] is None
        clock.return_value += 0.25
        yield b'data: {"choices":[{"delta":{"content":"Hi"}}]}'
        clock.return_value += 0.75
        yield b'data: {"choices":[{"delta":{"content":"!"}}]}'
        yield b"data: [DONE]"

    @asynccontextmanager
    async def post(*args, **kwargs):
        yield SimpleNamespace(status=200, content=events())

    async def open_engine(*args):
        clock.return_value += 3.0
        assert sim.active.timing()["load_ms"] == 3000
        return simulation.ResidentEngine(
            model,
            SimpleNamespace(returncode=None),
            SimpleNamespace(post=post),
            AsyncExitStack(),
        )

    monkeypatch.setattr(sim.files, "prepare", prepare)
    monkeypatch.setattr(simulation, "_open_engine", open_engine)

    async def run():
        for cold in (True, False):
            start = sim.start(
                "admin",
                model,
                [{"role": "user", "content": "hi"}],
                "P",
                "S",
                "S",
                Settings(),
            )
            assert start["timing"]["elapsed_ms"] == 0
            await sim.task
            clock.return_value += 20  # The dashboard can poll long after completion.
            result = sim.find("admin", start["id"]).view(sim.loaded_model)
            assert result["answer"] == " Hi!" and result["status"] == "done"
            assert result["timing"] == {
                "load_mode": "cold" if cold else "resident",
                "prepare_ms": 2000 if cold else None,
                "load_ms": 3000 if cold else None,
                "first_text_ms": 5500 if cold else 500,
                "elapsed_ms": 6250 if cold else 1250,
            }
        await sim.close()

    asyncio.run(run())


def test_cancelling_while_the_native_engine_starts_reaps_the_child(monkeypatch):
    async def run():
        launching, release = asyncio.Event(), asyncio.Event()
        proc = SimpleNamespace(returncode=None, terminate=Mock(), wait=AsyncMock())

        async def spawn(*args):
            launching.set()
            await release.wait()
            return proc

        monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)

        async def use_engine():
            async with simulation._engine_process("llama-server"):
                pytest.fail("Cancelled job reached generation")

        task = asyncio.create_task(use_engine())
        await launching.wait()
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        proc.terminate.assert_called_once_with()
        proc.wait.assert_awaited_once_with()

    asyncio.run(run())


def test_simulation_access_isolation_switching_and_history(
    entry, monkeypatch, native_engines
):
    async def run():
        async with entry.running({"local_chat_provider_id": "my-tuned-model"}) as env:
            plugin = env.plugin
            labels = plugin.simulation.models(plugin.settings())
            assert [x["kind"] for x in labels] == [
                "local",
                "local",
                "bailian",
                "bailian",
                "configured",
            ]
            assert [x["id"] for x in labels if x["kind"] != "bailian"] == [
                "Qwen3.5-2B",
                "Qwen3-8B",
                "configured",
            ]

            async def api(**data):
                env.request.payload = data
                return await plugin.page_simulation()

            barrier = asyncio.Event()
            captures = []

            async def fake_reply(client, core, messages, job):
                captures.append((job.model, messages, core))
                await barrier.wait()
                job.answer = "试聊回复"

            monkeypatch.setattr(simulation, "_stream", fake_reply)
            plugin.simulation.files.prepare = AsyncMock(
                return_value=("engine", "weights")
            )
            env.request.username = None
            for action in ("active", "load", "unload", "select"):
                assert (await api(action=action, model="Qwen3-8B")).status_code == 403
            assert (
                await api(action="send", model="Qwen3-8B", message="测试")
            ).status_code == 403
            env.request.username = "A"
            history = [
                {"role": "user", "content": "上文"},
                {"role": "assistant", "content": "收到"},
            ]
            for model in ("Qwen3.5-2B", "Qwen3-8B"):
                barrier.clear()
                started = await api(
                    action="send", model=model, message="继续", history=history
                )
                assert started["status"] == "running"
                await asyncio.sleep(0)
                assert (
                    await api(action="send", model=model, message="重复")
                ).status_code == 409
                env.request.username = "B"
                foreign = await api(action="active")
                assert foreign["job"] is None and foreign["busy"]
                assert (await api(action="unload", model=model)).status_code == 409
                assert (await api(action="cancel", id=started["id"])).status_code == 404
                assert (await api(action="poll", id=started["id"])).status_code == 404
                env.request.username = "A"
                barrier.set()
                await plugin.simulation.task
                result = await api(action="poll", id=started["id"])
                assert result["answer"] == "试聊回复" and result["status"] == "done"
                assert result["loaded_model"] == model
                assert captures[-1][:2] == (
                    model,
                    [*history, {"role": "user", "content": "继续"}],
                )

            env.context.llm_generate.assert_not_called()
            env.context.llm_generate.return_value = SimpleNamespace(
                completion_text="自定义回答"
            )
            job = await api(
                action="send", model="configured", message="试试", history=history
            )
            await plugin.simulation.task
            result = await api(action="poll", id=job["id"])
            assert result["answer"] == "自定义回答"
            assert result["timing"]["elapsed_ms"] >= 0
            assert all(
                result["timing"][key] is None
                for key in ("load_mode", "prepare_ms", "load_ms", "first_text_ms")
            )
            args = env.context.llm_generate.await_args.kwargs
            assert args["chat_provider_id"] == "my-tuned-model"
            assert args["contexts"] == history and args["tools"] is None
            assert args["system_prompt"] == captures[0][2]
            assert plugin.simulation.loaded_model is None
            assert all(engine.process.returncode == 0 for engine in native_engines)
            env.context.send_message.assert_not_called()
            assert plugin.store.pending_sources() == []

    asyncio.run(run())


def test_manual_load_release_and_warm_messages(entry, monkeypatch, native_engines):
    small, large = simulation.MODELS

    async def run():
        async with entry.running({"local_chat_provider_id": "custom"}) as env:
            sim = env.plugin.simulation
            sim.files.prepare = AsyncMock(return_value=("engine", "weights"))

            async def reply(client, system, messages, job):
                assert client is native_engines[-1].client
                job.answer = "收到"

            monkeypatch.setattr(simulation, "_stream", reply)

            async def api(action, **data):
                env.request.payload = dict(action=action, **data)
                return await env.plugin.page_simulation()

            async def finish(job):
                await sim.task
                result = await api("poll", id=job["id"])
                assert result["status"] == "done"
                return result

            assert (await api("select", model=large))["job"] is None
            assert (await api("load", model="configured")).status_code == 400
            loaded = await finish((await api("load", model=small))["job"])
            assert loaded["loaded_model"] == small and loaded["operation"] == "load"
            assert loaded["answer"] == "" and loaded["question"] == ""
            assert loaded["timing"]["load_ms"] is not None
            assert loaded["timing"]["first_text_ms"] is None
            assert (await env.plugin.page_state())["simulation_loaded_model"] == small
            assert (await api("load", model=small))["job"] is None
            for question in ("你好", "再聊一句"):
                chat = await finish(await api("send", model=small, message=question))
                assert chat["answer"] == "收到" and chat["loaded_model"] == small
            assert len(native_engines) == 1 and sim.files.prepare.await_count == 1

            switched = await finish((await api("select", model=large))["job"])
            assert (
                switched["loaded_model"] == large
                and native_engines[0].process.returncode == 0
            )
            assert (await api("unload", model=small)).status_code == 409
            assert sim.loaded_model == large
            released = await finish((await api("unload", model=large))["job"])
            assert released["loaded_model"] is None
            assert released["timing"]["elapsed_ms"] >= 0
            assert released["timing"]["load_ms"] is None
            assert (await api("unload", model=large))["job"] is None
            await finish(await api("send", model=large, message="再加载"))
            assert len(native_engines) == 3

            native_engines[-1].process.returncode = 9
            assert (await api("active"))["loaded_model"] is None
            await finish(await api("send", model=large, message="恢复"))
            assert len(native_engines) == 4 and sim.loaded_model == large
            await finish((await api("select", model="configured"))["job"])
            assert sim.loaded_model is None
            await finish((await api("load", model=small))["job"])
        assert native_engines[-1].process.returncode == 0

    asyncio.run(run())


def test_bailian_uses_selected_deployment_persona_and_history(
    entry, monkeypatch, native_engines
):
    calls = []

    @asynccontextmanager
    async def post(client, url, **kwargs):
        calls.append((url, kwargs))
        body = {"output": {"choices": [{"message": {"content": "  接住上文了  "}}]}}
        yield SimpleNamespace(status=200, json=AsyncMock(return_value=body))

    monkeypatch.setattr(aiohttp.ClientSession, "post", post)
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-only-secret")

    async def run():
        async with entry.running() as env:
            plugin = env.plugin
            sim = plugin.simulation
            sim.files.prepare = AsyncMock(return_value=("engine", "weights"))

            async def api(action, **data):
                env.request.payload = dict(action=action, **data)
                return await plugin.page_simulation()

            state = await plugin.page_state()
            assert "test-only-secret" not in json.dumps(state)
            cloud = next(
                option
                for option in state["simulation_models"]
                if option["kind"] == "bailian"
            )
            assert (
                cloud["id"]
                == "bailian:llm-qd5s21mahp3999y0:qwen3-4b-instruct-2507-438f534673d5-v1"
            )
            assert not cloud["unavailable_reason"]
            await api("load", model="Qwen3.5-2B")
            await sim.task
            selecting = await api("select", model=cloud["id"])
            assert selecting["job"]["operation"] == "unload"
            await sim.task
            assert (
                sim.loaded_model is None and native_engines[0].process.returncode == 0
            )
            for action in ("load", "unload"):
                assert (await api(action, model=cloud["id"])).status_code == 400
            assert not calls

            # A changed workspace or removed deployment invalidates stale page choices.
            plugin.config.update(
                bailian_workspace_id="ws-new", bailian_model_codes=["tuned-v2"]
            )
            old = await api("send", model=cloud["id"], message="旧页面")
            assert old.status_code == 400 and not calls
            model = "bailian:ws-new:tuned-v2"
            env.request.username = None
            assert (await api("send", model=model, message="未登录")).status_code == 403
            env.request.username = "admin"
            history = [
                {"role": "user", "content": "上文"},
                {"role": "assistant", "content": "收到"},
            ]
            started = await api("send", model=model, message="继续", history=history)
            await sim.task
            result = await api("poll", id=started["id"])
            assert result["status"] == "done" and result["answer"] == "接住上文了"
            assert (
                result["timing"]["elapsed_ms"] >= 0
                and result["timing"]["first_text_ms"] is None
            )
            persona = await env.context.persona_manager.get_persona(result["persona"])
            assert len(calls) == 1
            url, request = calls[0]
            assert (
                url
                == "https://ws-new.cn-beijing.maas.aliyuncs.com/api/v1/services/aigc/text-generation/generation"
            )
            assert request["headers"]["Authorization"] == "Bearer test-only-secret"
            assert request["allow_redirects"] is False
            assert request["json"] == {
                "model": "tuned-v2",
                "input": {
                    "messages": [
                        {"role": "system", "content": persona.system_prompt},
                        *history,
                        {"role": "user", "content": "继续"},
                    ]
                },
                "parameters": {
                    "result_format": "message",
                    "max_tokens": simulation.MAX_OUTPUT,
                    "enable_thinking": False,
                },
            }
            sim.files.prepare.assert_awaited_once()
            env.context.llm_generate.assert_not_called()
            env.context.send_message.assert_not_called()
            assert plugin.store.pending_sources() == []

            monkeypatch.delenv("DASHSCOPE_API_KEY")
            missing = await api("send", model=model, message="缺少密钥")
            assert (
                missing.status_code == 400 and "百炼试聊 API Key" in missing["message"]
            )
            assert len(calls) == 1

    asyncio.run(run())


def test_group_chat_v2_uses_its_evaluated_settings_and_only_returns_the_answer(
    entry, monkeypatch
):
    requests = []

    @asynccontextmanager
    async def post(client, url, **kwargs):
        requests.append(kwargs["json"])
        message = {"content": "  星河  ", "reasoning_content": "模型的内部推理"}
        body = {"output": {"choices": [{"message": message}]}}
        yield SimpleNamespace(status=200, json=AsyncMock(return_value=body))

    monkeypatch.setattr(aiohttp.ClientSession, "post", post)
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-workspace-key")

    async def run():
        async with entry.running() as env:
            plugin = env.plugin
            state = await plugin.page_state()
            model = "bailian:llm-qd5s21mahp3999y0:qwen3-8b-d39bed90dfab"
            cloud = next(
                row for row in state["simulation_models"] if row["id"] == model
            )
            assert "群聊微调 Qwen3-8B v2" in cloud["label"]
            history = [
                {"role": "user", "content": "代号叫星河"},
                {"role": "assistant", "content": "记住了"},
            ]
            env.request.payload = dict(
                action="send", model=model, message="代号是什么？", history=history
            )
            job = await plugin.page_simulation()
            await plugin.simulation.task
            env.request.payload = {"action": "poll", "id": job["id"]}
            result = await plugin.page_simulation()
            assert (result["status"], result["answer"]) == ("done", "星河")
            persona = await env.context.persona_manager.get_persona(result["persona"])
            assert requests == [
                {
                    "model": "qwen3-8b-d39bed90dfab",
                    "input": {
                        "messages": [
                            {"role": "system", "content": persona.system_prompt},
                            *history,
                            {"role": "user", "content": "代号是什么？"},
                        ]
                    },
                    "parameters": {
                        "result_format": "message",
                        "enable_thinking": True,
                        "max_tokens": 1536,
                        "temperature": 0.6,
                        "top_p": 0.95,
                        "top_k": 20,
                    },
                }
            ]

    asyncio.run(run())


@pytest.mark.parametrize(
    ("status", "body", "detail"),
    [
        (403, {"message": "private provider diagnostics"}, "出口 IP"),
        (429, {}, "限流"),
        (200, {"output": {"choices": []}}, "格式"),
        (200, {"output": {"choices": [{"message": {"content": ""}}]}}, "正文"),
    ],
)
def test_bailian_failures_remain_visible_without_fallback(
    entry, monkeypatch, status, body, detail
):
    @asynccontextmanager
    async def post(*args, **kwargs):
        yield SimpleNamespace(status=status, json=AsyncMock(return_value=body))

    monkeypatch.setattr(aiohttp.ClientSession, "post", post)
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-only-secret")

    async def run():
        async with entry.running() as env:
            sim = env.plugin.simulation
            settings = env.plugin.settings()
            model = next(
                row["id"] for row in sim.models(settings) if row["kind"] == "bailian"
            )
            chat = sim.start(
                "admin",
                model,
                [{"role": "user", "content": "测试"}],
                "可可子",
                "人设",
                "已保存的人格",
                settings,
            )
            await sim.task
            result = sim.find("admin", chat["id"]).view(sim.loaded_model)
            assert result["status"] == "error" and detail in result["detail"]
            assert (
                "private" not in result["detail"]
                and "test-only-secret" not in result["detail"]
            )
            assert sim.active is None
            env.context.llm_generate.assert_not_called()

    asyncio.run(run())


def test_cancel_reply_keeps_engine_and_cancel_switch_finishes_release(
    entry, monkeypatch, native_engines
):
    small, large = simulation.MODELS

    async def run():
        async with entry.running() as env:
            sim = env.plugin.simulation
            sim.files.prepare = AsyncMock(return_value=("engine", "weights"))
            generating, releasing, allow_release = (
                asyncio.Event(),
                asyncio.Event(),
                asyncio.Event(),
            )

            async def long_reply(*args):
                generating.set()
                await asyncio.Event().wait()

            monkeypatch.setattr(simulation, "_stream", long_reply)
            msg = [{"role": "user", "content": "长回复"}]
            chat = sim.start(
                "admin",
                small,
                msg,
                "可可子",
                "人设",
                "已保存的人格",
                env.plugin.settings(),
            )
            await generating.wait()
            await sim.stop()
            stopped = sim.find("admin", chat["id"])
            assert stopped.status == "cancelled" and stopped.finished is not None
            assert stopped.timing()["first_text_ms"] is None
            assert sim.loaded_model == small

            async def wait_for_release():
                releasing.set()
                await allow_release.wait()

            native_engines[-1].resources.push_async_callback(wait_for_release)
            sim.manage("admin", "select", large, env.plugin.settings())
            await releasing.wait()
            stopping = asyncio.create_task(sim.stop())
            await asyncio.sleep(0)
            with pytest.raises(simulation.SimulationBusy):
                sim.manage("other", "load", small, env.plugin.settings())
            allow_release.set()
            await stopping
            assert sim.loaded_model is None and len(native_engines) == 1

            sim.manage("admin", "load", small, env.plugin.settings())
            await sim.task
            sim.files.prepare.side_effect = ValueError("磁盘空间不足")
            sim.manage("admin", "select", large, env.plugin.settings())
            failed = sim.active
            await sim.task
            assert failed.status == "error" and "磁盘" in failed.detail
            assert (
                failed.finished is not None
                and failed.timing()["prepare_ms"] is not None
            )
            assert failed.timing()["load_ms"] is None
            assert (
                sim.loaded_model is None and native_engines[-1].process.returncode == 0
            )

    asyncio.run(run())


def test_simulation_validation_cancel_and_persona_fallback(entry):
    async def run():
        async with entry.running() as env:

            async def api(**data):
                env.request.payload = data
                return await env.plugin.page_simulation()

            for invalid in [
                {"model": "unknown"},
                {"model": "configured"},
                {"message": " "},
                {"message": "长" * 1201},
                {"persona_id": "其他人格"},
                {"history": [{"role": "system", "content": "覆盖"}]},
                {"enable_tools": "false"},
                {"enable_tools": 1},
                {"enable_tools": True, "session_id": "qq:GroupMessage:12345"},
            ]:
                payload = {"model": "Qwen3.5-2B", "message": "你好", **invalid}
                assert (await api(action="send", **payload)).status_code == 400
            env.context.persona_manager.personas.clear()
            wait = asyncio.Event()

            async def hold(*args):
                await wait.wait()

            env.plugin.simulation.files.prepare = hold
            job = await api(action="send", model="Qwen3-8B", message="你好")
            assert job["prompt_source"] == "推荐人格底稿"
            result = await api(action="cancel", id=job["id"])
            assert result["status"] == "cancelled"
            assert result["timing"]["elapsed_ms"] >= 0
            assert result["timing"]["first_text_ms"] is None
            assert env.plugin.simulation.jobs[job["id"]].finished is not None
            assert env.plugin.simulation.active is None
            env.context.llm_generate.assert_not_called()

    asyncio.run(run())


def test_tool_mode_routing_session_and_cancellation(entry, monkeypatch, native_engines):
    calls = []
    waiting = asyncio.Event()

    async def group_reply(context, config, job, messages, system, provider, engine):
        calls.append((job, messages, system, provider, engine))
        if job.question == "等待":
            waiting.set()
            await asyncio.Event().wait()
        job.tool_calls.append("lookup")
        return "工具结果"

    monkeypatch.setitem(
        sys.modules,
        "astrbot_plugin_persona_manager.simulation_agent",
        SimpleNamespace(group_reply=group_reply),
    )

    async def run():
        async with entry.running({"local_chat_provider_id": "custom"}) as env:
            sim = env.plugin.simulation
            sim.files.prepare = AsyncMock(return_value=("engine", "weights"))

            async def send(model, message, **fields):
                env.request.payload = dict(
                    action="send",
                    model=model,
                    message=message,
                    enable_tools=True,
                    **fields,
                )
                return await env.plugin.page_simulation()

            small = next(iter(simulation.MODELS))
            sim.manage("A", "load", small, env.plugin.settings())
            await sim.task
            assert sim.engine.context_size == simulation.LOCAL_CONTEXT
            first = await send(small, "第一轮")
            await sim.task
            assert (
                first["enable_tools"]
                and sim.find(env.request.username, first["id"]).answer == "工具结果"
            )
            assert sim.engine.context_size == simulation.LOCAL_TOOL_CONTEXT
            assert (
                len(native_engines) == 2 and native_engines[0].process.returncode == 0
            )
            second = await send(small, "继续", session_id=first["session_id"])
            await sim.task
            assert (
                second["session_id"] == first["session_id"] and len(native_engines) == 2
            )
            assert calls[-1][-1] is sim.engine
            pending = await send("configured", "等待")
            await waiting.wait()
            assert pending["session_id"] != first["session_id"] and sim.engine is None
            assert calls[-1][-2:] == ("custom", None)
            assert sim.state(env.request.username)["job"]["enable_tools"] is True
            assert sim.state("foreign")["job"] is None
            await sim.stop()
            assert sim.find(env.request.username, pending["id"]).status == "cancelled"
            env.request.payload = dict(action="load", model=small, enable_tools=True)
            loaded = (await env.plugin.page_simulation())["job"]
            await sim.task
            assert sim.find(env.request.username, loaded["id"]).status == "done"
            assert sim.engine.context_size == simulation.LOCAL_TOOL_CONTEXT
            await send(small, "已预热")
            await sim.task
            assert calls[-1][0].load_mode == "resident"
            env.context.llm_generate.assert_not_called()
            env.context.send_message.assert_not_called()
            assert not env.plugin.store.pending_sources()

    asyncio.run(run())


def test_asset_resume_validation_and_cache(tmp_path):
    async def run():
        data = b"test model data"
        seen = []

        async def serve(request):
            seen.append(request.headers.get("Range"))
            if request.path == "/bad":
                return web.Response(body=b"x" * len(data))
            if seen[-1]:
                start, end = map(int, seen[-1].removeprefix("bytes=").split("-"))
                return web.Response(
                    status=206,
                    body=data[start : end + 1],
                    headers={"Content-Range": f"bytes {start}-{end}/{len(data)}"},
                )
            return web.Response(body=data)

        app = web.Application()
        app.router.add_get("/{name}", serve)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        files = ModelFiles(tmp_path)
        base = f"http://127.0.0.1:{runner.addresses[0][1]}"
        digest = hashlib.sha256(data).hexdigest()
        try:
            async with aiohttp.ClientSession() as session:
                asset = Asset("model.gguf", len(data), digest, (base + "/model",))
                (tmp_path / "model.gguf.part").write_bytes(data[:4])
                result = await files.fetch(session, asset, lambda _: None)
                assert result.read_bytes() == data and seen == [
                    f"bytes=4-{len(data) - 1}"
                ]
                await files.fetch(session, asset, lambda _: None)
                assert len(seen) == 1
                bad = Asset("corrupt", len(data), digest, (base + "/bad",))
                with pytest.raises(ValueError, match="SHA256"):
                    await files.fetch(session, bad, lambda _: None)
                assert not (tmp_path / "corrupt").exists()
                fallback = Asset(
                    "fallback", len(data), digest, (base + "/bad", base + "/model")
                )
                assert (
                    await files.fetch(session, fallback, lambda _: None)
                ).read_bytes() == data
        finally:
            await runner.cleanup()

    asyncio.run(run())
