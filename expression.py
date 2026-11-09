"""Concrete, situational expression forms and local eligibility rules."""

import hashlib
import json
import re

from .retrieval import intent_of, normalized, overlap

INTENTS = {"casual", "acknowledge", "question", "tease", "closing", "serious", "any"}


def validate_form(value):
    if not isinstance(value, dict) or value.get("kind") not in (
        "phrase",
        "grammar",
        "response",
    ):
        raise ValueError("表达类型须为 phrase / grammar / response")
    output = {"kind": value["kind"]}
    for key, limit in (("situation", 200), ("pattern", 300), ("avoid", 200)):
        text = value.get(key, "")
        if (
            not isinstance(text, str)
            or len(text) > limit
            or (key != "avoid" and not text.strip())
        ):
            raise ValueError("表达须填写具体情境和说法，且不能过长")
        if re.search(
            r"系统提示|system\s*prompt|管理员权限|绕过权限|忽略.{0,8}规则|修改.{0,8}人格",
            text,
            re.I,
        ):
            raise ValueError("表达不能包含人格、权限或系统指令")
        output[key] = text.strip()
    intent = value.get("intent", "any")
    keywords = value.get("keywords", [])
    if (
        intent not in INTENTS
        or not isinstance(keywords, list)
        or len(keywords) > 8
        or any(
            not isinstance(x, str) or not 2 <= len(x.strip()) <= 24 for x in keywords
        )
    ):
        raise ValueError("表达意图或情境关键词无效")
    if intent == "any" and not keywords:
        raise ValueError("通用意图仍须有明确的情境关键词")
    output.update(
        intent=intent, keywords=list(dict.fromkeys(x.strip() for x in keywords))
    )
    return output


def signature(form):
    values = [
        form["kind"],
        form["intent"],
        *[
            re.sub(r"[\s，。！？,.!?]+", "", normalized(form[key]))
            for key in ("pattern", "situation")
        ],
    ]
    return hashlib.sha256(json.dumps(values, ensure_ascii=False).encode()).hexdigest()


def eligibility(row, query):
    """Return zero for unknown/mismatched situations. Cooldown is handled separately."""
    intent = intent_of(query)
    form_intent = row.get("intent", "casual")
    if intent == "serious":
        return 0  # Learned forms never decorate a professional or distressed turn.
    if form_intent not in ("any", intent):
        return 0
    for rule in row.get("avoid", []):
        if any(
            normalized(x) in normalized(query)
            for x in re.split(r"[、，,；;\s]+", rule)
            if len(x) >= 2
        ):
            return 0
    keywords = row.get("keywords", [])
    keyword_hit = any(normalized(x) in normalized(query) for x in keywords)
    if keywords and not keyword_hit:
        return 0
    if row.get("signature"):
        return 1 + int(keyword_hit) + overlap(query, row.get("situation", ""))
    # Old phrase records keep conservative local matching during migration.
    from .style_matching import fits_conversation

    return 1 if fits_conversation(row, query) else 0
