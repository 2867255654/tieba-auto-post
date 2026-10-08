"""一键登录贴吧，把登录态保存下来供发帖脚本反复使用。

为什么要用它：
    贴吧的 BDUSS 会失效（服务器会主动清除过期或被顶掉的登录态），
    而只填 BDUSS 一个字段往往不够 —— 百度会校验设备指纹，
    缺 BAIDUID / STOKEN 等会判为异常登录。手动去 F12 里抄 Cookie 又麻烦。
    用本脚本登录一次，它会自动把**完整 Cookie** 写进 .env，之后直接发帖即可。

用法：
    双击 login_helper.bat（或在命令行运行 python login_helper.py）
    → 弹出的浏览器里登录贴吧（扫码 / 账号密码都行）
    → 回到命令行按回车 → 完成。

产出：
    .env 里的 COOKIE_STRING=...（完整 Cookie，requests 发帖路线直接可用）
    .auth_state.json（浏览器通道用的登录态，BROWSER_MODE=1 时生效）
"""
import sys
from pathlib import Path

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    print("缺少 playwright，请先运行：pip install playwright")
    sys.exit(1)

ROOT = Path(__file__).resolve().parent
STATE = ROOT / ".auth_state.json"
ENV = ROOT / ".env"

# 优先挑选这些关键 Cookie（其余也一并写入，多多益善）
KEY_ORDER = [
    "BDUSS", "BDUSS_BFESS", "STOKEN", "PTOKEN", "PASSID",
    "BAIDUID", "BAIDUID_BFESS", "BAIDU_WISE_UID",
    "USER_JUMP", "TIEBAUID", "TIEBA_NEW_PC", "TIEBA_SID",
]


def _build_cookie_string(cookies: list) -> str:
    """把浏览器 Cookie 列表拼成 "k=v; k=v" 串，关键字段排前面。"""
    by_name = {}
    for c in cookies:
        if c.get("domain", "").endswith("baidu.com"):
            by_name[c["name"]] = c["value"]
    ordered = [k for k in KEY_ORDER if k in by_name]
    rest = [k for k in by_name if k not in ordered]
    return "; ".join(f"{k}={by_name[k]}" for k in ordered + rest)


def _write_env_cookie(cookie_str: str) -> bool:
    """把 COOKIE_STRING 写进 .env（存在则替换该行，不存在则追加）。"""
    if not ENV.exists():
        print(f"未找到 {ENV}，跳过写入（可手动把 Cookie 串填到 COOKIE_STRING=）")
        return False
    lines = ENV.read_text(encoding="utf-8").splitlines()
    out, replaced = [], False
    for ln in lines:
        if ln.strip().startswith("COOKIE_STRING="):
            out.append("COOKIE_STRING=" + cookie_str)
            replaced = True
        else:
            out.append(ln)
    if not replaced:
        out.append("COOKIE_STRING=" + cookie_str)
    ENV.write_text("\n".join(out) + "\n", encoding="utf-8")
    return True


def main():
    print("=" * 56)
    print("  贴吧一键登录 —— 登录态会写入 .env")
    print("=" * 56)
    ok = None
    cookie_str = ""
    with sync_playwright() as pw:
        browser, err = None, None
        for channel in ("chrome", "msedge", None):
            try:
                kw = {"headless": False}
                if channel:
                    kw["channel"] = channel
                browser = pw.chromium.launch(**kw)
                print(f"已启动浏览器（{channel or '自带内核'}）")
                break
            except Exception as e:  # noqa: BLE001
                err = e
                continue
        if browser is None:
            print(f"无法启动浏览器：{err}")
            sys.exit(1)

        ctx = browser.new_context(locale="zh-CN")
        page = ctx.new_page()
        page.goto("https://tieba.baidu.com/", wait_until="domcontentloaded", timeout=45000)
        print()
        print(">>> 请在弹出的浏览器窗口里登录贴吧（扫码或账号密码均可）")
        print(">>> 登录成功后【不用关浏览器】，回到这里按回车即可")
        try:
            input()
        except (EOFError, KeyboardInterrupt):
            pass

        # 1) 校验登录态是否真的生效
        try:
            info = page.evaluate(
                "async()=>{try{const r=await fetch('/f/user/json_userinfo',"
                "{credentials:'include'});return await r.text();}catch(e){return 'ERR'}}"
            )
            ok = bool(info) and info.strip() not in ("null", "") and not info.startswith("ERR")
        except Exception:  # noqa: BLE001
            ok = None

        # 2) 提取完整 Cookie 并保存
        cookie_str = _build_cookie_string(ctx.cookies())
        try:
            ctx.storage_state(path=str(STATE))
        except Exception:  # noqa: BLE001
            pass
        browser.close()

    print()
    if ok is True:
        print("登录态校验：有效，登录成功")
    elif ok is False:
        print("登录态校验：仍未登录 —— 请确认浏览器里已完成登录后重跑本脚本")
    else:
        print("登录态校验：无法校验（不影响，可继续尝试发帖）")

    if cookie_str:
        n = cookie_str.count("=")
        if _write_env_cookie(cookie_str):
            print(f"已写入 .env → COOKIE_STRING（{len(cookie_str)} 字符，含 {n} 个字段）")
        print(f"已保存浏览器登录态 → {STATE.name}")
    else:
        print("未取到任何 Cookie，未写入 .env")

    if ok is not False:
        print("\n下一步：双击 run_local.bat 发帖（会使用刚保存的完整登录态）。")


if __name__ == "__main__":
    main()
