"""Proactive FPL advisor: squad watch alerts and pre-deadline briefings on Telegram.

Runs inside the MCP server process so it shares the single FPL login (two
processes refreshing the same rotating token would invalidate each other).

* Squad watch (every tick, no LLM): alerts when a player in Tashi's squad gets
  an injury/suspension flag, changes chance of playing, recovers, or changes
  price.
* Deadline messages: a preview about a day before each deadline and a final
  call a few hours before, each sent once. Written by, in order of preference:
  a self-hosted LLM (LLM_BASE_URL, OpenAI-compatible, e.g. llama.cpp; free),
  Claude (ANTHROPIC_API_KEY; paid, optional), or a rule-based squad check
  (flags, captain, bench cover, bank, transfers, chips) needing neither.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import sqlite3
import time
from contextlib import closing
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx

from .alerts import send_message, telegram_configured
from .analysis import FPLAnalysis
from .client import FPLAPIError, FPLClient
from .config import Settings

log = logging.getLogger(__name__)

ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
MCP_BETA = "mcp-client-2025-11-20"
MCP_SERVER_NAME = "tashi-fpl"

SQUAD_KEYS = (
    "squad_position",
    "name",
    "team",
    "position",
    "selling_price",
    "price",
    "status",
    "chance_next_round",
    "news",
    "form",
    "points_per_game",
    "expected_points_next",
    "total_points",
    "is_captain",
    "is_vice_captain",
)
TARGET_KEYS = (
    "name",
    "team",
    "price",
    "status",
    "chance_next_round",
    "form",
    "points_per_game",
    "expected_points_next",
    "total_points",
    "heuristic_score",
    "average_fixture_difficulty",
)
FIXTURE_KEYS = ("home_team", "away_team", "home_difficulty", "away_difficulty")

THINK_BLOCK = re.compile(r"<think>.*?</think>", re.DOTALL)

CHIP_NAMES = {
    "wildcard": "Wildcard",
    "freehit": "Free Hit",
    "bboost": "Bench Boost",
    "3xc": "Triple Captain",
}

UNAVAILABLE = {"i": "injured", "s": "suspended", "u": "unavailable", "n": "not available"}

SYSTEM_PROMPT = """You are Tashi's professional Fantasy Premier League pundit. His goal is \
to win his DCPL mini-league (league 918045) and climb overall rank.

Before advising, use the Tashi FPL tools to check: his live private squad, bank, free \
transfers and chips (get_my_team); the gameweek and deadline (get_fpl_overview); fixtures \
for this and the next four gameweeks; any flagged player in his squad (get_player); and \
transfer targets by position within his real budget (rank_transfer_targets).

How to recommend:
- Pick players on expected points: form, minutes security, underlying output and fixtures. \
Never copy rival or top-ranked teams; mini-league ownership is only a tiebreaker.
- Be conservative and evidence-based: no points hits unless clearly worth it, don't chase \
last week's points or price moves, roll free transfers when nothing is clearly better, but \
act fast on confirmed injuries or suspensions.
- Follow FPL rules: budget uses selling prices plus bank; max 3 players per club; formation \
1 GK, 3-5 DEF, 2-5 MID, 1-3 FWD; the first-half chips expire after GW19.
- Never claim a transfer was made. Tashi makes every change himself.

Format for Telegram as plain text: no markdown tables, no ** or # symbols. Use these \
sections in order: TRANSFERS, STARTING XI (by position), CAPTAIN / VICE-CAPTAIN, BENCH \
ORDER, CHIP, WATCH LIST. End with a PUNDIT'S VERDICT section of 3-5 lines, each starting \
with 🟢 (do it), 🟡 (watch) or 🔴 (risk). Keep the whole message under 2,500 characters."""

LOCAL_SYSTEM_PROMPT = SYSTEM_PROMPT.split("Before advising", 1)[0] + (
    "You are given Tashi's live squad, fixtures and a ranked transfer shortlist as JSON. "
    "You have no tools; use only that data.\n\nHow to recommend:"
    + SYSTEM_PROMPT.split("How to recommend:", 1)[1]
)


class Advisor:
    def __init__(self, settings: Settings, client: FPLClient, analysis: FPLAnalysis) -> None:
        self.settings = settings
        self.client = client
        self.analysis = analysis
        self.local_tz = timezone(timedelta(hours=settings.advisor_utc_offset_hours))
        self.db_path = Path(settings.fpl_token_db) if settings.fpl_token_db else None
        if self.db_path is not None:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            with closing(self._connect()) as conn, conn:
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS advisor_state ("
                    "key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at INTEGER NOT NULL)"
                )

    # ---- state -----------------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        assert self.db_path is not None
        return sqlite3.connect(self.db_path, timeout=30)

    def _get(self, key: str) -> Any:
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT value FROM advisor_state WHERE key = ?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def _set(self, key: str, value: Any) -> None:
        with closing(self._connect()) as conn, conn:
            conn.execute(
                "INSERT INTO advisor_state (key, value, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
                "updated_at = excluded.updated_at",
                (key, json.dumps(value), int(time.time())),
            )

    # ---- loop ------------------------------------------------------------------

    def enabled_problems(self) -> list[str]:
        problems = []
        if not self.settings.advisor_enabled:
            problems.append("ADVISOR_ENABLED is off")
        if self.db_path is None:
            problems.append("FPL_TOKEN_DB is not set")
        if not telegram_configured(self.settings) and not self.settings.alert_webhook_url:
            problems.append("no Telegram or webhook configured")
        return problems

    async def run(self) -> None:
        problems = self.enabled_problems()
        if problems:
            log.info("FPL advisor not started: %s", "; ".join(problems))
            return
        await asyncio.sleep(60)  # let the login keep-alive verify the token first
        while True:
            try:
                await self.tick(datetime.now(UTC))
            except Exception:  # keep the loop alive whatever happens in one tick
                log.exception("FPL advisor tick failed")
            await asyncio.sleep(self.settings.advisor_interval_minutes * 60)

    async def tick(self, now: datetime) -> None:
        try:
            await self.watch_squad()
        except FPLAPIError as exc:
            log.warning("Squad watch skipped: %s", exc)
        await self.deadline_briefs(now)

    # ---- squad watch -------------------------------------------------------------

    async def watch_squad(self) -> list[str]:
        team = await self.analysis.my_team()
        current = {
            str(pick["player_id"]): {
                "name": pick.get("name"),
                "team": pick.get("team"),
                "status": pick.get("status"),
                "chance": pick.get("chance_next_round"),
                "news": pick.get("news"),
                "price": pick.get("price"),
            }
            for pick in team.get("picks", [])
        }
        previous = await asyncio.to_thread(self._get, "squad_snapshot")
        await asyncio.to_thread(self._set, "squad_snapshot", current)
        if previous is None:
            return []  # first run records a baseline only

        lines = []
        for player_id, now_row in current.items():
            before = previous.get(player_id)
            if before is None:
                continue  # newly bought player: no baseline yet
            if (now_row["status"], now_row["chance"]) != (before["status"], before["chance"]):
                lines.append(_availability_line(now_row))
            if now_row["price"] != before["price"] and None not in (
                now_row["price"],
                before["price"],
            ):
                arrow = "📈" if now_row["price"] > before["price"] else "📉"
                lines.append(
                    f"{arrow} {now_row['name']} price £{before['price']:.1f}m → "
                    f"£{now_row['price']:.1f}m"
                )
        if lines:
            await send_message(self.settings, "🚨 FPL squad watch\n\n" + "\n".join(lines))
        return lines

    # ---- deadline briefings ----------------------------------------------------------

    async def deadline_briefs(self, now: datetime) -> str | None:
        bootstrap = await self.client.bootstrap()
        event = next((row for row in bootstrap.get("events", []) if row.get("is_next")), None)
        if not event or not event.get("deadline_time"):
            return None
        deadline = datetime.fromisoformat(str(event["deadline_time"]).replace("Z", "+00:00"))
        hours_left = (deadline - now).total_seconds() / 3600
        if hours_left <= 0:
            return None
        windows = (
            ("final", self.settings.advisor_final_hours),
            ("preview", self.settings.advisor_preview_hours),
        )
        for kind, window in windows:
            if hours_left <= window:
                key = f"brief:gw{event['id']}:{kind}"
                if await asyncio.to_thread(self._get, key):
                    return None
                text = await self.brief(int(event["id"]), kind, deadline, now)
                if await send_message(self.settings, text):
                    await asyncio.to_thread(self._set, key, True)
                return kind
        return None

    async def brief(
        self, gameweek: int, kind: str, deadline: datetime, now: datetime | None = None
    ) -> str:
        now = now or datetime.now(UTC)
        fmt = "%a %d %b %H:%M"
        deadline_local = deadline.astimezone(self.local_tz).strftime(fmt)
        now_local = now.astimezone(self.local_tz).strftime(fmt)
        left = _time_left(deadline - now)
        header = (
            f"⚽ GW{gameweek} {'FINAL CALL' if kind == 'final' else 'PREVIEW'}\n"
            f"Deadline: {deadline_local} Bhutan time ({left} left)\n\n"
        )
        if self.settings.llm_base_url:
            try:
                return header + await self._ask_local_llm(kind, now_local, deadline_local, left)
            except (httpx.HTTPError, ValueError, KeyError, FPLAPIError) as exc:
                log.warning("Local LLM briefing failed: %s", exc)
                return (
                    header
                    + f"(Local LLM unavailable: {exc}. Rule-based check instead.)\n\n"
                    + await self.squad_check()
                )
        if not self.settings.anthropic_api_key:
            return header + await self.squad_check()
        task = (
            f"Write the GW{gameweek} "
            + (
                "final pre-deadline call. Give definitive decisions; only the latest team news "
                "should change them."
                if kind == "final"
                else "preview, about a day before the deadline. Give the plan and clearly flag "
                "anything that depends on press conferences or team news."
            )
            + f" Current time: {now_local} Bhutan time. Deadline: {deadline_local} Bhutan "
            f"time. Time remaining: exactly {left}. Use these figures as given; do not work "
            "out times yourself, and quote every time in Bhutan time (UTC+6) only, never UK "
            "time or UTC."
        )
        try:
            return header + await self._ask_claude(task)
        except (httpx.HTTPError, ValueError, KeyError) as exc:
            log.warning("Briefing generation failed: %s", exc)
            return header + f"Briefing could not be generated ({exc}). Ask Claude in chat."

    async def squad_check(self) -> str:
        """Rule-based pre-deadline check (no API key needed): what could cost points."""
        team = await self.analysis.my_team()
        picks = sorted(team.get("picks", []), key=lambda row: row.get("squad_position") or 99)
        starters = [row for row in picks if (row.get("squad_position") or 99) <= 11]
        bench = [row for row in picks if (row.get("squad_position") or 0) > 11]
        captain = next((row for row in picks if row.get("is_captain")), None)
        vice = next((row for row in picks if row.get("is_vice_captain")), None)
        chips = [
            CHIP_NAMES.get(str(chip.get("name")), str(chip.get("name")))
            for chip in team.get("chips", [])
            if chip.get("status_for_entry") == "available"
        ]

        def flag(row: dict[str, Any]) -> str:
            if row.get("status") in (None, "a"):
                return ""
            chance = row.get("chance_next_round")
            return f" ⚠️ {chance}%" if chance is not None else " ⚠️ out"

        verdict = []
        if captain and captain.get("status") not in (None, "a"):
            verdict.append(
                f"🔴 Captain {captain['name']} is flagged{flag(captain)}: change captain"
            )
        flagged = [row for row in starters if row.get("status") not in (None, "a")]
        for row in flagged:
            if row is not captain:
                verdict.append(f"🟡 {row['name']} flagged{flag(row)}: bench cover is ready")
        bench_flagged = [row for row in bench[1:] if row.get("status") not in (None, "a")]
        if flagged and bench_flagged:
            verdict.append(
                "🔴 A flagged starter could be covered by a flagged sub: fix bench order"
            )
        if not verdict:
            verdict.append("🟢 No injury or suspension flags in your XI")
        verdict.append(f"🟢 {team.get('current_free_transfers')} free transfer(s) available")

        lines = [
            f"Bank £{team.get('current_bank', 0):.1f}m | Free transfers: "
            f"{team.get('current_free_transfers')} | Chips: {', '.join(chips) or 'none'}",
            "",
            "STARTING XI",
            *[f"{row['position'][:3].upper()} {row['name']}{flag(row)}" for row in starters],
            "",
            f"CAPTAIN: {captain['name'] if captain else '-'}{flag(captain) if captain else ''}",
            f"VICE-CAPTAIN: {vice['name'] if vice else '-'}{flag(vice) if vice else ''}",
            "",
            "BENCH: " + ", ".join(f"{row['name']}{flag(row)}" for row in bench),
            "",
            "PUNDIT'S VERDICT",
            *verdict,
        ]
        return "\n".join(lines)

    async def briefing_context(self) -> str:
        """Compact, factual input for a model without tool access."""
        team = await self.analysis.my_team()
        overview = await self.analysis.overview()
        gameweek = (overview.get("next_gameweek") or {}).get("id")
        fixtures = (await self.analysis.fixtures(gameweek)).get("fixtures", []) if gameweek else []
        targets = {}
        for position, label in ((1, "GK"), (2, "DEF"), (3, "MID"), (4, "FWD")):
            rows = (await self.analysis.transfer_targets(position, None, 5, 8)).get("targets", [])
            targets[label] = [_pick(row, TARGET_KEYS) for row in rows]
        squad = [_pick(row, SQUAD_KEYS) for row in team.get("picks", [])]
        chips = [
            CHIP_NAMES.get(str(chip.get("name")), str(chip.get("name")))
            for chip in team.get("chips", [])
            if chip.get("status_for_entry") == "available"
        ]
        data = {
            "bank_millions": team.get("current_bank"),
            "free_transfers": team.get("current_free_transfers"),
            "hit_cost_per_extra_transfer": 4,
            "chips_available": chips,
            "squad (squad_position 1-11 start, 12-15 bench in order)": squad,
            f"gameweek_{gameweek}_fixtures (difficulty 1 easy - 5 hard)": [
                _pick(row, FIXTURE_KEYS) for row in fixtures
            ],
            "top_transfer_targets_next_5_gameweeks_by_position": targets,
        }
        return json.dumps(data, ensure_ascii=False, separators=(",", ":"))

    async def _ask_local_llm(
        self, kind: str, now_local: str, deadline_local: str, left: str
    ) -> str:
        context = await self.briefing_context()
        task = (
            (
                "Final pre-deadline call: give definitive decisions."
                if kind == "final"
                else "Preview a day before the deadline: give the plan and flag what "
                "depends on team news."
            )
            + f" Current time {now_local} Bhutan, deadline {deadline_local} Bhutan, {left} left; "
            "quote times in Bhutan time only. Base every recommendation ONLY on this data "
            "(selling_price + bank is the budget; never invent players, prices or fixtures):\n"
            + context
        )
        if self.settings.llm_disable_thinking:
            task += "\n/no_think"
        headers = {"content-type": "application/json"}
        if self.settings.llm_api_key:
            headers["authorization"] = f"Bearer {self.settings.llm_api_key}"
        async with httpx.AsyncClient(timeout=self.settings.llm_timeout_seconds) as http:
            response = await http.post(
                self.settings.llm_base_url.rstrip("/") + "/chat/completions",
                headers=headers,
                json={
                    "model": self.settings.llm_model,
                    "temperature": 0.3,
                    "max_tokens": 2000,
                    "messages": [
                        {"role": "system", "content": LOCAL_SYSTEM_PROMPT},
                        {"role": "user", "content": task},
                    ],
                },
            )
            response.raise_for_status()
            text = response.json()["choices"][0]["message"]["content"] or ""
        text = THINK_BLOCK.sub("", text).strip()
        if not text:
            raise ValueError("local LLM returned no text")
        return text

    async def _ask_claude(self, task: str) -> str:
        headers = {
            "x-api-key": str(self.settings.anthropic_api_key),
            "anthropic-version": "2023-06-01",
            "anthropic-beta": MCP_BETA,
            "content-type": "application/json",
        }
        messages: list[dict[str, Any]] = [{"role": "user", "content": task}]
        texts: list[str] = []
        async with httpx.AsyncClient(timeout=300) as http:
            for _ in range(6):  # a long tool loop can pause; continue it a few times
                response = await http.post(
                    ANTHROPIC_URL,
                    headers=headers,
                    json={
                        "model": self.settings.advisor_model,
                        "max_tokens": 4000,
                        "system": SYSTEM_PROMPT,
                        "messages": messages,
                        "mcp_servers": [
                            {
                                "type": "url",
                                "url": self.settings.advisor_mcp_url,
                                "name": MCP_SERVER_NAME,
                            }
                        ],
                        "tools": [{"type": "mcp_toolset", "mcp_server_name": MCP_SERVER_NAME}],
                    },
                )
                if response.status_code >= 400:
                    raise ValueError(
                        f"Claude API HTTP {response.status_code}: {response.text[:300]}"
                    )
                payload = response.json()
                content = payload.get("content", [])
                texts = [block["text"] for block in content if block.get("type") == "text"]
                if payload.get("stop_reason") != "pause_turn":
                    break
                messages = [*messages, {"role": "assistant", "content": content}]
        text = "\n".join(part.strip() for part in texts if part.strip())
        if not text:
            raise ValueError("Claude returned no text")
        return text


def _pick(row: dict[str, Any], keys: tuple[str, ...]) -> dict[str, Any]:
    return {key: row[key] for key in keys if row.get(key) not in (None, "", False)}


def _time_left(delta: timedelta) -> str:
    minutes = max(int(delta.total_seconds() // 60), 0)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02d}m"


def _availability_line(row: dict[str, Any]) -> str:
    status = row.get("status")
    news = f" ({row['news']})" if row.get("news") else ""
    who = f"{row['name']} ({row['team']})"
    if status == "a":
        return f"🟢 {who} is available again"
    if status == "d":
        return f"🟡 {who} now {row.get('chance') or '?'}% to play{news}"
    return f"🔴 {who} {UNAVAILABLE.get(str(status), 'flagged')}{news}"
