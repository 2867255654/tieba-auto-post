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
    if cfg.baidu_wise_uid:
        cookies["BAIDU_WISE_UID"] = cfg.baidu_wise_uid
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


def _looks_like_real_name(name: str | None) -> bool:
    """过滤掉雷达页/通用页/小程序页返回的无意义标题。"""
    if not name:
        return False
    name = name.strip()
    bad = {"", "吧", "贴吧", "百度贴吧", "贴吧小程序", "百度"}
    if name in bad:
        return False
    # 登录错误页、小程序页 title 里常见这些词
    if re.search(r'登录|小程序|错误|error|登陆|Auth', name, re.I):
        return False
    # 真实吧名通常以“吧”结尾
    return name.endswith("吧")


def _canonical_name_from_title(title: str | None) -> str | None:
    """从 <title>Steam吧_百度贴吧</title> 提取规范吧名（去后缀）。"""
    if not title:
        return None
    name = re.sub(r'[_-]?\s*百度贴吧.*$', '', title).strip()
    return name if _looks_like_real_name(name) else None


def _fid_via_share_api(session: requests.Session, encoded: str, referer: str):
    """分享 API 返回 JSON，最稳，通常不被“雷达接入”网页拦截；顺便拿规范吧名。

    返回 (fid, canonical_name)。两条路径：fnameShareApi 与 fname。
    """
    fid = None
    canonical = None
    for path in ("/f/commit/share/fnameShareApi", "/f/commit/share/fname"):
        try:
            api_url = f"{TIEBA}{path}?ie=utf-8&fname={encoded}"
            r = session.get(
                api_url,
                headers=_headers({"Referer": referer, "X-Requested-With": "XMLHttpRequest"}),
                cookies=_cookies(),
                timeout=15,
            )
            print(f"[publisher] share_api {path} status: {r.status_code}")
            raw = r.text.strip()
            print(f"[publisher] share_api {path} raw[:200]: {raw[:200]!r}")
            # 处理 JSONP 包装：callback({...}) 或 ({...})
            if raw.startswith("(") and raw.endswith(")"):
                raw = raw[1:-1]
            # 去掉常见 callback 前缀/后缀
            raw = re.sub(r'^[\w.]+\(', '', raw)
            raw = re.sub(r'\);?$', '', raw)
            # 稳健提取最外层 JSON 对象：从第一个 { 到最后一个 }
            start = raw.find("{")
            end = raw.rfind("}")
            if start == -1 or end == -1 or end <= start:
                continue
            data = json.loads(raw[start:end + 1])
            print(f"[publisher] share_api {path} response: {data}")
            d = data.get("data", {}) if isinstance(data.get("data"), dict) else {}
            fid = d.get("fid") or d.get("forum_id") or data.get("fid") or data.get("forum_id")
            candidate = d.get("forum_name") or d.get("name")
            if _looks_like_real_name(candidate):
                canonical = candidate
            if fid == 0:
                err = data.get("error") or d.get("error") or ""
                print(f"[publisher] share_api {path} 返回 fid=0（error={err!r}），该吧名可能不存在或已被合并")
                fid = None
            elif fid:
                print(f"[publisher] get_fid share_api OK: {fid}")
                return int(fid), canonical
        except Exception as e:  # noqa: BLE001
            print(f"[publisher] share_api {path} failed: {type(e).__name__}: {e}")
    return None, canonical


def _fid_via_mobile(session: requests.Session, encoded: str, referer: str):
    """移动端接口，干净移动头返回 JSON 风格页面；顺带拿规范吧名。"""
    fid = None
    canonical = None
    # 多个移动入口：部分高热度吧对特定路径/参数风控强度不同
    mo_urls = [
        f"{TIEBA}/mo/q/fid?kw={encoded}",
        f"{TIEBA}/mo/q/fid?kw={encoded}&ie=utf-8",
        f"{TIEBA}/f?kw={encoded}&lp=5028&mo_device=1&is_baidu=1",
        f"https://wapp.baidu.com/f?kw={encoded}&lp=5028&mo_device=1",
    ]
    mo_headers = {
        "User-Agent": (
            "Mozilla/5.0 (Linux; Android 10; SM-G981B) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Mobile Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Referer": f"{TIEBA}/",
    }
    for mo_url in mo_urls:
        try:
            r = session.get(mo_url, headers=mo_headers, cookies=_cookies(), timeout=15)
            print(f"[publisher] get_fid mo status: {r.status_code}, url: {r.url}")
            raw = r.text.strip()
            # 移动端页面常见字段："forum_id":123 或 "fid":123 或 "id":123
            for pat in (
                r'"forum_id"\s*:\s*(\d+)',
                r'"fid"\s*:\s*(\d+)',
                r'"id"\s*:\s*(\d+)',
            ):
                mf = re.search(pat, raw)
                if mf:
                    fid = int(mf.group(1))
                    print(f"[publisher] get_fid mo OK: {fid}")
                    break
            # 移动端页面里常见的规范吧名/标题
            mt = re.search(r'"forum_name"\s*:\s*"([^"]+)"', raw) or re.search(
                r'<title>([^<]+)</title>', raw, re.I
            )
            if mt:
                name = _canonical_name_from_title(mt.group(1))
                if name:
                    canonical = name
            if fid:
                return fid, canonical
        except Exception as e:  # noqa: BLE001
            print(f"[publisher] get_fid mo {mo_url} failed: {type(e).__name__}: {e}")
    return fid, canonical


def _fid_via_rss(session: requests.Session, encoded: str, referer: str):
    """RSS 订阅源绕开网页雷达页；部分吧能从频道信息拿到 fid / 规范名。"""
    fid = None
    canonical = None
    try:
        rss_url = f"{TIEBA}/f?kw={encoded}&rss=1&ie=utf-8"
        r = session.get(rss_url, headers=_headers({"Referer": referer}), cookies=_cookies(), timeout=15)
        print(f"[publisher] get_fid rss status: {r.status_code}")
        text = r.text
        mt = re.search(r'<title>(.*?)</title>', text, re.S)
        if mt:
            rss_title = mt.group(1).strip()
            print(f"[publisher] get_fid rss title: {rss_title[:80]}")
            name = _canonical_name_from_title(rss_title)
            if name:
                canonical = name
        # 某些 RSS 模板会内联 forum_id
        mf = re.search(r'forum_id["\']?\s*[:=]\s*["\']?(\d+)', text)
        if mf:
            fid = int(mf.group(1))
            print(f"[publisher] get_fid rss OK: {fid}")
    except Exception as e:  # noqa: BLE001
        print(f"[publisher] get_fid rss failed: {type(e).__name__}: {e}")
    return fid, canonical


def _fid_via_home_html(session: requests.Session, encoded: str, referer: str):
    """吧首页 HTML，最容易被雷达页拦截，放最后；顺带拿规范吧名。"""
    fid = None
    canonical = None
    urls_to_try = [
        f"{TIEBA}/f?kw={encoded}&fr=home",
        f"{TIEBA}/f?kw={encoded}&ie=utf-8&fr=home",
        f"{TIEBA}/f?kw={encoded}&ie=utf-8",
        f"{TIEBA}/f?kw={encoded}",
        f"{TIEBA}/f?kw={encoded}&ie=utf-8&pn=0",
        f"{TIEBA}/f?kw={encoded}&fr=search",
    ]
    for url in urls_to_try:
        r = session.get(url, headers=_headers({"Referer": referer}), cookies=_cookies(), timeout=15)
        print(f"[publisher] get_fid home status: {r.status_code}, url: {r.url}")
        if r.history:
            for i, h in enumerate(r.history, 1):
                print(f"[publisher] get_fid home redirect {i}: {h.status_code} -> {h.headers.get('Location', h.url)}")
        raw = r.text
        mt = re.search(r'<title>([^<]+)</title>', raw, re.I)
        if mt:
            name = _canonical_name_from_title(mt.group(1))
            if name:
                canonical = name
        fid = _extract_fid(raw)
        if fid:
            print(f"[publisher] get_fid home OK: {fid}")
            return fid, canonical
    snippet = raw[:600].replace("\n", " ")
    print(f"[publisher] get_fid home HTML snippet: {snippet}")
    return None, canonical


def _fid_via_like(session: requests.Session, encoded: str, referer: str):
    """关注/签到接口兜底。"""
    try:
        like_url = f"{TIEBA}/f/like/furank?kw={encoded}&ie=utf-8"
        r = session.get(like_url, headers=_headers({"Referer": referer}), cookies=_cookies(), timeout=15)
        print(f"[publisher] get_fid like status: {r.status_code}")
        fid = _extract_fid(r.text)
        if fid:
            print(f"[publisher] get_fid like OK: {fid}")
            return fid, None
    except Exception as e:  # noqa: BLE001
        print(f"[publisher] get_fid like failed: {type(e).__name__}: {e}")
    return None, None


def get_fid(session: requests.Session, forum_name: str):
    encoded = requests.utils.quote(forum_name)
    referer = f"{TIEBA}/"

    # 优先使用手动 fid 映射（高热度/反爬严重的吧可在 .env 里直接写死）
    manual_fid = cfg.forum_fid_map.get(forum_name)
    if manual_fid:
        print(f"[publisher] get_fid 使用手动映射：{forum_name} -> {manual_fid}")
        return int(manual_fid)

    canonical: str | None = None

    # 方案 A：分享 API（JSON，最稳，通常不被雷达页拦截）
    fid, c = _fid_via_share_api(session, encoded, referer)
    if c:
        canonical = c
    if fid:
        return fid

    # 方案 B：移动端接口（JSON 风格，干净移动头）
    fid, c = _fid_via_mobile(session, encoded, referer)
    if c:
        canonical = c
    if fid:
        return fid

    # 方案 C：RSS 订阅源（绕开网页雷达页）
    fid, c = _fid_via_rss(session, encoded, referer)
    if c:
        canonical = c
    if fid:
        return fid

    # 方案 D：吧首页 HTML（最容易被雷达页拦截，放最后）
    fid, c = _fid_via_home_html(session, encoded, referer)
    if c:
        canonical = c
    if fid:
        return fid

    # 方案 E：关注/签到接口
    fid, _ = _fid_via_like(session, encoded, referer)
    if fid:
        return fid

    # 拿到规范吧名但没拿到 fid：多半是大小写/别名问题，用规范名重试一次
    if canonical and canonical != forum_name:
        print(f"[publisher] 用规范吧名重试：{forum_name} -> {canonical}")
        return get_fid(session, canonical)

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
            raise RuntimeError(
                "无法获取 fid。可能原因：1）吧名不存在或已被合并；"
                "2）该吧被百度“雷达接入”反爬页拦截；3）BDUSS 登录态失效。"
                "请检查 run_log.txt 中 share_api/mobile/rss 等步骤的日志。"
            )
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
