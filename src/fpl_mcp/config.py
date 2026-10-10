from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    fpl_entry_id: int = Field(default=354978, ge=1)
    fpl_base_url: str = "https://fantasy.premierleague.com/api"
    fpl_user_agent: str = "Tashi-FPL-MCP/1.0"
    fpl_refresh_token: str | None = None
    # SQLite file that persists the rotating refresh token; unset keeps it in memory only.
    fpl_token_db: str | None = None
    # Refresh this often even when idle, so the refresh token never lapses (0 disables).
    fpl_token_keepalive_hours: float = Field(default=6.0, ge=0, le=72)
    # Optional alerts when the FPL login needs attention.
    alert_telegram_bot_token: str | None = None
    alert_telegram_chat_id: str | None = None
    alert_webhook_url: str | None = None
    # Proactive advisor: squad watch + pre-deadline briefings sent to Telegram/webhook.
    advisor_enabled: bool = False
    # Free, self-hosted briefings: any OpenAI-compatible server, e.g. llama.cpp's
    # llama-server ("http://10.0.0.5:8080/v1"). Used before ANTHROPIC_API_KEY.
    llm_base_url: str | None = None
    llm_model: str = "local"
    llm_api_key: str | None = None
    llm_timeout_seconds: float = Field(default=600, ge=30, le=1800)
    llm_disable_thinking: bool = True
    # Optional paid alternative to a local LLM; leave empty to never use the API.
    anthropic_api_key: str | None = None
    advisor_model: str = "claude-sonnet-5-5"
    advisor_mcp_url: str = "https://fpl.dcpl.bt/mcp"
    advisor_interval_minutes: int = Field(default=30, ge=5, le=360)
    advisor_preview_hours: float = Field(default=24.0, gt=0, le=96)
    advisor_final_hours: float = Field(default=3.0, gt=0, le=24)
    advisor_utc_offset_hours: float = Field(default=6.0, ge=-12, le=14)
    fpl_token_url: str = "https://account.premierleague.com/as/token"
    fpl_client_id: str = "bfcbaf69-aade-4c1b-8f00-c1cb8a193030"
    request_timeout_seconds: float = Field(default=15.0, gt=0, le=60)
    max_league_pages: int = Field(default=5, ge=1, le=20)
    mcp_host: str = "0.0.0.0"
    mcp_port: int = Field(default=8000, ge=1, le=65535)
    mcp_allowed_hosts: list[str] = ["localhost", "localhost:*", "127.0.0.1", "127.0.0.1:*"]
    mcp_allowed_origins: list[str] = ["http://localhost:*", "http://127.0.0.1:*"]


@lru_cache
def get_settings() -> Settings:
    return Settings()
