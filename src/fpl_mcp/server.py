from __future__ import annotations

from datetime import UTC, datetime
from functools import wraps
from typing import Annotated, Any

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from pydantic import Field

from .analysis import FPLAnalysis
from .client import FPLClient, source_records
from .config import get_settings

settings = get_settings()
client = FPLClient(settings)
analysis = FPLAnalysis(client, settings.fpl_entry_id)

READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=True)

mcp = FastMCP(
    "Tashi FPL Analyst",
    instructions=(
        "Read-only Fantasy Premier League data and mini-league analysis. Before recommending a "
        "transfer or captain, check the manager's actual latest public squad, the current "
        "gameweek, player availability, fixtures, and relevant mini-league ownership. Clearly "
        "separate official "
        "data from heuristic rankings. Never claim a transfer has been made."
    ),
    host=settings.mcp_host,
    port=settings.mcp_port,
    stateless_http=True,
    json_response=True,
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=settings.mcp_allowed_hosts,
        allowed_origins=settings.mcp_allowed_origins,
    ),
)


def sourced_tool(*, title: str, annotations: ToolAnnotations):
    """Attach actual upstream fetch times, including cache ages, to every successful tool."""

    def decorate(function):
        @wraps(function)
        async def wrapped(*args, **kwargs):
            records: list[dict[str, Any]] = []
            token = source_records.set(records)
            try:
                result = await function(*args, **kwargs)
                unique = {record["source_url"]: record for record in records}
                return {
                    **result,
                    "data_sources": list(unique.values()),
                    "response_at_utc": datetime.now(UTC).isoformat(),
                }
            finally:
                source_records.reset(token)

        return mcp.tool(title=title, annotations=annotations)(wrapped)

    return decorate


@sourced_tool(title="Get my latest published squad", annotations=READ_ONLY)
async def get_my_team() -> dict[str, Any]:
    """Get the default entry's latest published deadline squad. Pending changes are unknown."""
    return await analysis.my_team()


@sourced_tool(title="Get official price changes", annotations=READ_ONLY)
async def get_price_changes() -> dict[str, Any]:
    """Get official net price changes this event. Does not predict nightly rises or falls."""
    return await analysis.price_changes()


@sourced_tool(title="Get FPL overview", annotations=READ_ONLY)
async def get_fpl_overview() -> dict[str, Any]:
    """Get the current or next gameweek, deadline, teams, data timestamp, and API limitations."""
    return await analysis.overview()


@sourced_tool(title="Search FPL players", annotations=READ_ONLY)
async def search_players(
    query: Annotated[
        str, Field(description="Full or partial player name; use an empty string to browse")
    ] = "",
    position: Annotated[
        int | None, Field(ge=1, le=4, description="1 GK, 2 DEF, 3 MID, 4 FWD")
    ] = None,
    team: Annotated[str | None, Field(description="Full or partial club name")] = None,
    limit: Annotated[int, Field(ge=1, le=50)] = 20,
) -> dict[str, Any]:
    """Find player IDs and current official price, status, form, points, and ownership data."""
    return await analysis.search_players(query, position, team, limit)


@sourced_tool(title="Get FPL player", annotations=READ_ONLY)
async def get_player(
    player_id: Annotated[int, Field(ge=1, description="Official FPL player element ID")],
    future_gameweeks: Annotated[int, Field(ge=1, le=10)] = 5,
) -> dict[str, Any]:
    """Get detailed official data and upcoming fixtures for one player."""
    return await analysis.player(player_id, future_gameweeks)


@sourced_tool(title="Get FPL fixtures", annotations=READ_ONLY)
async def get_fixtures(
    gameweek: Annotated[int | None, Field(ge=1, le=38)] = None,
    team_id: Annotated[int | None, Field(ge=1, description="Official FPL team ID")] = None,
) -> dict[str, Any]:
    """Get fixtures and official fixture difficulty, optionally for one gameweek or club."""
    return await analysis.fixtures(gameweek, team_id)


@sourced_tool(title="Get FPL manager", annotations=READ_ONLY)
async def get_manager(
    manager_id: Annotated[
        int | None, Field(ge=1, description="FPL entry ID; defaults to Tashi's team")
    ] = None,
) -> dict[str, Any]:
    """Get a manager's public team summary, rank, points, and classic league memberships."""
    return await analysis.manager(manager_id)


@sourced_tool(title="Get manager history", annotations=READ_ONLY)
async def get_manager_history(
    manager_id: Annotated[
        int | None, Field(ge=1, description="FPL entry ID; defaults to Tashi's team")
    ] = None,
) -> dict[str, Any]:
    """Get gameweek history, chips, ranks, transfer costs, and past-season totals."""
    return await analysis.manager_history(manager_id)


@sourced_tool(title="Get manager transfers", annotations=READ_ONLY)
async def get_manager_transfers(
    manager_id: Annotated[
        int | None, Field(ge=1, description="FPL entry ID; defaults to Tashi's team")
    ] = None,
) -> dict[str, Any]:
    """Get a manager's completed public transfer history with player names and prices."""
    return await analysis.manager_transfers(manager_id)


@sourced_tool(title="Get manager gameweek squad", annotations=READ_ONLY)
async def get_manager_gameweek(
    gameweek: Annotated[int, Field(ge=1, le=38)],
    manager_id: Annotated[
        int | None, Field(ge=1, description="FPL entry ID; defaults to Tashi's team")
    ] = None,
) -> dict[str, Any]:
    """Get picks, captain, bench, chip, points, and transfer cost for a manager and gameweek."""
    return await analysis.manager_gameweek(gameweek, manager_id)


@sourced_tool(title="Get mini-league standings", annotations=READ_ONLY)
async def get_mini_league_standings(
    league_id: Annotated[int, Field(ge=1, description="Classic mini-league ID")],
    pages: Annotated[
        int, Field(ge=1, le=20, description="Load up to this many 50-manager pages")
    ] = 1,
) -> dict[str, Any]:
    """Get public classic mini-league standings and manager IDs for rival analysis."""
    return await analysis.league_standings(league_id, pages)


@sourced_tool(title="Compare FPL managers", annotations=READ_ONLY)
async def compare_managers(
    manager_a: Annotated[int, Field(ge=1)],
    manager_b: Annotated[int, Field(ge=1)],
    gameweek: Annotated[int | None, Field(ge=1, le=38)] = None,
) -> dict[str, Any]:
    """Compare two squads, showing common picks, differentials, and captains for a gameweek."""
    return await analysis.compare_managers(manager_a, manager_b, gameweek)


@sourced_tool(title="Analyze mini-league ownership", annotations=READ_ONLY)
async def analyze_mini_league_ownership(
    league_id: Annotated[int, Field(ge=1, description="Classic mini-league ID")],
    gameweek: Annotated[int, Field(ge=1, le=38)],
    pages: Annotated[int, Field(ge=1, le=20)] = 1,
) -> dict[str, Any]:
    """Calculate ownership, effective ownership, and captain counts inside a mini-league."""
    return await analysis.league_ownership(league_id, gameweek, pages)


@sourced_tool(title="Analyze captain candidates", annotations=READ_ONLY)
async def analyze_captains(
    gameweek: Annotated[int | None, Field(ge=1, le=38)] = None,
    league_id: Annotated[
        int | None, Field(ge=1, description="Include mini-league EO when supplied")
    ] = None,
    pages: Annotated[int, Field(ge=1, le=20)] = 1,
    limit: Annotated[int, Field(ge=1, le=25)] = 10,
) -> dict[str, Any]:
    """Rank captain candidates with current-data heuristics and optional mini-league EO."""
    return await analysis.captain_analysis(gameweek, league_id, pages, limit)


@sourced_tool(title="Rank transfer targets", annotations=READ_ONLY)
async def rank_transfer_targets(
    position: Annotated[
        int | None, Field(ge=1, le=4, description="1 GK, 2 DEF, 3 MID, 4 FWD")
    ] = None,
    max_price: Annotated[
        float | None, Field(gt=0, description="Maximum price in millions, e.g. 7.5")
    ] = None,
    horizon: Annotated[int, Field(ge=1, le=10)] = 5,
    limit: Annotated[int, Field(ge=1, le=30)] = 15,
) -> dict[str, Any]:
    """Create a current transfer shortlist by price, position, availability, form, and fixtures."""
    return await analysis.transfer_targets(position, max_price, horizon, limit)


@sourced_tool(title="Get live mini-league", annotations=READ_ONLY)
async def get_live_mini_league(
    league_id: Annotated[int, Field(ge=1, description="Classic mini-league ID")],
    pages: Annotated[int, Field(ge=1, le=20)] = 1,
) -> dict[str, Any]:
    """Estimate live mini-league ranks from official live points, picks, hits, and prior totals."""
    return await analysis.live_league(league_id, pages)


def main() -> None:
    mcp.run(transport="streamable-http")


if __name__ == "__main__":
    main()
