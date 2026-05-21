import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .config import DATA_DIR, DB_PATH


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def json_loads(value: str | None, fallback: Any = None) -> Any:
    if not value:
        return fallback
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return fallback


def dict_from_row(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return dict(row)


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db(path: Path = DB_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS accounts (
                id TEXT PRIMARY KEY,
                mp_name TEXT NOT NULL,
                mp_intro TEXT,
                mp_cover TEXT,
                status INTEGER,
                article_count INTEGER DEFAULT 0,
                full_text_count INTEGER DEFAULT 0,
                last_publish_time INTEGER,
                profile_json TEXT,
                score REAL DEFAULT 0,
                confidence TEXT DEFAULT '',
                tags_json TEXT DEFAULT '[]',
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS articles (
                id TEXT PRIMARY KEY,
                mp_id TEXT,
                mp_name TEXT,
                title TEXT NOT NULL,
                url TEXT,
                description TEXT,
                content TEXT,
                content_html TEXT,
                pic_url TEXT,
                publish_time INTEGER,
                has_content INTEGER DEFAULT 0,
                status INTEGER,
                source_updated_at INTEGER,
                content_chars INTEGER DEFAULT 0,
                content_hash TEXT,
                summary_json TEXT,
                value_score REAL DEFAULT 0,
                tags_json TEXT DEFAULT '[]',
                read_status TEXT DEFAULT 'unread',
                favorite INTEGER DEFAULT 0,
                created_at TEXT,
                synced_at TEXT NOT NULL,
                summarized_at TEXT,
                summary_model TEXT,
                summary_prompt_tokens INTEGER,
                summary_completion_tokens INTEGER,
                summary_total_tokens INTEGER
            );

            CREATE TABLE IF NOT EXISTS runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_type TEXT NOT NULL,
                status TEXT NOT NULL,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                message TEXT,
                stats_json TEXT
            );

            CREATE TABLE IF NOT EXISTS media_assets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                article_id TEXT NOT NULL,
                mp_id TEXT,
                source_url TEXT NOT NULL,
                local_path TEXT,
                content_type TEXT,
                bytes INTEGER DEFAULT 0,
                original_bytes INTEGER DEFAULT 0,
                optimized INTEGER DEFAULT 0,
                stored_format TEXT,
                status TEXT NOT NULL,
                error TEXT,
                cached_at TEXT,
                UNIQUE(article_id, source_url)
            );

            CREATE TABLE IF NOT EXISTS rag_chunks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                article_id TEXT NOT NULL,
                mp_id TEXT,
                chunk_index INTEGER NOT NULL,
                chunk_text TEXT NOT NULL,
                chunk_hash TEXT NOT NULL,
                token_count INTEGER DEFAULT 0,
                embedding_json TEXT,
                embedding_model TEXT,
                source_type TEXT DEFAULT 'article',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(article_id, chunk_hash)
            );

            CREATE INDEX IF NOT EXISTS idx_articles_publish_time ON articles(publish_time DESC);
            CREATE INDEX IF NOT EXISTS idx_articles_score ON articles(value_score DESC);
            CREATE INDEX IF NOT EXISTS idx_articles_mp_id ON articles(mp_id);
            CREATE INDEX IF NOT EXISTS idx_articles_summary ON articles(summarized_at);
            CREATE INDEX IF NOT EXISTS idx_media_article_id ON media_assets(article_id);
            CREATE INDEX IF NOT EXISTS idx_rag_chunks_article_id ON rag_chunks(article_id);
            CREATE INDEX IF NOT EXISTS idx_rag_chunks_mp_id ON rag_chunks(mp_id);
            CREATE INDEX IF NOT EXISTS idx_rag_chunks_hash ON rag_chunks(chunk_hash);
            """
        )
        ensure_column(conn, "articles", "content_hash", "TEXT")
        ensure_column(conn, "articles", "created_at", "TEXT")
        ensure_column(conn, "articles", "summary_model", "TEXT")
        ensure_column(conn, "articles", "summary_prompt_tokens", "INTEGER")
        ensure_column(conn, "articles", "summary_completion_tokens", "INTEGER")
        ensure_column(conn, "articles", "summary_total_tokens", "INTEGER")
        ensure_column(conn, "media_assets", "mp_id", "TEXT")
        ensure_column(conn, "media_assets", "original_bytes", "INTEGER DEFAULT 0")
        ensure_column(conn, "media_assets", "optimized", "INTEGER DEFAULT 0")
        ensure_column(conn, "media_assets", "stored_format", "TEXT")
        ensure_column(conn, "rag_chunks", "mp_id", "TEXT")
        ensure_column(conn, "rag_chunks", "token_count", "INTEGER DEFAULT 0")
        ensure_column(conn, "rag_chunks", "embedding_json", "TEXT")
        ensure_column(conn, "rag_chunks", "embedding_model", "TEXT")
        ensure_column(conn, "rag_chunks", "source_type", "TEXT DEFAULT 'article'")
        ensure_column(conn, "rag_chunks", "created_at", "TEXT")
        ensure_column(conn, "rag_chunks", "updated_at", "TEXT")
        conn.execute("UPDATE articles SET created_at = synced_at WHERE created_at IS NULL")


def ensure_column(conn: sqlite3.Connection, table: str, column: str, declaration: str) -> None:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    names = {row["name"] for row in rows}
    if column not in names:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")


def set_setting(key: str, value: Any) -> None:
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO settings(key, value, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
            """,
            (key, json_dumps(value), utc_now()),
        )


def get_setting(key: str, fallback: Any = None) -> Any:
    with connect() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return json_loads(row["value"] if row else None, fallback)
