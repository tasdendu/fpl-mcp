import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import respx

from fpl_mcp import advisor as advisor_module
from fpl_mcp.advisor import ANTHROPIC_URL, Advisor
from fpl_mcp.alerts import split_message
from fpl_mcp.config import Settings

DEADLINE = datetime(2026, 10, 10, 10, 0, tzinfo=UTC)


def pick(player_id, name, status="a", chance=None, price=6.0, news=None):
    return {
        "player_id": player_id,
        "name": name,
        "team": "Chelsea",
        "status": status,
        "chance_next_round": chance,
        "news": news,
        "price": price,
    }


class FakeAnalysis:
    def __init__(self):
        self.picks = []

    async def my_team(self):
        return {"picks": self.picks}


class FakeClient:
    async def bootstrap(self):
        return {"events": [{"id": 6, "is_next": True, "deadline_time": "2026-10-10T10:00:00Z"}]}


@pytest.fixture
def sent(monkeypatch):
    messages: list[str] = []

    async def fake_send(settings, text):
        messages.append(text)
        return True

    monkeypatch.setattr(advisor_module, "send_message", fake_send)
    return messages


def make_advisor(tmp_path, **extra) -> Advisor:
    settings = Settings(
        fpl_token_db=str(tmp_path / "state.db"),
        advisor_enabled=True,
        alert_telegram_bot_token="t",
        alert_telegram_chat_id="1",
        **extra,
    )
    advisor = Advisor(settings, FakeClient(), FakeAnalysis())
    return advisor


async def test_squad_watch_alerts_on_flag_and_price_changes_only(tmp_path, sent) -> None:
    advisor = make_advisor(tmp_path)
    advisor.analysis.picks = [pick(1, "João Pedro", "d", 75), pick(2, "Haaland", price=15.6)]
    assert await advisor.watch_squad() == []  # baseline
    assert await advisor.watch_squad() == []  # nothing changed

    advisor.analysis.picks = [
        pick(1, "João Pedro", "i", 0, news="Knee injury"),
        pick(2, "Haaland", price=15.7),
        pick(3, "Schade"),  # new signing: no alert
    ]
    lines = await advisor.watch_squad()

    assert lines == [
        "🔴 João Pedro (Chelsea) injured (Knee injury)",
        "📈 Haaland price £15.6m → £15.7m",
    ]
    assert len(sent) == 1


async def test_briefings_are_sent_once_per_window(tmp_path, sent, monkeypatch) -> None:
    advisor = make_advisor(tmp_path)
    kinds: list[str] = []

    async def fake_brief(gameweek, kind, deadline, now):
        kinds.append(kind)
        return f"brief {kind}"

    monkeypatch.setattr(advisor, "brief", fake_brief)

    assert await advisor.deadline_briefs(DEADLINE - timedelta(hours=30)) is None
    assert await advisor.deadline_briefs(DEADLINE - timedelta(hours=20)) == "preview"
    assert await advisor.deadline_briefs(DEADLINE - timedelta(hours=10)) is None
    assert await advisor.deadline_briefs(DEADLINE - timedelta(hours=2)) == "final"
    assert await advisor.deadline_briefs(DEADLINE - timedelta(hours=1)) is None
    assert await advisor.deadline_briefs(DEADLINE + timedelta(hours=1)) is None
    assert kinds == ["preview", "final"]
    assert sent == ["brief preview", "brief final"]


@respx.mock
async def test_claude_request_uses_mcp_connector_and_continues_paused_turns(tmp_path) -> None:
    advisor = make_advisor(tmp_path, anthropic_api_key="sk-test")
    route = respx.post(ANTHROPIC_URL).mock(
        side_effect=[
            httpx.Response(200, json={"stop_reason": "pause_turn", "content": []}),
            httpx.Response(
                200,
                json={
                    "stop_reason": "end_turn",
                    "content": [{"type": "text", "text": "STARTING XI ...\n🟢 Captain Haaland"}],
                },
            ),
        ]
    )

    now = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)  # Fri 18:00 Bhutan
    text = await advisor.brief(6, "final", DEADLINE, now)

    assert text.startswith(
        "⚽ GW6 FINAL CALL\nDeadline: Sat 10 Oct 16:00 Bhutan time (22h 00m left)"
    )
    assert "🟢 Captain Haaland" in text
    request = route.calls[0].request
    body = json.loads(request.content)
    assert request.headers["anthropic-beta"] == "mcp-client-2025-11-20"
    assert body["mcp_servers"][0]["url"] == "https://fpl.dcpl.bt/mcp"
    assert body["tools"] == [{"type": "mcp_toolset", "mcp_server_name": "tashi-fpl"}]
    assert len(json.loads(route.calls[1].request.content)["messages"]) == 2
    task = body["messages"][0]["content"]
    assert "Current time: Fri 09 Oct 18:00 Bhutan time" in task
    assert "Time remaining: exactly 22h 00m" in task


async def test_without_api_key_sends_rule_based_squad_check(tmp_path) -> None:
    advisor = make_advisor(tmp_path)
    squad = []
    for slot in range(1, 16):
        row = pick(slot, f"P{slot}")
        row.update(position="Midfielder", squad_position=slot)
        squad.append(row)
    squad[9].update(name="Haaland", is_captain=True)
    squad[10].update(name="João Pedro", status="d", chance_next_round=75)
    squad[8].update(name="Groß", is_vice_captain=True)
    advisor.analysis.picks = squad

    async def my_team():
        return {
            "picks": squad,
            "current_bank": 0.5,
            "current_free_transfers": 1,
            "chips": [{"name": "wildcard", "status_for_entry": "available"}],
        }

    advisor.analysis.my_team = my_team
    now = datetime(2026, 10, 10, 7, 0, tzinfo=UTC)
    text = await advisor.brief(6, "final", DEADLINE, now)

    assert "(3h 00m left)" in text
    assert "Bank £0.5m | Free transfers: 1 | Chips: Wildcard" in text
    assert "CAPTAIN: Haaland" in text
    assert "MID João Pedro ⚠️ 75%" in text
    assert "🟡 João Pedro flagged ⚠️ 75%: bench cover is ready" in text


def test_advisor_reports_why_it_is_off() -> None:
    advisor = Advisor(Settings(), FakeClient(), FakeAnalysis())
    assert "ADVISOR_ENABLED is off" in advisor.enabled_problems()


def test_split_message_respects_telegram_limit() -> None:
    text = "\n".join(f"line {i} " + "x" * 50 for i in range(200))
    chunks = split_message(text, limit=1000)
    assert all(len(chunk) <= 1000 for chunk in chunks)
    assert "".join(chunks).replace("\n", "") == text.replace("\n", "")
