import httpx
import pytest
import respx

from fpl_mcp.client import FPLAPIError, FPLClient
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
    with pytest.raises(FPLAPIError, match="FPL_REFRESH_TOKEN"):
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
            httpx.Response(
                200, json={"access_token": "a1", "refresh_token": "refresh-rotated"}
            ),
            httpx.Response(200, json={"access_token": "a2", "refresh_token": "refresh-3"}),
        ]
    )

    first = FPLClient(settings)
    assert await first._get_access_token() == "a1"

    # Simulated restart: the stale .env token must not be reused.
    second = FPLClient(settings)
    assert await second._get_access_token() == "a2"
    assert "refresh-rotated" in str(token_route.calls[1].request.content)
    assert TokenStore(db).load() == "refresh-3"


def test_parse_token_input_accepts_oidc_user_json() -> None:
    assert parse_token_input('{"refresh_token": "abc", "access_token": "x"}') == "abc"
    assert parse_token_input("  bare-token \n") == "bare-token"
