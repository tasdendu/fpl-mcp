from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    fpl_entry_id: int = Field(default=354978, ge=1)
    fpl_base_url: str = "https://fantasy.premierleague.com/api"
    fpl_user_agent: str = "Tashi-FPL-MCP/1.0"
    request_timeout_seconds: float = Field(default=15.0, gt=0, le=60)
    max_league_pages: int = Field(default=5, ge=1, le=20)
    mcp_host: str = "0.0.0.0"
    mcp_port: int = Field(default=8000, ge=1, le=65535)
    mcp_allowed_hosts: list[str] = ["localhost", "localhost:*", "127.0.0.1", "127.0.0.1:*"]
    mcp_allowed_origins: list[str] = ["http://localhost:*", "http://127.0.0.1:*"]


@lru_cache
def get_settings() -> Settings:
    return Settings()
