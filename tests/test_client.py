import httpx
import pytest
import respx

from fpl_mcp.client import FPLAPIError, FPLClient
from fpl_mcp.config import Settings


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
