# Project Notes

This is a small standalone FastAPI application. Keep secrets out of source files; use `.env`, environment variables, or the settings table in `data/werss_insight.db`.

The application is intentionally single-process and SQLite-based so it can run beside the existing WeRSS container without modifying it.
