# WeRSS Insight

WeRSS Insight 是一个可独立运行的微信公众号文章阅读与总结面板。它从本地 WeRSS API 同步文章，调用任意 OpenAI-compatible 大模型接口生成摘要、价值评分和作者画像，并按计划每三天更新一次。
werss项目地址：https://github.com/rachelos/we-mp-rss/wiki/WeRSS-%E2%80%90-%E5%BE%AE%E4%BF%A1%E5%85%AC%E4%BC%97%E5%8F%B7%E8%AE%A2%E9%98%85%E5%8A%A9%E6%89%8B

## 功能

- 同步 WeRSS 的公众号、文章、正文状态和运行状态。
- 对新文章生成一句话摘要、要点、阅读价值、标签和 1-10 分评分。
- 为每个公众号维护作者画像：能力判断、擅长方向、标签、评分和置信度。
- 前端面板支持配置、手动运行、阅读队列、文章详情、作者画像和运行记录。
- 配置页提供近 14 天新增文章、摘要数量和摘要 token 消耗看板。
- 支持导出/导入 ZIP 备份包，迁移 Docker 实例时可直接带走配置、文章、摘要和作者画像。
- 可选 Webhook 阅读提醒：每次计划更新后推送高分文章列表。
- 没有配置大模型时，会使用本地启发式规则兜底，不阻塞同步。

## 本地运行

```powershell
cd C:\Users\madri\Documents\Codex\2026-05-09\werss-insight
Copy-Item .env.example .env
notepad .env
python -m pip install -r requirements.txt
.\run.ps1
```

打开：

```text
http://localhost:8765
```

也可以先不写 `.env`，启动后在面板的「配置」页填写 WeRSS 和大模型接口。密钥会保存到 `data/werss_insight.db`，不会写进源码。

## Docker 部署

本地构建：

```bash
cd /path/to/werss-insight
cp .env.example .env
nano .env
docker compose up -d --build
```

访问：

```text
http://服务器IP:8765
```

这个 compose 只创建 `werss-insight` 一个容器，使用独立端口 `8765` 和当前目录下的 `data` 卷，不会修改你的 `we-mp-rss` 容器。

## GitHub + GHCR 部署

把源码推到 GitHub 后，`.github/workflows/docker-publish.yml` 会在 `main` 分支更新时自动构建镜像并推送到 GitHub Container Registry：

```text
ghcr.io/<你的 GitHub 用户名>/<仓库名>:latest
```

服务器上可以只保留 `.env`、`docker-compose.ghcr.yml` 和 `data/`：

```bash
mkdir -p /var/apps/werss-insight
cd /var/apps/werss-insight
wget https://raw.githubusercontent.com/<你的 GitHub 用户名>/<仓库名>/main/docker-compose.ghcr.yml
wget https://raw.githubusercontent.com/<你的 GitHub 用户名>/<仓库名>/main/.env.example -O .env
nano .env
```

把 `docker-compose.ghcr.yml` 里的默认镜像改成你的镜像，或者在 `.env` 里加入：

```env
WERSS_INSIGHT_IMAGE=ghcr.io/<你的 GitHub 用户名>/<仓库名>:latest
WERSS_INSIGHT_PORT=8765
```

启动：

```bash
docker compose -f docker-compose.ghcr.yml pull
docker compose -f docker-compose.ghcr.yml up -d
```

更新：

```bash
cd /var/apps/werss-insight
docker compose -f docker-compose.ghcr.yml pull
docker compose -f docker-compose.ghcr.yml up -d
```

## 大模型接口

配置项按 OpenAI-compatible `/chat/completions` 约定：

- `LLM_BASE_URL`：例如 `https://api.openai.com/v1`、自建网关或其他兼容服务。
- `LLM_API_KEY`：模型接口密钥。
- `LLM_MODEL`：模型名称。
- `ALLOW_LLM=false` 时只用本地规则评分和画像。

## 阅读提醒

配置 `NOTIFY_WEBHOOK_URL` 后，计划任务完成时会把评分超过 `NOTIFY_MIN_SCORE` 的前 `NOTIFY_TOP_N` 篇文章发送到该 Webhook。默认关闭提醒。

## 计划任务

默认配置：

- `AUTO_RUN=true`
- `SCHEDULE_DAYS=3`
- `SCHEDULE_TIME=09:00`

到点后会执行完整流程：同步 WeRSS -> 总结新文章 -> 更新作者画像。也可以在面板里手动点击「同步并总结」。

## 数据位置

- SQLite 数据库：`data/werss_insight.db`
- 备份包目录：`data/backups`
- 媒体目录：`data/media`
- 配置、文章摘要、作者画像、阅读状态都在这个库里。

## 迁移与备份

在「配置」页点击「下载备份包」会生成 ZIP，包含：

- `data/werss_insight.db`：数据库快照，包含文章正文、摘要、画像、阅读状态和配置。
- `config.json`：配置导出，方便人工检查。
- `data/media`：媒体目录，用于保存同步时缓存下来的文章图片。

在目标 Docker 实例的「配置」页上传该 ZIP 即可恢复。恢复会覆盖目标实例数据库，导入前请确认目标实例可被替换。

## API

- `GET /api/dashboard`
- `GET /api/stats/daily`
- `GET /api/backup/export`
- `POST /api/backup/import`
- `GET /api/articles`
- `GET /api/articles/{id}`
- `PATCH /api/articles/{id}`
- `GET /api/accounts`
- `GET /api/config`
- `PUT /api/config`
- `POST /api/run/full`
- `POST /api/run/sync`
- `POST /api/run/summarize`
- `GET /api/werss/status`
