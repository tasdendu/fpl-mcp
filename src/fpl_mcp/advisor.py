"""Proactive FPL advisor: squad watch alerts and pre-deadline briefings on Telegram.

Runs inside the MCP server process so it shares the single FPL login (two
processes refreshing the same rotating token would invalidate each other).

* Squad watch (every tick, no LLM): alerts when a player in Tashi's squad gets
  an injury/suspension flag, changes chance of playing, recovers, or changes
  price.
* Briefings (Claude via the Messages API MCP connector): a preview about a day
  before each deadline and a final call a few hours before. Each is sent once.
"""

from __future__ import annotations

import asyncio
import json
import logging
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
        if not self.settings.anthropic_api_key:
            return header + "ANTHROPIC_API_KEY is not set, so no briefing could be written."
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
