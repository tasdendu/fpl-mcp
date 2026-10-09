import httpx
import pytest
import respx

from fpl_mcp import client as client_module
from fpl_mcp.client import FPLAPIError, FPLAuthExpiredError, FPLClient
from fpl_mcp.config import Settings
from fpl_mcp.token_store import TokenStore, parse_token_input


@pytest.fixture
def client() -> FPLClient:
    return FPLClient(Settings(fpl_base_url="https://example.test/api"))


@respx.mock
async def test_bootstrap_is_cached(client: FPLClient) -> None:
    route = respx.get("https://example.test/api/bootstrap-static/").mock(
        return_value=httpx.Response(200, json={"events": [], "elements": []})
    )

    first = await client.bootstrap()
    second = await client.bootstrap()

    assert first == second
    assert route.call_count == 1


@respx.mock
async def test_not_found_has_clear_error(client: FPLClient) -> None:
    respx.get("https://example.test/api/entry/999/").mock(return_value=httpx.Response(404))

    with pytest.raises(FPLAPIError, match="not found"):
        await client.entry(999)


@respx.mock
async def test_private_team_refreshes_token_and_retries_authorized_request() -> None:
    settings = Settings(
        fpl_base_url="https://example.test/api",
        fpl_token_url="https://auth.example.test/as/token",
        fpl_refresh_token="refresh-one",
    )
    client = FPLClient(settings)
    token_route = respx.post("https://auth.example.test/as/token").mock(
        side_effect=[
            httpx.Response(
                200,
                json={
                    "access_token": "access-one",
                    "refresh_token": "refresh-two",
                    "expires_in": 3600,
                },
            ),
            httpx.Response(200, json={"access_token": "access-two", "expires_in": 3600}),
        ]
    )
    team_route = respx.get("https://example.test/api/my-team/354978/").mock(
        side_effect=[
            httpx.Response(403),
            httpx.Response(200, json={"picks": [{"element": 1}], "chips": []}),
        ]
    )

    result = await client.my_team(354978)

    assert result["picks"][0]["element"] == 1
    assert token_route.call_count == 2
    assert team_route.call_count == 2
    assert team_route.calls[0].request.headers["X-API-Authorization"] == "Bearer access-one"
    assert team_route.calls[1].request.headers["X-API-Authorization"] == "Bearer access-two"
    assert "refresh-two" in str(token_route.calls[1].request.content)


@respx.mock
async def test_private_team_requires_refresh_token() -> None:
    client = FPLClient(Settings(fpl_base_url="https://example.test/api"))
    with pytest.raises(FPLAPIError, match="No FPL refresh token"):
        await client.my_team(354978)


@respx.mock
async def test_rotated_refresh_token_survives_restart(tmp_path) -> None:
    db = tmp_path / "tokens.db"
    settings = Settings(
        fpl_base_url="https://example.test/api",
        fpl_token_url="https://auth.example.test/as/token",
        fpl_refresh_token="refresh-env",
        fpl_token_db=str(db),
    )
    token_route = respx.post("https://auth.example.test/as/token").mock(
        side_effect=[
            httpx.Response(200, json={"access_token": "a1", "refresh_token": "refresh-rotated"}),
            httpx.Response(200, json={"access_token": "a2", "refresh_token": "refresh-3"}),
        ]
    )

    first = FPLClient(settings)
    assert await first._get_access_token() == "a1"

    # Simulated restart: the stale .env token must not be reused.
    second = FPLClient(settings)
    assert await second._get_access_token() == "a2"
    assert "refresh-rotated" in str(token_route.calls[1].request.content)
    assert TokenStore(db).load().refresh_token == "refresh-3"


def test_parse_token_input_accepts_oidc_user_json() -> None:
    assert parse_token_input('{"refresh_token": "abc", "access_token": "x"}') == "abc"
    assert parse_token_input("  bare-token \n") == "bare-token"


TOKEN_URL = "https://auth.example.test/as/token"


def auth_settings(tmp_path, env_token="refresh-env", **extra) -> Settings:
    return Settings(
        fpl_base_url="https://example.test/api",
        fpl_token_url=TOKEN_URL,
        fpl_refresh_token=env_token,
        fpl_token_db=str(tmp_path / "tokens.db"),
        **extra,
    )


def no_wait(client: FPLClient) -> FPLClient:
    client.retry_delays = (0.0, 0.0)
    return client


@respx.mock
async def test_new_env_token_is_adopted_without_reusing_dead_db_token(tmp_path) -> None:
    db = tmp_path / "tokens.db"
    TokenStore(db).resolve("refresh-old-env")  # earlier deployment seeded the store
    TokenStore(db).save("refresh-rotated-dead")
    route = respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(200, json={"access_token": "a", "refresh_token": "r2"})
    )

    client = FPLClient(auth_settings(tmp_path, env_token="refresh-new-env"))
    await client._get_access_token()

    assert "refresh-new-env" in str(route.calls[0].request.content)


@respx.mock
async def test_unchanged_env_token_does_not_override_rotated_token(tmp_path) -> None:
    db = tmp_path / "tokens.db"
    TokenStore(db).resolve("refresh-env")
    TokenStore(db).save("refresh-rotated")
    route = respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(200, json={"access_token": "a", "refresh_token": "r2"})
    )

    await FPLClient(auth_settings(tmp_path))._get_access_token()

    assert "refresh-rotated" in str(route.calls[0].request.content)


def test_legacy_row_keeps_its_token_and_records_seed(tmp_path) -> None:
    store = TokenStore(tmp_path / "tokens.db")
    store.save("refresh-rotated")  # written before seed tracking existed

    assert store.resolve("refresh-env") == "refresh-rotated"
    assert store.resolve("refresh-env") == "refresh-rotated"
    assert store.resolve("refresh-newer-env") == "refresh-newer-env"


@respx.mock
async def test_set_token_is_picked_up_by_running_client(tmp_path) -> None:
    route = respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(200, json={"access_token": "a", "refresh_token": "r2"})
    )
    client = FPLClient(auth_settings(tmp_path))
    await client._get_access_token()

    TokenStore(tmp_path / "tokens.db").save("refresh-pasted")  # fpl-mcp-set-token
    await client._get_access_token(force_refresh=True)

    assert "refresh-pasted" in str(route.calls[1].request.content)


@respx.mock
async def test_invalid_grant_flags_relogin_and_alerts_once(tmp_path, monkeypatch) -> None:
    alerts: list[str] = []

    async def fake_alert(settings, message):
        alerts.append(message)

    monkeypatch.setattr(client_module, "send_alert", fake_alert)
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(400, json={"error": "invalid_grant"}))
    client = no_wait(FPLClient(auth_settings(tmp_path)))

    for _ in range(2):
        with pytest.raises(FPLAuthExpiredError, match="fpl-mcp-set-token"):
            await client._get_access_token()

    assert client.auth_status["needs_relogin"] is True
    assert client.auth_status["consecutive_failures"] == 2
    assert len(alerts) == 1


@respx.mock
async def test_transient_errors_are_retried_then_recovery_is_announced(
    tmp_path, monkeypatch
) -> None:
    alerts: list[str] = []

    async def fake_alert(settings, message):
        alerts.append(message)

    monkeypatch.setattr(client_module, "send_alert", fake_alert)
    route = respx.post(TOKEN_URL).mock(
        side_effect=[httpx.Response(503)] * 9
        + [httpx.Response(200, json={"access_token": "a", "refresh_token": "r2"})]
    )
    client = no_wait(FPLClient(auth_settings(tmp_path)))

    for _ in range(3):
        with pytest.raises(FPLAPIError, match="unavailable"):
            await client._get_access_token()
    assert client.auth_status["needs_relogin"] is False
    assert len(alerts) == 1  # third consecutive failure

    assert await client._get_access_token() == "a"
    assert route.call_count == 10
    assert alerts[-1] == "FPL login is working again."
    assert client.auth_status["consecutive_failures"] == 0
