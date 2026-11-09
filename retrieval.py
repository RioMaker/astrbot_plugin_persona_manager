"""Local lexical retrieval and conservative conversation intent, without models."""

import re
import unicodedata


def normalized(value):
    return unicodedata.normalize("NFKC", str(value)).casefold().strip()


def terms(value):
    result = set()
    for part in re.findall(r"[a-z0-9_]+|[\u4e00-\u9fff]+", normalized(value)):
        if re.fullmatch(r"[a-z0-9_]+", part):
            if len(part) >= 2:
                result.add(part)
        else:
            result.update(part[i : i + 2] for i in range(len(part) - 1))
    return result - {
        "之前",
        "上次",
        "记得",
        "什么",
        "一下",
        "那个",
        "这个",
        "我的",
        "我是",
    }


def overlap(query, value):
    needles, hay = terms(query), terms(value)
    return len(needles & hay) / max(1, len(needles))


def intent_of(text):
    text = normalized(text)
    if re.search(r"求助|难受|解卦|排盘|报错|成绩|分析|怎么练|认真|别开玩笑", text):
        return "serious"
    if re.search(r"晚安|再见|先走了|不聊了|拜拜", text):
        return "closing"
    if re.fullmatch(r"(?:嗯+|哦+|好[的吧]?|行|收到|知道了|谢谢)[。!！~～\s]*", text):
        return "acknowledge"
    if re.search(r"怎么|为什么|如何|[?？]|能不能|帮我|请问", text):
        return "question"
    if re.search(r"哈哈|笑死|逗你|开玩笑|调侃|吹牛", text):
        return "tease"
    return "casual"


def recall_requested(text):
    return bool(re.search(r"上次|之前|昨天|记得|后来|进展|去年|以前", text))
