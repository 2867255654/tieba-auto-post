"""编排入口：采集 → 摘抄生成 → 发布（默认草稿）。

用法：
  python main.py            # 正常跑（采集真实 RSS，草稿预览）
  python main.py --demo     # 用内置样本跑通全流程（无需网络）
  python main.py --post     # 强制发帖（仍需 .env 中 POST_MODE=1 且填了 BDUSS）

建议配合定时任务（cron / 任务计划 / GitHub Actions）每天跑一次。
"""
import json
import random
import sys
import time

from collector import SAMPLE_ITEMS, collect
from config import cfg, OUTPUT_DIR
from digest import generate_digest
from publisher import publish


def main():
    demo = "--demo" in sys.argv
    force_post = "--post" in sys.argv

    mode = "发帖" if (cfg.post_mode and cfg.bduss and not demo) else "草稿预览"
    print(f"=== 贴吧游戏资讯摘抄 | 目标吧：{', '.join(cfg.forum_names)} | 模式：{mode} ===")

    items = SAMPLE_ITEMS if demo else collect()
    print(f"[collect] 命中 {len(items)} 条")
    for it in items:
        print(f"  - {it['title']}  ({it['source']})")

    if not items:
        print("[collect] 未采集到资讯，结束（可检查 sources.yaml / 网络 / RSSHUB_BASE）")
        return

    title, body = generate_digest(items)
    print(f"\n[digest] 生成标题：{title}\n")

    dry_run = None if not force_post else (not (cfg.post_mode and cfg.bduss))
    will_post = mode == "发帖"
    results = {}
    for idx, forum in enumerate(cfg.forum_names):
        print(f"\n=== 发往：{forum} ===")
        res = publish(title, body, forum_name=forum, dry_run=dry_run)
        results[forum] = res
        if will_post and idx < len(cfg.forum_names) - 1:
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
