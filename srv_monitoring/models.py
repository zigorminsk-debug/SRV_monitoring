from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


SourceType = Literal["vmware", "redfish", "ipmi", "backup_json"]
HealthState = Literal["ok", "warning", "critical", "unknown"]


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    username: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=1, max_length=256)


class SourcePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=100)
    type: SourceType
    config: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True

    @field_validator("name")
    @classmethod
    def clean_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Название обязательно")
        return value


class AgentCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=100)

    @field_validator("name")
    @classmethod
    def clean_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Название обязательно")
        return value


class SettingsPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    poll_interval_seconds: int = Field(ge=15, le=86400)


class CheckReport(BaseModel):
    model_config = ConfigDict(extra="allow")

    name: str = Field(min_length=1, max_length=180)
    status: HealthState
    message: str = Field(default="", max_length=2000)
    value: Any | None = None


class AgentEvent(BaseModel):
    model_config = ConfigDict(extra="allow")

    source: str = Field(default="Windows", max_length=80)
    event_id: int | str | None = None
    level: str = Field(default="error", max_length=40)
    timestamp: datetime | None = None
    message: str = Field(default="", max_length=1500)


class AgentReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    hostname: str = Field(min_length=1, max_length=180)
    agent_version: str = Field(default="unknown", max_length=40)
    collected_at: datetime
    metrics: dict[str, Any] = Field(default_factory=dict)
    checks: list[CheckReport] = Field(default_factory=list, max_length=250)
    events: list[AgentEvent] = Field(default_factory=list, max_length=100)


class AlertAck(BaseModel):
    model_config = ConfigDict(extra="forbid")

    acknowledged: bool = True
