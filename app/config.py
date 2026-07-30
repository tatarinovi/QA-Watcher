from __future__ import annotations

import ipaddress
from pathlib import Path
from typing import Annotated, Any, Self
from urllib.parse import urlsplit

from pydantic import Field, HttpUrl, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    gitlab_url: HttpUrl
    gitlab_project_ids: Annotated[tuple[int, ...], NoDecode] = ()
    gitlab_project_id: int | None = Field(default=None, gt=0)
    gitlab_token: SecretStr = Field(min_length=1)
    gitlab_timeout_seconds: float = Field(default=30.0, gt=0, le=120)
    poll_interval_seconds: int = Field(default=60, ge=5, le=86400)
    scheduler_enabled: bool = True
    state_path: Path = Path("data/state.json")
    reports_path: Path = Path("reports")
    bind_host: str = "127.0.0.1"
    port: int = Field(default=8000, ge=1, le=65535)
    allow_unsafe_non_loopback: bool = False
    mcp_allowed_hosts: list[str] = Field(default_factory=list)
    log_level: str = "INFO"
    dev_branch: str = Field(default="develop", min_length=1, max_length=255)
    test_branch: str = Field(default="testing", min_length=1, max_length=255)
    stage_branch: str = Field(default="stable", min_length=1, max_length=255)
    prod_branch: str = Field(default="master", min_length=1, max_length=255)
    telegram_bot_token: SecretStr | None = None
    telegram_chat_id: str | None = None
    telegram_message_thread_id: int | None = Field(default=None, gt=0)
    telegram_timeout_seconds: float = Field(default=10, gt=0, le=30)

    @field_validator("gitlab_project_ids", mode="before")
    @classmethod
    def parse_project_ids(cls, value: Any) -> Any:
        if isinstance(value, str):
            if not value.strip():
                return ()
            return tuple(part.strip() for part in value.split(","))
        return value

    @field_validator(
        "telegram_bot_token",
        "telegram_chat_id",
        "telegram_message_thread_id",
        mode="before",
    )
    @classmethod
    def empty_telegram_setting_is_disabled(cls, value: Any) -> Any:
        return None if value == "" else value

    @model_validator(mode="after")
    def safe_runtime(self) -> Self:
        if self.gitlab_project_id is not None and self.gitlab_project_ids:
            raise ValueError("set only GITLAB_PROJECT_IDS, not both project ID settings")
        configured_ids = (
            self.gitlab_project_ids
            if self.gitlab_project_ids
            else ((self.gitlab_project_id,) if self.gitlab_project_id is not None else ())
        )
        if not configured_ids or any(
            isinstance(project_id, bool) or project_id <= 0 for project_id in configured_ids
        ):
            raise ValueError("GITLAB_PROJECT_IDS must contain positive numeric IDs")
        object.__setattr__(self, "gitlab_project_ids", tuple(dict.fromkeys(configured_ids)))
        if (self.telegram_bot_token is None) != (self.telegram_chat_id is None):
            raise ValueError("Telegram bot token and chat ID must be configured together")
        if self.telegram_message_thread_id is not None and self.telegram_bot_token is None:
            raise ValueError("Telegram message thread ID requires bot token and chat ID")
        if self.telegram_chat_id is not None and not self.telegram_chat_id.removeprefix("-").isdigit():
            raise ValueError("Telegram chat ID must be numeric")
        parsed = urlsplit(str(self.gitlab_url))
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("GITLAB_URL must be HTTP(S)")
        if not _is_loopback(self.bind_host):
            if not self.allow_unsafe_non_loopback:
                raise ValueError(
                    "non-loopback bind requires ALLOW_UNSAFE_NON_LOOPBACK=true"
                )
            if not self.mcp_allowed_hosts:
                raise ValueError(
                    "non-loopback bind requires explicit MCP_ALLOWED_HOSTS"
                )
        level = self.log_level.upper()
        if level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError("LOG_LEVEL is invalid")
        object.__setattr__(self, "log_level", level)
        return self

    @property
    def environment_branches(self) -> dict[str, str]:
        return {
            "dev": self.dev_branch,
            "test": self.test_branch,
            "stage": self.stage_branch,
            "prod": self.prod_branch,
        }


def _is_loopback(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False
