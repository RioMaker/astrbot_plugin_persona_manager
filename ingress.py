"""Read trusted event metadata without changing routing or any other plugin state."""

import hashlib
import json
import math
import re
import time
from typing import Any

from .settings import Settings

SECRET = re.compile(
    r"(?:sk-[A-Za-z0-9_-]{8,}|(?:api[_ -]?key|token|密码|密钥)\s*[:：=]\s*\S+)",
    re.I,
)


def own_text(event: Any) -> str:
    chain = getattr(getattr(event, "message_obj", None), "message", None)
    if chain is None:
        return ""  # No fallback to outlines containing quoted/forwarded contents.
    return "".join(
        str(part.text) for part in chain if type(part).__name__ == "Plain"
    ).strip()


def group_identity(event: Any, settings: Settings) -> dict[str, str] | None:
    if not settings.enabled or not settings.persona_id or event.is_stopped():
        return None
    if event.is_private_chat() or event.get_platform_name() != "aiocqhttp":
        return None
    group_id = str(event.get_group_id() or "")
    user_id = str(event.get_sender_id() or "")
    bot_id = str(event.get_self_id() or "")
    platform_id = str(event.get_platform_id() or "")
    if not all(re.fullmatch(r"[1-9]\d{3,19}", x) for x in (group_id, user_id, bot_id)):
        return None
    if not platform_id or user_id == bot_id or user_id in settings.ignored_qq:
        return None
    sender = getattr(getattr(event, "message_obj", None), "sender", None)
    if (
        getattr(sender, "is_bot", False) is True
        or getattr(sender, "bot", False) is True
    ):
        return None
    return {
        "persona_id": settings.persona_id,
        "platform_id": platform_id,
        "bot_id": bot_id,
        "group_id": group_id,
        "user_id": user_id,
        "scope_key": json.dumps(
            [settings.persona_id, platform_id, bot_id, group_id],
            ensure_ascii=False,
            separators=(",", ":"),
        ),
    }


def sample(event: Any, settings: Settings, purpose: str) -> dict[str, Any] | None:
    identity = group_identity(event, settings)
    if identity is None:
        return None
    text = own_text(event)
    if not text or text.startswith(("/", "!", "！")) or SECRET.search(text):
        return None
    if event.get_extra("handlers_parsed_params", {}):
        return None
    obj = event.message_obj
    message_id = str(getattr(obj, "message_id", "") or "")
    try:
        stamp = float(getattr(obj, "timestamp", 0))
    except (ValueError, TypeError, OverflowError):
        return None
    # Require the source timestamp; do not reinterpret old messages as today.
    if (
        not message_id
        or not math.isfinite(stamp)
        or stamp <= 0
        or stamp > time.time() + 300
    ):
        return None
    identity.update(
        id=hashlib.sha256(
            json.dumps([identity["scope_key"], message_id], ensure_ascii=False).encode()
        ).hexdigest(),
        text=text[: settings.max_source_chars],
        sent_at=stamp,
        purpose=purpose,
        message_id=message_id,
        umo=str(event.unified_msg_origin),
    )
    return identity
