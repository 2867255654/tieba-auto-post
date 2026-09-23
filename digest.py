"""第二步：把多条资讯摘抄 / 浓缩成一篇贴吧风格摘要帖。

优先用 LLM（OpenAI 兼容接口，可接 DeepSeek 等国内兼容地址）润色；
未配置 API key 时走内置兜底摘要（依然带来源标注，合规）。
"""
import json
from datetime import datetime

import requests

from config import cfg

SYSTEM_PROMPT = """你是一个贴吧游戏资讯整理助手。请为「{forum}」吧写一篇"每日游戏资讯摘抄"主题帖。

要求：
1. 标题要有吸引力（贴吧风格，可带一点梗，但不过分标题党、不低俗、不造谣）。
2. 正文先写一句轻松的开场白，再用条目列出今日重点资讯（最多 {n} 条）。
3. 每条资讯包含：一句话要点 + 来源媒体名 + 原文链接（链接务必原样保留）。
4. 语言口语化、适合贴吧读者；只根据给定素材整理，不编造未提供的信息。
5. 输出严格使用如下格式（不要有多余解释）：
【标题】
<标题文字>
【正文】
<正文内容，使用换行分隔条目>"""


def _build_user_content(items: list) -> str:
    lines = []
    for i, it in enumerate(items, 1):
        summary = (it.get("summary") or "")[:200]
        lines.append(
            f"{i}. 标题：{it['title']}\n"
            f"   摘要：{summary}\n"
            f"   来源：{it['source']} | 链接：{it['link']}"
        )
    return "\n".join(lines)


def _split_title_body(text: str):
    title, body = "", text.strip()
    m = None
    import re

    m = re.search(r"【标题】\s*(.*?)\s*【正文】\s*(.*)", text, re.S)
    if m:
        title = m.group(1).strip()
        body = m.group(2).strip()
    return title or "每日游戏资讯摘抄", body


def _llm_digest(items: list, forum: str, n: int):
    url = cfg.llm_base_url.rstrip("/") + "/chat/completions"
    payload = {
        "model": cfg.llm_model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT.format(forum=forum, n=n)},
            {"role": "user", "content": _build_user_content(items)},
        ],
        "temperature": 0.7,
    }
    headers = {
        "Authorization": f"Bearer {cfg.llm_api_key}",
        "Content-Type": "application/json",
    }
    resp = requests.post(url, json=payload, headers=headers, timeout=60)
    resp.raise_for_status()
    content = resp.json()["choices"][0]["message"]["content"]
    return _split_title_body(content)


def _fallback_digest(items: list, forum: str, n: int):
    today = datetime.now().strftime("%Y-%m-%d")
    title = f"【每日游戏资讯摘抄】{today} 热点速览（共 {len(items)} 条）"
    lines = [
        f"兄弟们，{today} 的游戏圈热点给大家整理好了，挑了 {len(items)} 条值得看的，来源都标在末尾👇",
        "",
    ]
    for i, it in enumerate(items, 1):
        summary = (it.get("summary") or "").replace("\n", " ")
        lines.append(f"{i}. {it['title']}")
        if summary:
            lines.append(f"   {summary[:120]}")
        lines.append(f"   来源：{it['source']} | {it['link']}")
        lines.append("")
    lines.append("—— 以上为资讯整理，点击来源可看原文。理性讨论，友善开黑 🎮")
    return title, "\n".join(lines)


def generate_digest(items: list, forum_name: str | None = None, top_n: int | None = None):
    forum = forum_name or cfg.forum_name
    n = top_n or cfg.top_n
    if cfg.llm_api_key:
        try:
            return _llm_digest(items, forum, n)
        except Exception as e:  # noqa: BLE001
            print(f"[digest] LLM 调用失败，回退兜底摘要：{e}")
    return _fallback_digest(items, forum, n)
