"""Configuration loading: framework settings plus one YAML file per application."""

import copy
from pathlib import Path
from typing import Any, Dict

import yaml


class ConfigError(Exception):
    pass


def _merge(base: Dict[str, Any], extra: Dict[str, Any]) -> Dict[str, Any]:
    out = copy.deepcopy(base)
    for key, value in (extra or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


class Settings:
    def __init__(self, config_dir: str = "config"):
        self.config_dir = Path(config_dir)
        framework_file = self.config_dir / "config.yaml"
        if not framework_file.exists():
            raise ConfigError(f"Framework config not found: {framework_file}")
        with open(framework_file, "r", encoding="utf-8") as fh:
            self.framework: Dict[str, Any] = yaml.safe_load(fh) or {}

    def section(self, name: str) -> Dict[str, Any]:
        return self.framework.get(name, {}) or {}

    def app_names(self):
        apps_dir = self.config_dir / "apps"
        return sorted(p.stem for p in apps_dir.glob("*.yaml")) if apps_dir.exists() else []

    def app(self, app_name: str) -> Dict[str, Any]:
        path = self.config_dir / "apps" / f"{app_name}.yaml"
        if not path.exists():
            raise ConfigError(f"Application '{app_name}' is not configured. Known: {', '.join(self.app_names()) or 'none'}")
        with open(path, "r", encoding="utf-8") as fh:
            return yaml.safe_load(fh) or {}

    def environment(self, app_name: str, env_name: str) -> Dict[str, Any]:
        """App config with the chosen environment merged over the app-level defaults."""
        app = self.app(app_name)
        envs = app.get("environments", {})
        if env_name not in envs:
            raise ConfigError(f"Environment '{env_name}' not defined for '{app_name}'. Known: {', '.join(envs) or 'none'}")
        merged = _merge({k: v for k, v in app.items() if k != "environments"}, envs[env_name])
        merged["app_name"] = app_name
        merged["env_name"] = env_name
        return merged
