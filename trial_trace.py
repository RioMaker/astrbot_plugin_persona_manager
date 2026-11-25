"""Recent, event-scoped diagnostics for explicitly selected QQ model trials."""

import asyncio
import json
import re
import time
from collections import deque
from contextlib import contextmanager
from uuid import uuid4

from .settings import BAILIAN_TRIAL_MODELS
from .usage import normalize_usage, usage_values

PER_GROUP = 20
MAX_TURNS = 60
MAX_STEPS = 80
FIELD_CHARS = 48_000
TURN_CHARS = 192_000
CLIPPED = "\n…（内容已截断）"
MASK = "[已隐藏]"
SECRET_NAME = r"(?:authorization|(?:access[_-]?|refresh[_-]?)?token|(?:api[_ -]?key)|password|secret|密码|密钥)"
SECRET_FIELD = re.compile(SECRET_NAME, re.I)
SECRET_TEXT = re.compile(
    rf"""(?i)(?:\bsk-[\w-]{{8,}}|\bbearer\s+[\w.=/+-]+|["']?{SECRET_NAME}["']?\s*[:：=]\s*(?:"[^"\n]*"|'[^'\n]*'|(?:bearer\s+)?[^\s,}}]+))"""
)


def clipped(text, limit):
    return (
        text if len(text) <= limit else text[: max(0, limit - len(CLIPPED))] + CLIPPED
    )


def clean(value, depth=0):
    if isinstance(value, str):
        if value.startswith("data:"):
            return "[媒体数据]"
        return SECRET_TEXT.sub(MASK, clipped(value, FIELD_CHARS))
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if depth >= 12:
        return "[嵌套内容已截断]"
    if isinstance(value, (list, tuple)):
        return [clean(item, depth + 1) for item in value[:256]] + (
            [CLIPPED] if len(value) > 256 else []
        )
    if isinstance(value, dict):
        if value.get("type") in ("image_url", "input_audio", "image", "audio"):
            return "[媒体内容]"
        return {
            key: MASK if SECRET_FIELD.fullmatch(key) else clean(item, depth + 1)
            for key, item in value.items()
            if isinstance(key, str)
            and key not in {"reasoning_content", "reasoning_signature"}
        }
    return f"[{type(value).__name__}]"


def display(value):
    if value is None:
        return ""
    value = clean(value)
    return (
        value
        if isinstance(value, str)
        else json.dumps(value, ensure_ascii=False, indent=2)
    )


def prompt_text(messages):
    parts = []
    for msg in messages or []:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role", "")
        content = msg.get("content")
        if isinstance(content, list):
            content = "\n".join(
                display(
                    part.get("text")
                    if isinstance(part, dict) and part.get("type") == "text"
                    else part
                )
                for part in content
            )
        label = " · ".join(
            str(x) for x in (role, msg.get("name"), msg.get("tool_call_id")) if x
        )
        body = display(content)
        if msg.get("tool_calls"):
            body += "\n" + display(msg["tool_calls"])
        parts.append(f"[{label}]\n{body}")
    text = "\n\n".join(parts)
    if len(text) <= FIELD_CHARS:
        return text
    # Preserve the beginning of the system prompt and the latest user/tool
    # messages when a long conversation exceeds one display field.
    tail = FIELD_CHARS // 3
    return text[: FIELD_CHARS - len(CLIPPED) - tail - 1] + CLIPPED + "\n" + text[-tail:]


def response_text(response):
    parts = [str(getattr(response, "completion_text", "") or "")]
    names = getattr(response, "tools_call_name", None) or []
    args = getattr(response, "tools_call_args", None) or []
    ids = getattr(response, "tools_call_ids", None) or []
    for i, name in enumerate(names):
        call_id = f" · {ids[i]}" if i < len(ids) else ""
        parts.append(
            f"调用 {name}{call_id}\n{display(args[i]) if i < len(args) else ''}"
        )
    return "\n".join(filter(None, parts))


def tool_output(result):
    if result is None:
        return "框架未返回工具内容"
    parts = [
        getattr(part, "text", "")
        if getattr(part, "type", "") == "text"
        else f"[{getattr(part, 'type', '工具内容')}]"
        for part in (getattr(result, "content", None) or [])
    ]
    if getattr(result, "structuredContent", None) is not None:
        parts.append(display(result.structuredContent))
    return "\n".join(parts) or "（空结果）"


class TrialStep:
    def __init__(self, turn, kind, label, started=None):
        self.turn = turn
        self.kind = kind
        self.label = label
        self.start = time.monotonic() if started is None else started
        self.end = None
        self.status = "running"
        self.fields = {}
        self.usage = usage_values() if kind == "model" else None
        self.payload = None
        self.partial = ""

    def write(self, key, text):
        if text is not None and text != "":
            self.fields[key] = self.turn.keep(display(text))

    def request_fields(self):
        # Both supported AstrBot adapters finalize messages and tools in this
        # per-call payload immediately before awaiting their OpenAI client.
        if self.payload is None:
            return {}
        fields = {"prompt": prompt_text(self.payload.get("messages"))}
        if self.payload.get("tools"):
            fields["tools"] = display(self.payload["tools"])
        return fields

    def finish(self, status="done", output=""):
        if self.end is not None:
            return
        self.end = time.monotonic()
        self.status = status
        if self.partial and output and status != "done":
            self.write("partial", self.partial)
        self.write("output", output or self.partial)
        self.partial = ""
        for key, value in self.request_fields().items():
            self.fields[key] = self.turn.keep(value)
        self.payload = None
        if self.kind == "model":
            self.turn.account(self.usage)

    def response(self, response):
        raw = getattr(response, "raw_completion", None)
        # The native adapter can turn missing/partial upstream usage into zeros.
        usage = normalize_usage(raw if raw is not None else response)
        if any(
            usage[key] is not None
            for key in ("input_tokens", "output_tokens", "total_tokens")
        ):
            self.usage = usage
        self.finish(
            "error" if getattr(response, "role", "") == "err" else "done",
            response_text(response),
        )

    def view(self):
        return {
            "kind": self.kind,
            "label": self.label,
            "status": self.status,
            "elapsed_ms": round(((self.end or time.monotonic()) - self.start) * 1000),
            "usage": self.usage,
            **self.fields,
            **({"output": display(self.partial)} if self.partial else {}),
            **{
                key: self.turn.fit(value)
                for key, value in self.request_fields().items()
            },
        }

    def chunk(self, response):
        if len(self.partial) < FIELD_CHARS:
            self.partial = clipped(
                self.partial + (response.completion_text or ""), FIELD_CHARS
            )
        if response.usage is not None:
            self.usage = normalize_usage(response)


class TrialTurn:
    def __init__(self, source, model, message, started):
        self.id = uuid4().hex
        self.scope = f"{source['platform_id']}:{source['group_id']}"
        self.persona = source["persona_id"]
        self.model = model
        self.start = started
        self.created_at = time.time() - (time.monotonic() - started)
        self.end = None
        self.status = "running"
        self.remaining = TURN_CHARS
        self.saved = {}
        self.preview = clipped(display(message), 100)
        self.steps = []
        self.omitted = 0
        self.model_calls = 0
        self.reported = 0
        self.tokens = 0
        self.last_model = None
        self.delivery = None
        self.has_receipt = False
        self.route = self.step("route", "选择测试模型", started)
        self.route.write("input", message)

    def fit(self, value):
        value = clipped(value, FIELD_CHARS)
        if value in self.saved:
            return self.saved[value]
        # Even a late round in a long tool loop retains a readable excerpt.
        return clipped(value, max(512, self.remaining))

    def keep(self, value):
        text = self.fit(value)
        if self.remaining and text not in self.saved:
            self.saved[text] = text
            self.remaining = max(0, self.remaining - len(text))
        return text

    def step(self, kind, label, started=None):
        step = TrialStep(self, kind, label, started)
        if len(self.steps) < MAX_STEPS:
            self.steps.append(step)
        else:
            self.omitted += 1
        return step

    @contextmanager
    def model_call(self, payload):
        self.model_calls += 1
        step = self.last_model = self.step("model", f"测试模型调用 {self.model_calls}")
        step.payload = payload
        try:
            yield step
        except (asyncio.CancelledError, GeneratorExit):
            step.finish("cancelled")
            raise
        except Exception as exc:
            code = getattr(exc, "status_code", None)
            error = type(exc).__name__
            if isinstance(code, int):
                error += f"（HTTP {code}）"
            step.finish("error", error)
            raise
        finally:
            step.finish("incomplete")

    def account(self, usage):
        if usage["total_tokens"] is not None:
            self.reported += 1
            self.tokens += usage["total_tokens"]

    def tool_start(self, name, args):
        self.step("tool", name).write("input", args)

    def tool_end(self, name, result):
        # AstrBot executes tools sequentially per event. End-hook arguments can
        # differ from the filtered arguments passed to the start hook.
        step = next(
            (
                s
                for s in reversed(self.steps)
                if s.kind == "tool" and s.label == name and s.end is None
            ),
            None,
        )
        if step:
            metadata = getattr(result, "meta", None)
            if isinstance(metadata, dict) and "usage" in metadata:
                step.usage = normalize_usage(metadata)
            status = "error" if getattr(result, "isError", False) else "done"
            step.finish(
                status if result is not None else "incomplete", tool_output(result)
            )

    def final_response(self, response):
        self.delivery = self.step("delivery", "框架回复与发送确认")
        self.delivery.write("input", response_text(response))
        if getattr(response, "role", "") == "err":
            self.delivery.finish("error")

    def delivered(self, text, *, by_tool=False):
        if by_tool:
            stage = self.step("receipt", "工具发送回执")
        elif self.delivery and self.delivery.end is None:
            stage = self.delivery
        elif self.has_receipt:
            return
        else:
            stage = self.step("receipt", "群消息发送回执")
        stage.finish(output=text)
        self.has_receipt = True

    def close(self, status=None):
        if self.end is not None:
            return
        self.end = time.monotonic()
        for step in self.steps:
            if step.end is None:
                step.finish("cancelled" if status == "cancelled" else "incomplete")
        if status:
            self.status = status
        elif self.last_model and self.last_model.status == "error":
            self.status = "test_failed"
        elif self.has_receipt:
            self.status = "delivered"
        else:
            self.status = "unconfirmed"

    def summary(self):
        return {
            "id": self.id,
            "model": self.model,
            "preview": self.preview,
            "started_at": self.created_at,
            "status": self.status,
            "elapsed_ms": round(((self.end or time.monotonic()) - self.start) * 1000),
            "calls": self.model_calls,
            "reported_calls": self.reported,
            "reported_tokens": self.tokens if self.reported else None,
        }

    def detail(self):
        return {
            **self.summary(),
            "steps": [s.view() for s in self.steps],
            "omitted": self.omitted,
        }


class TrialHistory:
    def __init__(self):
        self.turns = deque(maxlen=MAX_TURNS)

    def begin(self, source, model, message, started):
        turn = TrialTurn(source, model, message, started)
        previous = [t for t in self.turns if t.scope == turn.scope]
        if len(previous) >= PER_GROUP:
            self.turns.remove(previous[-1])
        self.turns.appendleft(turn)
        return turn

    def page(self, settings, known_groups, scope, turn_id):
        names = {
            (g["platform_id"], g["group_id"]): g["group_name"] for g in known_groups
        }
        groups = []
        for key, model in sorted(settings.active_trial_models().items()):
            platform, group = key.split(":", 1)
            groups.append(
                {
                    "scope": key,
                    "platform": platform,
                    "group": group,
                    "name": names.get((platform, group)) or "",
                    "model": model,
                    "label": BAILIAN_TRIAL_MODELS[model],
                }
            )
        selected = scope or (groups[0]["scope"] if groups else "")
        if not any(g["scope"] == selected for g in groups):
            selected = ""
        turns = [
            t
            for t in self.turns
            if t.scope == selected and t.persona == settings.persona_id
        ]
        turn = (
            next((t for t in turns if t.id == turn_id), None)
            if turn_id
            else next(iter(turns), None)
        )
        return {
            "groups": groups,
            "scope": selected,
            "turns": [t.summary() for t in turns],
            "detail": turn.detail() if turn else None,
        }
