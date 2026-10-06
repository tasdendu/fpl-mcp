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
        self._token_lock = asyncio.Lock()
        self._access_token: str | None = None
        self._token_expires_at = 0.0
        self._refresh_token = settings.fpl_refresh_token

    async def _get_access_token(self, *, force_refresh: bool = False) -> str:
        if not self._refresh_token:
            raise FPLAPIError("FPL_REFRESH_TOKEN is required to read the private team")
        async with self._token_lock:
            if (
                not force_refresh
                and self._access_token
                and asyncio.get_running_loop().time() < self._token_expires_at - 30
            ):
                return self._access_token
            try:
                async with httpx.AsyncClient(
                    timeout=self.settings.request_timeout_seconds,
                    follow_redirects=True,
                ) as http:
                    response = await http.post(
                        self.settings.fpl_token_url,
                        data={
                            "grant_type": "refresh_token",
                            "client_id": self.settings.fpl_client_id,
                            "refresh_token": self._refresh_token,
                        },
                        headers={"User-Agent": self.settings.fpl_user_agent},
                    )
                    response.raise_for_status()
                    payload = response.json()
                access_token = payload.get("access_token")
                if not access_token:
                    raise FPLAPIError("FPL token response did not include an access token")
                self._access_token = str(access_token)
                self._token_expires_at = asyncio.get_running_loop().time() + int(
                    payload.get("expires_in", 28800)
                )
                if payload.get("refresh_token"):
                    self._refresh_token = str(payload["refresh_token"])
                return self._access_token
            except FPLAPIError:
                raise
            except (httpx.HTTPError, ValueError, TypeError) as exc:
                raise FPLAPIError("Unable to refresh FPL access token") from exc

    async def _get(
        self, path: str, *, ttl: int, params: dict[str, Any] | None = None,
        authenticated: bool = False,
    ) -> Any:
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

        if authenticated:
            # Private team state is intentionally never cached.
            async def private_load() -> Any:
                transport = httpx.AsyncHTTPTransport(retries=2)
                async with (
                    self._requests,
                    httpx.AsyncClient(
                        base_url=f"{self.settings.fpl_base_url.rstrip('/')}/",
                        transport=transport,
                        timeout=httpx.Timeout(self.settings.request_timeout_seconds),
                        headers={
                            "User-Agent": self.settings.fpl_user_agent,
                            "Accept": "application/json",
                        },
                        follow_redirects=True,
                    ) as http,
                ):
                    for attempt in range(2):
                        token = await self._get_access_token(force_refresh=attempt == 1)
                        try:
                            response = await http.get(
                                normalized, params=params,
                                headers={"X-API-Authorization": f"Bearer {token}"},
                            )
                            if response.status_code in (401, 403) and attempt == 0:
                                continue
                            response.raise_for_status()
                            data = response.json()
                            sources = source_records.get()
                            if sources is not None:
                                sources.append(
                                    {
                                        "source_url": str(response.url),
                                        "fetched_at_utc": datetime.now(UTC).isoformat(),
                                        "cache_ttl_seconds": 0,
                                    }
                                )
                            return data
                        except httpx.HTTPStatusError as exc:
                            status = exc.response.status_code
                            if status == 404:
                                raise FPLAPIError(f"FPL resource not found: {normalized}") from exc
                            raise FPLAPIError(
                                f"FPL API returned HTTP {status} for {normalized}"
                            ) from exc
                        except (httpx.HTTPError, ValueError) as exc:
                            raise FPLAPIError(f"Unable to read FPL data from {normalized}") from exc
                raise FPLAPIError("FPL authentication failed after refreshing the access token")

            return await private_load()

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

    async def my_team(self, manager_id: int) -> dict[str, Any]:
        return await self._get(f"my-team/{manager_id}/", ttl=0, authenticated=True)

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
