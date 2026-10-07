"""Loads and validates process Settings (.env) and per-app AppConfig (YAML).

Fails closed: any missing/invalid required value raises ConfigError with a clear
message instead of silently falling back to a default that was never configured.
"""

from __future__ import annotations

import os
from pathlib import Path

import yaml
from dotenv import load_dotenv
from pydantic import ValidationError

from framework.config.schema import AppConfig, Settings


class ConfigError(RuntimeError):
    """Raised when configuration is missing or invalid. Execution must stop, not guess."""


def _bool_env(name: str, raw: str) -> bool:
    val = raw.strip().lower()
    if val in ("true", "1", "yes", "y"):
        return True
    if val in ("false", "0", "no", "n"):
        return False
    raise ConfigError(f"Environment variable {name}={raw!r} is not a valid boolean")


def load_settings(env_file: str | Path = ".env") -> Settings:
    """Load process-level Settings strictly from environment variables.

    Secrets never live in YAML or code; they are read here only.
    """
    load_dotenv(dotenv_path=env_file, override=False)

    def req(name: str) -> str:
        val = os.environ.get(name)
        if val is None or val == "":
            raise ConfigError(f"Required environment variable {name} is not set")
        return val

    def opt(name: str) -> str | None:
        return os.environ.get(name) or None

    try:
        provider = req("LLM_PROVIDER")
        settings = Settings(
            llm_provider=provider,
            openai_api_key=opt("OPENAI_API_KEY"),
            azure_openai_endpoint=opt("AZURE_OPENAI_ENDPOINT"),
            azure_openai_api_key=opt("AZURE_OPENAI_API_KEY"),
            azure_openai_deployment=opt("AZURE_OPENAI_DEPLOYMENT"),
            azure_openai_api_version=opt("AZURE_OPENAI_API_VERSION"),
            framework_env=req("FRAMEWORK_ENV"),
            framework_headless=_bool_env("FRAMEWORK_HEADLESS", req("FRAMEWORK_HEADLESS")),
            framework_debug=_bool_env("FRAMEWORK_DEBUG", req("FRAMEWORK_DEBUG")),
            browser_engine=req("BROWSER_ENGINE"),
            browser_headless=_bool_env("BROWSER_HEADLESS", req("BROWSER_HEADLESS")),
            action_timeout_ms=int(req("ACTION_TIMEOUT_MS")),
            navigation_timeout_ms=int(req("NAVIGATION_TIMEOUT_MS")),
            stop_on_failure=_bool_env("STOP_ON_FAILURE", req("STOP_ON_FAILURE")),
            healing_auto_approve=_bool_env(
                "HEALING_AUTO_APPROVE", req("HEALING_AUTO_APPROVE")
            ),
            llm_min_confidence=float(req("LLM_MIN_CONFIDENCE")),
            llm_request_timeout=int(req("LLM_REQUEST_TIMEOUT")),
            fingerprint_cache_threshold=float(req("FINGERPRINT_CACHE_THRESHOLD")),
            fingerprint_fallback_threshold=float(req("FINGERPRINT_FALLBACK_THRESHOLD")),
            fingerprint_ai_threshold=float(req("FINGERPRINT_AI_THRESHOLD")),
            fingerprint_fallback_margin=float(req("FINGERPRINT_FALLBACK_MARGIN")),
            fingerprint_ai_margin=float(req("FINGERPRINT_AI_MARGIN")),
            log_level=req("LOG_LEVEL"),
            log_dir=req("LOG_DIR"),
            database_url=req("DATABASE_URL"),
            run_history_db=req("RUN_HISTORY_DB"),
            llm_trace=_bool_env("LLM_TRACE", req("LLM_TRACE")),
        )
    except (ValidationError, ValueError) as exc:
        raise ConfigError(f"Invalid settings: {exc}") from exc

    if settings.llm_provider == "openai" and not settings.openai_api_key:
        raise ConfigError("LLM_PROVIDER=openai requires OPENAI_API_KEY to be set")
    if settings.llm_provider == "azure" and not (
        settings.azure_openai_endpoint
        and settings.azure_openai_api_key
        and settings.azure_openai_deployment
    ):
        raise ConfigError(
            "LLM_PROVIDER=azure requires AZURE_OPENAI_ENDPOINT, "
            "AZURE_OPENAI_API_KEY and AZURE_OPENAI_DEPLOYMENT to be set"
        )

    return settings


def load_app_config(path: str | Path) -> AppConfig:
    """Load and validate an apps/<name>/config.yaml file. No app logic lives here."""
    p = Path(path)
    if not p.is_file():
        raise ConfigError(f"App config file not found: {p}")
    try:
        raw = yaml.safe_load(p.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"App config at {p} is not valid YAML: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"App config at {p} must be a YAML mapping at the top level")
    try:
        return AppConfig.model_validate(raw)
    except ValidationError as exc:
        raise ConfigError(f"App config at {p} failed validation: {exc}") from exc
