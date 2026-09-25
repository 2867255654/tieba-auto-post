"""第一步：采集热门游戏资讯。

数据源来自 sources.yaml：
  - type=rss    直接 RSS 地址
  - type=rsshub 由 RSSHUB_BASE + route 拼出（需可用实例）

流程：抓取 → 按时间窗口过滤 → 去重 → 按「新鲜度 + 游戏相关度」打分排序 → 取 Top N。
"""
import concurrent.futures
import difflib
import re
from datetime import datetime, timedelta, timezone

import feedparser
import requests

from config import cfg

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}

# 游戏相关度关键词（命中加分）
GAME_KEYWORDS = [
    "游戏", "手游", "端游", "主机", "Steam", "PS5", "PS4", "Xbox", "Switch",
    "任天堂", "英雄联盟", "LOL", "原神", "王者荣耀", "永劫无间", "蛋仔派对",
    "二次元", "崩坏", "米哈游", "腾讯游戏", "网易游戏", "版号", "发售", "上线",
    "公测", "更新", "补丁", "DLC", "联动", "电竞", "战队", "赛事", "暴雪",
    "Riot", "育碧", "卡普空", "索尼", "微软", "虚幻", "Unity", "独立游戏",
    "主播", "开服", "停运", "买断", "内购", "抽卡", "皮肤", "赛季",
]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _clean(html: str) -> str:
    return re.sub(r"<[^>]+>", "", html or "").strip()


def _parse_date(entry: dict) -> datetime:
    for key in ("published_parsed", "updated_parsed"):
        val = entry.get(key)
        if val:
            try:
                return datetime(*val[:6], tzinfo=timezone.utc)
            except Exception:
                pass
    return _now()


def _download_parse(url: str, timeout: int):
    """用 requests 带超时下载，再交给 feedparser 解析。
    这样能真正控制网络超时（feedparser.parse(url) 内部无超时，会卡死）。"""
    resp = requests.get(url, headers=HEADERS, timeout=timeout)
    resp.encoding = resp.apparent_encoding or "utf-8"
    return feedparser.parse(resp.content)


def _fetch_rss(url: str, source_name: str, game_source: bool = False, timeout: int = 12) -> list:
    items = []
    try:
        # 单源超时隔离：避免某个源卡住拖垮整体
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
            future = ex.submit(_download_parse, url, timeout)
            data = future.result(timeout=timeout + 3)
    except Exception as e:  # noqa: BLE001
        print(f"[collector] RSS 超时/失败 {url}: {e}")
        return items
    if data.bozo and not data.entries:
        print(f"[collector] RSS 可能不可用 {url}: {getattr(data, 'bozo_exception', '')}")
    for e in data.entries:
        title = (e.get("title") or "").strip()
        link = e.get("link", "")
        summary = _clean(e.get("summary", e.get("description", "")))
        published = _parse_date(e)
        if not title or not link:
            continue
        items.append(
            {
                "title": title,
                "link": link,
                "summary": summary,
                "published": published,
                "source": source_name,
                "game_source": game_source,
            }
        )
    return items


def _is_dup(title: str, others: list, threshold: float = 0.82) -> bool:
    for o in others:
        if difflib.SequenceMatcher(None, title, o).ratio() > threshold:
            return True
    return False


def _dedupe(items: list) -> list:
    seen_titles, out = [], []
    for it in items:
        if _is_dup(it["title"], seen_titles):
            continue
        seen_titles.append(it["title"])
        out.append(it)
    return out


def _score(it: dict) -> float:
    age_h = (_now() - it["published"]).total_seconds() / 3600
    s = max(0.0, float(cfg.max_age_hours) - age_h)  # 新鲜度
    # 游戏垂直媒体源（无论中英文）条目即游戏相关，给基础分，避免被语言过滤
    if it.get("game_source"):
        s += 6.0
    text = it["title"] + it["summary"]
    for kw in GAME_KEYWORDS:
        if kw.lower() in text.lower():
            s += 3.0
    return s


def _fetch_rsshub_chain(urls: list, name: str, game_source: bool) -> list:
    """依次尝试每个 RSSHub 实例，返回首个有结果的条目（并行采集时源级别串行 base）。"""
    for url in urls:
        items = _fetch_rss(url, name, game_source=game_source)
        if items:
            return items
    print(f"[collector] 所有 RSSHub 实例均不可用，跳过路由 {name}")
    return []


def collect() -> list:
    # 把一个 source 展开成一组 (name, game_source, urls) 任务；
    # rsshub 源会展开成多个 base 候选（base 之间仍串行尝试，但不同源之间并行）。
    jobs = []
    for src in cfg.sources:
        stype = src.get("type", "rss")
        name = src.get("name", "unknown")
        game_source = bool(src.get("game_source", False))
        if stype == "rss":
            url = src.get("url", "")
            if url:
                jobs.append((name, game_source, "rss", [url]))
        elif stype == "rsshub":
            route = src.get("route", "")
            if route and cfg.rsshub_bases:
                urls = [base + route for base in cfg.rsshub_bases]
                jobs.append((name, game_source, "rsshub", urls))
        # 其他类型忽略

    raw = []
    if not jobs:
        return raw

    # 并行采集：整体耗时取决于最慢的单个源，而不是所有源串行叠加
    workers = min(20, len(jobs))
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
        futures = []
        for name, game_source, stype, urls in jobs:
            if stype == "rsshub":
                futures.append(ex.submit(_fetch_rsshub_chain, urls, name, game_source))
            else:
                futures.append(ex.submit(_fetch_rss, urls[0], name, game_source))
        for f in concurrent.futures.as_completed(futures):
            try:
                items = f.result()
                if items:
                    raw += items
            except Exception as e:  # noqa: BLE001
                print(f"[collector] 源任务异常：{e}")

    cutoff = _now() - timedelta(hours=cfg.max_age_hours)
    recent = [it for it in raw if it["published"] >= cutoff]
    deduped = _dedupe(recent)
    for it in deduped:
        it["score"] = _score(it)
    deduped.sort(key=lambda x: x["score"], reverse=True)
    # 跨源均衡：单源最多取 max(2, top_n//2) 条，避免一个大源霸屏
    max_per = max(2, cfg.top_n // 2)
    per_count: dict = {}
    balanced = []
    for it in deduped:
        src = it["source"]
        if per_count.get(src, 0) >= max_per:
            continue
        per_count[src] = per_count.get(src, 0) + 1
        balanced.append(it)
        if len(balanced) >= cfg.top_n:
            break
    return balanced


# 离线演示样本（无网络 / RSS 不可用时可用 --demo 跑通全流程）
SAMPLE_ITEMS = [
    {
        "title": "《英雄联盟》14.19 版本更新：打野装备大改，多个热门英雄削弱",
        "link": "https://example.com/lol-1419",
        "summary": "本次版本对打野刀与野区经济做出调整，盲僧、赵信等前期打野遭到削弱，中路法师小幅增强。",
        "published": _now(),
        "source": "演示·游民星空",
    },
    {
        "title": "米哈游新作曝光：开放世界射击游戏《代号：雷索纳斯》开启测试",
        "link": "https://example.com/rezones",
        "summary": "官方放出首支实机演示，强调阵容搭配与卡牌机制结合的玩法，预约量已破百万。",
        "published": _now(),
        "source": "演示·3DM",
    },
    {
        "title": "Steam 秋季特卖明日开启，数千款游戏参与折扣",
        "link": "https://example.com/steam-autumn",
        "summary": " Valve 宣布秋季特卖将于本周五凌晨开始，多款 3A 大作迎来年内最低价。",
        "published": _now(),
        "source": "演示·机核",
    },
]
