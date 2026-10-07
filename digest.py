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

SYSTEM_PROMPT = """你是一位混迹抗压吧多年的老哥，负责把今天的游戏资讯整理成一篇贴吧帖。请为「{forum}」吧写一篇"每日游戏资讯摘抄"（最多 {n} 条）。

核心要求（重要程度从高到低）：
1. 【把事说清楚】每条先用一句通顺自然的中文讲明白"发生了什么"，这是第一位的。素材若是英文，翻成地道中文，不要逐字硬译——例如 fart trails 应译成"排气尾迹/特效"这类游戏圈说法，绝不能写成"放屁轨迹"；游戏名、主机名用通用译名。
2. 【玩梗要节制】梗只用在标题、开场白和结尾。正文条目以讲事实为主，每条最多带一个口语词；严禁每条都写"这波操作""家人们谁懂啊""咱就是说"，全篇不许重复同一个句式。
3. 【不要编造】只根据给定素材整理；可以调侃厂商/工作室，但不造谣、不人身攻击、不低俗。
4. 【不要输出网址】正文里禁止出现任何网址（http/https）。每条末尾只标来源媒体名，格式固定为：（来源：媒体名）
5. 【标题】要有贴吧爆款感，可用"典中典/寄了/回旋镖/破防/蚌埠住了"等梗，但别纯标题党。参考：悬念式"XX 这波，究竟谁破防了？"、盘点式"今日游戏圈 N 大瓜，最后一个绷不住"。
6. 【开头结尾】开场写一句老哥口吻的话（自己写，不要照抄示例）；结尾先来一句"今日锐评"，再来一句收尾梗。
7. 严格按下面格式输出，不要任何多余解释：
【标题】
<标题文字>
【正文】
<开场白：一句话，独占一行，严禁和条目 1 混在一起>

1. 事实描述（来源：媒体名）
2. 事实描述（来源：媒体名）
（每条独占一行，一句讲清"谁做了什么、结果如何"，不超过 60 字）

今日锐评：<一句话>
<收尾梗>"""


def _has_chinese(s: str) -> bool:
    return bool(re.search(r"[\u4e00-\u9fff]", s or ""))


def _clean_text(s: str) -> str:
    """彻底清洗文本：先剥 HTML 标签，再 HTML 实体多轮解码（防 &amp;quot; 双重编码），最后压缩空白。"""
    if not s:
        return ""
    prev, cur = None, s
    for _ in range(3):  # 最多 3 轮：覆盖 &amp;lt;br&amp;gt; -> <br> -> 剥掉 这种嵌套
        prev = cur
        cur = re.sub(r"<[^>]+>", "", cur)
        cur = html.unescape(cur)
        if cur == prev:
            break
    return re.sub(r"\s+", " ", cur).strip()


def _clean_summary(text: str, limit: int = 150) -> str:
    """清洗并安全截断摘要：解码 HTML 实体、压缩空白，按句子/词边界断，不半句截断。"""
    text = _clean_text(text)
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


def _strip_links(text: str) -> str:
    """去掉正文里的网址（贴吧版面塞长链接很乱）：先清「链接：URL」整块，再兜底清残留 URL。"""
    if not text:
        return ""
    text = re.sub(r"(?:[|｜]\s*)?(?:链接|原文链接|原文|来源链接)\s*[:：]?\s*<?https?://\S+>?", "", text)
    text = re.sub(r"https?://\S+", "", text)
    text = re.sub(r"[（(]\s*[）)]", "", text)      # 删链接后可能留下空括号
    text = re.sub(r"[ \t]+", " ", text)
    text = "\n".join(line.strip() for line in text.splitlines())
    return text


def _build_user_content(items: list) -> str:
    """给 LLM 的素材：只给标题/摘要/来源名，不提供链接（从源头避免模型把长 URL 写进正文）。"""
    lines = []
    for i, it in enumerate(items, 1):
        title = _clean_text(it.get("title") or "")
        summary = _clean_summary(it.get("summary") or "", limit=200)
        lines.append(
            f"{i}. 标题：{title}\n"
            f"   摘要：{summary}\n"
            f"   来源：{it.get('source', '')}"
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
    resp = None
    try:
        print(f"[digest] LLM 请求：{url}  model={cfg.llm_model}")
        resp = requests.post(url, json=payload, headers=headers, timeout=60)
        if resp.status_code != 200:
            print(f"[digest] LLM 响应异常 status={resp.status_code} body={resp.text[:400]!r}")
        resp.raise_for_status()
        content = resp.json()["choices"][0]["message"]["content"]
        title, body = _split_title_body(content)
        return title, _strip_links(body)
    except Exception as e:  # noqa: BLE001
        # 把响应状态码/正文带出来，便于区分：401/403=权限、404=模型名或端点、429=限流
        status = getattr(resp, "status_code", "?") if resp is not None else "?"
        body = ""
        if resp is not None:
            try:
                body = f" body={resp.text[:400]!r}"
            except Exception:
                pass
        print(f"[digest] LLM 调用失败（status={status}{body}）：{type(e).__name__}: {e}")
        raise


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
        "—— 瓜吃完了，理性讨论别急眼，友善开黑不互喷 🎮",
        "—— 今日瓜暂且这些，觉得乐的扣个 1，理性开麦不互喷 🎮",
        "—— 盘完了，有想法的评论区开麦，友善交流不引战 🎮",
    ]

    opener = random.choice(openers)
    lines = [opener, ""]
    # 条目前缀避开开场白里已用过的词，避免同一帖里"家人们谁懂啊"重复出现
    lead_pool = [l for l in leads if l not in opener] or leads
    for i, it in enumerate(chosen, 1):
        title = _clean_text(it.get("title") or "")
        summary = _clean_summary(it.get("summary") or "")
        lead = random.choice(lead_pool)
        lines.append(f"{i}. {lead}，{title}")
        if summary:
            lines.append(f"   {summary}")
        lines.append(f"   （来源：{it['source']}）")
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
