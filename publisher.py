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
    """组装贴吧所需 Cookie。

    优先级：
      1) COOKIE_STRING —— 从浏览器 F12 整串复制的完整 Cookie（最可靠）。
         只填 BDUSS 往往不够：百度会校验设备指纹，缺 BAIDUID/STOKEN 等易被判异常登录。
      2) 单字段组合 BDUSS + BDUSS_BFESS + STOKEN + BAIDUID ...
    """
    import os

    raw = (os.getenv("COOKIE_STRING") or "").strip()
    if raw:
        parsed = {}
        for part in raw.split(";"):
            if "=" not in part:
                continue
            k, v = part.split("=", 1)
            k, v = k.strip(), v.strip()
            if k and v:
                parsed[k] = v
        if parsed:
            return parsed

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
    if cfg.baiduid_bfess:
        cookies["BAIDUID_BFESS"] = cfg.baiduid_bfess
    if cfg.baidu_wise_uid:
        cookies["BAIDU_WISE_UID"] = cfg.baidu_wise_uid
    return cookies


def _sanitize_for_tieba(text: str) -> str:
    """清洗贴吧正文/标题里的危险字符。

    【重要】贴吧 content 是富文本格式，正文里的半角尖括号 `<...>` 会被服务端
    当成 HTML 标签解析，导致「参数校验未通过」（no=2000，实测复现）。
    LLM 常输出 `<收尾梗>` 这类标记，必须替换掉。
    这里统一换成全角 ＜＞：视觉几乎一致，但不会被当作标签。
    """
    if not text:
        return text
    return text.replace("<", "＜").replace(">", "＞")


def _format_content(content: str, images: list | None = None) -> str:
    """把普通正文转成贴吧富文本格式 [[0,1,"段落"], ...]，可选插入配图。

    贴吧发帖接口要求 content 字段是这种 JSON 数组字符串；直接传纯文本会被服务器拒绝。
    images 为 [{'pic_id','width','height'}]；第一张放最前（当帖子封面/列表缩略图），其余放末尾。
    """
    paragraphs = [p.strip() for p in content.split("\n") if p.strip()]
    if not paragraphs:
        paragraphs = [content]
    # 实测：只给数组分段，贴吧渲染时不会换行；必须在每段文本末尾补 \n 才会真正换行。
    # 同时清洗尖括号（见 _sanitize_for_tieba，这是 no=2000 的真正元凶）。
    arr = [[0, 1, _sanitize_for_tieba(p) + "\n"] for p in paragraphs]
    if images:
        pic_paras = [
            [0, 1, f"#(pic,{im['pic_id']},{im['width']},{im['height']})\n"] for im in images
        ]
        arr = pic_paras[:1] + arr + pic_paras[1:]
    # 保持中文原样（ensure_ascii=False）。实测发帖 body 里 title/content 的 UTF-8 百分号
    # 编码完全正确（%E8%90%BD%E5%B9%95 = "落幕"），回显里的乱码是服务端侧编码问题，
    # 不是我们发错；改成 \uXXXX 反而会让正文显示成转义字面量。
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


def _fid_via_wap(session: requests.Session, encoded: str, referer: str):
    """WAP 旧版贴吧（waptieba.baidu.com）服务端渲染，HTML 源码里直接带 fid，
    不像 PC 版用 JS 异步填充，能绕开“雷达接入”壳页。"""
    fid = None
    canonical = None
    wap_urls = [
        f"https://waptieba.baidu.com/f?kw={encoded}",
        f"https://waptieba.baidu.com/f?kw={encoded}&fr=home",
    ]
    wap_headers = {
        "User-Agent": (
            "Mozilla/5.0 (Linux; Android 10; SM-G981B) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Mobile Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Referer": "https://waptieba.baidu.com/",
    }
    for wap_url in wap_urls:
        try:
            r = session.get(wap_url, headers=wap_headers, cookies=_cookies(), timeout=15)
            print(f"[publisher] get_fid wap status: {r.status_code}, url: {r.url}")
            text = r.text
            mt = re.search(r'<title>([^<]+)</title>', text, re.I)
            if mt:
                name = _canonical_name_from_title(mt.group(1))
                if name:
                    canonical = name
            # WAP 页链接形如 /p/123?fid=67890 或 f?kw=xxx&fid=67890；也可能内联 forum_id
            for pat in (
                r'[?&]fid=(\d+)',
                r'data-fid="(\d+)"',
                r'"forum_id"\s*:\s*(\d+)',
                r'"fid"\s*:\s*(\d+)',
            ):
                mf = re.search(pat, text)
                if mf:
                    fid = int(mf.group(1))
                    print(f"[publisher] get_fid wap OK: {fid}")
                    return fid, canonical
            print(f"[publisher] get_fid wap HTML snippet: {text[:300].replace(chr(10), ' ')}")
        except Exception as e:  # noqa: BLE001
            print(f"[publisher] get_fid wap {wap_url} failed: {type(e).__name__}: {e}")
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
    """返回 (fid, canonical_name)。

    canonical 是百度认可的**规范吧名**（拿不到时为 None）。发帖时应优先用它作为 kw，
    否则当用户输入的写法与百度规范名不一致时，会被拒发（错误码 2000 无效参数 fname）。
    """
    encoded = requests.utils.quote(forum_name)
    referer = f"{TIEBA}/"

    # 优先使用手动 fid 映射（高热度/反爬严重的吧可在 .env 里直接写死）
    manual_fid = cfg.forum_fid_map.get(forum_name)
    if manual_fid:
        print(f"[publisher] get_fid 使用手动映射：{forum_name} -> {manual_fid}")
        return int(manual_fid), None

    canonical: str | None = None

    # 方案 A：分享 API（JSON，最稳，通常不被雷达页拦截）
    fid, c = _fid_via_share_api(session, encoded, referer)
    if c:
        canonical = c
    if fid:
        return fid, canonical

    # 方案 A2：WAP 旧版（服务端渲染，源码直接带 fid，绕开 PC 雷达壳页）
    fid, c = _fid_via_wap(session, encoded, referer)
    if c:
        canonical = c
    if fid:
        return fid, canonical

    # 方案 B：移动端接口（JSON 风格，干净移动头）
    fid, c = _fid_via_mobile(session, encoded, referer)
    if c:
        canonical = c
    if fid:
        return fid, canonical

    # 方案 C：RSS 订阅源（绕开网页雷达页）
    fid, c = _fid_via_rss(session, encoded, referer)
    if c:
        canonical = c
    if fid:
        return fid, canonical

    # 方案 D：吧首页 HTML（最容易被雷达页拦截，放最后）
    fid, c = _fid_via_home_html(session, encoded, referer)
    if c:
        canonical = c
    if fid:
        return fid, canonical

    # 方案 E：关注/签到接口
    fid, _ = _fid_via_like(session, encoded, referer)
    if fid:
        return fid, canonical

    # 拿到规范吧名但没拿到 fid：多半是大小写/别名问题，用规范名重试一次
    if canonical and canonical != forum_name:
        print(f"[publisher] 用规范吧名重试：{forum_name} -> {canonical}")
        return get_fid(session, canonical)

    return None, None


def download_image(url: str, timeout: int = 20) -> bytes | None:
    """下载源站配图，准备交给贴吧图床。"""
    try:
        r = requests.get(url, headers=HEADERS, timeout=timeout)
        if r.status_code == 200 and r.content:
            return r.content
        print(f"[publisher] download_image status={r.status_code} url={url[:100]}")
    except Exception as e:  # noqa: BLE001
        print(f"[publisher] download_image 失败：{type(e).__name__}: {e}")
    return None


def upload_image(
    session: requests.Session,
    fid: int,
    image_bytes: bytes,
    filename: str = "pic.jpg",
    tbs: str | None = None,
) -> dict | None:
    """把图片上传到贴吧图床，返回 {'pic_id','width','height'}；失败返回 None。

    接口（网页端发帖时上传图片走的那个）：
        POST https://uploadphotos.baidu.com/upload/pic?tbs=..&fid=..&save_yun_album=1
        返回 {"err_no":0,"info":{"pic_id_encode":"..","fullpic_width":626,"fullpic_height":292}}
    拿到 pic_id 后，正文里用 #(pic,<pic_id>,<宽>,<高>) 插入图片。
    """
    if tbs is None:
        tbs = get_tbs(session)
    url = (
        "https://uploadphotos.baidu.com/upload/pic"
        f"?tbs={requests.utils.quote(str(tbs))}&fid={fid}&save_yun_album=1"
    )
    headers = _headers({"Referer": f"{TIEBA}/f?kw={fid}", "Origin": "https://tieba.baidu.com"})
    try:
        r = session.post(
            url,
            files={"file": (filename, image_bytes, "image/jpeg")},
            headers=headers,
            cookies=_cookies(),
            timeout=45,
        )
        print(f"[publisher] upload_image status: {r.status_code}")
        data = r.json()
    except Exception as e:  # noqa: BLE001
        print(f"[publisher] upload_image 失败：{type(e).__name__}: {e}")
        return None
    if data.get("err_no") != 0:
        print(f"[publisher] upload_image 被拒：err_no={data.get('err_no')} err_msg={data.get('err_msg')!r}")
        return None
    info = data.get("info") or {}
    pic_id = info.get("pic_id_encode") or info.get("pic_id")
    if not pic_id:
        print(f"[publisher] upload_image 无 pic_id：{str(data)[:300]}")
        return None
    result = {
        "pic_id": pic_id,
        "width": info.get("fullpic_width") or info.get("width") or 0,
        "height": info.get("fullpic_height") or info.get("height") or 0,
    }
    print(f"[publisher] upload_image OK: {result}")
    return result


def post_thread(
    session: requests.Session,
    fid: int,
    forum_name: str,
    title: str,
    content: str,
    tbs: str,
    images: list | None = None,
) -> dict:
    # 恢复为完整参数字段（排查期曾精简，但已确认参数与 no=2000 无关）。
    # rich_text=1 为富文本声明，换行与 #(pic,...) 图片标记依赖它。
    data = {
        "ie": "utf-8",
        "fid": fid,
        "kw": forum_name,
        "is_video": "false",
        "src": "1",
        "rich_text": "1",
        "title": _sanitize_for_tieba(title),
        "content": _format_content(content, images),
        "tbs": tbs,
        "vericode": "",  # 正常无验证码时留空；触发验证码需人工处理
        "vote_info": "",
        "post_source": "1",
        "__type__": "thread",
    }
    # 显式用 UTF-8 编码请求体，并在 Content-Type 里声明 charset：否则服务端可能按 GBK 解析，
    # 中文会变成乱码（"圆弧" -> "鍦嗘弧"），并可能被判定为参数无效（no=2000）。
    from urllib.parse import urlencode

    body = urlencode(data, encoding="utf-8").encode("ascii")
    print(f"[publisher] POST body({len(body)}B) 摘要: {body[:110]!r}")
    post_headers = _headers(
        {
            "Referer": f"{TIEBA}/f?kw={requests.utils.quote(forum_name)}",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        }
    )
    try:
        r = session.post(
            f"{TIEBA}/f/commit/thread/add",
            data=body,
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
            data=body,
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


# ===================== 浏览器发帖（Playwright）=====================
# 背景：贴吧 PC 端发帖接口已改到 /c/c/thread/add，强制要求 sign / jt 等由页面 JS
# 生成的浏览器行为签名，纯 requests 发帖会被判定为机器人（no=2000 参数校验未通过）。
# 这里用真实 Chrome 打开贴吧页面、走和真人一样的「点发贴 → 填标题正文 → 发表」流程，
# 所有签名交给页面自己算，从根本上绕开风控。
BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
# 由 login_helper.py 生成的登录态文件（含完整 Cookie）。存在则优先使用它，
# 免去手动维护 BDUSS —— BDUSS 会失效，而这个文件可随时重新登录刷新。
_AUTH_STATE = Path(__file__).resolve().parent / ".auth_state.json"
_BROWSER_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-dev-shm-usage",
]


def _browser_cookies() -> list:
    """构造注入浏览器的 Cookie 列表。

    优先使用 COOKIE_STRING（从浏览器 F12 整串复制的完整 Cookie，最可靠）；
    未填时退回 BDUSS / STOKEN / BAIDUID 等单字段组合。
    """
    import os

    raw = (os.getenv("COOKIE_STRING") or "").strip()
    pairs: list[tuple[str, str]] = []
    if raw:
        for part in raw.split(";"):
            if "=" not in part:
                continue
            k, v = part.split("=", 1)
            k, v = k.strip(), v.strip()
            if k and v:
                pairs.append((k, v))
    if not pairs:
        pairs = list(_cookies().items())

    out, seen = [], set()
    for k, v in pairs:
        if not v or k in seen:
            continue
        seen.add(k)
        out.append(
            {
                "name": k,
                "value": v,
                "path": "/",
                # STOKEN 只挂在贴吧域；其余统一挂 .baidu.com
                "domain": ".tieba.baidu.com" if k.upper() == "STOKEN" else ".baidu.com",
            }
        )
    return out


def _browser_login_ok(page) -> tuple[bool, str]:
    """用页面内 fetch 问贴吧用户信息接口，判断登录态是否有效。"""
    try:
        info = page.evaluate(
            "async()=>{try{const r=await fetch('/f/user/json_userinfo',"
            "{credentials:'include'});return await r.text();}catch(e){return 'ERR:'+e.message}}"
        )
    except Exception as e:  # noqa: BLE001
        return False, f"登录态检查异常：{str(e)[:120]}"
    s = (info or "").strip()
    if not s or s == "null" or s.startswith("ERR"):
        return False, f"未登录（接口返回 {s[:60]!r}）"
    return True, s[:120]


def _post_via_browser(
    forum: str, title: str, content: str, image_urls: list | None = None
) -> dict:
    """用真实浏览器发帖，返回形如 {'no':0,'data':{'tid':...}} 的结果字典。"""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as e:  # noqa: BLE001
        raise RuntimeError(
            "未安装 playwright —— 请执行：pip install playwright"
        ) from e

    cookies = _browser_cookies()
    if not cookies and not _AUTH_STATE.exists():
        raise RuntimeError(
            "没有可用登录态 —— 请先运行 login_helper.py 登录一次"
            "（或填写 .env 的 COOKIE_STRING / BDUSS）"
        )
    if cookies:
        print(f"[publisher] 浏览器将注入 {len(cookies)} 个 cookie：{[c['name'] for c in cookies]}")

    with sync_playwright() as pw:
        browser, last_err = None, None
        for channel in ([cfg.browser_channel] if cfg.browser_channel else []) + [None]:
            try:
                kw = {"headless": cfg.browser_headless, "args": _BROWSER_ARGS}
                if channel:
                    kw["channel"] = channel
                browser = pw.chromium.launch(**kw)
                print(
                    f"[publisher] 浏览器已启动：channel={channel or 'bundled'} "
                    f"headless={cfg.browser_headless}"
                )
                break
            except Exception as e:  # noqa: BLE001
                last_err = e
                print(f"[publisher] 启动浏览器失败（channel={channel}）：{str(e)[:120]}")
        if browser is None:
            raise RuntimeError(f"无法启动浏览器：{last_err}")

        ctx_kwargs = {
            "user_agent": BROWSER_UA,
            "locale": "zh-CN",
            "viewport": {"width": 1366, "height": 900},
        }
        use_state = _AUTH_STATE.exists()
        if use_state:
            ctx_kwargs["storage_state"] = str(_AUTH_STATE)
        ctx = browser.new_context(**ctx_kwargs)
        ctx.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
        )
        if use_state:
            print(f"[publisher] 复用已保存的登录态：{_AUTH_STATE.name}")
        else:
            ctx.add_cookies(cookies)
        page = ctx.new_page()
        try:
            return _browser_flow(page, forum, title, content, image_urls)
        finally:
            try:
                ctx.close()
            except Exception:  # noqa: BLE001
                pass
            browser.close()


def _browser_flow(page, forum: str, title: str, content: str, image_urls) -> dict:
    """打开吧首页 → 检查登录 → 点发贴 → 填标题正文 → 发表 → 判定结果。"""
    url = f"{TIEBA}/f?kw={requests.utils.quote(forum)}"
    print(f"[publisher] 打开吧首页：{url}")
    page.goto(url, wait_until="domcontentloaded", timeout=45000)
    page.wait_for_timeout(3000)

    ok, detail = _browser_login_ok(page)
    print(f"[publisher] 登录态：{'✅ 有效' if ok else '❌ 无效'} {detail}")
    if not ok:
        raise RuntimeError(
            "浏览器登录态无效 —— 请重新登录贴吧后，从浏览器复制最新 Cookie "
            "填入 .env 的 COOKIE_STRING（或 BDUSS/STOKEN）"
        )

    # 1) 点开发贴编辑器
    opened = False
    for sel in (".add-post .add-btn", ".button-wrapper--add-post", "text=发贴"):
        try:
            page.click(sel, timeout=6000)
            opened = True
            print(f"[publisher] 已点击发贴入口：{sel}")
            break
        except Exception:  # noqa: BLE001
            continue
    if not opened:
        raise RuntimeError("找不到发帖入口（.add-post .add-btn），页面结构可能已改版")
    page.wait_for_timeout(2500)

    # 2) 填标题（编辑器里的单行输入框）
    title_box = None
    for sel in (
        "input[placeholder*='标题']",
        ".post-title input",
        "input.editor-title",
        ".edui-body-container input",
    ):
        loc = page.locator(sel).first
        try:
            if loc.count() and loc.is_visible():
                title_box = loc
                break
        except Exception:  # noqa: BLE001
            continue
    if title_box is None:
        # 兜底：编辑器区域里第一个可见的 text 输入框
        cand = page.locator("input[type='text']")
        for i in range(min(cand.count(), 6)):
            if cand.nth(i).is_visible():
                title_box = cand.nth(i)
                break
    if title_box is None:
        raise RuntimeError("找不到标题输入框")
    title_box.click()
    title_box.fill(title)
    print(f"[publisher] 标题已填：{title}")

    # 3) 填正文（富文本 contenteditable）
    body_box = None
    for sel in ("[contenteditable='true']", ".edui-body-container", "textarea"):
        loc = page.locator(sel).first
        try:
            if loc.count() and loc.is_visible():
                body_box = loc
                break
        except Exception:  # noqa: BLE001
            continue
    if body_box is None:
        raise RuntimeError("找不到正文编辑区")
    body_box.click()
    # 富文本区用逐行输入，保证换行保留
    for i, line in enumerate(content.split("\n")):
        if i:
            page.keyboard.press("Enter")
        if line:
            page.keyboard.type(line, delay=2)
    print(f"[publisher] 正文已填（{len(content)} 字）")

    # 4) 点“发表”
    page.wait_for_timeout(600)
    submitted = False
    for sel in ("text=发表", "button:has-text('发表')", ".poster_submit", ".publish-btn"):
        try:
            page.click(sel, timeout=5000)
            submitted = True
            print(f"[publisher] 已点击发表：{sel}")
            break
        except Exception:  # noqa: BLE001
            continue
    if not submitted:
        raise RuntimeError("找不到“发表”按钮")

    # 5) 判定结果：成功会跳转到帖子页 /p/xxxx
    tid = ""
    for _ in range(20):
        page.wait_for_timeout(1000)
        m = re.search(r"/p/(\d+)", page.url)
        if m:
            tid = m.group(1)
            break
    body_text = ""
    try:
        body_text = page.inner_text("body")[:400]
    except Exception:  # noqa: BLE001
        pass
    if tid:
        print(f"[publisher] ✅ 浏览器发帖成功，tid={tid}")
        return {"no": 0, "err_code": 0, "data": {"tid": tid, "fname": forum}}

    # 没跳转：尝试从页面文案判断失败原因
    for kw in ("验证码", "频繁", "失败", "禁止", "删帖", "违规"):
        if kw in body_text:
            print(f"[publisher] ❌ 页面提示包含「{kw}」")
            return {"no": 2000, "error": kw, "data": {"tid": "", "fname": forum, "msg": body_text[:200]}}
    return {"no": 2000, "error": "unknown", "data": {"tid": "", "fname": forum, "msg": body_text[:200]}}


# 百度贴吧发帖常见错误码（把原始响应翻译成人话，便于排查）
_TIEBA_POST_ERR = {
    2000: (
        "参数校验未通过。实测最常见原因：正文里含半角尖括号 < >，"
        "会被贴吧当成 HTML 标签而拒收（程序已自动替换为全角 ＜＞）。"
        "其余可能：吧名写法不对（应填 Steam / Epic，不带“吧”字）"
    ),
    40: "发帖过于频繁，已被限流 —— 请间隔一段时间再试（本地反复调试时最容易触发）",
    2101: "本吧仅登录用户可发帖，或登录态已失效",
    4010: "账号存在异常，需绑定手机后再操作",
    220034: "操作太频繁，被限流，稍后再试",
    260005: "登录状态已过期，请重新获取 BDUSS",
    340012: "账号还未关注本吧（部分吧要求先关注才能发帖）",
    210009: "未知错误：标题可能含特殊符号",
}


def _judge_post_response(res) -> tuple:
    """把百度发帖响应翻译成 (是否成功, 人话说明)。
    成功形如 {'no':0,'err_code':0,'data':{'tid':'...'}}；
    失败形如 {'no':2000,'error':232000,'data':{'tid':'0','fname':'...'}}。"""
    if not isinstance(res, dict):
        return False, f"响应格式异常：{str(res)[:200]}"
    data = res.get("data") or {}
    no = res.get("no", res.get("err_code"))
    tid = str(data.get("tid") or "")
    fname = data.get("fname") or ""
    if str(no) == "0" and tid and tid != "0":
        return True, f"tid={tid}  吧={fname}"
    try:
        hint = _TIEBA_POST_ERR.get(int(no), "")
    except (TypeError, ValueError):
        hint = ""
    detail = f"no={no}"
    if res.get("error") not in (None, "", 0):
        detail += f" error={res.get('error')}"
    if fname:
        detail += f" fname={fname!r}"
    if hint:
        detail += f"\n    ↳ 可能原因：{hint}"
    return False, detail


def publish(
    title: str,
    content: str,
    forum_name: str | None = None,
    dry_run: bool | None = None,
    image_urls: list | None = None,
):
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

    # 真正发帖：优先走浏览器（贴吧已加浏览器行为签名校验，纯 requests 会被判定为机器人）
    if cfg.browser_mode:
        try:
            res = _post_via_browser(forum, title, content, image_urls)
            print(f"[publisher] 发帖响应：{res}")
            ok, why = _judge_post_response(res)
            print(f"[publisher] {'✅ 发帖成功：' if ok else '❌ 发帖失败：'}{why}")
            (OUTPUT_DIR / f"post_response_{time.strftime('%Y%m%d_%H%M%S')}.txt").write_text(
                str(res), encoding="utf-8"
            )
            return res
        except Exception as e:  # noqa: BLE001
            print(f"[publisher] 浏览器发帖失败（{forum}）：{type(e).__name__}: {e}")
            ts = time.strftime("%Y%m%d_%H%M%S")
            safe = re.sub(r'[\\/:*?"<>|]', "_", forum)
            fallback = OUTPUT_DIR / f"draft_{safe}_{ts}.txt"
            fallback.write_text(
                f"吧名：{forum}\n标题：{title}\n\n{content}", encoding="utf-8"
            )
            print(f"[publisher] 已保存失败草稿：{fallback}")
            return {"error": str(e), "forum": forum}

    # 备用通道：原生 requests（贴吧若放宽限制仍可用）
    try:
        # 先预热 Session（访问首页建立会话），否则风控会返回"雷达接入"通用页
        session = _create_session()
        tbs = get_tbs(session)
        fid, canonical = get_fid(session, forum)
        # 优先用百度认可的规范吧名发帖，避免输入写法与规范名不一致时被拒（错误码 2000）
        if canonical and canonical != forum:
            print(f"[publisher] 采用百度规范吧名发帖：{forum!r} -> {canonical!r}")
            forum = canonical
        if not fid:
            raise RuntimeError(
                "无法获取 fid。可能原因：1）吧名不存在或已被合并；"
                "2）该吧被百度“雷达接入”反爬页拦截；3）BDUSS 登录态失效。"
                "请检查 run_log.txt 中 share_api/mobile/rss 等步骤的日志。"
            )
        if not tbs:
            raise RuntimeError("无法获取 tbs（CSRF 令牌），请检查 BDUSS 是否有效")
        # 配图：取前 2 张源站图片，下载后转存到贴吧图床，拿 pic_id 插入正文
        images = []
        for u in (image_urls or [])[:2]:
            if not u:
                continue
            raw = download_image(u)
            if not raw:
                continue
            up = upload_image(session, fid, raw, tbs=tbs)
            if up:
                images.append(up)
        if images:
            print(f"[publisher] 已上传 {len(images)} 张配图")
        time.sleep(random.uniform(1, max(1.0, cfg.delay_seconds)))
        res = post_thread(session, fid, forum, title, content, tbs, images=images)
        print(f"[publisher] 发帖响应：{res}")
        ok, why = _judge_post_response(res)
        print(f"[publisher] {'✅ 发帖成功：' if ok else '❌ 发帖失败：'}{why}")
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
