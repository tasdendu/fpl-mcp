from __future__ import annotations

import asyncio
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any

import httpx

from .cache import AsyncTTLCache
from .config import Settings

source_records: ContextVar[list[dict[str, Any]] | None] = ContextVar("sources", default=None)


class FPLAPIError(RuntimeError):
    pass


class FPLClient:
    def __init__(self, settings: Settings, cache: AsyncTTLCache | None = None) -> None:
        self.settings = settings
        self.cache = cache or AsyncTTLCache()
        self._requests = asyncio.Semaphore(4)

    async def _get(self, path: str, *, ttl: int, params: dict[str, Any] | None = None) -> Any:
        normalized = path.lstrip("/")
        query_key = "&".join(f"{key}={value}" for key, value in sorted((params or {}).items()))
        cache_key = f"{normalized}?{query_key}"

        async def load() -> Any:
            transport = httpx.AsyncHTTPTransport(retries=2)
            timeout = httpx.Timeout(self.settings.request_timeout_seconds)
            headers = {"User-Agent": self.settings.fpl_user_agent, "Accept": "application/json"}
            async with (
                self._requests,
                httpx.AsyncClient(
                    base_url=f"{self.settings.fpl_base_url.rstrip('/')}/",
                    transport=transport,
                    timeout=timeout,
                    headers=headers,
                    follow_redirects=True,
                ) as http,
            ):
                try:
                    response = await http.get(normalized, params=params)
                    response.raise_for_status()
                    return {
                        "data": response.json(),
                        "source_url": str(response.url),
                        "fetched_at_utc": datetime.now(UTC).isoformat(),
                        "cache_ttl_seconds": ttl,
                    }
                except httpx.HTTPStatusError as exc:
                    status = exc.response.status_code
                    if status == 404:
                        raise FPLAPIError(f"FPL resource not found: {normalized}") from exc
                    raise FPLAPIError(f"FPL API returned HTTP {status} for {normalized}") from exc
                except (httpx.HTTPError, ValueError) as exc:
                    raise FPLAPIError(f"Unable to read FPL data from {normalized}: {exc}") from exc

        record = await self.cache.get_or_set(cache_key, ttl, load)
        sources = source_records.get()
        if sources is not None:
            sources.append({key: value for key, value in record.items() if key != "data"})
        return record["data"]

    async def bootstrap(self) -> dict[str, Any]:
        return await self._get("bootstrap-static/", ttl=300)

    async def fixtures(self, gameweek: int | None = None) -> list[dict[str, Any]]:
        params = {"event": gameweek} if gameweek is not None else None
        return await self._get("fixtures/", ttl=300, params=params)

    async def live(self, gameweek: int) -> dict[str, Any]:
        return await self._get(f"event/{gameweek}/live/", ttl=30)

    async def entry(self, manager_id: int) -> dict[str, Any]:
        return await self._get(f"entry/{manager_id}/", ttl=60)

    async def history(self, manager_id: int) -> dict[str, Any]:
        return await self._get(f"entry/{manager_id}/history/", ttl=300)

    async def transfers(self, manager_id: int) -> list[dict[str, Any]]:
        return await self._get(f"entry/{manager_id}/transfers/", ttl=120)

    async def picks(self, manager_id: int, gameweek: int) -> dict[str, Any]:
        return await self._get(f"entry/{manager_id}/event/{gameweek}/picks/", ttl=60)

    async def league_page(self, league_id: int, page: int = 1) -> dict[str, Any]:
        return await self._get(
            f"leagues-classic/{league_id}/standings/",
            ttl=60,
            params={"page_standings": page},
        )

    async def league_standings(self, league_id: int, pages: int = 1) -> dict[str, Any]:
        page_limit = min(max(1, pages), self.settings.max_league_pages)
        first = await self.league_page(league_id, 1)
        results = list(first.get("standings", {}).get("results", []))
        has_next = bool(first.get("standings", {}).get("has_next"))
        page = 2
        while has_next and page <= page_limit:
            payload = await self.league_page(league_id, page)
            standings = payload.get("standings", {})
            results.extend(standings.get("results", []))
            has_next = bool(standings.get("has_next"))
            page += 1

        return {
            "league": first.get("league", {}),
            "standings": results,
            "pages_loaded": page - 1,
            "has_more": has_next,
        }
