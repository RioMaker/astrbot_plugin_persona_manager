"""Authenticated Dashboard chats with local and remote text models."""

import asyncio
import json
import logging
import os
import re
import secrets
import sys
import tempfile
import time
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path

import aiohttp

from . import simulation_bailian
from .settings import BAILIAN_TRIAL_MODELS
from .simulation_models import MODELS, ModelFiles

MAX_ROUNDS = 6
MAX_OUTPUT = 160
CONFIGURED = "configured"
SEED_SOURCE = "推荐人格底稿"
LOCAL_CONTEXT = 4096
LOCAL_TOOL_CONTEXT = 16384


class SimulationBusy(ValueError):
    pass


class SimulationToolError(ValueError):
    """An actionable tool-mode error that is safe to display on the dashboard."""


def chat_messages(payload):
    prompt = payload.get("message")
    history = payload.get("history", [])
    if not isinstance(prompt, str) or not 0 < len(prompt.strip()) <= 1200:
        raise ValueError("请输入 1～1200 字的消息")
    if (
        not isinstance(history, list)
        or len(history) > 2 * MAX_ROUNDS
        or len(history) % 2
    ):
        raise ValueError("模拟历史最多包含最近六轮完整对话")
    messages = []
    for i, row in enumerate(history):
        role = "assistant" if i % 2 else "user"
        if not isinstance(row, dict) or row.get("role") != role:
            raise ValueError("模拟历史的消息顺序无效")
        content = row.get("content")
        if not isinstance(content, str) or not 0 < len(content.strip()) <= (
            4000 if i % 2 else 1200
        ):
            raise ValueError("模拟历史包含无效正文")
        messages.append({"role": role, "content": content.strip()})
    return [*messages, {"role": "user", "content": prompt.strip()}]


@dataclass
class Job:
    owner: str
    model: str
    question: str = ""
    persona: str = ""
    prompt_source: str = ""
    operation: str = "send"
    enable_tools: bool = False
    session_id: str | None = None
    tool_calls: list[str] = field(default_factory=list)
    id: str = field(default_factory=lambda: secrets.token_hex(16))
    status: str = "running"
    detail: str = "准备模拟对话"
    answer: str = ""
    started: float = field(default_factory=lambda: time.monotonic())
    load_mode: str | None = None
    prepare_started: float | None = None
    load_started: float | None = None
    ready_at: float | None = None
    first_text_at: float | None = None
    finished: float | None = None
    stopping: bool = False

    def progress(self, detail):
        self.detail = detail

    def timing(self):
        end = self.finished if self.finished is not None else time.monotonic()

        def milliseconds(start, stop):
            if start is not None and stop is not None:
                return round((stop - start) * 1000)
            return None

        return {
            "load_mode": self.load_mode,
            "elapsed_ms": milliseconds(self.started, end),
            "prepare_ms": milliseconds(
                self.prepare_started,
                self.load_started if self.load_started is not None else end,
            ),
            "load_ms": milliseconds(
                self.load_started, self.ready_at if self.ready_at is not None else end
            ),
            "first_text_ms": milliseconds(self.started, self.first_text_at),
        }

    def view(self, loaded_model):
        return {
            "id": self.id,
            "model": self.model,
            "operation": self.operation,
            "enable_tools": self.enable_tools,
            "session_id": self.session_id,
            "tool_calls": list(self.tool_calls),
            "loaded_model": loaded_model,
            "persona": self.persona,
            "question": self.question,
            "prompt_source": self.prompt_source,
            "status": self.status,
            "detail": self.detail,
            "answer": self.answer,
            "timing": self.timing(),
        }


@dataclass
class ResidentEngine:
    model: str
    process: asyncio.subprocess.Process
    client: aiohttp.ClientSession
    resources: AsyncExitStack
    context_size: int = LOCAL_CONTEXT


class Simulation:
    def __init__(self, directory, context, config=None):
        self.files = ModelFiles(directory)
        self.context = context
        self.config = config if config is not None else {}
        self.jobs = {}
        self.active = None
        self.task = None
        self._closing = None
        self.engine: ResidentEngine | None = None

    @property
    def loaded_model(self):
        if self.engine and self.engine.process.returncode is None:
            return self.engine.model
        return None

    def state(self, owner):
        job = self.active
        return {
            "job": job.view(self.loaded_model) if job and job.owner == owner else None,
            "busy": job is not None,
            "loaded_model": self.loaded_model,
        }

    def models(self, settings):
        choices = [
            {
                "id": name,
                "kind": "local",
                "label": f"{name} · 原版 · {asset.size / 1e9:.2f} GB",
            }
            for name, asset in MODELS.items()
        ]
        if settings.bailian_workspace_id:
            reason = (
                ""
                if simulation_bailian.api_key(self.config)
                else simulation_bailian.MISSING_KEY
            )
            choices.extend(
                {
                    "id": f"bailian:{settings.bailian_workspace_id}:{code}",
                    "kind": "bailian",
                    "label": (
                        "阿里云百炼 · 群聊微调 Qwen3-8B v2"
                        if simulation_bailian.is_group_chat_v2(
                            settings.bailian_workspace_id, code
                        )
                        else f"阿里云百炼 · {code}"
                    ),
                    "unavailable_reason": reason,
                    "group_trial_label": BAILIAN_TRIAL_MODELS.get(code, ""),
                }
                for code in sorted(settings.bailian_model_codes)
            )
        provider = settings.local_chat_provider_id
        if provider:
            choices.append(
                {
                    "id": CONFIGURED,
                    "kind": "configured",
                    "label": f"自定义模型 · {provider}",
                }
            )
        return choices

    def find(self, owner, job_id):
        job = self.jobs.get(job_id) if isinstance(job_id, str) else None
        if (
            job is None
            or job.owner != owner
            or (job.finished is not None and time.monotonic() - job.finished >= 900)
        ):
            raise ValueError("模拟任务已过期或不存在")
        return job

    def _check_available(self, model, settings, enable_tools):
        if not isinstance(enable_tools, bool):
            raise ValueError("插件能力开关必须是布尔值")
        choice = next(
            (item for item in self.models(settings) if item["id"] == model), None
        )
        if choice is None:
            raise ValueError("请选择可用的模拟模型")
        if self._closing is not None or self.active is not None:
            raise SimulationBusy("模拟引擎正在处理另一个请求，请稍后重试")
        return choice

    def start(
        self,
        owner,
        model,
        messages,
        persona,
        system,
        source,
        settings,
        *,
        enable_tools=False,
        session_id=None,
    ):
        choice = self._check_available(model, settings, enable_tools)
        if reason := choice.get("unavailable_reason"):
            raise ValueError(reason)
        if session_id is not None and (
            not isinstance(session_id, str)
            or not re.fullmatch(r"[0-9a-f]{32}", session_id)
        ):
            raise ValueError("模拟会话标识无效")
        job = Job(
            owner,
            model,
            messages[-1]["content"],
            persona,
            source,
            enable_tools=enable_tools,
            session_id=(session_id or secrets.token_hex(16)) if enable_tools else None,
        )
        self._submit(job, messages, system, settings.local_chat_provider_id)
        return job.view(self.loaded_model)

    def _resident_for(self, model, enable_tools):
        return self.loaded_model == model and (
            not enable_tools or self.engine.context_size >= LOCAL_TOOL_CONTEXT
        )

    def manage(self, owner, action, model, settings, *, enable_tools=False):
        self._check_available(model, settings, enable_tools)
        loaded = self.loaded_model
        if action == "select":
            if not loaded:
                return
            action = "load" if model in MODELS else "unload"
        elif model not in MODELS:
            raise ValueError("此模型的加载和释放由提供商管理")
        elif action == "unload" and loaded and loaded != model:
            raise SimulationBusy("驻留模型已变更，请核对当前模型后再释放")

        if (action == "load" and self._resident_for(model, enable_tools)) or (
            action == "unload" and self.engine is None
        ):
            return
        detail = "准备释放本地模型" if action == "unload" else f"准备加载 {model}"
        self._submit(
            Job(
                owner, model, operation=action, detail=detail, enable_tools=enable_tools
            )
        )

    def _submit(self, job, messages=(), system="", provider=""):
        now = time.monotonic()
        self.jobs = {
            k: v
            for k, v in self.jobs.items()
            if v.finished is None or now - v.finished < 900
        }
        while len(self.jobs) >= 8:
            self.jobs.pop(next(iter(self.jobs)))
        self.jobs[job.id] = self.active = job
        self.task = asyncio.create_task(self._run(job, messages, system, provider))
        self.task.add_done_callback(lambda task: self._finished(job, task))

    def _finished(self, job, task):
        # Cancellation before _run's first instruction must also release the slot.
        if task.cancelled():
            job.status, job.detail = "cancelled", "已停止"
            job.finished = time.monotonic()
        self.active = None

    async def _load(self, job):
        context_size = LOCAL_TOOL_CONTEXT if job.enable_tools else LOCAL_CONTEXT
        if self._resident_for(job.model, job.enable_tools):
            job.load_mode = "resident"
            return self.engine
        job.load_mode = "cold"
        await self._release(job)
        job.prepare_started = time.monotonic()
        binary, weights = await self.files.prepare(job.model, job.progress)
        job.load_started = time.monotonic()
        job.progress(f"正在加载 {job.model}")
        self.engine = await _open_engine(job.model, binary, weights, context_size)
        job.ready_at = time.monotonic()
        return self.engine

    async def _release(self, job=None):
        engine = self.engine
        if engine is None:
            return
        if job:
            job.progress(f"正在释放 {engine.model}")
        # Finish reaping the old process before another load can claim this slot.
        closing = asyncio.create_task(engine.resources.aclose())
        try:
            await asyncio.shield(closing)
        except asyncio.CancelledError:
            await closing
            raise
        finally:
            self.engine = None

    async def _group_reply(self, job, messages, system, provider_id):
        try:
            from .simulation_agent import group_reply
        except ImportError as exc:
            raise SimulationToolError(
                "无法加载 AstrBot 群聊 Agent，请检查框架版本和依赖"
            ) from exc

        engine = None
        if job.model in MODELS:
            engine = await self._load(job)
        else:
            await self._release(job)
        job.progress("正在准备群聊插件与 Skill")
        try:
            return await asyncio.wait_for(
                group_reply(
                    self.context,
                    self.config,
                    job,
                    messages,
                    system,
                    provider_id,
                    engine,
                ),
                timeout=360,
            )
        except asyncio.TimeoutError:
            raise SimulationToolError(
                "群聊模拟超时，请缩短问题或检查耗时的工具"
            ) from None
        except SimulationToolError:
            raise
        except Exception as exc:
            status = getattr(exc, "status_code", None)
            reason = f"HTTP {status}" if isinstance(status, int) else type(exc).__name__
            logging.getLogger("astrbot").warning(
                "[可可子人格] %s 群聊模拟调用失败（%s）", job.model, reason
            )
            raise SimulationToolError(
                f"群聊能力调用失败（{reason}），请检查模型服务、工具调用支持和上下文长度"
            ) from exc

    async def _run(self, job, messages, system, provider):
        try:
            if job.operation == "unload":
                await self._release(job)
                job.detail = "本地模型已释放"
            elif job.enable_tools and job.operation == "send":
                job.answer = await self._group_reply(job, messages, system, provider)
            elif job.model.startswith("bailian:"):
                await self._release(job)
                job.progress("正在等待阿里云百炼回复")
                _, workspace, model_code = job.model.split(":", 2)
                job.answer = await simulation_bailian.generate(
                    workspace,
                    model_code,
                    system,
                    messages,
                    MAX_OUTPUT,
                    simulation_bailian.api_key(self.config),
                )
            elif job.model == CONFIGURED:
                await self._release(job)
                job.progress("自定义模型正在回复")
                result = await asyncio.wait_for(
                    self.context.llm_generate(
                        chat_provider_id=provider,
                        system_prompt=system,
                        contexts=messages[:-1],
                        prompt=messages[-1]["content"],
                        tools=None,
                        max_tokens=MAX_OUTPUT,
                    ),
                    timeout=360,
                )
                job.answer = (getattr(result, "completion_text", "") or "").strip()
            else:
                engine = await self._load(job)
                if job.operation == "load":
                    job.detail = f"{job.model} 已加载，可以试聊"
                else:
                    job.progress(f"{job.model} 正在回复")
                    try:
                        await _stream(engine.client, system, messages, job)
                    except (aiohttp.ClientConnectionError, asyncio.TimeoutError):
                        await self._release()
                        raise
            if job.operation == "send":
                if not job.answer.strip():
                    raise ValueError("模型没有返回正文，请重试")
                job.detail = "回复完成"
            job.status = "done"
        except asyncio.CancelledError:
            job.status, job.detail = "cancelled", "已停止"
        except SimulationToolError as exc:
            job.status, job.detail = "error", str(exc)
        except ValueError as exc:
            job.status, job.detail = (
                "error",
                str(exc)
                if job.model != CONFIGURED or job.operation != "send"
                else "自定义模型调用失败，请检查已配置提供商的连接和模型名",
            )
        except Exception as exc:
            job.status, job.detail = (
                "error",
                f"模拟调用失败（{type(exc).__name__}），请检查模型服务、磁盘余量或网络后重试",
            )
        finally:
            job.finished = time.monotonic()

    async def stop(self):
        task, job = self.task, self.active
        if task and job:
            if not job.stopping:
                job.stopping = True
                job.detail = "正在停止"
                task.cancel()
            await asyncio.shield(asyncio.gather(task, return_exceptions=True))

    async def close(self):
        if self._closing is None:
            self._closing = asyncio.create_task(self._shutdown())
        await asyncio.shield(self._closing)

    async def _shutdown(self):
        await self.stop()
        await self._release()
        self.jobs.clear()


async def _open_engine(model, binary, weights, context_size):
    binary, weights = binary.resolve(), weights.resolve()
    async with AsyncExitStack() as resources:
        run_dir = resources.enter_context(
            tempfile.TemporaryDirectory(prefix="persona-sim-")
        )
        socket = Path(run_dir) / "chat.sock"
        lib_path = os.pathsep.join(
            filter(None, (str(binary.parent), os.environ.get("LD_LIBRARY_PATH")))
        )
        proc = await resources.enter_async_context(
            _engine_process(
                sys.executable,
                "-I",
                str(Path(__file__).with_name("simulation_exec.py").resolve()),
                str(os.getpid()),
                str(binary),
                "--model",
                str(weights),
                "--alias",
                model,
                "--host",
                str(socket),
                "--threads",
                "2",
                "--threads-batch",
                "2",
                "--ctx-size",
                str(context_size),
                "--parallel",
                "1",
                "--sleep-idle-seconds",
                "-1",
                "--cache-ram",
                "0",
                "--n-predict",
                str(MAX_OUTPUT),
                "--no-webui",
                "--no-warmup",
                "--log-disable",
                "--jinja",
                "--chat-template-kwargs",
                '{"enable_thinking":false}',
                env={**os.environ, "LD_LIBRARY_PATH": lib_path},
                cwd=binary.parent,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
        )
        client = await resources.enter_async_context(
            aiohttp.ClientSession(
                connector=aiohttp.UnixConnector(path=str(socket)),
                timeout=aiohttp.ClientTimeout(total=360),
            )
        )
        deadline = time.monotonic() + 300
        while time.monotonic() < deadline:
            if proc.returncode is not None:
                raise ValueError(
                    f"CPU 引擎启动失败（退出码 {proc.returncode}），请检查可用内存及系统是否支持运行 Linux x86_64 程序"
                )
            try:
                async with client.get("http://localhost/health", timeout=2) as check:
                    if check.status == 200:
                        break
            except (aiohttp.ClientError, asyncio.TimeoutError):
                pass
            await asyncio.sleep(0.3)
        else:
            raise ValueError("加载模型超时，请检查服务器内存及 CPU 占用")
        return ResidentEngine(model, proc, client, resources.pop_all(), context_size)


@asynccontextmanager
async def _engine_process(*args, **kwargs):
    starting = asyncio.create_task(asyncio.create_subprocess_exec(*args, **kwargs))
    try:
        yield await asyncio.shield(starting)
    finally:
        # A stop can arrive while the OS is creating the child, before it has a handle.
        proc = await starting
        if proc.returncode is None:
            try:
                proc.terminate()
            except ProcessLookupError:
                pass
        try:
            await asyncio.wait_for(proc.wait(), 5)
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            await proc.wait()


async def _stream(client, system, messages, job):
    while True:
        body = {
            "messages": [{"role": "system", "content": system}, *messages],
            "stream": True,
            "max_tokens": MAX_OUTPUT,
        }
        async with client.post(
            "http://localhost/v1/chat/completions", json=body
        ) as response:
            if response.status != 200:
                error = (await response.json()).get("error", {})
                if error.get("type") == "exceed_context_size_error":
                    if len(messages) > 1:
                        messages = messages[2:]
                        continue
                    raise ValueError(
                        "当前人设和消息超过本地模型的上下文窗口，请缩短后重试"
                    )
                raise ValueError(f"本地引擎返回 HTTP {response.status}")
            async for line in response.content:
                if not line.startswith(b"data:"):
                    continue
                data = line[5:].strip()
                if data == b"[DONE]":
                    return
                chunk = json.loads(data)
                if "error" in chunk:
                    raise ValueError("模型生成中断，请重试")
                for choice in chunk.get("choices", []):
                    content = choice.get("delta", {}).get("content") or ""
                    if job.first_text_at is None and content.strip():
                        job.first_text_at = time.monotonic()
                    job.answer += content
            raise ValueError("服务在回复完成前断开，请重试")
