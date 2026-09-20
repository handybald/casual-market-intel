"""Loads and resolves config/data_sources.yaml into typed, ready-to-use values.

This is the single place that knows about the repo root, config file
location, and "end_date: null means today" resolution. Everything else
imports from here instead of re-reading YAML.
"""
from __future__ import annotations

import copy
import datetime as dt
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = REPO_ROOT / "config" / "data_sources.yaml"
DEFAULT_EVENT_MAPPING_PATH = REPO_ROOT / "config" / "event_mapping.yaml"


class AppConfig:
    """Typed accessor around the raw data_sources.yaml dict."""

    def __init__(self, raw: Dict[str, Any], config_path: Path):
        self._raw = raw
        self._config_path = config_path

    def with_mql5_overrides(self, input_csv=None, country=None, currency=None):
        """Return a run-specific config without changing the saved configuration."""
        raw = copy.deepcopy(self._raw)
        if input_csv is not None:
            raw["providers"]["mql5"]["input_csv"] = str(input_csv)
        if country is not None:
            raw["macro"]["country"] = country
        if currency is not None:
            raw["macro"]["currency"] = currency
        return AppConfig(raw, self._config_path)

    # -- top level -----------------------------------------------------
    @property
    def start_date(self) -> dt.date:
        return dt.date.fromisoformat(self._raw["historical"]["start_date"])

    @property
    def end_date(self) -> dt.date:
        raw_end = self._raw["historical"].get("end_date")
        if raw_end is None:
            return dt.date.today()
        return dt.date.fromisoformat(raw_end)

    @property
    def macro_country(self) -> str:
        return self._raw["macro"]["country"]

    @property
    def macro_currency(self) -> str:
        return self._raw["macro"]["currency"]

    @property
    def market_symbols(self) -> List[str]:
        return list(self._raw["market"]["symbols"])

    @property
    def market_timeframe(self) -> str:
        return self._raw["market"]["timeframe"]

    # -- storage ---------------------------------------------------------
    def resolve_path(self, relative: str) -> Path:
        p = Path(relative)
        return p if p.is_absolute() else (REPO_ROOT / p)

    # kept for internal callers written before resolve_path existed
    _resolve = resolve_path

    @property
    def raw_root(self) -> Path:
        return self._resolve(self._raw["storage"]["raw_root"])

    @property
    def interim_root(self) -> Path:
        return self._resolve(self._raw["storage"]["interim_root"])

    @property
    def processed_root(self) -> Path:
        return self._resolve(self._raw["storage"]["processed_root"])

    @property
    def manifest_path(self) -> Path:
        return self._resolve(self._raw["storage"]["manifest_path"])

    # -- providers ---------------------------------------------------------
    def provider(self, name: str) -> Dict[str, Any]:
        return self._raw["providers"][name]

    def provider_raw_dir(self, name: str) -> Path:
        return self._resolve(self.provider(name)["raw_dir"])

    def env(self, key: str, default: Optional[str] = None) -> Optional[str]:
        return os.environ.get(key, default)


def load_config(config_path: Optional[Path] = None) -> AppConfig:
    path = config_path or DEFAULT_CONFIG_PATH
    with open(path, "r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)
    return AppConfig(raw, path)


def load_dotenv_if_present() -> None:
    """Load .env from repo root into os.environ, without overriding real env vars."""
    from dotenv import load_dotenv

    env_path = REPO_ROOT / ".env"
    if env_path.exists():
        load_dotenv(dotenv_path=env_path, override=False)
