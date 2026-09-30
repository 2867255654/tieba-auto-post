"""第二步：把多条资讯摘抄 / 浓缩成一篇贴吧风格摘要帖。

优先用 LLM（OpenAI 兼容接口，可接 DeepSeek 等国内兼容地址）润色；
未配置 API key 时走内置兜底摘要（依然带来源标注，合规）。
"""
import html
import json
import random
import re
from datetime import datetime

import requests

from config import cfg

SYSTEM_PROMPT = """你是一位混迹抗压吧多年的老哥，最擅长用贴吧梗式口吻吐槽、盘游戏圈那点事。请为「{forum}」吧写一篇"每日游戏资讯摘抄"主题帖（最多 {n} 条）。

文风要求（抗压吧梗式）：
1. 整体语气：调侃、玩梗、带点阴阳怪气，但绝不造谣、不人身攻击、不低俗；把厂商/工作室当"乐子"盘，而不是干巴巴播新闻。
2. 标题要像贴吧爆款，可用抗压吧常用梗（典中典/寄了/回旋镖/急了/蚌埠住了/破防等），抓眼球但别纯标题党。可任选一类公式：
   - 悬念式："XX 这波操作，究竟谁破防了？"
   - 盘点式："今日游戏圈 N 大瓜，最后一个绷不住"
   - 梗句式："典中典，XX 又整新活了"
3. 开场白用老哥口吻（例："老哥们坐好，今天游戏圈又整了一堆活，义务给大家盘一盘："），正文穿插"这波啊""家人们谁懂啊""咱就是说""建议直接入土"等口语梗，别全程一个调。
4. 每条资讯先用一句梗式点评引出要点（可以阴阳怪气，但事实依据必须来自素材，不能瞎编），再给出来源媒体名 + 原文链接（链接务必原样保留，别漏）。
5. 英文素材必须翻成通顺中文的梗式表达，不要大段保留英文原文。
6. 只根据给定素材整理，不编造；结尾先用一句"今日锐评"收个尾（例："今日最佳乐子已送达，剩下的交给评论区。"），再来一句收尾梗（例："瓜吃完了，理性讨论别急眼，友善开黑不互喷 🎮"）。
7. 输出严格使用如下格式（不要有多余解释）：
【标题】
<标题文字>
【正文】
<正文内容，使用换行分隔条目>"""


def _has_chinese(s: str) -> bool:
    return bool(re.search(r"[\u4e00-\u9fff]", s or ""))


def _clean_summary(text: str, limit: int = 150) -> str:
    """清洗并安全截断摘要：解码 HTML 实体、压缩空白，按句子/词边界断，不半句截断。"""
    if not text:
        return ""
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= limit:
        return text
    cut = text[:limit]
    # 优先在中文/英文句末标点处断句
    m = re.search(r"^(.*[。！？!?])", cut)
    if m:
        return m.group(1).strip()
    # 英文按空格断词
    sp = cut.rfind(" ")
    if sp > limit * 0.5:
        return cut[:sp].strip() + "…"
    return cut.strip() + "…"


def _build_user_content(items: list) -> str:
    lines = []
    for i, it in enumerate(items, 1):
        summary = _clean_summary(it.get("summary") or "", limit=200)
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
    # 中文优先：只保留含中文的条目（针对中文贴吧；纯英文条目对读者像"乱码"）。
    # 仅当当天一条中文都没有时，才兜底用原文，避免空帖。
    # 想保留/翻译英文，请配置 LLM_API_KEY（提示词已要求把英文翻译成中文）。
    zh = [it for it in items if _has_chinese(it.get("title", "")) or _has_chinese(it.get("summary", ""))]
    chosen = zh[:n] if zh else items[:n]
    count = len(chosen)

    # 抗压吧梗式：标题 / 开场 / 收尾都从池子里随机抽，避免每天一个模板的机械感。
    title_pool = [
        f"【每日游戏圈吃瓜】{today} 一锅炖了（共 {count} 条）",
        f"游戏圈今日大事件 {today}：{count} 个瓜，老哥们接好",
        f"{today} 游戏圈速报｜{count} 条新鲜瓜，瓜保熟",
        f"抗压日报 {today}｜{count} 个乐子，建议直接下饭",
        f"今日游戏圈：{count} 条瓜已打包，{today} 准时投喂",
    ]
    openers = [
        f"老哥们坐好，{today} 的游戏圈又整了一堆活，本贴吧义务劳动给大家盘一盘，瓜保熟，来源全在底下👇",
        f"家人们谁懂啊，{today} 的游戏圈又整新活了，挑了 {count} 个瓜给大伙盘盘👇",
        f"坐稳了老哥，{today} 的游戏圈这几件事儿值得一瞅，来源都标底下了👇",
        f"又到饭点，{today} 游戏圈 {count} 个瓜端上来了，慢慢品👇",
    ]
    # 条目前缀：均为中性"老哥点评"，不替事实下结论，只负责带梗的节奏感。
    leads = [
        "来，给大伙盘一盘", "划重点", "家人们谁懂啊", "速来围观",
        "这波真有点东西", "老哥们细品", "这事儿给大伙捋捋",
        "重点来了", "吃瓜预警", "速览",
    ]
    closers = [
        "—— 瓜吃完了，理性讨论别急眼，友善开黑不互喷 🎮（来源点开可看原文）",
        "—— 今日瓜暂且这些，觉得乐的扣个 1，理性开麦不互喷 🎮（来源点开看原文）",
        "—— 盘完了，有想法的评论区开麦，友善交流不引战 🎮（来源在每条底下）",
    ]

    lines = [random.choice(openers), ""]
    for i, it in enumerate(chosen, 1):
        summary = _clean_summary(it.get("summary") or "")
        lead = random.choice(leads)
        lines.append(f"{i}. {lead}，{it['title']}")
        if summary:
            lines.append(f"   {summary}")
        lines.append(f"   来源：{it['source']} | {it['link']}")
        lines.append("")
    lines.append(random.choice(closers))
    return random.choice(title_pool), "\n".join(lines)


def generate_digest(items: list, forum_name: str | None = None, top_n: int | None = None):
    forum = forum_name or cfg.forum_names[0]
    n = top_n or cfg.top_n
    if cfg.llm_api_key:
        try:
            return _llm_digest(items, forum, n)
        except Exception as e:  # noqa: BLE001
            print(f"[digest] LLM 调用失败，回退兜底摘要：{e}")
    return _fallback_digest(items, forum, n)
