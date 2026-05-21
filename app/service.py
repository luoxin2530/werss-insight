import asyncio
import hashlib
import re
import shutil
import sqlite3
import tempfile
import zipfile
from dataclasses import asdict
from io import BytesIO
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx
from PIL import Image, ImageOps, UnidentifiedImageError

from .config import DATA_DIR, DB_PATH, Settings, get_settings, settings_from_mapping
from .db import connect, get_setting, init_db, json_dumps, json_loads, set_setting, utc_now
from .llm import (
    EmbeddingClient,
    LlmClient,
    article_outline,
    cosine_similarity,
    heuristic_article_summary,
    heuristic_profile,
    split_text_for_rag,
    strip_html,
)
from .werss_client import WeRssClient


RUN_LOCK = asyncio.Lock()
CURRENT_RUN: dict[str, Any] = {
    "running": False,
    "status": "idle",
    "run_type": None,
    "run_id": None,
    "stage": "空闲",
    "message": "等待任务",
    "started_at": None,
    "updated_at": None,
    "finished_at": None,
    "progress": None,
    "stats": {},
    "error": "",
}
TZ = ZoneInfo("Asia/Hong_Kong")


def is_run_active() -> bool:
    return RUN_LOCK.locked() or bool(CURRENT_RUN.get("running"))


def prepare_run_status(run_type: str, message: str = "任务已启动，等待执行") -> None:
    now = utc_now()
    CURRENT_RUN.clear()
    CURRENT_RUN.update(
        {
            "running": True,
            "status": "queued",
            "run_type": run_type,
            "run_id": None,
            "stage": "排队",
            "message": message,
            "started_at": now,
            "updated_at": now,
            "finished_at": None,
            "progress": 0,
            "stats": {},
            "error": "",
        }
    )


def start_current_run(run_type: str, run_id: int | None = None, message: str = "任务开始执行") -> None:
    now = utc_now()
    CURRENT_RUN.clear()
    CURRENT_RUN.update(
        {
            "running": True,
            "status": "running",
            "run_type": run_type,
            "run_id": run_id,
            "stage": "启动",
            "message": message,
            "started_at": now,
            "updated_at": now,
            "finished_at": None,
            "progress": 0,
            "stats": {},
            "error": "",
        }
    )


def update_run_progress(
    stage: str | None = None,
    message: str | None = None,
    stats: dict[str, Any] | None = None,
    progress: float | None = None,
) -> None:
    if not CURRENT_RUN:
        return
    if stage is not None:
        CURRENT_RUN["stage"] = stage
    if message is not None:
        CURRENT_RUN["message"] = message
    if stats is not None:
        CURRENT_RUN["stats"] = dict(stats)
    if progress is not None:
        CURRENT_RUN["progress"] = max(0, min(100, round(float(progress), 1)))
    CURRENT_RUN["updated_at"] = utc_now()


def complete_current_run(status: str, message: str = "", stats: dict[str, Any] | None = None) -> None:
    now = utc_now()
    CURRENT_RUN["running"] = False
    CURRENT_RUN["status"] = status
    CURRENT_RUN["stage"] = "完成" if status == "success" else "失败"
    CURRENT_RUN["message"] = message
    CURRENT_RUN["finished_at"] = now
    CURRENT_RUN["updated_at"] = now
    CURRENT_RUN["progress"] = 100 if status == "success" else CURRENT_RUN.get("progress")
    if stats is not None:
        CURRENT_RUN["stats"] = dict(stats)
    if status != "success":
        CURRENT_RUN["error"] = message


def latest_run() -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 1").fetchone()
    return hydrate_run(row) if row else None


def current_run_status() -> dict[str, Any]:
    latest = latest_run()
    data = dict(CURRENT_RUN)
    data["running"] = is_run_active()
    if not data.get("run_type") and latest:
        data.update(
            {
                "status": latest.get("status") or "idle",
                "run_type": latest.get("run_type"),
                "run_id": latest.get("id"),
                "stage": "最近结果",
                "message": latest.get("message") or "",
                "started_at": latest.get("started_at"),
                "finished_at": latest.get("finished_at"),
                "updated_at": latest.get("finished_at") or latest.get("started_at"),
                "progress": 100 if latest.get("status") == "success" else None,
                "stats": latest.get("stats") or {},
                "error": latest.get("message") if latest.get("status") == "failed" else "",
            }
        )
    data["latest_run"] = latest
    return data


def media_dir() -> Path:
    path = DATA_DIR / "media"
    path.mkdir(parents=True, exist_ok=True)
    return path


def media_public_path(relative_path: str) -> str:
    normalized = relative_path.replace("\\", "/").lstrip("/")
    if normalized.startswith("media/"):
        normalized = normalized[len("media/") :]
    return f"/media/{normalized}"


def extract_image_urls(raw_html: str) -> list[str]:
    if not raw_html:
        return []
    urls: list[str] = []
    for tag in re.findall(r"<img\b[^>]*>", raw_html, flags=re.I):
        src = ""
        for attr in ("src", "data-src", "data-original", "data-url"):
            found = re.search(rf'{attr}\s*=\s*"([^"]+)"', tag, flags=re.I)
            if not found:
                found = re.search(rf"{attr}\s*=\s*'([^']+)'", tag, flags=re.I)
            if found:
                src = found.group(1)
                break
        if src.startswith("//"):
            src = f"https:{src}"
        if re.match(r"^https?://", src, flags=re.I) and src not in urls:
            urls.append(src)
    return urls


def media_extension(content_type: str, url: str) -> str:
    mapping = {
        "image/jpeg": ".jpg",
        "image/jpg": ".jpg",
        "image/png": ".png",
        "image/gif": ".gif",
        "image/webp": ".webp",
        "image/svg+xml": ".svg",
    }
    if content_type in mapping:
        return mapping[content_type]
    suffix = Path(url.split("?", 1)[0]).suffix.lower()
    return suffix if suffix in {".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg"} else ".img"


def content_type_from_path(path: str, fallback: str = "image/jpeg") -> str:
    suffix = Path(path).suffix.lower()
    mapping = {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".gif": "image/gif",
        ".webp": "image/webp",
        ".svg": "image/svg+xml",
    }
    return mapping.get(suffix, fallback)


def safe_path_segment(value: str | None, fallback: str = "unknown") -> str:
    text = str(value or "").strip()
    if not text:
        text = fallback
    text = re.sub(r"[^\w.-]+", "_", text, flags=re.UNICODE).strip("._")
    return text[:96] or fallback


def media_target_dir(mp_id: str | None, article_id: str) -> Path:
    return media_dir() / "accounts" / safe_path_segment(mp_id, "unknown_account") / safe_path_segment(article_id, "unknown_article")


def media_filename(index: int, url: str, suffix: str) -> str:
    return f"{index:03d}-{hashlib.sha256(url.encode('utf-8')).hexdigest()[:12]}{suffix}"


def optimize_image_bytes(
    content: bytes,
    content_type: str,
    settings: Settings,
) -> tuple[bytes, str, str, bool]:
    if content_type in {"image/gif", "image/svg+xml"}:
        return content, content_type, media_extension(content_type, ""), False

    try:
        with Image.open(BytesIO(content)) as image:
            image = ImageOps.exif_transpose(image)
            if settings.media_max_width > 0 and image.width > settings.media_max_width:
                ratio = settings.media_max_width / image.width
                target_height = max(1, round(image.height * ratio))
                image = image.resize((settings.media_max_width, target_height), Image.LANCZOS)

            has_alpha = image.mode in {"RGBA", "LA"} or (
                image.mode == "P" and "transparency" in image.info
            )
            quality = max(50, min(95, int(settings.media_image_quality)))
            candidates: list[tuple[bytes, str, str]] = []

            if settings.media_prefer_webp and content_type != "image/svg+xml":
                output = BytesIO()
                webp_image = image.convert("RGBA" if has_alpha else "RGB")
                webp_image.save(output, format="WEBP", quality=quality, method=6)
                candidates.append((output.getvalue(), "image/webp", ".webp"))

            if has_alpha or content_type == "image/png":
                output = BytesIO()
                png_image = image.convert("RGBA" if has_alpha else "RGB")
                png_image.save(output, format="PNG", optimize=True)
                candidates.append((output.getvalue(), "image/png", ".png"))
            else:
                output = BytesIO()
                jpeg_image = image.convert("RGB")
                jpeg_image.save(output, format="JPEG", quality=quality, optimize=True, progressive=True)
                candidates.append((output.getvalue(), "image/jpeg", ".jpg"))

            best_content, best_type, best_suffix = min(candidates, key=lambda item: len(item[0]))
            if len(best_content) < len(content):
                return best_content, best_type, best_suffix, True
            return content, content_type, media_extension(content_type, ""), False
    except (UnidentifiedImageError, OSError):
        return content, content_type, media_extension(content_type, ""), False


def migrate_cached_media(
    article_id: str,
    mp_id: str | None,
    index: int,
    url: str,
    row: Any,
    settings: Settings,
) -> dict[str, Any] | None:
    local_path = str(row["local_path"] or "")
    source = DATA_DIR / local_path
    if not local_path or not source.exists():
        return None

    already_organized = local_path.replace("\\", "/").startswith("media/accounts/")
    already_optimized = bool(row["optimized"]) if "optimized" in row.keys() else False
    if already_organized and already_optimized:
        return {"stored_bytes": source.stat().st_size, "optimized": False, "migrated": False}

    content_type = str(row["content_type"] or content_type_from_path(local_path))
    original = source.read_bytes()
    stored, stored_type, suffix, optimized = optimize_image_bytes(original, content_type, settings)
    target_dir = media_target_dir(mp_id, article_id)
    target_dir.mkdir(parents=True, exist_ok=True)
    destination = target_dir / media_filename(index, url, suffix)
    destination.write_bytes(stored)
    relative = destination.relative_to(DATA_DIR).as_posix()

    with connect() as conn:
        conn.execute(
            """
            UPDATE media_assets
            SET mp_id=?, local_path=?, content_type=?, bytes=?, original_bytes=?,
                optimized=?, stored_format=?, status='cached', error='', cached_at=?
            WHERE article_id=? AND source_url=?
            """,
            (
                mp_id,
                relative,
                stored_type,
                len(stored),
                len(original),
                1 if optimized else 0,
                suffix.lstrip("."),
                utc_now(),
                article_id,
                url,
            ),
        )
        still_used = conn.execute(
            "SELECT COUNT(*) AS c FROM media_assets WHERE local_path=?",
            (local_path,),
        ).fetchone()["c"]
    if still_used == 0 and source != destination:
        source.unlink(missing_ok=True)

    return {
        "stored_bytes": len(stored),
        "original_bytes": len(original),
        "optimized": optimized,
        "migrated": source != destination,
    }


def cached_media_map(article_id: str) -> dict[str, str]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT source_url, local_path FROM media_assets WHERE article_id=? AND status='cached' AND local_path IS NOT NULL",
            (article_id,),
        ).fetchall()
    return {row["source_url"]: media_public_path(row["local_path"]) for row in rows}


async def cache_article_images(article_id: str, mp_id: str | None, raw_html: str) -> dict[str, Any]:
    settings = effective_settings()
    mode = (settings.media_cache_mode or "optimized_local").strip().lower()
    urls = extract_image_urls(raw_html)
    stats = {
        "images_seen": len(urls),
        "images_cached": 0,
        "image_errors": 0,
        "image_original_bytes": 0,
        "image_stored_bytes": 0,
        "images_optimized": 0,
        "media_cache_mode": mode,
    }
    if not urls or mode in {"off", "none", "disabled"}:
        return stats
    if mode in {"remote", "passthrough", "external"}:
        with connect() as conn:
            for url in urls:
                conn.execute(
                    """
                    INSERT INTO media_assets(article_id, mp_id, source_url, status, error, cached_at)
                    VALUES (?, ?, ?, 'remote', '', ?)
                    ON CONFLICT(article_id, source_url) DO UPDATE SET
                        mp_id=excluded.mp_id,
                        status='remote',
                        error='',
                        cached_at=excluded.cached_at
                    """,
                    (article_id, mp_id, url, utc_now()),
                )
        return stats

    target_dir = media_target_dir(mp_id, article_id)
    target_dir.mkdir(parents=True, exist_ok=True)
    async with httpx.AsyncClient(timeout=20, follow_redirects=True, trust_env=False) as client:
        for index, url in enumerate(urls, 1):
            with connect() as conn:
                row = conn.execute(
                    "SELECT * FROM media_assets WHERE article_id=? AND source_url=?",
                    (article_id, url),
                ).fetchone()
            if row and row["status"] == "cached" and row["local_path"] and (DATA_DIR / row["local_path"]).exists():
                migrated = migrate_cached_media(article_id, mp_id, index, url, row, settings)
                if migrated:
                    stats["image_stored_bytes"] += int(migrated.get("stored_bytes") or 0)
                    stats["image_original_bytes"] += int(migrated.get("original_bytes") or 0)
                    if migrated.get("optimized"):
                        stats["images_optimized"] += 1
                stats["images_cached"] += 1
                continue
            try:
                response = await client.get(url, headers={"Referer": "https://mp.weixin.qq.com/"})
                response.raise_for_status()
                content_type = response.headers.get("content-type", "").split(";", 1)[0].lower()
                if not content_type.startswith("image/"):
                    raise RuntimeError(f"not an image: {content_type or 'unknown'}")
                original = response.content
                stats["image_original_bytes"] += len(original)
                stored, stored_type, suffix, optimized = optimize_image_bytes(original, content_type, settings)
                filename = media_filename(index, url, suffix)
                destination = target_dir / filename
                destination.write_bytes(stored)
                relative = destination.relative_to(DATA_DIR).as_posix()
                with connect() as conn:
                    conn.execute(
                        """
                        INSERT INTO media_assets(
                            article_id, mp_id, source_url, local_path, content_type, bytes,
                            original_bytes, optimized, stored_format, status, error, cached_at
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'cached', '', ?)
                        ON CONFLICT(article_id, source_url) DO UPDATE SET
                            mp_id=excluded.mp_id,
                            local_path=excluded.local_path,
                            content_type=excluded.content_type,
                            bytes=excluded.bytes,
                            original_bytes=excluded.original_bytes,
                            optimized=excluded.optimized,
                            stored_format=excluded.stored_format,
                            status='cached',
                            error='',
                            cached_at=excluded.cached_at
                        """,
                        (
                            article_id,
                            mp_id,
                            url,
                            relative,
                            stored_type,
                            len(stored),
                            len(original),
                            1 if optimized else 0,
                            suffix.lstrip("."),
                            utc_now(),
                        ),
                    )
                stats["images_cached"] += 1
                stats["image_stored_bytes"] += len(stored)
                if optimized:
                    stats["images_optimized"] += 1
            except Exception as exc:
                with connect() as conn:
                    conn.execute(
                        """
                        INSERT INTO media_assets(article_id, mp_id, source_url, status, error, cached_at)
                        VALUES (?, ?, ?, 'failed', ?, ?)
                        ON CONFLICT(article_id, source_url) DO UPDATE SET
                            mp_id=excluded.mp_id,
                            status='failed',
                            error=excluded.error,
                            cached_at=excluded.cached_at
                        """,
                        (article_id, mp_id, url, str(exc), utc_now()),
                    )
                stats["image_errors"] += 1
    return stats


def snapshot_database(destination: Path) -> None:
    source = sqlite3.connect(DB_PATH)
    target = sqlite3.connect(destination)
    try:
        source.backup(target)
    finally:
        target.close()
        source.close()


def create_backup_archive() -> Path:
    init_db()
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    backup_dir = DATA_DIR / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(TZ).strftime("%Y%m%d-%H%M%S")
    archive_path = backup_dir / f"werss-insight-backup-{stamp}.zip"
    manifest = {
        "app": "werss-insight",
        "version": 1,
        "created_at": utc_now(),
        "contains": ["database", "config", "media"],
    }

    with tempfile.TemporaryDirectory() as temp_name:
        temp_dir = Path(temp_name)
        db_copy = temp_dir / "werss_insight.db"
        snapshot_database(db_copy)
        (temp_dir / "manifest.json").write_text(json_dumps(manifest), encoding="utf-8")
        (temp_dir / "config.json").write_text(json_dumps(get_setting("config", {})), encoding="utf-8")
        media = media_dir()
        with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.write(temp_dir / "manifest.json", "manifest.json")
            zf.write(temp_dir / "config.json", "config.json")
            zf.write(db_copy, "data/werss_insight.db")
            for item in media.rglob("*"):
                if item.is_file():
                    zf.write(item, str(Path("data/media") / item.relative_to(media)))
    return archive_path


def restore_backup_archive(archive_path: Path) -> dict[str, Any]:
    if is_run_active():
        raise RuntimeError("有任务正在运行，恢复前请等待任务完成")
    restored: dict[str, Any] = {"database": False, "config": False, "media_files": 0}
    with tempfile.TemporaryDirectory() as temp_name:
        temp_dir = Path(temp_name)
        with zipfile.ZipFile(archive_path) as zf:
            names = set(zf.namelist())
            if "manifest.json" not in names or "data/werss_insight.db" not in names:
                raise RuntimeError("备份包格式不正确")
            zf.extractall(temp_dir)

        backup_dir = DATA_DIR / "backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        if DB_PATH.exists():
            safety_copy = backup_dir / f"pre-restore-{datetime.now(TZ).strftime('%Y%m%d-%H%M%S')}.db"
            snapshot_database(safety_copy)
            restored["previous_database_backup"] = str(safety_copy)

        shutil.copy2(temp_dir / "data" / "werss_insight.db", DB_PATH)
        for suffix in ("-wal", "-shm"):
            stale_path = Path(str(DB_PATH) + suffix)
            if stale_path.exists():
                stale_path.unlink()
        restored["database"] = True

        config_path = temp_dir / "config.json"
        if config_path.exists():
            config = json_loads(config_path.read_text(encoding="utf-8"), {})
            if isinstance(config, dict):
                set_setting("config", config)
                restored["config"] = True

        media_source = temp_dir / "data" / "media"
        if media_source.exists():
            target = media_dir()
            for item in media_source.rglob("*"):
                if item.is_file():
                    destination = target / item.relative_to(media_source)
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(item, destination)
                    restored["media_files"] += 1

    init_db()
    return restored


def effective_settings() -> Settings:
    overrides = get_setting("config", {})
    return settings_from_mapping(overrides or {})


def save_settings(update: dict[str, Any]) -> Settings:
    current = asdict(effective_settings())
    for key, value in update.items():
        if key not in current:
            continue
        if value in (None, "") and key.endswith("_key"):
            continue
        if key in {
            "schedule_days",
            "sync_limit",
            "max_article_chars",
            "llm_timeout_seconds",
            "media_max_width",
            "media_image_quality",
            "rag_chunk_size",
            "rag_chunk_overlap",
            "rag_top_k",
        }:
            value = int(value)
        if key == "llm_temperature":
            value = float(value)
        if key == "notify_min_score":
            value = float(value)
        if key == "notify_top_n":
            value = int(value)
        if key in {"auto_run", "allow_llm", "media_prefer_webp", "rag_enabled"}:
            value = bool(value)
        if key == "rag_chunk_size":
            value = max(200, min(3000, int(value)))
        if key == "rag_chunk_overlap":
            value = max(0, min(800, int(value)))
        if key == "rag_top_k":
            value = max(1, min(30, int(value)))
        if key == "media_cache_mode":
            value = str(value or "optimized_local").strip().lower()
            if value not in {"optimized_local", "remote", "off"}:
                value = "optimized_local"
        if key == "media_image_quality":
            value = max(50, min(95, int(value)))
        if key == "media_max_width":
            value = max(0, min(6000, int(value)))
        if key in {"werss_base_url", "llm_base_url", "rag_api_base_url"} and value:
            value = str(value).rstrip("/")
        current[key] = value
    set_setting("config", current)
    return settings_from_mapping(current)


def start_run(run_type: str) -> int:
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO runs(run_type, status, started_at) VALUES (?, ?, ?)",
            (run_type, "running", utc_now()),
        )
        return int(cur.lastrowid)


def finish_run(run_id: int, status: str, message: str = "", stats: dict[str, Any] | None = None) -> None:
    with connect() as conn:
        conn.execute(
            """
            UPDATE runs
            SET status=?, finished_at=?, message=?, stats_json=?
            WHERE id=?
            """,
            (status, utc_now(), message, json_dumps(stats or {}), run_id),
        )


def article_hash(article: dict[str, Any]) -> str:
    text = "\n".join(
        [
            str(article.get("title") or ""),
            str(article.get("description") or ""),
            str(article.get("content") or article.get("content_html") or ""),
        ]
    )
    return hashlib.sha256(text.encode("utf-8", errors="ignore")).hexdigest()


def chunk_hash(text: str) -> str:
    compact = re.sub(r"\s+", " ", text).strip()
    return hashlib.sha256(compact.encode("utf-8", errors="ignore")).hexdigest()


def article_rag_text(article: dict[str, Any]) -> str:
    parts = [
        str(article.get("title") or ""),
        str(article.get("description") or ""),
        str(article.get("content") or article.get("content_html") or ""),
    ]
    summary = article.get("summary_json")
    if isinstance(summary, str) and summary:
        parts.append(summary)
    elif isinstance(summary, dict):
        parts.append(json_dumps(summary))
    return "\n\n".join(part for part in parts if part.strip())


def normalize_publish_time(value: Any) -> int | None:
    try:
        if value is None:
            return None
        ts = int(value)
        if ts > 10_000_000_000:
            ts = int(ts / 1000)
        return ts
    except Exception:
        return None


def content_available(article: dict[str, Any]) -> bool:
    text = strip_html(str(article.get("content") or article.get("content_html") or ""))
    return len(text) >= 80


def upsert_account(account: dict[str, Any]) -> None:
    with connect() as conn:
        row = conn.execute(
            "SELECT profile_json, score, confidence, tags_json FROM accounts WHERE id=?",
            (account.get("id"),),
        ).fetchone()
        now = utc_now()
        conn.execute(
            """
            INSERT INTO accounts(
                id, mp_name, mp_intro, mp_cover, status, profile_json, score,
                confidence, tags_json, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                mp_name=excluded.mp_name,
                mp_intro=excluded.mp_intro,
                mp_cover=excluded.mp_cover,
                status=excluded.status,
                updated_at=excluded.updated_at
            """,
            (
                account.get("id"),
                account.get("mp_name") or "Unknown",
                account.get("mp_intro") or "",
                account.get("mp_cover") or "",
                account.get("status"),
                row["profile_json"] if row else None,
                row["score"] if row else 0,
                row["confidence"] if row else "",
                row["tags_json"] if row else "[]",
                now,
            ),
        )


def upsert_article(article: dict[str, Any]) -> bool:
    publish_time = normalize_publish_time(article.get("publish_time"))
    content = str(article.get("content") or "")
    content_html = str(article.get("content_html") or "")
    content_text = strip_html(content or content_html)
    digest = article_hash(article)
    now = utc_now()
    with connect() as conn:
        row = conn.execute("SELECT content_hash, created_at FROM articles WHERE id=?", (article.get("id"),)).fetchone()
        changed = (row is None) or (row["content_hash"] != digest)
        conn.execute(
            """
            INSERT INTO articles(
                id, mp_id, mp_name, title, url, description, content, content_html,
                pic_url, publish_time, has_content, status, source_updated_at,
                content_chars, content_hash, created_at, synced_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                mp_id=excluded.mp_id,
                mp_name=excluded.mp_name,
                title=excluded.title,
                url=excluded.url,
                description=excluded.description,
                content=excluded.content,
                content_html=excluded.content_html,
                pic_url=excluded.pic_url,
                publish_time=excluded.publish_time,
                has_content=excluded.has_content,
                status=excluded.status,
                source_updated_at=excluded.source_updated_at,
                content_chars=excluded.content_chars,
                content_hash=excluded.content_hash,
                created_at=COALESCE(articles.created_at, excluded.created_at),
                synced_at=excluded.synced_at,
                summary_json=CASE
                    WHEN COALESCE(articles.content_hash, '') != COALESCE(excluded.content_hash, '')
                    THEN NULL ELSE articles.summary_json END,
                value_score=CASE
                    WHEN COALESCE(articles.content_hash, '') != COALESCE(excluded.content_hash, '')
                    THEN 0 ELSE articles.value_score END,
                tags_json=CASE
                    WHEN COALESCE(articles.content_hash, '') != COALESCE(excluded.content_hash, '')
                    THEN '[]' ELSE articles.tags_json END,
                summarized_at=CASE
                    WHEN COALESCE(articles.content_hash, '') != COALESCE(excluded.content_hash, '')
                    THEN NULL ELSE articles.summarized_at END
            """,
            (
                article.get("id"),
                article.get("mp_id"),
                article.get("mp_name") or "",
                article.get("title") or "Untitled",
                article.get("url") or "",
                article.get("description") or "",
                content,
                content_html,
                article.get("pic_url") or "",
                publish_time,
                int(article.get("has_content") or content_available(article)),
                article.get("status"),
                article.get("updated_at") or article.get("updated_at_millis"),
                len(content_text),
                digest,
                row["created_at"] if row and row["created_at"] else now,
                now,
            ),
        )
    return changed


async def sync_from_werss(limit: int | None = None) -> dict[str, Any]:
    settings = effective_settings()
    client = WeRssClient(settings)
    max_items = int(limit if limit is not None else settings.sync_limit)
    unlimited = max_items <= 0
    stats = {
        "accounts": 0,
        "articles_seen": 0,
        "details_loaded": 0,
        "changed": 0,
        "errors": 0,
        "images_seen": 0,
        "images_cached": 0,
        "image_errors": 0,
        "limit": None if unlimited else max_items,
        "total_available": None,
    }
    update_run_progress("同步账号", "正在读取公众号列表", stats, 0)

    accounts = await client.get_accounts(limit=100)
    for account in accounts:
        upsert_account(account)
    stats["accounts"] = len(accounts)
    update_run_progress("同步文章", f"已读取 {len(accounts)} 个公众号，开始同步文章", stats, 5)

    offset = 0
    page_size = 100 if unlimited else max(1, min(100, max_items))
    while unlimited or stats["articles_seen"] < max_items:
        articles, total = await client.get_articles(limit=page_size, offset=offset)
        stats["total_available"] = total
        expected = total if unlimited else min(max_items, total)
        update_run_progress(
            "同步文章",
            f"正在拉取文章列表：已看到 {stats['articles_seen']} / {expected}",
            stats,
            5 if not expected else 5 + (stats["articles_seen"] / max(expected, 1)) * 90,
        )
        if not articles:
            break
        for article in articles:
            if not unlimited and stats["articles_seen"] >= max_items:
                break
            stats["articles_seen"] += 1
            detail = dict(article)
            try:
                loaded = await client.get_article_detail(str(article["id"]))
                detail.update(loaded)
                stats["details_loaded"] += 1
            except Exception:
                stats["errors"] += 1
            if upsert_article(detail):
                stats["changed"] += 1
            image_stats = await cache_article_images(
                str(article["id"]),
                str(detail.get("mp_id") or article.get("mp_id") or ""),
                str(detail.get("content_html") or detail.get("content") or ""),
            )
            stats["images_seen"] += image_stats["images_seen"]
            stats["images_cached"] += image_stats["images_cached"]
            stats["image_errors"] += image_stats["image_errors"]
            expected = stats["total_available"] if unlimited else min(max_items, stats["total_available"] or max_items)
            update_run_progress(
                "同步文章",
                (
                    f"已处理 {stats['articles_seen']} / {expected} 篇，"
                    f"详情 {stats['details_loaded']}，更新 {stats['changed']}，"
                    f"图片 {stats['images_cached']}，错误 {stats['errors'] + stats['image_errors']}"
                ),
                stats,
                5 + (stats["articles_seen"] / max(expected or stats["articles_seen"], 1)) * 90,
            )
        offset += len(articles)
        if offset >= total:
            break

    refresh_account_counts()
    update_run_progress(
        "同步完成",
        f"同步完成：看到 {stats['articles_seen']} 篇，更新 {stats['changed']} 篇，错误 {stats['errors']} 个",
        stats,
        100,
    )
    return stats


def refresh_account_counts() -> None:
    with connect() as conn:
        conn.execute(
            """
            UPDATE accounts SET
                article_count = (
                    SELECT COUNT(*) FROM articles WHERE articles.mp_id = accounts.id
                ),
                full_text_count = (
                    SELECT COUNT(*) FROM articles WHERE articles.mp_id = accounts.id AND has_content = 1
                ),
                last_publish_time = (
                    SELECT MAX(publish_time) FROM articles WHERE articles.mp_id = accounts.id
                )
            """
        )


def summary_needs_llm_refresh(summary: dict[str, Any]) -> bool:
    if not summary:
        return True
    method = str(summary.get("method") or "")
    if not method or method == "heuristic":
        return True
    required_fields = {"one_sentence", "thesis", "key_points", "takeaways", "why_read"}
    if any(not summary.get(field) for field in required_fields):
        return True
    if summary.get("limited_evidence") and not summary.get("one_sentence"):
        return True
    return False


def pending_articles(limit: int | None = None, allow_refresh: bool = False) -> list[dict[str, Any]]:
    limit_value = int(limit) if limit is not None else None
    limited = limit_value is not None and limit_value > 0
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT *
            FROM articles
            ORDER BY
                CASE WHEN summary_json IS NULL THEN 0 ELSE 1 END,
                has_content DESC,
                value_score DESC,
                publish_time DESC
            """
        ).fetchall()
    items: list[dict[str, Any]] = []
    for row in rows:
        article = dict(row)
        summary = json_loads(article.get("summary_json"), {})
        if article.get("summary_json") is None:
            items.append(article)
        elif allow_refresh and summary_needs_llm_refresh(summary):
            items.append(article)
        if limited and len(items) >= limit_value:
            break
    return items


def coerce_summary(summary: dict[str, Any], fallback: dict[str, Any], method: str) -> dict[str, Any]:
    merged = {**fallback, **(summary or {})}
    merged["one_sentence"] = str(merged.get("one_sentence") or fallback["one_sentence"]).strip()
    merged["thesis"] = str(merged.get("thesis") or merged["one_sentence"]).strip()[:220]
    merged["why_read"] = str(merged.get("why_read") or fallback["why_read"]).strip()
    merged["recommended_for"] = str(
        merged.get("recommended_for") or fallback["recommended_for"]
    ).strip()
    merged["difficulty"] = str(merged.get("difficulty") or fallback["difficulty"]).lower()
    if merged["difficulty"] not in {"low", "medium", "high"}:
        merged["difficulty"] = fallback["difficulty"]
    merged["confidence"] = str(merged.get("confidence") or fallback["confidence"]).lower()
    if merged["confidence"] not in {"low", "medium", "high"}:
        merged["confidence"] = fallback["confidence"]
    merged["limited_evidence"] = bool(merged.get("limited_evidence"))
    merged["value_score"] = max(1.0, min(10.0, float(merged.get("value_score") or fallback["value_score"])))

    tags = merged.get("tags") or fallback["tags"]
    key_points = merged.get("key_points") or fallback["key_points"]
    takeaways = merged.get("takeaways") or key_points[:3]

    merged["tags"] = [str(tag).strip() for tag in tags if str(tag).strip()][:8]
    merged["key_points"] = [str(point).strip() for point in key_points if str(point).strip()][:6]
    merged["takeaways"] = [str(point).strip() for point in takeaways if str(point).strip()][:4]
    merged["reading_time_minutes"] = max(
        1, int(merged.get("reading_time_minutes") or fallback.get("reading_time_minutes") or 1)
    )
    merged["method"] = method
    return merged


async def summarize_article(article: dict[str, Any], settings: Settings, llm: LlmClient) -> dict[str, Any]:
    fallback = heuristic_article_summary(article)
    if not llm.enabled:
        return fallback

    system = """
你是资深中文阅读助手，负责把公众号文章做成真正可用的阅读摘要。
要求：
1. 必须基于全文综合判断，禁止只摘抄第一段，禁止把标题改写一遍就当摘要。
2. 抓结论、论据、信息增量、适用读者与阅读价值。
3. 如果证据不足，要明确写 limited_evidence=true，不要装作看过全文。
4. 只输出 JSON，不要输出 Markdown，不要解释。
"""
    user = f"""
请总结下面这篇公众号文章，并输出 JSON，字段必须完整：
{{
  "one_sentence": "一句话摘要，20到60字",
  "thesis": "核心论点或结论，1到2句",
  "key_points": ["3到5条要点，每条尽量概括而不是抄原句"],
  "takeaways": ["2到4条可执行或可记忆结论"],
  "why_read": "这篇文章值不值得读，以及应该怎么读",
  "recommended_for": "适合什么读者",
  "tags": ["最多8个标签"],
  "value_score": 1-10,
  "difficulty": "low|medium|high",
  "confidence": "low|medium|high",
  "limited_evidence": true
}}

补充要求：
- 如果正文可用，key_points 必须体现文章中后段的内容，不能只围绕开头。
- 如果文章更像快评、资料汇编或动态跟踪，要如实说明，不要拔高。
- value_score 反映信息密度、可读性、可复用性，不是文采评分。

文章材料：
{article_outline(article, settings.max_article_chars)}
"""
    result, usage = await llm.chat_json_with_usage(system.strip(), user.strip())
    summary = coerce_summary(result, fallback, "llm")
    summary["usage"] = {
        "model": settings.llm_model,
        "prompt_tokens": usage.get("prompt_tokens"),
        "completion_tokens": usage.get("completion_tokens"),
        "total_tokens": usage.get("total_tokens"),
    }
    return summary


def store_article_summary(article_id: str, summary: dict[str, Any]) -> None:
    usage = summary.get("usage") or {}
    with connect() as conn:
        conn.execute(
            """
            UPDATE articles
            SET summary_json=?, value_score=?, tags_json=?, summarized_at=?,
                summary_model=?, summary_prompt_tokens=?, summary_completion_tokens=?,
                summary_total_tokens=?
            WHERE id=?
            """,
            (
                json_dumps(summary),
                float(summary.get("value_score") or 0),
                json_dumps(summary.get("tags") or []),
                utc_now(),
                usage.get("model"),
                usage.get("prompt_tokens"),
                usage.get("completion_tokens"),
                usage.get("total_tokens"),
                article_id,
            ),
        )


async def summarize_pending(limit: int | None = None) -> dict[str, Any]:
    settings = effective_settings()
    llm = LlmClient(settings)
    articles = pending_articles(limit=limit, allow_refresh=llm.enabled)
    stats = {
        "pending": len(articles),
        "limit": int(limit) if limit is not None and int(limit) > 0 else None,
        "summarized": 0,
        "errors": 0,
        "llm_enabled": llm.enabled,
        "llm_summaries": 0,
        "heuristic_summaries": 0,
    }
    update_run_progress(
        "生成摘要",
        f"待总结 {len(articles)} 篇，模型{'已启用' if llm.enabled else '未启用'}",
        stats,
        0 if articles else 100,
    )
    for index, article in enumerate(articles, 1):
        stats["current"] = index
        stats["current_title"] = article.get("title") or ""
        update_run_progress(
            "生成摘要",
            f"正在总结 {index} / {len(articles)}：{article.get('title') or '未命名文章'}",
            stats,
            ((index - 1) / max(len(articles), 1)) * 100,
        )
        try:
            summary = await summarize_article(article, settings, llm)
            store_article_summary(str(article["id"]), summary)
            stats["summarized"] += 1
            if summary.get("method") == "llm":
                stats["llm_summaries"] += 1
                usage = summary.get("usage") or {}
                stats["prompt_tokens"] = int(stats.get("prompt_tokens") or 0) + int(usage.get("prompt_tokens") or 0)
                stats["completion_tokens"] = int(stats.get("completion_tokens") or 0) + int(usage.get("completion_tokens") or 0)
                stats["total_tokens"] = int(stats.get("total_tokens") or 0) + int(usage.get("total_tokens") or 0)
            else:
                stats["heuristic_summaries"] += 1
        except Exception as exc:
            fallback = heuristic_article_summary(article)
            fallback["confidence"] = "low"
            fallback["error"] = str(exc)
            store_article_summary(str(article["id"]), fallback)
            stats["summarized"] += 1
            stats["errors"] += 1
            stats["heuristic_summaries"] += 1
        update_run_progress(
            "生成摘要",
            (
                f"已总结 {stats['summarized']} / {len(articles)}，"
                f"LLM {stats['llm_summaries']}，规则 {stats['heuristic_summaries']}，错误 {stats['errors']}"
            ),
            stats,
            (index / max(len(articles), 1)) * 100,
        )
        await asyncio.sleep(0.15)
    stats.pop("current", None)
    stats.pop("current_title", None)
    update_run_progress(
        "摘要完成",
        f"摘要完成：{stats['summarized']} 篇，错误 {stats['errors']} 个",
        stats,
        100,
    )
    return stats


def account_articles(account_id: str, limit: int = 30) -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT id, mp_id, mp_name, title, url, description, pic_url, publish_time,
                   has_content, content_chars, summary_json, value_score, tags_json,
                   read_status, favorite, summarized_at
            FROM articles
            WHERE mp_id=?
            ORDER BY publish_time DESC
            LIMIT ?
            """,
            (account_id, limit),
        ).fetchall()
    return [hydrate_article(row, include_content=False) for row in rows]


def coerce_profile(profile: dict[str, Any], fallback: dict[str, Any], method: str) -> dict[str, Any]:
    merged = {**fallback, **(profile or {})}
    merged["capability_judgment"] = str(
        merged.get("capability_judgment") or fallback["capability_judgment"]
    ).strip()
    merged["strengths"] = [str(item).strip() for item in (merged.get("strengths") or fallback["strengths"]) if str(item).strip()][:8]
    merged["weaknesses"] = [str(item).strip() for item in (merged.get("weaknesses") or fallback["weaknesses"]) if str(item).strip()][:5]
    merged["tags"] = [str(item).strip() for item in (merged.get("tags") or fallback["tags"]) if str(item).strip()][:8]
    merged["score"] = max(1.0, min(10.0, float(merged.get("score") or fallback["score"])))
    merged["confidence"] = str(merged.get("confidence") or fallback["confidence"]).lower()
    if merged["confidence"] not in {"low", "medium", "high"}:
        merged["confidence"] = fallback["confidence"]
    merged["style"] = str(merged.get("style") or fallback.get("style") or "").strip()
    merged["best_use_cases"] = [
        str(item).strip()
        for item in (merged.get("best_use_cases") or fallback.get("best_use_cases") or [])
        if str(item).strip()
    ][:5]
    merged["method"] = method
    return merged


async def profile_account(account: dict[str, Any], settings: Settings, llm: LlmClient) -> dict[str, Any]:
    articles = account_articles(str(account["id"]), limit=40)
    fallback = heuristic_profile(account, articles)
    if not llm.enabled:
        return fallback

    article_samples = []
    for item in articles[:18]:
        summary = item.get("summary") or {}
        article_samples.append(
            {
                "title": item.get("title"),
                "publish_time": item.get("publish_time"),
                "value_score": item.get("value_score"),
                "tags": item.get("tags"),
                "one_sentence": summary.get("one_sentence"),
                "why_read": summary.get("why_read"),
                "difficulty": summary.get("difficulty"),
                "has_content": item.get("has_content"),
            }
        )

    system = """
你是信息源评估分析师，负责给公众号作者做画像。
要求：
1. 基于多篇文章的长期风格和内容范围来判断，不要只看单篇标题。
2. 既要说作者擅长什么，也要说使用限制，不夸大。
3. 只输出 JSON，不要输出 Markdown。
"""
    user = f"""
请根据下面的公众号信息和文章样本，输出作者画像 JSON：
{{
  "capability_judgment": "对作者能力层级和稳定性的判断",
  "strengths": ["擅长方向"],
  "weaknesses": ["使用限制"],
  "tags": ["最多8个标签"],
  "score": 1-10,
  "confidence": "low|medium|high",
  "style": "写作风格或产出类型",
  "best_use_cases": ["适合如何使用这个信息源"]
}}

公众号：{account.get("mp_name")}
简介：{account.get("mp_intro") or ""}
文章样本：{json_dumps(article_samples)}
"""
    try:
        result = await llm.chat_json(system.strip(), user.strip())
        return coerce_profile(result, fallback, "llm")
    except Exception as exc:
        fallback["error"] = str(exc)
        fallback["confidence"] = "low"
        return fallback


async def refresh_profiles() -> dict[str, Any]:
    settings = effective_settings()
    llm = LlmClient(settings)
    with connect() as conn:
        accounts = [dict(row) for row in conn.execute("SELECT * FROM accounts ORDER BY article_count DESC").fetchall()]
    stats = {"accounts": len(accounts), "profiled": 0, "llm_enabled": llm.enabled}
    update_run_progress(
        "更新画像",
        f"待更新 {len(accounts)} 个公众号画像",
        stats,
        0 if accounts else 100,
    )
    for index, account in enumerate(accounts, 1):
        stats["current"] = index
        stats["current_account"] = account.get("mp_name") or ""
        update_run_progress(
            "更新画像",
            f"正在更新 {index} / {len(accounts)}：{account.get('mp_name') or '未知公众号'}",
            stats,
            ((index - 1) / max(len(accounts), 1)) * 100,
        )
        profile = await profile_account(account, settings, llm)
        with connect() as conn:
            conn.execute(
                """
                UPDATE accounts
                SET profile_json=?, score=?, confidence=?, tags_json=?, updated_at=?
                WHERE id=?
                """,
                (
                    json_dumps(profile),
                    float(profile.get("score") or 0),
                    str(profile.get("confidence") or ""),
                    json_dumps(profile.get("tags") or []),
                    utc_now(),
                    account["id"],
                ),
            )
        stats["profiled"] += 1
        update_run_progress(
            "更新画像",
            f"已更新 {stats['profiled']} / {len(accounts)} 个公众号画像",
            stats,
            (index / max(len(accounts), 1)) * 100,
        )
        await asyncio.sleep(0.15)
    stats.pop("current", None)
    stats.pop("current_account", None)
    update_run_progress("画像完成", f"画像完成：{stats['profiled']} 个公众号", stats, 100)
    return stats


def knowledge_status() -> dict[str, Any]:
    with connect() as conn:
        row = conn.execute(
            """
            SELECT
              COUNT(*) AS chunks,
              COALESCE(SUM(CASE WHEN embedding_json IS NOT NULL AND embedding_json != '' THEN 1 ELSE 0 END), 0) AS embedded,
              COALESCE(SUM(CASE WHEN embedding_json IS NULL OR embedding_json = '' THEN 1 ELSE 0 END), 0) AS pending
            FROM rag_chunks
            """
        ).fetchone()
        article_row = conn.execute(
            "SELECT COUNT(*) AS c FROM articles WHERE has_content=1 OR content_chars > 0"
        ).fetchone()
    settings = effective_settings()
    return {
        "enabled": settings.rag_enabled,
        "embedding_configured": bool(settings.rag_api_key and settings.rag_api_base_url and settings.rag_embedding_model),
        "articles": int(article_row["c"] or 0),
        "chunks": int(row["chunks"] or 0),
        "embedded": int(row["embedded"] or 0),
        "pending": int(row["pending"] or 0),
        "model": settings.rag_embedding_model,
    }


async def rebuild_knowledge_index(limit: int | None = None, embed_batch_size: int = 100) -> dict[str, Any]:
    settings = effective_settings()
    max_articles = int(limit) if limit is not None and int(limit) > 0 else None
    query = """
        SELECT *
        FROM articles
        WHERE (has_content=1 OR content_chars > 0 OR summary_json IS NOT NULL)
        ORDER BY publish_time DESC
    """
    params: list[Any] = []
    if max_articles:
        query += " LIMIT ?"
        params.append(max_articles)
    with connect() as conn:
        articles = [dict(row) for row in conn.execute(query, params).fetchall()]

    stats = {
        "articles": len(articles),
        "chunks": 0,
        "inserted": 0,
        "skipped": 0,
        "embedded": 0,
        "errors": 0,
        "rag_enabled": settings.rag_enabled,
    }
    update_run_progress("构建知识库", f"准备处理 {len(articles)} 篇文章", stats, 0 if articles else 100)
    for index, article in enumerate(articles, 1):
        try:
            result = upsert_rag_chunks(article)
            stats["chunks"] += result["chunks"]
            stats["inserted"] += result["inserted"]
            stats["skipped"] += result["skipped"]
        except Exception:
            stats["errors"] += 1
        update_run_progress(
            "构建知识库",
            f"已切分 {index} / {len(articles)} 篇，新增 {stats['inserted']} 个片段",
            stats,
            (index / max(len(articles), 1)) * 45,
        )

    if settings.rag_enabled:
        while True:
            batch = await embed_pending_chunks(limit=embed_batch_size)
            if not batch.get("enabled"):
                stats["embedding_configured"] = False
                break
            count = int(batch.get("embedded") or 0)
            if count <= 0:
                break
            stats["embedded"] += count
            status = knowledge_status()
            update_run_progress(
                "生成向量",
                f"已生成 {stats['embedded']} 个片段向量，剩余 {status['pending']} 个",
                stats,
                45 + min(50, (stats["embedded"] / max(stats["inserted"] or stats["chunks"], 1)) * 50),
            )
            await asyncio.sleep(0.1)

    stats["status"] = knowledge_status()
    update_run_progress("知识库完成", f"知识库索引完成：{stats['status']['embedded']} 个可检索片段", stats, 100)
    return stats


async def knowledge_rebuild_update(limit: int | None = None, embed_batch_size: int = 100) -> dict[str, Any]:
    async with RUN_LOCK:
        run_id = start_run("knowledge")
        start_current_run("knowledge", run_id, "开始构建知识库索引")
        try:
            stats = await rebuild_knowledge_index(limit=limit, embed_batch_size=embed_batch_size)
            finish_run(run_id, "success", "knowledge index completed", stats)
            complete_current_run("success", "知识库索引完成", stats)
            return stats
        except Exception as exc:
            finish_run(run_id, "failed", str(exc), {})
            complete_current_run("failed", str(exc), {})
            raise


async def ask_knowledge(question: str, top_k: int | None = None, mp_id: str | None = None) -> dict[str, Any]:
    settings = effective_settings()
    if not settings.rag_enabled:
        raise RuntimeError("知识库问答未启用")
    if not question.strip():
        raise RuntimeError("问题不能为空")

    embedder = EmbeddingClient(settings)
    if not embedder.enabled:
        raise RuntimeError("向量模型未配置")
    query_embedding = (await embedder.embed([question.strip()]))[0]
    matches = search_rag_chunks(query_embedding, top_k=top_k or settings.rag_top_k, mp_id=mp_id)
    if not matches:
        return {
            "answer": "当前知识库里没有找到足够相关的文章片段。",
            "sources": [],
            "matches": [],
            "confidence": "low",
        }

    contexts = []
    sources = []
    seen_articles: set[str] = set()
    for index, match in enumerate(matches, 1):
        contexts.append(
            "\n".join(
                [
                    f"[{index}] 公众号：{match.get('mp_name') or ''}",
                    f"标题：{match.get('title') or ''}",
                    f"相似度：{float(match.get('score') or 0):.3f}",
                    f"片段：{match.get('chunk_text') or ''}",
                ]
            )
        )
        article_id = str(match.get("article_id") or "")
        if article_id and article_id not in seen_articles:
            seen_articles.add(article_id)
            sources.append(
                {
                    "article_id": article_id,
                    "title": match.get("title"),
                    "mp_name": match.get("mp_name"),
                    "url": match.get("url"),
                    "publish_time": match.get("publish_time"),
                    "score": round(float(match.get("score") or 0), 4),
                }
            )

    llm_settings = settings_from_mapping(
        {
            **asdict(settings),
            "llm_base_url": settings.rag_api_base_url,
            "llm_api_key": settings.rag_api_key,
            "llm_model": settings.rag_chat_model,
            "allow_llm": True,
        }
    )
    llm = LlmClient(llm_settings)
    system = """
你是公众号文章知识库问答助手。只能基于给定片段回答。
要求：
1. 先直接回答问题。
2. 如果多个来源观点不同，要分公众号/文章说明各自逻辑。
3. 标明共识、分歧和证据不足之处。
4. 引用来源编号，例如 [1]、[2]。
5. 只输出 JSON。
"""
    user = f"""
问题：{question.strip()}

相关片段：
{chr(10).join(contexts)}

请输出 JSON：
{{
  "answer": "综合回答",
  "consensus": ["共识"],
  "differences": ["分歧"],
  "source_notes": ["按来源说明观点和逻辑"],
  "confidence": "low|medium|high"
}}
"""
    result = await llm.chat_json(system.strip(), user.strip())
    return {
        "answer": str(result.get("answer") or ""),
        "consensus": result.get("consensus") or [],
        "differences": result.get("differences") or [],
        "source_notes": result.get("source_notes") or [],
        "confidence": result.get("confidence") or "medium",
        "sources": sources,
        "matches": [
            {
                "article_id": item.get("article_id"),
                "title": item.get("title"),
                "mp_name": item.get("mp_name"),
                "score": round(float(item.get("score") or 0), 4),
                "chunk_text": str(item.get("chunk_text") or "")[:420],
            }
            for item in matches
        ],
    }


async def full_update(limit: int | None = None, summarize_limit: int | None = None) -> dict[str, Any]:
    async with RUN_LOCK:
        run_id = start_run("full_update")
        start_current_run("full_update", run_id, "开始同步、总结和画像更新")
        try:
            sync_stats = await sync_from_werss(limit=limit)
            summary_stats = await summarize_pending(limit=summarize_limit)
            profile_stats = await refresh_profiles()
            update_run_progress("发送提醒", "正在生成阅读提醒", progress=95)
            notify_stats = await send_reading_digest()
            stats = {
                "sync": sync_stats,
                "summaries": summary_stats,
                "profiles": profile_stats,
                "notification": notify_stats,
            }
            stats["sync"]["errors"] = int(stats["sync"].get("errors") or 0) + int(stats["sync"].get("image_errors") or 0)
            finish_run(run_id, "success", "update completed", stats)
            complete_current_run("success", "更新完成", stats)
            set_setting("last_update_at", utc_now())
            return stats
        except Exception as exc:
            finish_run(run_id, "failed", str(exc), {})
            complete_current_run("failed", str(exc), {})
            raise


async def sync_update(limit: int | None = None) -> dict[str, Any]:
    async with RUN_LOCK:
        run_id = start_run("sync")
        start_current_run("sync", run_id, "开始同步 WeRSS 文章")
        try:
            stats = await sync_from_werss(limit=limit)
            stats["errors"] = int(stats.get("errors") or 0) + int(stats.get("image_errors") or 0)
            finish_run(run_id, "success", "sync completed", stats)
            complete_current_run("success", "同步完成", stats)
            set_setting("last_update_at", utc_now())
            return stats
        except Exception as exc:
            finish_run(run_id, "failed", str(exc), {})
            complete_current_run("failed", str(exc), {})
            raise


async def summarize_update(limit: int | None = None) -> dict[str, Any]:
    async with RUN_LOCK:
        run_id = start_run("summarize")
        start_current_run("summarize", run_id, "开始生成文章摘要")
        try:
            stats = await summarize_pending(limit=limit)
            finish_run(run_id, "success", "summarize completed", stats)
            complete_current_run("success", "总结完成", stats)
            return stats
        except Exception as exc:
            finish_run(run_id, "failed", str(exc), {})
            complete_current_run("failed", str(exc), {})
            raise


async def pull_werss_status() -> dict[str, Any]:
    settings = effective_settings()
    client = WeRssClient(settings)
    result: dict[str, Any] = {
        "available": False,
        "system": {},
        "queue": {},
        "content_queue": {},
        "errors": {},
    }
    checks = {
        "system": client.get_system_info,
        "queue": client.get_queue_status,
        "content_queue": client.get_content_queue_status,
    }
    success_count = 0
    for name, fetcher in checks.items():
        try:
            result[name] = await fetcher()
            success_count += 1
        except Exception as exc:
            result["errors"][name] = str(exc)
    result["available"] = success_count > 0
    if result["errors"]:
        result["error"] = "; ".join(f"{name}: {message}" for name, message in result["errors"].items())
    return result


def dashboard_stats() -> dict[str, Any]:
    with connect() as conn:
        article = conn.execute(
            """
            SELECT
              COUNT(*) AS total,
              COALESCE(SUM(CASE WHEN has_content=1 THEN 1 ELSE 0 END), 0) AS full_text,
              COALESCE(SUM(CASE WHEN summary_json IS NOT NULL THEN 1 ELSE 0 END), 0) AS summarized,
              COALESCE(SUM(CASE WHEN read_status='read' THEN 1 ELSE 0 END), 0) AS read_count,
              AVG(value_score) AS avg_score
            FROM articles
            """
        ).fetchone()
        account = conn.execute("SELECT COUNT(*) AS total FROM accounts").fetchone()
        run_rows = conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 8").fetchall()
    settings = effective_settings()
    return {
        "articles": dict(article) if article else {},
        "accounts": dict(account) if account else {},
        "runs": [hydrate_run(row) for row in run_rows],
        "last_update_at": get_setting("last_update_at", None),
        "next_run_at": next_run_at(settings).isoformat(timespec="minutes"),
        "running": is_run_active(),
    }


def daily_activity(days: int = 14) -> dict[str, Any]:
    days = max(1, min(int(days), 90))
    with connect() as conn:
        new_rows = conn.execute(
            """
            SELECT substr(COALESCE(created_at, synced_at), 1, 10) AS day, COUNT(*) AS articles
            FROM articles
            WHERE COALESCE(created_at, synced_at) >= datetime('now', ?)
            GROUP BY day
            ORDER BY day
            """,
            (f"-{days - 1} days",),
        ).fetchall()
        summary_rows = conn.execute(
            """
            SELECT substr(summarized_at, 1, 10) AS day,
                   COUNT(*) AS summarized,
                   COALESCE(SUM(summary_prompt_tokens), 0) AS prompt_tokens,
                   COALESCE(SUM(summary_completion_tokens), 0) AS completion_tokens,
                   COALESCE(SUM(summary_total_tokens), 0) AS total_tokens,
                   COALESCE(SUM(CASE WHEN summary_json LIKE '%"method":"llm"%' THEN 1 ELSE 0 END), 0) AS llm_summaries,
                   COALESCE(SUM(CASE WHEN summary_json LIKE '%"method":"heuristic"%' THEN 1 ELSE 0 END), 0) AS heuristic_summaries
            FROM articles
            WHERE summarized_at IS NOT NULL
              AND summarized_at >= datetime('now', ?)
            GROUP BY day
            ORDER BY day
            """,
            (f"-{days - 1} days",),
        ).fetchall()

    by_day: dict[str, dict[str, Any]] = {}
    for row in new_rows:
        by_day.setdefault(row["day"], {"day": row["day"]})
        by_day[row["day"]]["new_articles"] = row["articles"]
    for row in summary_rows:
        by_day.setdefault(row["day"], {"day": row["day"]})
        by_day[row["day"]].update(
            {
                "summarized": row["summarized"],
                "prompt_tokens": row["prompt_tokens"],
                "completion_tokens": row["completion_tokens"],
                "total_tokens": row["total_tokens"],
                "llm_summaries": row["llm_summaries"],
                "heuristic_summaries": row["heuristic_summaries"],
            }
        )

    end = datetime.now(TZ).date()
    rows: list[dict[str, Any]] = []
    for index in range(days - 1, -1, -1):
        day = (end - timedelta(days=index)).isoformat()
        item = {
            "day": day,
            "new_articles": 0,
            "summarized": 0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
            "llm_summaries": 0,
            "heuristic_summaries": 0,
        }
        item.update(by_day.get(day, {}))
        rows.append(item)

    period_totals = {
        "articles": sum(int(item.get("new_articles") or 0) for item in rows),
        "summarized": sum(int(item.get("summarized") or 0) for item in rows),
        "prompt_tokens": sum(int(item.get("prompt_tokens") or 0) for item in rows),
        "completion_tokens": sum(int(item.get("completion_tokens") or 0) for item in rows),
        "total_tokens": sum(int(item.get("total_tokens") or 0) for item in rows),
    }

    return {
        "days": days,
        "rows": rows,
        "totals": period_totals,
        "token_note": "历史摘要如果生成时未记录 usage，会显示为 0；后续 LLM 摘要会记录 token。",
    }


def hydrate_run(row: Any) -> dict[str, Any]:
    data = dict(row)
    data["stats"] = json_loads(data.pop("stats_json", None), {})
    return data


def list_articles(
    limit: int = 50,
    offset: int = 0,
    account_id: str | None = None,
    read_status: str | None = None,
    search: str | None = None,
    sort: str = "value",
) -> dict[str, Any]:
    filters: list[str] = []
    params: list[Any] = []
    if account_id:
        filters.append("mp_id=?")
        params.append(account_id)
    if read_status:
        filters.append("read_status=?")
        params.append(read_status)
    if search:
        filters.append("(title LIKE ? OR mp_name LIKE ? OR description LIKE ?)")
        keyword = f"%{search}%"
        params.extend([keyword, keyword, keyword])
    where = f"WHERE {' AND '.join(filters)}" if filters else ""
    order = "value_score DESC, publish_time DESC" if sort == "value" else "publish_time DESC"
    with connect() as conn:
        total = conn.execute(f"SELECT COUNT(*) AS c FROM articles {where}", params).fetchone()["c"]
        rows = conn.execute(
            f"""
            SELECT id, mp_id, mp_name, title, url, description, pic_url, publish_time,
                   has_content, content_chars, summary_json, value_score, tags_json,
                   read_status, favorite, summarized_at
            FROM articles
            {where}
            ORDER BY {order}
            LIMIT ? OFFSET ?
            """,
            params + [limit, offset],
        ).fetchall()
    return {"total": total, "list": [hydrate_article(row, include_content=False) for row in rows]}


def classify_paragraph(text: str) -> str:
    compact = re.sub(r"\s+", "", text or "").lower()
    if not compact:
        return "empty"
    disclaimer_signals = [
        "免责声明",
        "风险自负",
    ]
    promo_signals = [
        "加入我的知识星球",
        "知识星球",
        "欢迎加入",
        "私信对话框",
        "入圈",
        "星标",
        "关注我们",
        "扫码",
        "点击下方",
        "点击蓝字",
        "商务合作",
    ]
    related_signals = [
        "继续跟踪这轮行情",
        "相关阅读",
        "延伸阅读",
        "往期推荐",
        "上期文章",
        "点击回顾",
        "更多内容",
    ]
    if any(signal in compact for signal in disclaimer_signals):
        return "disclaimer"
    if any(signal in compact for signal in promo_signals):
        return "promo"
    if any(signal in compact for signal in related_signals):
        return "related"
    return "body"


def sanitize_article_html(raw_html: str) -> str:
    if not raw_html:
        return ""
    html = raw_html
    html = re.sub(r"<script[^>]*>.*?</script>", "", html, flags=re.S | re.I)
    html = re.sub(r"<style[^>]*>.*?</style>", "", html, flags=re.S | re.I)
    html = re.sub(r"<iframe[^>]*>.*?</iframe>", "", html, flags=re.S | re.I)
    html = re.sub(r"<(object|embed|form|input|button|textarea|select)[^>]*>.*?</\1>", "", html, flags=re.S | re.I)
    html = re.sub(r"\s+on[a-zA-Z]+\s*=\s*(['\"]).*?\1", "", html, flags=re.S)
    html = re.sub(r"\s+style\s*=\s*(['\"]).*?\1", "", html, flags=re.S)

    def replace_img(match: re.Match[str]) -> str:
        tag = match.group(0)
        src = ""
        for attr in ("src", "data-src", "data-original", "data-url"):
            found = re.search(rf'{attr}\s*=\s*"([^"]+)"', tag, flags=re.I)
            if found:
                src = found.group(1)
                break
            found = re.search(rf"{attr}\s*=\s*'([^']+)'", tag, flags=re.I)
            if found:
                src = found.group(1)
                break
        if src.startswith("//"):
            src = f"https:{src}"
        if not re.match(r"^(https?:|data:image/)", src, flags=re.I):
            return ""
        alt_match = re.search(r'alt\s*=\s*"([^"]*)"', tag, flags=re.I) or re.search(
            r"alt\s*=\s*'([^']*)'", tag, flags=re.I
        )
        alt = alt_match.group(1) if alt_match else "article image"
        return (
            '<figure class="article-figure">'
            f'<img src="{src}" alt="{alt}" loading="lazy" referrerpolicy="no-referrer" />'
            "</figure>"
        )

    html = re.sub(r"<img\b[^>]*>", replace_img, html, flags=re.I)
    html = re.sub(r"<a\b([^>]*)href\s*=\s*(['\"])\s*javascript:[^'\"]*\2", r"<a\1", html, flags=re.I)
    html = re.sub(
        r"<a\b([^>]*)href\s*=\s*(['\"])(https?://[^'\"]+)\2([^>]*)>",
        lambda match: (
            f'<a class="article-anchor" href="{match.group(3)}" target="_blank" '
            'rel="noreferrer nofollow">'
        ),
        html,
        flags=re.I,
    )
    html = re.sub(r"<p>\s*</p>", "", html, flags=re.I)

    paragraph_pattern = re.compile(r"<p\b[^>]*>(.*?)</p>", flags=re.S | re.I)
    rebuilt: list[str] = []
    current_aux: str | None = None

    for token in re.split(r"(<p\b[^>]*>.*?</p>)", html, flags=re.S | re.I):
        if not token:
            continue
        match = paragraph_pattern.fullmatch(token.strip())
        if not match:
            rebuilt.append(token)
            continue

        inner_html = match.group(1).strip()
        text = strip_html(inner_html)
        if not inner_html:
            continue
        if "<figure" in inner_html:
            rebuilt.append(f'<div class="article-block image-block">{inner_html}</div>')
            current_aux = None
            continue

        block_kind = classify_paragraph(text)
        if block_kind == "body":
            classes = ["article-paragraph"]
            short_text = re.sub(r"\s+", "", text)
            if len(short_text) <= 18 and "：" not in text and ":" not in text:
                classes.append("lead-paragraph")
            rebuilt.append(f'<p class="{" ".join(classes)}">{inner_html}</p>')
            current_aux = None
            continue

        section_class = f"article-{block_kind}-section"
        if current_aux != section_class:
            rebuilt.append(f'<section class="article-aside {section_class}">')
            current_aux = section_class
        rebuilt.append(f'<p class="article-paragraph article-{block_kind}">{inner_html}</p>')

    final_parts: list[str] = []
    aside_open = False
    for part in rebuilt:
        if part.startswith('<section class="article-aside'):
            if aside_open:
                final_parts.append("</section>")
            aside_open = True
            final_parts.append(part)
            continue
        if aside_open and (
            part.startswith('<p class="article-paragraph') or part.startswith('<div class="article-block image-block">')
        ) and "article-promo" not in part and "article-disclaimer" not in part and "article-related" not in part:
            final_parts.append("</section>")
            aside_open = False
        final_parts.append(part)
    if aside_open:
        final_parts.append("</section>")

    content = "".join(final_parts).strip()
    return f'<div class="article-body">{content}</div>' if content else ""


def replace_cached_image_sources(html: str, image_map: dict[str, str]) -> str:
    if not html or not image_map:
        return html
    for source, local in sorted(image_map.items(), key=lambda item: len(item[0]), reverse=True):
        html = html.replace(source, local)
    return html


def hydrate_article(row: Any, include_content: bool = False) -> dict[str, Any]:
    data = dict(row)
    data["summary"] = json_loads(data.pop("summary_json", None), {})
    data["tags"] = json_loads(data.pop("tags_json", None), [])
    if include_content:
        html = str(data.get("content_html") or "")
        text = strip_html(str(data.get("content") or html or data.get("description") or ""))
        data["content_text"] = text
        rendered = sanitize_article_html(html) if html else ""
        data["rendered_html"] = replace_cached_image_sources(rendered, cached_media_map(str(data.get("id") or "")))
    else:
        data.pop("content", None)
        data.pop("content_html", None)
    return data


def get_article(article_id: str) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM articles WHERE id=?", (article_id,)).fetchone()
    return hydrate_article(row, include_content=True) if row else None


def list_accounts() -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute("SELECT * FROM accounts ORDER BY score DESC, article_count DESC").fetchall()
    result: list[dict[str, Any]] = []
    for row in rows:
        data = dict(row)
        data["profile"] = json_loads(data.pop("profile_json", None), {})
        data["tags"] = json_loads(data.pop("tags_json", None), [])
        result.append(data)
    return result


def rag_chunk_count() -> int:
    with connect() as conn:
        row = conn.execute("SELECT COUNT(*) AS c FROM rag_chunks").fetchone()
    return int(row["c"] or 0)


def upsert_rag_chunks(article: dict[str, Any], source_type: str = "article") -> dict[str, Any]:
    settings = effective_settings()
    text = article_rag_text(article)
    chunks = split_text_for_rag(text, chunk_size=settings.rag_chunk_size, overlap=settings.rag_chunk_overlap)
    inserted = 0
    skipped = 0
    now = utc_now()
    with connect() as conn:
        for index, chunk in enumerate(chunks, 1):
            digest = chunk_hash(chunk)
            row = conn.execute(
                "SELECT id, chunk_text, embedding_json, embedding_model FROM rag_chunks WHERE article_id=? AND chunk_hash=?",
                (article.get("id"), digest),
            ).fetchone()
            if row and row["chunk_text"] == chunk:
                skipped += 1
                continue
            conn.execute(
                """
                INSERT INTO rag_chunks(
                    article_id, mp_id, chunk_index, chunk_text, chunk_hash, token_count,
                    embedding_json, embedding_model, source_type, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(article_id, chunk_hash) DO UPDATE SET
                    mp_id=excluded.mp_id,
                    chunk_index=excluded.chunk_index,
                    chunk_text=excluded.chunk_text,
                    token_count=excluded.token_count,
                    source_type=excluded.source_type,
                    updated_at=excluded.updated_at
                """,
                (
                    article.get("id"),
                    article.get("mp_id"),
                    index,
                    chunk,
                    digest,
                    max(1, round(len(chunk) / 3.5)),
                    None,
                    None,
                    source_type,
                    now,
                    now,
                ),
            )
            inserted += 1
    return {"chunks": len(chunks), "inserted": inserted, "skipped": skipped}


async def embed_pending_chunks(limit: int = 200) -> dict[str, Any]:
    settings = effective_settings()
    client = EmbeddingClient(settings)
    if not client.enabled:
        return {"enabled": False, "embedded": 0, "updated": 0, "pending": 0}

    with connect() as conn:
        rows = conn.execute(
            """
            SELECT id, article_id, chunk_text
            FROM rag_chunks
            WHERE embedding_json IS NULL OR embedding_json = ''
            ORDER BY id ASC
            LIMIT ?
            """,
            (max(1, int(limit)),),
        ).fetchall()

    if not rows:
        return {"enabled": True, "embedded": 0, "updated": 0, "pending": 0}

    texts = [row["chunk_text"] for row in rows]
    embeddings = await client.embed(texts)
    updated = 0
    with connect() as conn:
        for row, embedding in zip(rows, embeddings):
            conn.execute(
                """
                UPDATE rag_chunks
                SET embedding_json=?, embedding_model=?, updated_at=?
                WHERE id=?
                """,
                (json_dumps(embedding), settings.rag_embedding_model, utc_now(), row["id"]),
            )
            updated += 1
    return {"enabled": True, "embedded": len(embeddings), "updated": updated, "pending": 0}


def fetch_rag_chunks(limit: int = 5000) -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT rc.*, a.title, a.mp_name, a.url, a.publish_time, a.summary_json, a.value_score, a.tags_json
            FROM rag_chunks rc
            LEFT JOIN articles a ON a.id = rc.article_id
            WHERE rc.embedding_json IS NOT NULL AND rc.embedding_json != ''
            ORDER BY rc.updated_at DESC
            LIMIT ?
            """,
            (max(1, int(limit)),),
        ).fetchall()
    items: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        item["embedding"] = json_loads(item.pop("embedding_json", None), [])
        item["summary"] = json_loads(item.pop("summary_json", None), {})
        item["tags"] = json_loads(item.pop("tags_json", None), [])
        items.append(item)
    return items


def search_rag_chunks(query_embedding: list[float], top_k: int = 8, mp_id: str | None = None) -> list[dict[str, Any]]:
    chunks = fetch_rag_chunks(limit=8000)
    scored: list[dict[str, Any]] = []
    for chunk in chunks:
        if mp_id and chunk.get("mp_id") != mp_id:
            continue
        score = cosine_similarity(query_embedding, chunk.get("embedding") or [])
        if score < 0:
            continue
        chunk = dict(chunk)
        chunk["score"] = score
        scored.append(chunk)
    scored.sort(key=lambda item: item["score"], reverse=True)
    return scored[: max(1, int(top_k))]


def get_account(account_id: str, article_limit: int = 80) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()
    if not row:
        return None
    data = dict(row)
    data["profile"] = json_loads(data.pop("profile_json", None), {})
    data["tags"] = json_loads(data.pop("tags_json", None), [])
    data["articles"] = account_articles(account_id, limit=article_limit)
    return data


def mark_article(article_id: str, read_status: str | None = None, favorite: bool | None = None) -> None:
    assignments: list[str] = []
    params: list[Any] = []
    if read_status:
        assignments.append("read_status=?")
        params.append(read_status)
    if favorite is not None:
        assignments.append("favorite=?")
        params.append(1 if favorite else 0)
    if not assignments:
        return
    params.append(article_id)
    with connect() as conn:
        conn.execute(f"UPDATE articles SET {', '.join(assignments)} WHERE id=?", params)


def merged_settings(update: dict[str, Any]) -> Settings:
    current = asdict(effective_settings())
    for key, value in update.items():
        if key not in current:
            continue
        if value in (None, "") and key.endswith("_key"):
            continue
        current[key] = value
    return settings_from_mapping(current)


async def test_llm_connection(update: dict[str, Any]) -> dict[str, Any]:
    settings = merged_settings(update)
    client = LlmClient(settings)
    return await client.test_connection()


async def send_reading_digest() -> dict[str, Any]:
    settings = effective_settings()
    if not settings.notify_webhook_url:
        return {"enabled": False}
    articles = list_articles(limit=settings.notify_top_n, sort="value")["list"]
    selected = [
        item for item in articles if float(item.get("value_score") or 0) >= settings.notify_min_score
    ][: settings.notify_top_n]
    if not selected:
        return {"enabled": True, "sent": False, "reason": "no articles above threshold"}
    lines = ["WeRSS Insight 阅读提醒", ""]
    for index, item in enumerate(selected, 1):
        summary = item.get("summary") or {}
        lines.append(f"{index}. [{item.get('value_score'):.1f}] {item.get('title')} - {item.get('mp_name')}")
        if summary.get("one_sentence"):
            lines.append(f"   {summary['one_sentence']}")
    payload = {"text": "\n".join(lines), "content": "\n".join(lines)}
    try:
        async with httpx.AsyncClient(timeout=20, trust_env=False) as client:
            response = await client.post(settings.notify_webhook_url, json=payload)
            response.raise_for_status()
        return {"enabled": True, "sent": True, "count": len(selected)}
    except Exception as exc:
        return {"enabled": True, "sent": False, "error": str(exc)}


def next_run_at(settings: Settings) -> datetime:
    now = datetime.now(TZ)
    hour, minute = [int(part) for part in settings.schedule_time.split(":", 1)]
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    last = get_setting("last_update_at", None)
    if last:
        try:
            last_dt = datetime.fromisoformat(last.replace("Z", "+00:00")).astimezone(TZ)
            target = last_dt + timedelta(days=settings.schedule_days)
            target = target.replace(hour=hour, minute=minute, second=0, microsecond=0)
        except Exception:
            pass
    if target <= now:
        target = target + timedelta(days=settings.schedule_days)
    return target


def schedule_due(settings: Settings) -> bool:
    if not settings.werss_base_url or not settings.werss_access_key or not settings.werss_secret_key:
        return False
    last = get_setting("last_update_at", None)
    now = datetime.now(TZ)
    hour, minute = [int(part) for part in settings.schedule_time.split(":", 1)]
    if not last:
        first_target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        return now >= first_target
    try:
        last_dt = datetime.fromisoformat(last.replace("Z", "+00:00")).astimezone(TZ)
    except Exception:
        return True
    due_at = (last_dt + timedelta(days=settings.schedule_days)).replace(
        hour=hour,
        minute=minute,
        second=0,
        microsecond=0,
    )
    return now >= due_at


async def scheduler_loop() -> None:
    init_db()
    while True:
        settings = effective_settings()
        if not settings.auto_run:
            await asyncio.sleep(60)
            continue
        if schedule_due(settings) and not is_run_active():
            try:
                await full_update(limit=settings.sync_limit)
            except Exception:
                pass
            await asyncio.sleep(60)
            continue
        target = next_run_at(settings)
        now = datetime.now(TZ)
        wait_seconds = max(30, min(3600, int((target - now).total_seconds())))
        await asyncio.sleep(wait_seconds)


def bootstrap() -> None:
    init_db()
    if get_setting("config", None) is None:
        set_setting("config", asdict(get_settings()))
