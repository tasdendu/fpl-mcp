from typing import Any

import pytest

from fpl_mcp.analysis import FPLAnalysis


def player(player_id: int, name: str, team: int, position: int = 3) -> dict[str, Any]:
    return {
        "id": player_id,
        "first_name": name,
        "second_name": "Player",
        "web_name": name,
        "team": team,
        "element_type": position,
        "now_cost": 75,
        "status": "a",
        "news": "",
        "chance_of_playing_next_round": None,
        "total_points": 20,
        "form": "5.0",
        "points_per_game": "5.0",
        "ep_next": "5.5",
        "selected_by_percent": "10.0",
        "minutes": 360,
        "starts": 4,
    }


class FakeClient:
    async def my_team(self, manager_id: int) -> dict[str, Any]:
        return {
            "transfers": {"bank": 15, "value": 1000, "limit": 2, "cost": 0},
            "chips": [{"name": "wildcard", "event": 4, "played_time": None}],
            "picks": [
                {
                    "element": 10,
                    "position": 1,
                    "multiplier": 2,
                    "is_captain": True,
                    "purchase_price": 70,
                    "selling_price": 75,
                }
            ],
        }

    async def bootstrap(self) -> dict[str, Any]:
        return {
            "events": [
                {"id": 4, "is_current": True, "is_next": False},
                {"id": 5, "is_current": False, "is_next": True},
            ],
            "teams": [{"id": 1, "name": "Alpha"}, {"id": 2, "name": "Beta"}],
            "elements": [player(10, "Haaland", 1, 4), player(20, "Salah", 2)],
        }

    async def fixtures(self, gameweek: int | None = None) -> list[dict[str, Any]]:
        return [
            {
                "id": 1,
                "event": gameweek or 5,
                "team_h": 1,
                "team_a": 2,
                "team_h_difficulty": 2,
                "team_a_difficulty": 4,
            }
        ]

    async def picks(self, manager_id: int, gameweek: int) -> dict[str, Any]:
        picks = {
            1: [
                {"element": 10, "multiplier": 2, "is_captain": True, "position": 1},
                {"element": 20, "multiplier": 1, "is_captain": False, "position": 2},
            ],
            2: [
                {"element": 10, "multiplier": 1, "is_captain": False, "position": 1},
                {"element": 20, "multiplier": 2, "is_captain": True, "position": 2},
            ],
            3: [{"element": 10, "multiplier": 3, "is_captain": True, "position": 1}],
        }
        return {"picks": picks[manager_id], "entry_history": {}, "active_chip": None}

    async def history(self, manager_id: int) -> dict[str, Any]:
        return {
            "current": [
                {"event": 1, "points": 20, "total_points": 20},
                {"event": 2, "points": 20, "event_transfers_cost": 4, "total_points": 36},
                {"event": 3, "points": 20, "event_transfers_cost": 8, "total_points": 48},
                {"event": 4, "points": 0, "event_transfers_cost": 4, "total_points": 44},
            ]
        }

    async def live(self, gameweek: int) -> dict[str, Any]:
        return {
            "elements": [
                {"id": 10, "stats": {"total_points": 8}},
                {"id": 20, "stats": {"total_points": 3}},
            ]
        }

    async def league_standings(self, league_id: int, pages: int = 1) -> dict[str, Any]:
        return {
            "league": {"id": league_id, "name": "Test League"},
            "standings": [
                {"entry": 1, "player_name": "One", "entry_name": "First"},
                {"entry": 2, "player_name": "Two", "entry_name": "Second"},
                {"entry": 3, "player_name": "Three", "entry_name": "Third"},
            ],
            "pages_loaded": pages,
            "has_more": False,
        }


@pytest.fixture
def analysis() -> FPLAnalysis:
    return FPLAnalysis(FakeClient(), default_manager_id=1)  # type: ignore[arg-type]


async def test_mini_league_effective_ownership(analysis: FPLAnalysis) -> None:
    result = await analysis.league_ownership(99, gameweek=4)
    haaland = next(row for row in result["players"] if row["player_id"] == 10)
    salah = next(row for row in result["players"] if row["player_id"] == 20)

    assert haaland["ownership_percent"] == 100.0
    assert haaland["effective_ownership_percent"] == 200.0
    assert haaland["captained_by"] == 2
    assert salah["ownership_percent"] == 66.7
    assert salah["effective_ownership_percent"] == 100.0


async def test_compare_managers(analysis: FPLAnalysis) -> None:
    result = await analysis.compare_managers(1, 3, gameweek=4)

    assert [row["player_id"] for row in result["common_players"]] == [10]
    assert [row["player_id"] for row in result["unique_to_a"]] == [20]
    assert result["unique_to_b"] == []
    assert result["captain_a"]["name"] == "Haaland"


async def test_search_players_is_accent_and_case_insensitive(analysis: FPLAnalysis) -> None:
    result = await analysis.search_players("haaLAND")
    assert result["count"] == 1
    assert result["players"][0]["player_id"] == 10


async def test_live_totals_preserve_prior_hits(analysis: FPLAnalysis) -> None:
    result = await analysis.live_league(99)
    row = next(row for row in result["standings"] if row["manager_id"] == 1)
    assert row["live_gameweek_points"] == 15  # 8*2 + 3 - 4
    assert row["live_total_points"] == 63  # previous cumulative net 48 + 15


async def test_captains_use_next_gw_and_prior_eo(analysis: FPLAnalysis) -> None:
    result = await analysis.captain_analysis(league_id=99)
    assert result["gameweek"] == 5
    assert result["ownership_gameweek"] == 4
    with pytest.raises(ValueError, match="next gameweek"):
        await analysis.captain_analysis(gameweek=3)


async def test_blank_week_players_excluded(analysis: FPLAnalysis, monkeypatch) -> None:
    async def blank(gameweek=None):
        return []

    monkeypatch.setattr(analysis.client, "fixtures", blank)
    assert (await analysis.captain_analysis())["candidates"] == []
    assert (await analysis.transfer_targets())["targets"] == []


async def test_partial_ownership_reports_coverage(analysis: FPLAnalysis, monkeypatch) -> None:
    original = analysis.client.picks

    async def missing(manager_id, gameweek):
        if manager_id == 3:
            raise ValueError("Unavailable")
        return await original(manager_id, gameweek)

    monkeypatch.setattr(analysis.client, "picks", missing)
    result = await analysis.league_ownership(99, 4)
    assert result["managers_analyzed"] == 2
    assert result["unavailable_manager_ids"] == [3]
    assert result["players"][0]["effective_ownership_percent"] == 150


async def test_my_team_uses_authenticated_private_squad(analysis: FPLAnalysis) -> None:
    result = await analysis.my_team()
    assert result["squad_scope"] == "Authenticated current private team"
    assert result["current_bank"] == 1.5
    assert result["current_free_transfers"] == 2
    assert result["chips"][0]["name"] == "wildcard"
    assert result["picks"][0]["selling_price"] == 7.5
    assert result["picks"][0]["purchase_price"] == 7.0
