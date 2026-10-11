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
    # Reuses the same declarative step shape as login_steps -- run when a
    # login attempt is rejected (e.g. a prior row deleted the account), to
    # establish a fresh working account before retrying the login once more.
    registration_steps: list[LoginStep] = Field(default_factory=list)
    # Some sites (this one included) leave a freshly registered account
    # already signed in, making a follow-up login attempt redundant -- and
    # on this site actively wrong, since the signup link it would click is
    # gone, replaced by "Logout". A short, visible snippet of text that only
    # appears once truly authenticated (e.g. "Logged in as") lets the
    # fallback recognize that state and skip the retry instead of assuming
    # one way or the other for every application.
    login_success_text: str | None = None
    test_account_env_vars: dict[str, str] = Field(default_factory=dict)
    # URL substrings (not full hosts -- matched as CDP request-blocking
    # patterns, e.g. "*doubleclick.net*") for third-party ad/tracker traffic
    # to block at the network layer so it never loads far enough to render an
    # overlay in the first place.
    blocked_ad_url_patterns: list[str] = Field(default_factory=list)
    # How a resolved, verified element actually gets clicked. "native"
    # (WebElement.click()) is the long-proven default. "action_chains"
    # (a real pointer move then click via the W3C Actions API) is more
    # reliable on some React SPAs where native click silently reaches
    # nothing -- but its pointer movement also triggers CSS :hover states
    # along the way, which regresses a site whose UI depends on hover-reveal
    # overlays (confirmed live: switching this globally broke several
    # hover-dependent flows on automationexercise.com while fixing saucedemo
    # .com's). There is no one mechanism safe for every site's UI, so this
    # is a per-app choice, not a framework default.
    click_strategy: Literal["native", "action_chains"] = "native"
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
