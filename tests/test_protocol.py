import httpx
import pytest
import respx
from starlette.testclient import TestClient

from fpl_mcp.server import mcp


def test_streamable_http_discovery_call_validation_and_host_guard():
    app = mcp.streamable_http_app()
    headers = {"Accept": "application/json, text/event-stream"}
    with TestClient(app, base_url="http://localhost") as client:

        def rpc(method, params=None):
            response = client.post(
                "/mcp",
                headers=headers,
                json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}},
            )
            assert response.status_code == 200, response.text
            return response.json()["result"]

        initialized = rpc(
            "initialize",
            {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "integration-test", "version": "1.0"},
            },
        )
        assert initialized["serverInfo"]["name"] == "Tashi FPL Analyst"
        discovered = rpc("tools/list")["tools"]
        assert len(discovered) == 16
        assert all(tool["annotations"]["readOnlyHint"] for tool in discovered)
        assert all(tool.get("outputSchema") for tool in discovered)
        with respx.mock:
            respx.get("https://fantasy.premierleague.com/api/bootstrap-static/").mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "elements": [
                            {
                                "id": 123,
                                "web_name": "Mamadou Sangaré",
                                "first_name": "Mamadou",
                                "second_name": "Sangaré",
                            }
                        ],
                        "events": [],
                        "teams": [],
                    },
                )
            )
            result = rpc(
                "tools/call", {"name": "search_players", "arguments": {"query": "sangare"}}
            )
            assert not result.get("isError")
            payload = result["structuredContent"]
            assert payload["players"][0]["player_id"] == 123
            assert payload["data_sources"][0]["fetched_at_utc"]

        invalid = rpc("tools/call", {"name": "get_player", "arguments": {"player_id": -1}})
        assert invalid["isError"]
        blocked = client.post("/mcp", headers={**headers, "Host": "evil.invalid"}, json={})
        assert blocked.status_code == 421


@pytest.mark.asyncio
async def test_my_team_returns_authenticated_private_fields():
    from test_analysis import FakeClient

    from fpl_mcp.analysis import FPLAnalysis

    fake = FakeClient()
    squad = await FPLAnalysis(fake, 1).my_team()
    assert squad["squad_scope"] == "Authenticated current private team"
    assert squad["current_free_transfers"] == 2
    assert squad["current_bank"] == 1.5
    assert squad["picks"][0]["selling_price"] == 7.5
