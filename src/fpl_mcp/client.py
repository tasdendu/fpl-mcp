from __future__ import annotations

import asyncio
import logging
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any

import httpx

from .alerts import send_alert
from .cache import AsyncTTLCache
from .config import Settings
from .token_store import TokenStore

log = logging.getLogger(__name__)

source_records: ContextVar[list[dict[str, Any]] | None] = ContextVar("sources", default=None)


class FPLAPIError(RuntimeError):
    pass


class FPLAuthExpiredError(FPLAPIError):
    """FPL rejected the refresh token; a fresh browser login is needed."""


RELOGIN_HINT = (
    "Log in at fantasy.premierleague.com, copy the oidc.user value from the browser's local "
    "storage, and run fpl-mcp-set-token on the server."
)


def _oauth_error(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return f"HTTP {response.status_code}"
    error = str(body.get("error", f"HTTP {response.status_code}"))
    description = body.get("error_description")
    return f"{error}: {description}" if description else error


class FPLClient:
    retry_delays: tuple[float, ...] = (1.0, 3.0)
    keepalive_startup_delay: float = 15.0

    def __init__(self, settings: Settings, cache: AsyncTTLCache | None = None) -> None:
        self.settings = settings
        self.cache = cache or AsyncTTLCache()
        self._requests = asyncio.Semaphore(4)
        self._token_lock = asyncio.Lock()
        self._access_token: str | None = None
        self._token_expires_at = 0.0
        self._refresh_token: str | None = settings.fpl_refresh_token
        self._token_store = TokenStore(settings.fpl_token_db) if settings.fpl_token_db else None
        self._alerted = False
        self.auth_status: dict[str, Any] = {
            "last_success_utc": None,
            "last_error": None,
            "last_error_utc": None,
            "consecutive_failures": 0,
            "needs_relogin": False,
        }

    @property
    def auth_configured(self) -> bool:
        return bool(self.settings.fpl_refresh_token or self._token_store)

    async def _get_access_token(self, *, force_refresh: bool = False) -> str:
        async with self._token_lock:
            loop = asyncio.get_running_loop()
            if (
                not force_refresh
                and self._access_token
                and loop.time() < self._token_expires_at - 60
            ):
                return self._access_token
            # The store is the source of truth: re-read it so a token set with
            # fpl-mcp-set-token, or a new one in .env, is used without a restart.
            if self._token_store is not None:
                self._refresh_token = await asyncio.to_thread(
                    self._token_store.resolve, self.settings.fpl_refresh_token
                )
            try:
                if not self._refresh_token:
                    raise FPLAuthExpiredError("No FPL refresh token configured. " + RELOGIN_HINT)
                payload = await self._exchange_refresh_token(self._refresh_token)
            except FPLAPIError as exc:
                await self._record_failure(exc)
                raise
            self._access_token = str(payload["access_token"])
            self._token_expires_at = loop.time() + int(payload.get("expires_in", 28800))
            if payload.get("refresh_token"):
                self._refresh_token = str(payload["refresh_token"])
                if self._token_store is not None:
                    # Persist immediately: the previous token is now invalid.
                    await asyncio.to_thread(self._token_store.save, self._refresh_token)
            await self._record_success()
            return self._access_token

    async def _exchange_refresh_token(self, refresh_token: str) -> dict[str, Any]:
        """Swap the refresh token for tokens, retrying only transient failures."""
        last_problem = "no response"
        for delay in (0.0, *self.retry_delays):
            if delay:
                await asyncio.sleep(delay)
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
                            "refresh_token": refresh_token,
                        },
                        headers={"User-Agent": self.settings.fpl_user_agent},
                    )
            except httpx.HTTPError as exc:
                last_problem = f"{type(exc).__name__}: {exc}"
                continue
            if response.status_code == 429 or response.status_code >= 500:
                last_problem = f"HTTP {response.status_code}"
                continue
            if response.status_code in (400, 401):
                error = _oauth_error(response)
                if response.status_code == 401 or error.startswith("invalid_grant"):
                    raise FPLAuthExpiredError(
                        f"FPL rejected the refresh token ({error}). " + RELOGIN_HINT
                    )
                raise FPLAPIError(f"FPL token request was refused ({error})")
            try:
                response.raise_for_status()
                payload = response.json()
            except (httpx.HTTPError, ValueError) as exc:
                raise FPLAPIError("Unable to refresh FPL access token") from exc
            if not isinstance(payload, dict) or not payload.get("access_token"):
                raise FPLAPIError("FPL token response did not include an access token")
            return payload
        raise FPLAPIError(
            f"Unable to refresh FPL access token: FPL login service unavailable ({last_problem})"
        )

    async def _record_failure(self, exc: FPLAPIError) -> None:
        status = self.auth_status
        status["consecutive_failures"] += 1
        status["last_error"] = str(exc)
        status["last_error_utc"] = datetime.now(UTC).isoformat()
        status["needs_relogin"] = isinstance(exc, FPLAuthExpiredError)
        # Alert once per incident: immediately for a dead login, after 3 tries otherwise.
        if not self._alerted and (status["needs_relogin"] or status["consecutive_failures"] >= 3):
            self._alerted = True
            await send_alert(self.settings, str(exc))

    async def _record_success(self) -> None:
        if self._alerted:
            await send_alert(self.settings, "FPL login is working again.")
            self._alerted = False
        self.auth_status.update(
            last_success_utc=datetime.now(UTC).isoformat(),
            consecutive_failures=0,
            needs_relogin=False,
        )

    async def keepalive(self) -> None:
        """Refresh on a schedule so an idle refresh token never lapses."""
        hours = self.settings.fpl_token_keepalive_hours
        if not hours or not self.auth_configured:
            return
        await asyncio.sleep(self.keepalive_startup_delay)  # check the login soon after start-up
        while True:
            try:
                await self._get_access_token(force_refresh=True)
                log.info("FPL token keep-alive refresh succeeded")
            except FPLAPIError as exc:
                log.warning("FPL token keep-alive refresh failed: %s", exc)
            await asyncio.sleep(hours * 3600)

    async def _get(
        self,
        path: str,
        *,
        ttl: int,
        params: dict[str, Any] | None = None,
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
                                normalized,
                                params=params,
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
