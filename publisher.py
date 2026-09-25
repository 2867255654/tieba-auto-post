"""第三步：发帖到贴吧。

安全设计：
  - 默认草稿模式（POST_MODE != 1 或 未填 BDUSS）：只把帖子写到 output/draft_*.txt，绝不外发。
  - 仅当 POST_MODE=1 且 配置了 BDUSS 才真正发帖。

实现用原生 requests：自己取 tbs（CSRF 令牌）与 fid（吧 ID），再 POST 发新帖接口。
比依赖第三方库更透明、更好排查；接口字段若随贴吧改版变动，按浏览器抓包对照调整即可。
"""
import json
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
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "sec-ch-ua": '"Not-A.Brand";v="99", "Chromium";v="124", "Google Chrome";v="124"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
    "X-Requested-With": "XMLHttpRequest",
}


def _cookies() -> dict:
    """组装贴吧所需 Cookie。不同账号/接口可能需要 BDUSS + BDUSS_BFESS + STOKEN + BAIDUID 组合。

    BDUSS_BFESS 在多数账号下与 BDUSS 值相同；若用户未填，自动复用 BDUSS。
    """
    cookies = {}
    bduss = cfg.bduss
    bduss_bfess = cfg.bduss_bfess or bduss  # 未填时复用 BDUSS
    if bduss:
        cookies["BDUSS"] = bduss
    if bduss_bfess:
        cookies["BDUSS_BFESS"] = bduss_bfess
    if cfg.stoken:
        cookies["STOKEN"] = cfg.stoken
    baiduid = cfg.baiduid or cfg.baiduid_bfess
    if baiduid:
        cookies["BAIDUID"] = baiduid
    return cookies


def _format_content(content: str) -> str:
    """把普通正文转成贴吧富文本格式 [[0,1,"段落"], ...]。

    贴吧发帖接口要求 content 字段是这种 JSON 数组字符串；直接传纯文本会被服务器拒绝。
    """
    paragraphs = [p.strip() for p in content.split("\n") if p.strip()]
    if not paragraphs:
        paragraphs = [content]
    arr = [[0, 1, p] for p in paragraphs]
    return json.dumps(arr, ensure_ascii=False, separators=(",", ":"))


def _headers(extra: dict | None = None) -> dict:
    h = dict(HEADERS)
    if extra:
        h.update(extra)
    return h


def _create_session() -> requests.Session:
    """创建 Session 并先访问贴吧首页预热，让百度完成风控校验、设置必要 Cookie。"""
    session = requests.Session()
    session.trust_env = False
    try:
        home_headers = _headers({
            "Referer": "https://www.baidu.com/",
            "Sec-Fetch-Site": "cross-site",
            "Sec-Fetch-Dest": "document",
        })
        r = session.get(f"{TIEBA}/", headers=home_headers, cookies=_cookies(), timeout=15)
        print(f"[publisher] home page status: {r.status_code}, url: {r.url}")
        if r.history:
            for i, h in enumerate(r.history, 1):
                print(f"[publisher] home redirect {i}: {h.status_code} -> {h.headers.get('Location', h.url)}")
    except Exception as e:  # noqa: BLE001
        print(f"[publisher] home page warn: {type(e).__name__}: {e}")
    return session


def get_tbs(session: requests.Session) -> str:
    r = session.get(
        f"{TIEBA}/dc/common/tbs",
        headers=_headers({"Referer": f"{TIEBA}/"}),
        cookies=_cookies(),
        timeout=15,
    )
    print(f"[publisher] tbs status: {r.status_code}, is_login: {r.json().get('is_login')}")
    return r.json().get("tbs", "")


def _extract_fid(html: str) -> int | None:
    patterns = (
        r'"forum_id"\s*:\s*(\d+)',
        r'data-fid="(\d+)"',
        r'[?&]fid=(\d+)',
        r'"id":(\d+),"name":"[^"]*"',
        r'"forum"\s*:\s*\{[^}]*"id"\s*:\s*(\d+)',
        r'window\.__forum__\s*=\s*\{[^}]*"id"\s*:\s*(\d+)',
        r'PageData\.forum\s*=\s*\{[^}]*"id"\s*:\s*(\d+)',
        r'"forum_id"\s*:\s*"?(\d+)"?',
    )
    for pat in patterns:
        m = re.search(pat, html)
        if m:
            return int(m.group(1))
    return None


def get_fid(session: requests.Session, forum_name: str):
    encoded = requests.utils.quote(forum_name)
    base_url = f"{TIEBA}/f?kw={encoded}"
    referer = f"{TIEBA}/"

    # 方案 A：吧首页 HTML（多种参数与入口，防 CDN/风控返回通用页）
    urls_to_try = [
        f"{TIEBA}/f?kw={encoded}&fr=home",
        f"{TIEBA}/f?kw={encoded}&ie=utf-8&fr=home",
        f"{TIEBA}/f?kw={encoded}&ie=utf-8",
        base_url,
        f"{TIEBA}/f?kw={encoded}&ie=utf-8&pn=0",
        f"{TIEBA}/f?kw={encoded}&fr=search",
    ]
    for url in urls_to_try:
        r = session.get(
            url,
            headers=_headers({"Referer": referer}),
            cookies=_cookies(),
            timeout=15,
        )
        print(f"[publisher] get_fid try URL: {r.url}")
        print(f"[publisher] get_fid status: {r.status_code}")
        if r.history:
            for i, h in enumerate(r.history, 1):
                print(f"[publisher] get_fid redirect {i}: {h.status_code} -> {h.headers.get('Location', h.url)}")
        fid = _extract_fid(r.text)
        if fid:
            print(f"[publisher] get_fid OK: {fid}")
            return fid

    # 调试：最后一次返回内容前 600 字符
    snippet = r.text[:600].replace("\n", " ")
    print(f"[publisher] get_fid HTML snippet: {snippet}")

    # 方案 B：用贴吧分享接口查 fname -> fid
    try:
        api_url = f"{TIEBA}/f/commit/share/fname?fname={encoded}&ie=utf-8"
        r2 = session.get(
            api_url,
            headers=_headers({"Referer": referer, "X-Requested-With": "XMLHttpRequest"}),
            cookies=_cookies(),
            timeout=15,
        )
        print(f"[publisher] get_fid API status: {r2.status_code}")
        print(f"[publisher] get_fid API raw: {r2.text[:300]}")
        raw = r2.text.strip()
        if raw.startswith("(") and raw.endswith(")"):
            raw = raw[1:-1]
        m = re.search(r'\{.*\}', raw)
        if m:
            data = json.loads(m.group(0))
        else:
            data = json.loads(raw)
        print(f"[publisher] get_fid API response: {data}")
        fid = data.get("data", {}).get("fid") or data.get("fid") or data.get("no") or data.get("forum_id")
        if fid:
            return int(fid)
    except Exception as e:  # noqa: BLE001
        print(f"[publisher] get_fid API failed: {type(e).__name__}: {e}")

    # 方案 C：移动端接口，通常对 Cookie 要求更松；必须用干净移动端头才返回真实吧页
    try:
        mo_url = f"{TIEBA}/mo/q/fid?kw={encoded}"
        # 不要用 PC 的 _headers()，否则 Sec-Fetch-* / sec-ch-ua 与移动端 UA 冲突，会返回通用页
        mo_headers = {
            "User-Agent": (
                "Mozilla/5.0 (Linux; Android 10; SM-G981B) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Mobile Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Referer": f"{TIEBA}/",
        }
        r3 = session.get(
            mo_url,
            headers=mo_headers,
            cookies=_cookies(),
            timeout=15,
        )
        print(f"[publisher] get_fid mo status: {r3.status_code}")
        print(f"[publisher] get_fid mo url: {r3.url}")
        print(f"[publisher] get_fid mo title: {re.search(r'<title>([^<]+)</title>', r3.text, re.I).group(1) if re.search(r'<title>([^<]+)</title>', r3.text, re.I) else 'N/A'}")
        print(f"[publisher] get_fid mo raw: {r3.text[:500]}")
        raw = r3.text.strip()
        # 移动端页面常见字段："forum_id":707597 或 "fid":707597
        m = re.search(r'"forum_id"\s*:\s*(\d+)', raw)
        if m:
            print(f"[publisher] get_fid mo OK: {m.group(1)}")
            return int(m.group(1))
        m = re.search(r'"fid"\s*:\s*(\d+)', raw)
        if m:
            print(f"[publisher] get_fid mo OK: {m.group(1)}")
            return int(m.group(1))
    except Exception as e:  # noqa: BLE001
        print(f"[publisher] get_fid mo failed: {type(e).__name__}: {e}")

    # 方案 D：关注/签到接口
    try:
        like_url = f"{TIEBA}/f/like/furank?kw={encoded}&ie=utf-8"
        r4 = session.get(
            like_url,
            headers=_headers({"Referer": referer}),
            cookies=_cookies(),
            timeout=15,
        )
        print(f"[publisher] get_fid like status: {r4.status_code}")
        fid = _extract_fid(r4.text)
        if fid:
            print(f"[publisher] get_fid like OK: {fid}")
            return fid
    except Exception as e:  # noqa: BLE001
        print(f"[publisher] get_fid like failed: {type(e).__name__}: {e}")

    return None


def post_thread(
    session: requests.Session,
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
        "content": _format_content(content),
        "tbs": tbs,
        "vericode": "",  # 正常无验证码时留空；触发验证码需人工处理
        "vote_info": "",
        "post_source": "1",
        "__type__": "thread",
    }
    post_headers = _headers(
        {"Referer": f"{TIEBA}/f?kw={requests.utils.quote(forum_name)}"}
    )
    try:
        r = session.post(
            f"{TIEBA}/f/commit/thread/add",
            data=data,
            headers=post_headers,
            cookies=_cookies(),
            timeout=20,
        )
    except requests.exceptions.TooManyRedirects as e:
        # 打印跳转链，帮助定位是缺 Cookie 还是被风控
        print(f"[publisher] POST 触发重定向循环：{e}")
        if e.response and e.response.history:
            for i, resp in enumerate(e.response.history, 1):
                print(f"[publisher] redirect {i}: {resp.status_code} -> {resp.url}")
        # 关闭自动重定向再试一次，看贴吧实际返回什么
        r = session.post(
            f"{TIEBA}/f/commit/thread/add",
            data=data,
            headers=post_headers,
            cookies=_cookies(),
            timeout=20,
            allow_redirects=False,
        )
        print(f"[publisher] 关闭重定向后状态码：{r.status_code}")
        print(f"[publisher] Location: {r.headers.get('Location', 'N/A')}")
        return {
            "status_code": r.status_code,
            "location": r.headers.get("Location", ""),
            "raw": r.text[:500],
        }
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
    try:
        # 先预热 Session（访问首页建立会话），否则风控会返回"雷达接入"通用页
        session = _create_session()
        tbs = get_tbs(session)
        fid = get_fid(session, forum)
        if not fid:
            raise RuntimeError("无法获取 fid，请检查论坛名或登录态（BDUSS）")
        if not tbs:
            raise RuntimeError("无法获取 tbs（CSRF 令牌），请检查 BDUSS 是否有效")
        time.sleep(random.uniform(1, max(1.0, cfg.delay_seconds)))
        res = post_thread(session, fid, forum, title, content, tbs)
        print(f"[publisher] 发帖响应：{res}")
        # 把响应也存到本地日志
        (OUTPUT_DIR / f"post_response_{time.strftime('%Y%m%d_%H%M%S')}.txt").write_text(
            str(res), encoding="utf-8"
        )
        return res
    except Exception as e:  # noqa: BLE001
        err_msg = f"[publisher] 真发失败（{forum}）：{type(e).__name__}: {e}"
        print(err_msg)
        # 失败时仍保存草稿，方便人工补发
        ts = time.strftime("%Y%m%d_%H%M%S")
        safe = re.sub(r'[\\/:*?"<>|]', "_", forum)
        fallback = OUTPUT_DIR / f"draft_{safe}_{ts}.txt"
        fallback.write_text(
            f"吧名：{forum}\n标题：{title}\n\n{content}",
            encoding="utf-8",
        )
        print(f"[publisher] 已保存失败草稿：{fallback}")
        return {"error": str(e), "forum": forum}
