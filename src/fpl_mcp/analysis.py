from __future__ import annotations

import asyncio
import re
import unicodedata
from collections import Counter, defaultdict
from typing import Any

from .client import FPLClient

POSITION_NAMES = {1: "Goalkeeper", 2: "Defender", 3: "Midfielder", 4: "Forward"}


def _number(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _normalize(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value)
    return re.sub(r"[^a-z0-9]+", " ", decomposed.encode("ascii", "ignore").decode().lower()).strip()


def _current_event(bootstrap: dict[str, Any]) -> dict[str, Any] | None:
    events = bootstrap.get("events", [])
    return next((event for event in events if event.get("is_current")), None) or next(
        (event for event in events if event.get("is_next")),
        None,
    )


def _next_event(bootstrap: dict[str, Any]) -> dict[str, Any]:
    event = next((row for row in bootstrap.get("events", []) if row.get("is_next")), None)
    if event is None:
        raise ValueError(
            "No upcoming gameweek is published; future recommendations are unavailable."
        )
    return event


class FPLAnalysis:
    def __init__(self, client: FPLClient, default_manager_id: int) -> None:
        self.client = client
        self.default_manager_id = default_manager_id

    async def context(self) -> tuple[dict[int, dict[str, Any]], dict[int, str], dict[str, Any]]:
        bootstrap = await self.client.bootstrap()
        players = {player["id"]: player for player in bootstrap.get("elements", [])}
        teams = {team["id"]: team["name"] for team in bootstrap.get("teams", [])}
        return players, teams, bootstrap

    async def overview(self) -> dict[str, Any]:
        players, teams, bootstrap = await self.context()
        event = _current_event(bootstrap)
        return {
            "current_or_next_gameweek": event,
            "next_gameweek": next(
                (row for row in bootstrap.get("events", []) if row.get("is_next")), None
            ),
            "player_count": len(players),
            "teams": teams,
            "last_updated": bootstrap.get("last_updated_data"),
            "default_manager_id": self.default_manager_id,
            "important_limit": (
                "Public FPL data shows a manager's gameweek squad only after that gameweek's "
                "deadline. Pending transfers, bank balance, and unused free transfers require "
                "authenticated FPL access and are intentionally not included in this "
                "read-only version."
            ),
        }

    async def search_players(
        self,
        query: str,
        position: int | None = None,
        team: str | None = None,
        limit: int = 20,
    ) -> dict[str, Any]:
        players, teams, _ = await self.context()
        needle = _normalize(query)
        team_needle = _normalize(team or "")
        matches = []
        for player in players.values():
            full_name = f"{player.get('first_name', '')} {player.get('second_name', '')}".strip()
            searchable = _normalize(f"{full_name} {player.get('web_name', '')}")
            if needle and needle not in searchable:
                continue
            if position and player.get("element_type") != position:
                continue
            team_name = teams.get(player.get("team"), "Unknown")
            if team_needle and team_needle not in _normalize(team_name):
                continue
            matches.append(self._player_summary(player, teams))
        matches.sort(key=lambda item: (-item["total_points"], item["name"]))
        return {"count": min(len(matches), limit), "players": matches[: max(1, min(limit, 50))]}

    async def player(self, player_id: int, future_gameweeks: int = 5) -> dict[str, Any]:
        players, teams, bootstrap = await self.context()
        player = players.get(player_id)
        if not player:
            raise ValueError(f"Unknown FPL player ID: {player_id}")
        event = _current_event(bootstrap)
        start_gw = int(event["id"]) if event else 1
        fixtures = await self.client.fixtures()
        upcoming = [
            self._fixture_view(item, teams, player.get("team"))
            for item in fixtures
            if item.get("event")
            and item["event"] >= start_gw
            and item["event"] < start_gw + future_gameweeks
            and not item.get("finished")
            and player.get("team") in (item.get("team_h"), item.get("team_a"))
        ]
        return {"player": self._player_summary(player, teams, detailed=True), "upcoming": upcoming}

    async def fixtures(
        self,
        gameweek: int | None = None,
        team_id: int | None = None,
    ) -> dict[str, Any]:
        _, teams, bootstrap = await self.context()
        if gameweek is None:
            event = _current_event(bootstrap)
            gameweek = int(event["id"]) if event else None
        rows = await self.client.fixtures(gameweek)
        if team_id:
            rows = [row for row in rows if team_id in (row.get("team_h"), row.get("team_a"))]
        return {
            "gameweek": gameweek,
            "fixtures": [self._fixture_view(row, teams, team_id) for row in rows],
        }

    async def manager(self, manager_id: int | None = None) -> dict[str, Any]:
        manager_id = manager_id or self.default_manager_id
        entry = await self.client.entry(manager_id)
        return {
            "manager_id": manager_id,
            "manager_name": (
                f"{entry.get('player_first_name', '')} {entry.get('player_last_name', '')}"
            ).strip(),
            "team_name": entry.get("name"),
            "overall_points": entry.get("summary_overall_points"),
            "overall_rank": entry.get("summary_overall_rank"),
            "gameweek_points": entry.get("summary_event_points"),
            "gameweek_rank": entry.get("summary_event_rank"),
            "started_gameweek": entry.get("started_event"),
            "favourite_team_id": entry.get("favourite_team"),
            "leagues": entry.get("leagues", {}),
        }

    async def manager_history(self, manager_id: int | None = None) -> dict[str, Any]:
        manager_id = manager_id or self.default_manager_id
        history = await self.client.history(manager_id)
        return {"manager_id": manager_id, **history}

    async def my_team(self) -> dict[str, Any]:
        players, teams, _ = await self.context()
        payload = await self.client.my_team(self.default_manager_id)
        transfers = payload.get("transfers", {}) or {}
        picks = []
        for pick in payload.get("picks", []):
            row = self._pick_view(pick, players, teams)
            row["purchase_price"] = _number(pick.get("purchase_price")) / 10
            row["selling_price"] = _number(pick.get("selling_price")) / 10
            picks.append(row)
        return {
            "manager_id": self.default_manager_id,
            "squad_scope": "Authenticated current private team",
            "current_bank": _number(transfers.get("bank")) / 10,
            "squad_value": _number(transfers.get("value")) / 10,
            "current_free_transfers": transfers.get("limit"),
            "transfer_cost": transfers.get("cost"),
            "chips": payload.get("chips", []),
            "picks": picks,
            "transfers": payload.get("transfers", []),
        }

    async def price_changes(self) -> dict[str, Any]:
        players, teams, _ = await self.context()
        rows = [
            self._player_summary(player, teams, detailed=True)
            for player in players.values()
            if _number(player.get("cost_change_event")) != 0
        ]
        rows.sort(key=lambda row: (-row["cost_change_event"], row["player_id"]))
        return {
            "scope": "Official net price changes in the current event, not nightly predictions",
            "predicted_price_changes": None,
            "players": rows,
        }

    async def manager_transfers(self, manager_id: int | None = None) -> dict[str, Any]:
        manager_id = manager_id or self.default_manager_id
        players, teams, _ = await self.context()
        transfers = await self.client.transfers(manager_id)
        rows = []
        for item in transfers:
            incoming = players.get(item.get("element_in"), {})
            outgoing = players.get(item.get("element_out"), {})
            rows.append(
                {
                    "gameweek": item.get("event"),
                    "time": item.get("time"),
                    "player_in": self._player_summary(incoming, teams)
                    if incoming
                    else item.get("element_in"),
                    "player_out": self._player_summary(outgoing, teams)
                    if outgoing
                    else item.get("element_out"),
                    "purchase_price": _number(item.get("element_in_cost")) / 10,
                    "sale_price": _number(item.get("element_out_cost")) / 10,
                }
            )
        return {"manager_id": manager_id, "transfer_count": len(rows), "transfers": rows}

    async def manager_gameweek(
        self, gameweek: int, manager_id: int | None = None
    ) -> dict[str, Any]:
        manager_id = manager_id or self.default_manager_id
        players, teams, _ = await self.context()
        payload = await self.client.picks(manager_id, gameweek)
        picks = [self._pick_view(pick, players, teams) for pick in payload.get("picks", [])]
        return {
            "manager_id": manager_id,
            "gameweek": gameweek,
            "active_chip": payload.get("active_chip"),
            "automatic_substitutions": payload.get("automatic_subs", []),
            "entry_history": payload.get("entry_history", {}),
            "picks": picks,
        }

    async def league_standings(self, league_id: int, pages: int = 1) -> dict[str, Any]:
        payload = await self.client.league_standings(league_id, pages)
        return {
            **payload,
            "standings": [
                {
                    "rank": row.get("rank"),
                    "last_rank": row.get("last_rank"),
                    "manager_id": row.get("entry"),
                    "manager_name": row.get("player_name"),
                    "team_name": row.get("entry_name"),
                    "gameweek_points": row.get("event_total"),
                    "total_points": row.get("total"),
                }
                for row in payload["standings"]
            ],
        }

    async def compare_managers(
        self,
        manager_a: int,
        manager_b: int,
        gameweek: int | None = None,
    ) -> dict[str, Any]:
        _, _, bootstrap = await self.context()
        if gameweek is None:
            event = _current_event(bootstrap)
            if not event:
                raise ValueError("FPL has no current or next gameweek")
            gameweek = int(event["id"])
        a, b = await asyncio.gather(
            self.manager_gameweek(gameweek, manager_a),
            self.manager_gameweek(gameweek, manager_b),
        )
        picks_a = {item["player_id"]: item for item in a["picks"]}
        picks_b = {item["player_id"]: item for item in b["picks"]}
        ids_a, ids_b = set(picks_a), set(picks_b)
        captain_a = next((item for item in a["picks"] if item["is_captain"]), None)
        captain_b = next((item for item in b["picks"] if item["is_captain"]), None)
        return {
            "gameweek": gameweek,
            "manager_a": manager_a,
            "manager_b": manager_b,
            "common_players": [picks_a[player_id] for player_id in sorted(ids_a & ids_b)],
            "unique_to_a": [picks_a[player_id] for player_id in sorted(ids_a - ids_b)],
            "unique_to_b": [picks_b[player_id] for player_id in sorted(ids_b - ids_a)],
            "captain_a": captain_a,
            "captain_b": captain_b,
        }

    async def league_ownership(
        self,
        league_id: int,
        gameweek: int,
        pages: int = 1,
    ) -> dict[str, Any]:
        players, teams, _ = await self.context()
        league = await self.client.league_standings(league_id, pages)
        manager_ids = [int(row["entry"]) for row in league["standings"]]
        responses = await asyncio.gather(
            *(self.client.picks(manager_id, gameweek) for manager_id in manager_ids),
            return_exceptions=True,
        )
        ownership: Counter[int] = Counter()
        effective: defaultdict[int, int] = defaultdict(int)
        captaincy: Counter[int] = Counter()
        valid = 0
        unavailable = []
        for manager_id, response in zip(manager_ids, responses, strict=True):
            if isinstance(response, Exception):
                unavailable.append(manager_id)
                continue
            valid += 1
            for pick in response.get("picks", []):
                player_id = int(pick["element"])
                ownership[player_id] += 1
                effective[player_id] += int(pick.get("multiplier", 0))
                if pick.get("is_captain"):
                    captaincy[player_id] += 1
        denominator = max(valid, 1)
        rows = []
        for player_id, count in ownership.most_common():
            player = players.get(player_id, {})
            rows.append(
                {
                    **self._player_summary(player or {"id": player_id}, teams),
                    "managers_owned": count,
                    "ownership_percent": round(count / denominator * 100, 1),
                    "effective_ownership_percent": round(
                        effective[player_id] / denominator * 100, 1
                    ),
                    "captained_by": captaincy[player_id],
                }
            )
        rows.sort(
            key=lambda item: (-item["effective_ownership_percent"], -item["ownership_percent"])
        )
        return {
            "league": league.get("league", {}),
            "gameweek": gameweek,
            "managers_analyzed": valid,
            "pages_loaded": league["pages_loaded"],
            "has_more": league["has_more"],
            "scope": "Loaded standings pages; percentages exclude unavailable squads",
            "eo_method": "100 * sum(published pick multipliers) / available managers",
            "unavailable_manager_ids": unavailable,
            "players": rows,
        }

    async def captain_analysis(
        self,
        gameweek: int | None = None,
        league_id: int | None = None,
        pages: int = 1,
        limit: int = 10,
    ) -> dict[str, Any]:
        players, teams, bootstrap = await self.context()
        next_event = _next_event(bootstrap)
        if gameweek is None:
            gameweek = int(next_event["id"])
        if gameweek != int(next_event["id"]):
            raise ValueError("Captain rankings support only the next gameweek (FPL ep_next).")
        fixtures = await self.client.fixtures(gameweek)
        difficulties: defaultdict[int, list[int]] = defaultdict(list)
        for fixture in fixtures:
            difficulties[int(fixture["team_h"])].append(int(fixture.get("team_h_difficulty", 3)))
            difficulties[int(fixture["team_a"])].append(int(fixture.get("team_a_difficulty", 3)))
        league_eo: dict[int, float] = {}
        eo_gameweek = None
        eo_coverage = None
        if league_id:
            current = next(
                (row for row in bootstrap.get("events", []) if row.get("is_current")), None
            )
            if current:
                eo_gameweek = int(current["id"])
                ownership = await self.league_ownership(league_id, eo_gameweek, pages)
                eo_coverage = {
                    key: ownership[key]
                    for key in ("managers_analyzed", "unavailable_manager_ids", "has_more")
                }
                league_eo = {
                    int(row["player_id"]): _number(row["effective_ownership_percent"])
                    for row in ownership["players"]
                }
        candidates = []
        for player in players.values():
            if player.get("status") not in {"a", "d"}:
                continue
            team_id = int(player["team"])
            fdr_values = difficulties.get(team_id, [])
            if not fdr_values:
                continue
            fixture_factor = sum(6 - value for value in fdr_values) / len(fdr_values)
            score = (
                _number(player.get("ep_next")) * 0.45
                + _number(player.get("form")) * 0.30
                + _number(player.get("points_per_game")) * 0.15
                + fixture_factor * 0.10
            )
            if player.get("chance_of_playing_next_round") is not None:
                score *= _number(player["chance_of_playing_next_round"]) / 100
            candidates.append(
                {
                    **self._player_summary(player, teams),
                    "heuristic_score": round(score, 2),
                    "fixture_difficulties": fdr_values,
                    "mini_league_effective_ownership_percent": league_eo.get(int(player["id"])),
                }
            )
        candidates.sort(key=lambda item: (-item["heuristic_score"], -item["total_points"]))
        return {
            "gameweek": gameweek,
            "ownership_gameweek": eo_gameweek,
            "ownership_coverage": eo_coverage,
            "ownership_note": "Past published EO; next-deadline rival captaincy is unknown.",
            "method": (
                "Transparent heuristic using FPL expected points, form, points per game, "
                "availability, and fixture difficulty; it is not a statistical forecast."
            ),
            "candidates": candidates[: max(1, min(limit, 25))],
        }

    async def transfer_targets(
        self,
        position: int | None = None,
        max_price: float | None = None,
        horizon: int = 5,
        limit: int = 15,
    ) -> dict[str, Any]:
        players, teams, bootstrap = await self.context()
        event = _next_event(bootstrap)
        start_gw = int(event["id"])
        fixtures = await self.client.fixtures()
        fdr: defaultdict[int, list[int]] = defaultdict(list)
        for fixture in fixtures:
            fixture_gw = fixture.get("event")
            if not fixture_gw or not start_gw <= fixture_gw < start_gw + horizon:
                continue
            fdr[int(fixture["team_h"])].append(int(fixture.get("team_h_difficulty", 3)))
            fdr[int(fixture["team_a"])].append(int(fixture.get("team_a_difficulty", 3)))
        candidates = []
        for player in players.values():
            if position and player.get("element_type") != position:
                continue
            price = _number(player.get("now_cost")) / 10
            if max_price is not None and price > max_price:
                continue
            if player.get("status") not in {"a", "d"} or _number(player.get("minutes")) <= 0:
                continue
            team_fdr = fdr.get(int(player["team"]), [])
            if not team_fdr:
                continue
            average_fdr = sum(team_fdr) / len(team_fdr) if team_fdr else 3.0
            fixture_score = 6 - average_fdr
            minutes_reliability = min(_number(player.get("minutes")) / max(start_gw * 90, 90), 1.0)
            score = (
                _number(player.get("ep_next")) * 0.35
                + _number(player.get("form")) * 0.25
                + _number(player.get("points_per_game")) * 0.20
                + fixture_score * 0.15
                + minutes_reliability * 0.05
            )
            if player.get("chance_of_playing_next_round") is not None:
                score *= _number(player["chance_of_playing_next_round"]) / 100
            candidates.append(
                {
                    **self._player_summary(player, teams),
                    "heuristic_score": round(score, 2),
                    "average_fixture_difficulty": round(average_fdr, 2),
                    "fixture_count": len(team_fdr),
                }
            )
        candidates.sort(key=lambda item: (-item["heuristic_score"], -item["total_points"]))
        return {
            "start_gameweek": start_gw,
            "horizon_gameweeks": horizon,
            "filters": {"position": POSITION_NAMES.get(position), "max_price": max_price},
            "method": (
                "Transparent shortlist heuristic; verify injuries and team news before acting."
            ),
            "targets": candidates[: max(1, min(limit, 30))],
        }

    async def live_league(self, league_id: int, pages: int = 1) -> dict[str, Any]:
        players, _, bootstrap = await self.context()
        event = next((item for item in bootstrap.get("events", []) if item.get("is_current")), None)
        if not event:
            raise ValueError("There is no current live gameweek")
        gameweek = int(event["id"])
        league, live = await asyncio.gather(
            self.client.league_standings(league_id, pages),
            self.client.live(gameweek),
        )
        live_points = {
            int(row["id"]): _number(row.get("stats", {}).get("total_points"))
            for row in live.get("elements", [])
        }
        standings = league["standings"]
        manager_ids = [int(row["entry"]) for row in standings]
        bundles = await asyncio.gather(
            *(
                asyncio.gather(
                    self.client.picks(manager_id, gameweek),
                    self.client.history(manager_id),
                )
                for manager_id in manager_ids
            ),
            return_exceptions=True,
        )
        rows = []
        unavailable = []
        for standing, bundle in zip(standings, bundles, strict=True):
            manager_id = int(standing["entry"])
            if isinstance(bundle, Exception):
                unavailable.append(manager_id)
                continue
            picks, history = bundle
            live_gameweek_points = sum(
                live_points.get(int(pick["element"]), 0) * int(pick.get("multiplier", 0))
                for pick in picks.get("picks", [])
            )
            event_history = next(
                (
                    item
                    for item in history.get("current", [])
                    if int(item.get("event", 0)) == gameweek
                ),
                {},
            )
            transfer_cost = _number(
                picks.get("entry_history", {}).get(
                    "event_transfers_cost", event_history.get("event_transfers_cost")
                )
            )
            prior_events = [
                item for item in history.get("current", []) if int(item.get("event", 0)) < gameweek
            ]
            previous_total = (
                _number(max(prior_events, key=lambda item: item["event"])["total_points"])
                if prior_events
                else 0
            )
            captain = next(
                (pick for pick in picks.get("picks", []) if pick.get("is_captain")), None
            )
            captain_player = players.get(captain.get("element"), {}) if captain else {}
            rows.append(
                {
                    "manager_id": manager_id,
                    "manager_name": standing.get("player_name"),
                    "team_name": standing.get("entry_name"),
                    "live_gameweek_points": int(live_gameweek_points - transfer_cost),
                    "transfer_cost": int(transfer_cost),
                    "live_total_points": int(previous_total + live_gameweek_points - transfer_cost),
                    "captain": captain_player.get("web_name"),
                    "captain_live_points": (
                        int(
                            live_points.get(int(captain["element"]), 0)
                            * int(captain.get("multiplier", 0))
                        )
                        if captain
                        else 0
                    ),
                }
            )
        rows.sort(key=lambda item: (-item["live_total_points"], -item["live_gameweek_points"]))
        for rank, row in enumerate(rows, start=1):
            row["provisional_rank_in_loaded_sample"] = (
                rows[rank - 2]["provisional_rank_in_loaded_sample"]
                if rank > 1 and row["live_total_points"] == rows[rank - 2]["live_total_points"]
                else rank
            )
        return {
            "league": league.get("league", {}),
            "gameweek": gameweek,
            "explain": (
                "Live totals use official live points, public picks, prior gameweek history, "
                "and transfer costs. Autosubs and bonus can still change until FPL finalizes the "
                "gameweek."
            ),
            "standings": rows,
            "pages_loaded": league["pages_loaded"],
            "has_more": league["has_more"],
            "ranking_note": "Overall season totals within loaded pages; ties share rank. "
            "Does not apply official transfer tie-breaks or custom league start events.",
            "unavailable_manager_ids": unavailable,
        }

    @staticmethod
    def _player_summary(
        player: dict[str, Any],
        teams: dict[int, str],
        *,
        detailed: bool = False,
    ) -> dict[str, Any]:
        result = {
            "player_id": player.get("id"),
            "name": player.get("web_name") or player.get("second_name"),
            "full_name": f"{player.get('first_name', '')} {player.get('second_name', '')}".strip(),
            "team": teams.get(player.get("team"), "Unknown"),
            "position": POSITION_NAMES.get(player.get("element_type"), "Unknown"),
            "price": _number(player.get("now_cost")) / 10,
            "status": player.get("status"),
            "news": player.get("news") or None,
            "chance_next_round": player.get("chance_of_playing_next_round"),
            "total_points": int(_number(player.get("total_points"))),
            "form": _number(player.get("form")),
            "points_per_game": _number(player.get("points_per_game")),
            "expected_points_next": _number(player.get("ep_next")),
            "selected_by_percent": _number(player.get("selected_by_percent")),
        }
        if detailed:
            result.update(
                {
                    "minutes": int(_number(player.get("minutes"))),
                    "starts": int(_number(player.get("starts"))),
                    "goals": int(_number(player.get("goals_scored"))),
                    "assists": int(_number(player.get("assists"))),
                    "clean_sheets": int(_number(player.get("clean_sheets"))),
                    "bonus": int(_number(player.get("bonus"))),
                    "bps": int(_number(player.get("bps"))),
                    "expected_goals": _number(player.get("expected_goals")),
                    "expected_assists": _number(player.get("expected_assists")),
                    "expected_goal_involvements": _number(player.get("expected_goal_involvements")),
                    "transfers_in_event": int(_number(player.get("transfers_in_event"))),
                    "transfers_out_event": int(_number(player.get("transfers_out_event"))),
                    "cost_change_event": _number(player.get("cost_change_event")) / 10,
                }
            )
        return result

    @staticmethod
    def _pick_view(
        pick: dict[str, Any],
        players: dict[int, dict[str, Any]],
        teams: dict[int, str],
    ) -> dict[str, Any]:
        player_id = int(pick["element"])
        return {
            **FPLAnalysis._player_summary(players.get(player_id, {"id": player_id}), teams),
            "squad_position": pick.get("position"),
            "multiplier": pick.get("multiplier"),
            "is_captain": bool(pick.get("is_captain")),
            "is_vice_captain": bool(pick.get("is_vice_captain")),
        }

    @staticmethod
    def _fixture_view(
        fixture: dict[str, Any],
        teams: dict[int, str],
        perspective_team_id: int | None = None,
    ) -> dict[str, Any]:
        result = {
            "fixture_id": fixture.get("id"),
            "gameweek": fixture.get("event"),
            "kickoff_time": fixture.get("kickoff_time"),
            "started": fixture.get("started"),
            "finished": fixture.get("finished"),
            "home_team": teams.get(fixture.get("team_h"), "Unknown"),
            "away_team": teams.get(fixture.get("team_a"), "Unknown"),
            "home_score": fixture.get("team_h_score"),
            "away_score": fixture.get("team_a_score"),
            "home_difficulty": fixture.get("team_h_difficulty"),
            "away_difficulty": fixture.get("team_a_difficulty"),
        }
        if perspective_team_id:
            is_home = fixture.get("team_h") == perspective_team_id
            result["perspective"] = {
                "venue": "home" if is_home else "away",
                "opponent": teams.get(fixture.get("team_a" if is_home else "team_h"), "Unknown"),
                "difficulty": fixture.get("team_h_difficulty" if is_home else "team_a_difficulty"),
            }
        return result
