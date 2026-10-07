"""Pydantic schema for app config (apps/<name>/config.yaml) and process-level settings (.env)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator


class LoginStep(BaseModel):
    """One declarative step of an app's login routine."""

    action: Literal["navigate", "click", "type", "select", "check"]
    target: str
    data: str | None = None


class EnvironmentConfig(BaseModel):
    name: str
    base_url: str
    allowed_hosts: list[str] = Field(min_length=1)


class AppConfig(BaseModel):
    """Everything specific to one application. Lives in apps/<name>/config.yaml only."""

    app_name: str
    environments: dict[str, EnvironmentConfig]
    login_steps: list[LoginStep] = Field(default_factory=list)
    test_account_env_vars: dict[str, str] = Field(default_factory=dict)
    rate_limit_delay_ms: int = 0
    max_concurrency: int = 1

    @field_validator("environments")
    @classmethod
    def _non_empty(cls, v: dict[str, EnvironmentConfig]) -> dict[str, EnvironmentConfig]:
        if not v:
            raise ValueError("environments must define at least one entry")
        return v


class Settings(BaseModel):
    """Process-level settings sourced only from environment variables (.env)."""

    llm_provider: Literal["openai", "azure"]
    openai_api_key: str | None = None
    azure_openai_endpoint: str | None = None
    azure_openai_api_key: str | None = None
    azure_openai_deployment: str | None = None
    azure_openai_api_version: str | None = None

    framework_env: str
    framework_headless: bool
    framework_debug: bool

    browser_engine: Literal["chromium", "firefox", "webkit"]
    browser_headless: bool
    action_timeout_ms: int
    navigation_timeout_ms: int

    stop_on_failure: bool
    healing_auto_approve: bool

    llm_min_confidence: float
    llm_request_timeout: int

    fingerprint_cache_threshold: float
    fingerprint_fallback_threshold: float
    fingerprint_ai_threshold: float
    fingerprint_fallback_margin: float
    fingerprint_ai_margin: float

    log_level: str
    log_dir: str

    database_url: str
    run_history_db: str

    llm_trace: bool

    @field_validator("openai_api_key")
    @classmethod
    def _require_openai_key_if_selected(cls, v: str | None, info) -> str | None:
        # Cross-field checks happen in loader.py after full parse (pydantic v2 model_validator
        # would also work; kept simple and explicit here per "fail closed, clear error").
        return v
