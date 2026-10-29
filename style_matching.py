"""Local, conservative group-tag matching. No additional model calls."""

import re
import unicodedata


def parse_tag_entry(entry):
    """Native list format: [platform:]group=tag,tag (edited via group cards)."""
    if not isinstance(entry, str) or "=" not in entry:
        raise ValueError("群标签格式须为 平台连接:群号=标签,标签")
    key, raw = entry.split("=", 1)
    key = key.strip()
    if not re.fullmatch(r"(?:[^\s:=]{1,100}:)?[1-9]\d{3,19}", key):
        raise ValueError("群标签包含无效群号或连接")
    tags = tuple(
        dict.fromkeys(x.strip() for x in re.split(r"[,，、;；\n]", raw) if x.strip())
    )
    if not 1 <= len(tags) <= 12 or any(
        len(tag) > 24 or not tag.isprintable() or any(c in tag for c in "=<>{}")
        for tag in tags
    ):
        raise ValueError("每群需1～12个标签，每个最多24字，不含控制符或尖括号")
    return key, tags


def normalize(text):
    return unicodedata.normalize("NFKC", text).casefold().strip()


def mentions(text, term):
    text, term = normalize(text), normalize(term)
    if not term:
        return False
    if re.fullmatch(r"[a-z0-9 _+-]+", term):
        return (
            re.search(r"(?<![a-z0-9])" + re.escape(term) + r"(?![a-z0-9])", text)
            is not None
        )
    return term in text


def fits_group(style, tags, query):
    """Require an explicit positive tag hit; exclusions always take priority."""
    if not tags:
        return False
    positive = " ".join([style["meaning"], *style["contexts"]])
    avoid = style["avoid"]
    if any(mentions(rule, tag) for rule in avoid for tag in tags):
        return False
    if any(
        mentions(query, word)
        for rule in avoid
        for word in re.split(r"[、，,；;\s]+", rule)
        if len(word) >= 2
    ):
        return False
    # A negative usage description is not positive evidence of suitability.
    if any(word in positive for word in ("不适合", "不适用", "禁止用于", "避免用于")):
        return False
    return any(mentions(positive, tag) for tag in tags)


def fits_conversation(style, query):
    return style["expression"] in query or any(
        word in query or word in {"闲聊", "日常闲聊", "轻松聊天"}
        for context in style["contexts"]
        for word in re.split(r"[、，,；;\s]+", context)
        if len(word) >= 2
    )
