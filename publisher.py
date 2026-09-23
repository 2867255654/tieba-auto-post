"""第三步：发帖到贴吧。

安全设计：
  - 默认草稿模式（POST_MODE != 1 或 未填 BDUSS）：只把帖子写到 output/draft_*.txt，绝不外发。
  - 仅当 POST_MODE=1 且 配置了 BDUSS 才真正发帖。

实现用原生 requests：自己取 tbs（CSRF 令牌）与 fid（吧 ID），再 POST 发新帖接口。
比依赖第三方库更透明、更好排查；接口字段若随贴吧改版变动，按浏览器抓包对照调整即可。
"""
import random
import re
import time
from pathlib import Path

import requests

from config import cfg, OUTPUT_DIR

TIEBA = "https://tieba.baidu.com"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}


def get_tbs(session: requests.Session, bduss: str) -> str:
    r = session.get(
        f"{TIEBA}/dc/common/tbs",
        headers=HEADERS,
        cookies={"BDUSS": bduss},
        timeout=15,
    )
    return r.json().get("tbs", "")


def get_fid(session: requests.Session, bduss: str, forum_name: str):
    url = f"{TIEBA}/f?kw={requests.utils.quote(forum_name)}"
    r = session.get(url, headers=HEADERS, cookies={"BDUSS": bduss}, timeout=15)
    html = r.text
    for pat in (r'"forum_id"\s*:\s*(\d+)', r'data-fid="(\d+)"', r'[?&]fid=(\d+)'):
        m = re.search(pat, html)
        if m:
            return int(m.group(1))
    return None


def post_thread(
    session: requests.Session,
    bduss: str,
    fid: int,
    forum_name: str,
    title: str,
    content: str,
    tbs: str,
) -> dict:
    data = {
        "ie": "utf-8",
        "fid": fid,
        "kw": forum_name,
        "is_video": "false",
        "src": "1",
        "title": title,
        "content": content,
        "tbs": tbs,
        "vericode": "",  # 正常无验证码时留空；触发验证码需人工处理
        "vote_info": "",
        "post_source": "1",
        "__type__": "thread",
    }
    r = session.post(
        f"{TIEBA}/f/commit/thread/add",
        data=data,
        headers={**HEADERS, "Referer": f"{TIEBA}/f?kw={requests.utils.quote(forum_name)}"},
        cookies={"BDUSS": bduss},
        timeout=20,
    )
    try:
        return r.json()
    except Exception:  # noqa: BLE001
        return {"raw": r.text[:500]}


def publish(title: str, content: str, forum_name: str | None = None, dry_run: bool | None = None):
    forum = forum_name or cfg.forum_names[0]
    should_post = (cfg.post_mode if dry_run is None else (not dry_run)) and bool(cfg.bduss)

    if not should_post:
        ts = time.strftime("%Y%m%d_%H%M%S")
        safe = re.sub(r'[\\/:*?"<>|]', "_", forum)
        path = OUTPUT_DIR / f"draft_{safe}_{ts}.txt"
        path.write_text(
            f"吧名：{forum}\n标题：{title}\n\n{content}",
            encoding="utf-8",
        )
        print(f"[publisher] 草稿模式（未发帖），已保存：{path}")
        print(f"[publisher] 标题：{title}")
        print(f"[publisher] 正文预览：\n{content[:600]}")
        return {"dry_run": True, "path": str(path)}

    # 真正发帖
    session = requests.Session()
    tbs = get_tbs(session, cfg.bduss)
    fid = get_fid(session, cfg.bduss, forum)
    if not fid:
        raise RuntimeError("无法获取 fid，请检查论坛名或登录态（BDUSS）")
    if not tbs:
        raise RuntimeError("无法获取 tbs（CSRF 令牌），请检查 BDUSS 是否有效")
    time.sleep(random.uniform(1, max(1.0, cfg.delay_seconds)))
    res = post_thread(session, cfg.bduss, fid, forum, title, content, tbs)
    print(f"[publisher] 发帖响应：{res}")
    return res
