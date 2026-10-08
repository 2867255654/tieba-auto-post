"""一键登录贴吧，把登录态保存到 .auth_state.json，供发帖脚本反复使用。

为什么需要它：
    贴吧的 BDUSS 会失效（服务器会主动清除过期或被顶掉的登录态），
    失效后所有发帖都会失败。手动去 F12 里找 Cookie 很麻烦，
    用本脚本登录一次即可，之后自动复用，无需再填任何 Cookie。

用法：
    双击本文件（或运行 python login_helper.py）→ 弹出的浏览器里登录贴吧
    （扫码 / 账号密码都行）→ 回到命令行按回车 → 完成。
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


def main():
    print("=" * 56)
    print("  贴吧一键登录 —— 登录态会保存到 .auth_state.json")
    print("=" * 56)
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
        ctx.storage_state(path=str(STATE))
        # 顺手校验一下登录是否真的生效
        try:
            info = page.evaluate(
                "async()=>{try{const r=await fetch('/f/user/json_userinfo',"
                "{credentials:'include'});return await r.text();}catch(e){return 'ERR'}}"
            )
            ok = bool(info) and info.strip() not in ("null", "") and not info.startswith("ERR")
        except Exception:  # noqa: BLE001
            ok = None
        browser.close()
    print(f"登录态校验：{'有效，登录成功' if ok else '未登录（请重试）' if ok is False else '无法校验'}")
    print(f"\n已保存登录态：{STATE}")
    if ok:
        print("现在可以直接双击 run_local.bat 发帖了（会自动复用该登录态）。")
    else:
        print("提示：校验显示未登录，请重新运行本脚本并确认已在浏览器里登录成功。")


if __name__ == "__main__":
    main()
