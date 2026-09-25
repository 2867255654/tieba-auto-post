"""编排入口：采集 → 摘抄生成 → 发布（默认草稿）。

用法：
  python main.py            # 正常跑（采集真实 RSS，草稿预览）
  python main.py --demo     # 用内置样本跑通全流程（无需网络）
  python main.py --post     # 强制发帖（仍需 .env 中 POST_MODE=1 且填了 BDUSS）

建议配合定时任务（cron / 任务计划 / GitHub Actions）每天跑一次。
"""
import sys

# Windows 控制台默认 GBK 编码，帖子正文里的 emoji 等字符会导致 print 时抛
# UnicodeEncodeError，使进程退出码非零、定时任务被判失败。把 stdout/stderr 的
# 错误处理改成 replace，无法编码的字符替换为 ?，流程不再因偶发字符而中断。
try:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
        sys.stderr.reconfigure(errors="replace")
except Exception:  # noqa: BLE001
    pass

import json
import os
import random
import time

from collector import SAMPLE_ITEMS, collect
from config import cfg, OUTPUT_DIR
from digest import generate_digest
from publisher import publish


def main():
    demo = "--demo" in sys.argv
    force_post = "--post" in sys.argv

    # 目标吧选择：默认从 FORUM_NAMES 随机挑 1 个发（更稳、更像真人）；
    # 设环境变量 FORUM_PICK_ALL=1 才全发（保留原批量行为）
    pick_all = os.getenv("FORUM_PICK_ALL", "0") == "1"
    if pick_all:
        target_forums = list(cfg.forum_names)
    else:
        target_forums = [random.choice(cfg.forum_names)]

    mode = "发帖" if (cfg.post_mode and cfg.bduss and not demo) else "草稿预览"
    pick_desc = "随机1吧" if not pick_all else f"全{len(target_forums)}吧"
    print(f"=== 贴吧游戏资讯摘抄 | 目标：{pick_desc} | 模式：{mode} ===")

    items = SAMPLE_ITEMS if demo else collect()
    print(f"[collect] 命中 {len(items)} 条")
    for it in items:
        print(f"  - {it['title']}  ({it['source']})")

    if not items:
        print("[collect] 未采集到资讯，结束（可检查 sources.yaml / 网络 / RSSHUB_BASE）")
        return

    title, body = generate_digest(items)
    print(f"\n[digest] 生成标题：{title}\n")

    if demo:
        dry_run = True  # demo 模式强制草稿，绝不真发
    elif force_post:
        dry_run = not (cfg.post_mode and cfg.bduss)
    else:
        dry_run = None  # 由 publisher 根据 cfg 自行判断
    will_post = mode == "发帖"
    results = {}
    for idx, forum in enumerate(target_forums):
        print(f"\n=== 发往：{forum} ===")
        res = publish(title, body, forum_name=forum, dry_run=dry_run)
        results[forum] = res
        # 仅「全发模式」才需要多吧间隔；随机单吧无需间隔
        if will_post and pick_all and idx < len(target_forums) - 1:
            gap = random.uniform(15, 45)
            print(f"[main] 多吧间隔 {gap:.0f}s，降低风控")
            time.sleep(gap)

    log = {
        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "forums": cfg.forum_names,
        "items": len(items),
        "title": title,
        "results": {k: (v if isinstance(v, dict) else str(v)) for k, v in results.items()},
    }
    (OUTPUT_DIR / f"run_{time.strftime('%Y%m%d')}.json").write_text(
        json.dumps(log, ensure_ascii=False, indent=2), encoding="utf-8"
    )


if __name__ == "__main__":
    main()
