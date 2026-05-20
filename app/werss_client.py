from typing import Any

import httpx

from .config import Settings


class WeRssClient:
    def __init__(self, settings: Settings):
        self.settings = settings

    @property
    def headers(self) -> dict[str, str]:
        if not self.settings.werss_access_key or not self.settings.werss_secret_key:
            return {}
        return {
            "Authorization": (
                f"AK-SK {self.settings.werss_access_key}:{self.settings.werss_secret_key}"
            )
        }

    async def request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        url = f"{self.settings.werss_base_url}{path}"
        # Local WeRSS runs on the LAN; avoid inheriting desktop proxy settings
        # that can turn healthy local requests into upstream 502s.
        async with httpx.AsyncClient(timeout=60, trust_env=False) as client:
            response = await client.request(method, url, headers=self.headers, **kwargs)
            response.raise_for_status()
            payload = response.json()
        if isinstance(payload, dict) and "data" in payload:
            return payload.get("data") or {}
        return payload

    async def get_accounts(self, limit: int = 100) -> list[dict[str, Any]]:
        data = await self.request("GET", f"/api/v1/wx/mps?limit={limit}&offset=0")
        return list(data.get("list") or [])

    async def get_articles(
        self,
        limit: int = 100,
        offset: int = 0,
        has_content: bool | None = None,
        status: str | None = None,
    ) -> tuple[list[dict[str, Any]], int]:
        params = [f"limit={limit}", f"offset={offset}"]
        if has_content is not None:
            params.append(f"has_content={'true' if has_content else 'false'}")
        if status:
            params.append(f"status={status}")
        data = await self.request("GET", f"/api/v1/wx/articles?{'&'.join(params)}")
        return list(data.get("list") or []), int(data.get("total") or 0)

    async def get_article_detail(self, article_id: str) -> dict[str, Any]:
        return await self.request("GET", f"/api/v1/wx/articles/{article_id}?content=true")

    async def get_system_info(self) -> dict[str, Any]:
        return await self.request("GET", "/api/v1/wx/sys/info")

    async def get_queue_status(self) -> dict[str, Any]:
        return await self.request("GET", "/api/v1/wx/task-queue/status")

    async def get_content_queue_status(self) -> dict[str, Any]:
        return await self.request("GET", "/api/v1/wx/task-queue/content/status")
