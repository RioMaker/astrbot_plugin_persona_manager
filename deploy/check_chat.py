"""Measure a short Chinese reply from the local llama.cpp service."""

import argparse
import json
import sys
from http.client import HTTPException
from pathlib import Path
from time import perf_counter
from urllib.request import ProxyHandler, Request, build_opener


def check_chat(port: int, system_prompt: str) -> None:
    body = {
        "model": "persona-chat",
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": "昨天说那首曲子总在最后断连，今天终于全连了！"},
        ],
        "stream": True,
        "stream_options": {"include_usage": True},
        "max_tokens": 96,
    }
    request = Request(
        f"http://127.0.0.1:{port}/v1/chat/completions",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    local_http = build_opener(ProxyHandler({}))
    started = perf_counter()
    first_text = None
    pieces = []
    usage = {}
    timings = {}
    with local_http.open(request, timeout=180) as stream:
        for line in stream:
            if not line.startswith(b"data:"):
                continue
            data = line[5:].strip()
            if data == b"[DONE]":
                break
            event = json.loads(data)
            if "error" in event:
                raise ValueError(event["error"])
            for choice in event.get("choices", []):
                content = choice.get("delta", {}).get("content")
                if content:
                    if first_text is None and content.strip():
                        first_text = perf_counter()
                    pieces.append(content)
            usage.update(event.get("usage") or {})
            timings.update(event.get("timings") or {})
        else:
            raise ValueError("服务在流式回复完成前断开")
    if first_text is None:
        raise ValueError("服务未返回可见回复")
    elapsed = perf_counter() - started
    print("".join(pieces).strip())
    print(f"首段正文 {first_text - started:.2f} 秒，整条回复 {elapsed:.2f} 秒")
    if usage:
        print(
            f"输入 {usage['prompt_tokens']} tokens，输出 {usage['completion_tokens']} tokens"
        )
    if timings.get("predicted_per_second") is not None:
        print(f"服务端生成速度 {timings['predicted_per_second']:.2f} tokens/s")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=18080)
    parser.add_argument(
        "--system-file", type=Path, help="用实际人格提示词测量首段正文延迟"
    )
    args = parser.parse_args()
    try:
        system_prompt = (
            args.system_file.read_text(encoding="utf-8")
            if args.system_file
            else "你是群里的可可子，用一两句自然的中文接话。"
        )
        check_chat(args.port, system_prompt)
    except (OSError, ValueError, HTTPException) as exc:
        sys.exit(f"本地推理检查失败，{exc}")
