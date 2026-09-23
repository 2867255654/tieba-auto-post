"""配置加载：环境变量 + sources.yaml。
所有敏感项（BDUSS）只从环境变量读取，不写进仓库。
"""
import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

try:
    from dotenv import load_dotenv

    load_dotenv()
except Exception:
    # python-dotenv 未安装也不影响运行（环境变量仍可来自系统）
    pass

ROOT = Path(__file__).resolve().parent
OUTPUT_DIR = ROOT / "output"
OUTPUT_DIR.mkdir(exist_ok=True)


@dataclass
class Config:
    # 目标吧列表（热门游戏吧，逗号分隔）。默认仅抗压背锅吧。
    # 兼容旧的 FORUM_NAME 单值写法；FORUM_NAMES 优先。
    forum_names: list = field(
        default_factory=lambda: [
            s.strip()
            for s in (os.getenv("FORUM_NAMES") or os.getenv("FORUM_NAME") or "抗压背锅吧").split(",")
            if s.strip()
        ]
    )

    # 贴吧登录态（仅 POST_MODE=1 时需要）。从浏览器 F12 → Cookie 复制 BDUSS 字段
    bduss: str = os.getenv("BDUSS", "")

    # 发帖开关：默认 False（草稿预览），绝不自动发帖
    post_mode: bool = os.getenv("POST_MODE", "0") in ("1", "true", "True", "yes")

    # 每天摘抄条数 / 资讯时间窗口（小时）
    top_n: int = int(os.getenv("TOP_N", "8"))
    max_age_hours: int = int(os.getenv("MAX_AGE_HOURS", "24"))

    # LLM（OpenAI 兼容接口，可填 DeepSeek / 通义 / 智谱 的兼容地址）
    llm_base_url: str = os.getenv("LLM_BASE_URL", "https://api.openai.com/v1")
    llm_api_key: str = os.getenv("LLM_API_KEY", "")
    llm_model: str = os.getenv("LLM_MODEL", "gpt-4o-mini")

    # RSSHub 实例（公开实例不稳定，建议自建）
    rsshub_base: str = os.getenv("RSSHUB_BASE", "https://rsshub.app")

    # 发帖前随机延迟上限（秒），降低风控命中
    delay_seconds: float = float(os.getenv("DELAY_SECONDS", "3"))

    sources: list = field(default_factory=list)


def load_sources(path: Path | None = None) -> list:
    p = path or (ROOT / "sources.yaml")
    if not p.exists():
        return []
    with open(p, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return data.get("sources", [])


cfg = Config()
cfg.sources = load_sources()
