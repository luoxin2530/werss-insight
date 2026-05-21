import os
from dataclasses import fields
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


ROOT_DIR = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT_DIR / "data"
DB_PATH = DATA_DIR / "werss_insight.db"

load_dotenv(ROOT_DIR / ".env")


def env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    werss_base_url: str = os.getenv("WERSS_BASE_URL", "http://192.168.68.100:8011").rstrip("/")
    werss_access_key: str = os.getenv("WERSS_ACCESS_KEY", "")
    werss_secret_key: str = os.getenv("WERSS_SECRET_KEY", "")
    llm_base_url: str = os.getenv("LLM_BASE_URL", "https://api.openai.com/v1").rstrip("/")
    llm_api_key: str = os.getenv("LLM_API_KEY", "")
    llm_model: str = os.getenv("LLM_MODEL", "gpt-4o-mini")
    llm_temperature: float = float(os.getenv("LLM_TEMPERATURE", "0.2"))
    llm_timeout_seconds: int = int(os.getenv("LLM_TIMEOUT_SECONDS", "120"))
    schedule_days: int = int(os.getenv("SCHEDULE_DAYS", "3"))
    schedule_time: str = os.getenv("SCHEDULE_TIME", "09:00")
    auto_run: bool = env_bool("AUTO_RUN", True)
    sync_limit: int = int(os.getenv("SYNC_LIMIT", "100"))
    max_article_chars: int = int(os.getenv("MAX_ARTICLE_CHARS", "12000"))
    allow_llm: bool = env_bool("ALLOW_LLM", True)
    notify_webhook_url: str = os.getenv("NOTIFY_WEBHOOK_URL", "")
    notify_min_score: float = float(os.getenv("NOTIFY_MIN_SCORE", "7.5"))
    notify_top_n: int = int(os.getenv("NOTIFY_TOP_N", "8"))
    media_cache_mode: str = os.getenv("MEDIA_CACHE_MODE", "optimized_local")
    media_max_width: int = int(os.getenv("MEDIA_MAX_WIDTH", "1800"))
    media_image_quality: int = int(os.getenv("MEDIA_IMAGE_QUALITY", "85"))
    media_prefer_webp: bool = env_bool("MEDIA_PREFER_WEBP", True)
    rag_enabled: bool = env_bool("RAG_ENABLED", True)
    rag_api_base_url: str = os.getenv("RAG_API_BASE_URL", "https://api.openai.com/v1").rstrip("/")
    rag_api_key: str = os.getenv("RAG_API_KEY", "")
    rag_embedding_model: str = os.getenv("RAG_EMBEDDING_MODEL", "text-embedding-3-small")
    rag_chat_model: str = os.getenv("RAG_CHAT_MODEL", "gpt-4o-mini")
    rag_chunk_size: int = int(os.getenv("RAG_CHUNK_SIZE", "900"))
    rag_chunk_overlap: int = int(os.getenv("RAG_CHUNK_OVERLAP", "140"))
    rag_top_k: int = int(os.getenv("RAG_TOP_K", "8"))


def get_settings() -> Settings:
    return Settings()


def settings_from_mapping(values: dict) -> Settings:
    defaults = get_settings()
    merged = {field.name: getattr(defaults, field.name) for field in fields(Settings)}
    for key, value in values.items():
        if key in merged and value is not None:
            merged[key] = value
    return Settings(**merged)


def public_settings(settings: Settings) -> dict:
    data = {field.name: getattr(settings, field.name) for field in fields(Settings)}
    for key in ["werss_access_key", "werss_secret_key", "llm_api_key", "rag_api_key", "notify_webhook_url"]:
        value = str(data.get(key) or "")
        data[key] = {
            "configured": bool(value),
            "preview": f"{value[:4]}...{value[-4:]}" if len(value) >= 8 else "",
        }
    return data
