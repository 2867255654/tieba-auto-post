# 部署到 GitHub Actions（云端每日自动跑）

本目录已包含 `.github/workflows/daily.yml`。按下面步骤即可实现「云端每日自动摘抄 + 可选自动发帖」。

## 一、把项目推送到 GitHub 仓库

```bash
cd tieba_game_digest
git init
git add .
git commit -m "feat: 贴吧游戏资讯每日摘抄流水线"
git branch -M main
git remote add origin https://github.com/<你的用户名>/<仓库名>.git
git push -u origin main
```

> 推送后 Actions 才会按 cron 自动运行。`.gitignore` 已屏蔽 `.env`，你的 BDUSS 不会进仓库。

## 二、在仓库配置 Secrets（加密，安全）

进入仓库 **Settings → Secrets and variables → Actions → New repository secret**，按需添加：

| 名称 | 必填 | 说明 |
|---|---|---|
| `BDUSS` | 仅真发时 | 浏览器 F12 → 任意贴吧请求 Cookie 里复制 `BDUSS=` 字段，**只复制该字段** |
| `POST_MODE` | 否 | 默认 `0`（草稿，绝不发帖）。要真发设 `1` 且同时填 `BDUSS` |
| `LLM_API_KEY` | 否 | 想用大模型润色摘要时填（如 DeepSeek key）；不填则走内置兜底摘要 |

## 三、可选：用 Repository variables 覆盖默认配置

**Settings → Secrets and variables → Actions → Variables**，可覆盖：
`FORUM_NAME`、`TOP_N`、`MAX_AGE_HOURS`、`RSSHUB_BASE`、`DELAY_SECONDS`、`LLM_BASE_URL`、`LLM_MODEL`。
不设则使用代码里的默认值（目标吧：抗压背锅吧，每天 8 条，24 小时窗口）。

## 四、运行与查看

- **定时**：北京时间每天 08:00 自动跑（cron `0 0 * * *`，GitHub 用 UTC）。
- **手动**：仓库 **Actions** 页 → 选工作流 → **Run workflow**。
- **结果**：每次运行在 **Actions → 对应 run → Artifacts** 里下载 `tieba-digest-output`，含当天草稿帖 `draft_*.txt` 与运行日志 `run_*.json`。

## 五、重要提醒

1. **默认不发帖**：`POST_MODE` 未设或 `0` 时只生成草稿，任何情况下都不会自动外发。要真发必须主动设 `POST_MODE=1` 并填 `BDUSS`。
2. **海外 Runner 访问国内源**：GitHub 的 runner 在境外，访问国内源（如机核 gcores、rsshub.app）可能偏慢或不稳定。建议：
   - 在 `sources.yaml` 增加海外可达的游戏 RSS（如 IGN、PCGamer、GameSpot 的 RSS）；或
   - 自建 RSSHub 并部署在海外可访问处，把 `RSSHUB_BASE` 指向它。
3. **合规风险**：贴吧协议禁止发贴机/批量刷帖，轻则删帖弹验证码，重则封号。建议老号/专用号、低频（每天 1 篇）、原创摘抄+来源标注。
4. **BDUSS 即账号钥匙**：泄露=被盗号，只放 Secrets，不写文件、不发给任何人。
