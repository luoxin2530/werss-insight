# WeRSS Insight V0.1.1

WeRSS Insight 是一个配合 [WeRSS](https://github.com/rachelos/we-mp-rss) 使用的微信公众号文章阅读、总结与知识库面板。它从 WeRSS API 同步公众号文章，用 OpenAI-compatible 模型生成摘要、评分和作者画像，并支持按文章库进行主题问答、图片缓存、阅读管理和数据迁移。

推荐架构：WeRSS 负责公众号授权、订阅和文章抓取；WeRSS Insight 负责阅读、摘要、作者画像、知识库、媒体缓存、备份和提醒。两个容器独立运行，通过 API 协作。

## V0.1.1 更新

- 默认内置本地免费向量模型 `BAAI/bge-small-zh-v1.5`，Docker 镜像构建时预下载到 `/app/bundled_models/fastembed`，部署后无需首次联网下载模型。
- 保留远程 OpenAI-compatible embedding 配置，`RAG_EMBEDDING_PROVIDER=remote` 时可继续使用 DashScope、OpenAI 或其他供应商。
- 知识库问答模型 `RAG_CHAT_MODEL` 变为可选，留空时复用摘要大模型的接口、Key 和模型名。
- 修复 DashScope embedding 批量请求过大导致 400 的问题，远程 DashScope 会自动按 10 条分批。
- 配置页补充向量来源、本地模型、远程模型和问答模型说明，减少误填。

## 当前功能

- 同步 WeRSS 的公众号、文章、正文状态和运行状态。
- 手动总结和计划总结默认处理全部待总结文章，不再隐藏限制为 30 篇。
- 使用通用中文阅读助手口径生成摘要、要点、阅读价值、标签、难度和 1-10 分评分。
- 为公众号生成作者画像，包括能力判断、擅长方向、使用限制、标签、评分和置信度。
- 作者卡片可点击查看该公众号的文章列表，文章标题可继续进入正文阅读。
- 保留正文图片和基础排版，并支持本地图片压缩缓存。
- 支持三种图片模式：`optimized_local`、`remote`、`off`。
- 支持知识库问答：文章切块、内置本地 FastEmbed 向量模型、可选远程 embedding、向量检索、LLM 综合回答和引用来源展示。
- 配置页支持模型连接测试，显示连接结果、HTTP 状态、延迟和 token。
- 配置页提供近况看板，按天统计新增文章、摘要数量和 token 消耗。
- 支持 ZIP 备份和恢复，包含数据库、配置和媒体缓存目录，便于 Docker 实例迁移。
- 支持定时同步、阅读状态、收藏和 Webhook 高分文章提醒。
- 未配置模型时仍可用本地规则摘要兜底，方便先部署后配置。

## 快速部署

### 使用预构建镜像

推荐在服务器上把项目放到你的数据目录，例如：

```bash
mkdir -p /vol1/1000/werss-insight
cd /vol1/1000/werss-insight
git clone https://github.com/luoxin2530/werss-insight.git .
cp .env.example .env
docker compose -f docker-compose.ghcr.yml up -d
```

访问：

```text
http://服务器IP:8765
```

可以先不填写 key。保持 `.env` 里的 key 为空，容器仍能启动；打开页面后在「配置」里填写 WeRSS 和模型接口，配置会保存到 `data/werss_insight.db`。

如果要固定使用你的服务器路径，可以在 `.env` 里设置：

```env
WERSS_INSIGHT_DATA_DIR=/vol1/1000/werss-insight/data
```

### 从源码构建

```bash
git clone https://github.com/luoxin2530/werss-insight.git
cd werss-insight
cp .env.example .env
docker compose up -d --build
```

### 本地开发运行

```powershell
git clone https://github.com/luoxin2530/werss-insight.git
cd werss-insight
Copy-Item .env.example .env
python -m pip install -r requirements.txt
.\run.ps1
```

打开：

```text
http://localhost:8765
```

## 关键配置

### WeRSS

如果 WeRSS 和 Insight 在同一个 Docker network，可以使用容器名：

```env
WERSS_BASE_URL=http://we-mp-rss:8001
WERSS_ACCESS_KEY=
WERSS_SECRET_KEY=
SYNC_LIMIT=100
```

如果 WeRSS 已经通过局域网端口暴露，例如 `http://192.168.68.100:8011`，就把 `WERSS_BASE_URL` 改成对应地址。

`SYNC_LIMIT=0` 表示同步时不限制文章数量。

### 摘要模型

使用 OpenAI-compatible `/chat/completions` 接口：

```env
ALLOW_LLM=true
LLM_BASE_URL=https://api.openai.com/v1
LLM_API_KEY=
LLM_MODEL=gpt-4o-mini
LLM_TEMPERATURE=0.2
LLM_TIMEOUT_SECONDS=120
MAX_ARTICLE_CHARS=12000
```

未配置模型时，会使用本地规则摘要和画像，流程不会阻塞。配置页可以测试连接并显示延迟。

### 知识库问答

知识库默认使用镜像内置的本地 FastEmbed 向量模型，也可以切换为远程 embedding API：

```env
RAG_ENABLED=true
RAG_EMBEDDING_PROVIDER=local
RAG_LOCAL_EMBEDDING_MODEL=BAAI/bge-small-zh-v1.5
RAG_API_BASE_URL=
RAG_API_KEY=
RAG_EMBEDDING_MODEL=text-embedding-3-small
RAG_CHAT_MODEL=
RAG_CHUNK_SIZE=900
RAG_CHUNK_OVERLAP=140
RAG_TOP_K=8
```

默认 `RAG_EMBEDDING_PROVIDER=local`，使用镜像内置的本地 FastEmbed 向量模型，不需要填写向量接口和向量 API Key。预构建镜像已经包含默认模型；如果从源码构建，Dockerfile 会在构建阶段下载模型。

如果要使用 DashScope、OpenAI 或其他 OpenAI-compatible 向量接口，把 `RAG_EMBEDDING_PROVIDER` 改成 `remote`，再填写 `RAG_API_BASE_URL`、`RAG_API_KEY` 和 `RAG_EMBEDDING_MODEL`。

`RAG_CHAT_MODEL` 是知识库问答的聊天模型名，不是接口地址，也不是 Key。留空时会直接复用“摘要大模型”里的 `LLM_BASE_URL`、`LLM_API_KEY` 和 `LLM_MODEL`；只有想让知识库问答使用另一个模型名时才需要填写。

使用步骤：

1. 在配置页填写知识库模型参数。
2. 打开「知识库」页。
3. 点击「重建索引」。
4. 索引完成后开始提问。

### 图片缓存

```env
MEDIA_CACHE_MODE=optimized_local
MEDIA_MAX_WIDTH=1800
MEDIA_IMAGE_QUALITY=85
MEDIA_PREFER_WEBP=true
```

模式说明：

- `optimized_local`：默认模式，下载图片、压缩、按公众号和文章保存。
- `remote`：不下载图片，只保留远程引用，最省空间但长期稳定性较弱。
- `off`：不缓存图片。

### 计划任务和提醒

```env
AUTO_RUN=true
SCHEDULE_DAYS=3
SCHEDULE_TIME=09:00
NOTIFY_WEBHOOK_URL=
NOTIFY_MIN_SCORE=7.5
NOTIFY_TOP_N=8
```

计划任务会执行完整流程：同步 WeRSS、总结未处理文章、更新作者画像、发送可选提醒。

## 数据目录

```text
data/
├─ werss_insight.db       # SQLite 数据库
├─ backups/               # ZIP 备份包
├─ models/fastembed/      # 自定义或 fallback 的本地向量模型缓存
└─ media/
   └─ accounts/           # 优化后的文章图片缓存
```

数据库保存配置、文章、摘要、画像、阅读状态、知识库片段和向量。图片默认不存入数据库，而是保存在 `data/media`。预构建镜像内置默认向量模型；如果切换其他本地模型，会缓存到 `data/models/fastembed`，重建容器后不会重复下载。

## 备份与迁移

在「配置」页点击「下载备份包」会生成 ZIP，包含：

- SQLite 数据库快照
- 配置导出
- 媒体缓存目录

在目标实例上传 ZIP 即可恢复。恢复会覆盖目标数据库，导入前请确认目标实例是空库或可以被替换。

## API

常用接口：

```text
GET  /api/dashboard
GET  /api/stats/daily
GET  /api/run/status
POST /api/run/full
POST /api/run/sync
POST /api/run/summarize

GET  /api/articles
GET  /api/articles/{id}
PATCH /api/articles/{id}

GET  /api/accounts
GET  /api/accounts/{id}

GET  /api/knowledge/status
POST /api/knowledge/rebuild
POST /api/knowledge/ask

GET  /api/config
PUT  /api/config
POST /api/config/test-llm

GET  /api/backup/export
POST /api/backup/import
```

## 发布镜像

GitHub Actions 会在推送 `main` 或 `v*.*.*` 标签时构建并发布镜像：

```text
ghcr.io/luoxin2530/werss-insight:latest
ghcr.io/luoxin2530/werss-insight:v0.1.1
```

## 安全提醒

- 不要把 `.env`、真实 API key、数据库、备份包或媒体缓存提交到 GitHub。
- 建议把数据目录挂载到宿主机，例如 `/vol1/1000/werss-insight/data:/app/data`。
- GitHub token 建议只给最小必要权限，发布后可按需轮换。
