import asyncio
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .config import DATA_DIR, ROOT_DIR, public_settings
from .db import init_db
from .service import (
    bootstrap,
    create_backup_archive,
    current_run_status,
    daily_activity,
    dashboard_stats,
    effective_settings,
    full_update,
    get_account,
    get_article,
    ask_knowledge,
    is_run_active,
    knowledge_rebuild_update,
    knowledge_status,
    list_accounts,
    list_articles,
    mark_article,
    prepare_run_status,
    pull_werss_status,
    restore_backup_archive,
    save_settings,
    scheduler_loop,
    summarize_update,
    sync_update,
    test_llm_connection,
)


class ConfigUpdate(BaseModel):
    werss_base_url: str | None = None
    werss_access_key: str | None = None
    werss_secret_key: str | None = None
    llm_base_url: str | None = None
    llm_api_key: str | None = None
    llm_model: str | None = None
    llm_temperature: float | None = None
    llm_timeout_seconds: int | None = None
    schedule_days: int | None = None
    schedule_time: str | None = None
    auto_run: bool | None = None
    sync_limit: int | None = None
    max_article_chars: int | None = None
    allow_llm: bool | None = None
    notify_webhook_url: str | None = None
    notify_min_score: float | None = None
    notify_top_n: int | None = None
    media_cache_mode: str | None = None
    media_max_width: int | None = None
    media_image_quality: int | None = None
    media_prefer_webp: bool | None = None
    rag_enabled: bool | None = None
    rag_embedding_provider: str | None = None
    rag_local_embedding_model: str | None = None
    rag_api_base_url: str | None = None
    rag_api_key: str | None = None
    rag_embedding_model: str | None = None
    rag_chat_model: str | None = None
    rag_chunk_size: int | None = None
    rag_chunk_overlap: int | None = None
    rag_top_k: int | None = None


class ArticlePatch(BaseModel):
    read_status: str | None = None
    favorite: bool | None = None


class LlmTestRequest(BaseModel):
    llm_base_url: str | None = None
    llm_api_key: str | None = None
    llm_model: str | None = None
    llm_temperature: float | None = None
    llm_timeout_seconds: int | None = None
    allow_llm: bool | None = None


class KnowledgeAskRequest(BaseModel):
    question: str
    top_k: int | None = None
    mp_id: str | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    bootstrap()
    task = asyncio.create_task(scheduler_loop())
    try:
        yield
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


app = FastAPI(title="WeRSS Insight", version="0.1.1", lifespan=lifespan)
static_dir = ROOT_DIR / "static"
media_dir = DATA_DIR / "media"
media_dir.mkdir(parents=True, exist_ok=True)
app.mount("/static", StaticFiles(directory=static_dir), name="static")
app.mount("/media", StaticFiles(directory=media_dir), name="media")


@app.get("/")
def index() -> FileResponse:
    return FileResponse(static_dir / "index.html")


@app.get("/api/config")
def get_config() -> dict[str, Any]:
    return public_settings(effective_settings())


@app.put("/api/config")
def update_config(payload: ConfigUpdate) -> dict[str, Any]:
    settings = save_settings(payload.model_dump(exclude_unset=True))
    return public_settings(settings)


@app.post("/api/config/test-llm")
async def config_test_llm(payload: LlmTestRequest) -> dict[str, Any]:
    return await test_llm_connection(payload.model_dump(exclude_unset=True))


@app.get("/api/dashboard")
def dashboard() -> dict[str, Any]:
    return dashboard_stats()


@app.get("/api/stats/daily")
def stats_daily(days: int = Query(14, ge=1, le=90)) -> dict[str, Any]:
    return daily_activity(days=days)


@app.get("/api/run/status")
def run_status() -> dict[str, Any]:
    return current_run_status()


@app.get("/api/backup/export")
def backup_export() -> FileResponse:
    archive_path = create_backup_archive()
    return FileResponse(
        archive_path,
        media_type="application/zip",
        filename=archive_path.name,
    )


@app.post("/api/backup/import")
async def backup_import(file: UploadFile = File(...)) -> dict[str, Any]:
    if not file.filename.lower().endswith(".zip"):
        raise HTTPException(status_code=400, detail="请上传 zip 备份包")
    with tempfile.NamedTemporaryFile(delete=False, suffix=".zip") as tmp:
        tmp_path = Path(tmp.name)
        while chunk := await file.read(1024 * 1024):
            tmp.write(chunk)
    try:
        restored = restore_backup_archive(tmp_path)
        return {"ok": True, "restored": restored}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        tmp_path.unlink(missing_ok=True)


@app.get("/api/werss/status")
async def werss_status() -> dict[str, Any]:
    return await pull_werss_status()


@app.get("/api/accounts")
def accounts() -> list[dict[str, Any]]:
    return list_accounts()


@app.get("/api/accounts/{account_id}")
def account_detail(account_id: str, article_limit: int = Query(80, ge=1, le=200)) -> dict[str, Any]:
    account = get_account(account_id, article_limit=article_limit)
    if not account:
        raise HTTPException(status_code=404, detail="account not found")
    return account


@app.get("/api/articles")
def articles(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    account_id: str | None = None,
    read_status: str | None = None,
    search: str | None = None,
    sort: str = Query("value", pattern="^(value|time)$"),
) -> dict[str, Any]:
    return list_articles(
        limit=limit,
        offset=offset,
        account_id=account_id,
        read_status=read_status,
        search=search,
        sort=sort,
    )


@app.get("/api/articles/{article_id}")
def article_detail(article_id: str) -> dict[str, Any]:
    article = get_article(article_id)
    if not article:
        raise HTTPException(status_code=404, detail="article not found")
    return article


@app.patch("/api/articles/{article_id}")
def patch_article(article_id: str, payload: ArticlePatch) -> dict[str, bool]:
    if payload.read_status and payload.read_status not in {"unread", "read", "skipped"}:
        raise HTTPException(status_code=400, detail="invalid read_status")
    mark_article(article_id, payload.read_status, payload.favorite)
    return {"ok": True}


@app.get("/api/knowledge/status")
def api_knowledge_status() -> dict[str, Any]:
    return knowledge_status()


@app.post("/api/knowledge/rebuild")
async def api_knowledge_rebuild(limit: int | None = None) -> dict[str, Any]:
    return schedule_task(lambda: knowledge_rebuild_update(limit=limit), "knowledge", "已启动知识库索引")


@app.post("/api/knowledge/ask")
async def api_knowledge_ask(payload: KnowledgeAskRequest) -> dict[str, Any]:
    try:
        return await ask_knowledge(payload.question, top_k=payload.top_k, mp_id=payload.mp_id)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def schedule_task(factory, run_type: str, message: str) -> dict[str, str]:
    if is_run_active():
        return {"status": "busy", "message": "已有更新任务正在运行"}
    prepare_run_status(run_type, message)
    asyncio.create_task(factory())
    return {"status": "started", "message": message}


@app.post("/api/run/sync")
async def run_sync(limit: int | None = None) -> dict[str, Any]:
    return schedule_task(lambda: sync_update(limit=limit), "sync", "已启动同步")


@app.post("/api/run/summarize")
async def run_summarize(limit: int | None = Query(None, ge=0, le=10000)) -> dict[str, Any]:
    return schedule_task(lambda: summarize_update(limit=limit), "summarize", "已启动总结")


@app.post("/api/run/full")
async def run_full(limit: int | None = None, summarize_limit: int | None = Query(None, ge=0, le=10000)) -> dict[str, Any]:
    return schedule_task(
        lambda: full_update(limit=limit, summarize_limit=summarize_limit),
        "full_update",
        "已启动同步并总结",
    )


@app.post("/api/init")
def initialize_db() -> dict[str, bool]:
    init_db()
    return {"ok": True}
