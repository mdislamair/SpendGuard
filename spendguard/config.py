"""Config loading and validation (TOML, standard library `tomllib`)."""
from __future__ import annotations

import datetime as dt
import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .core import PERIODS, STARTER_PRICES, Price


class ConfigError(Exception):
    pass


@dataclass
class Config:
    timezone: dt.tzinfo
    warn_at: float = 0.8
    limits: dict[str, float] = field(default_factory=dict)
    project_limits: dict[str, dict[str, float]] = field(default_factory=dict)
    prices: dict[str, Price] = field(default_factory=lambda: dict(STARTER_PRICES))
    claude_dirs: list[str] = field(default_factory=list)
    usage_files: list[str] = field(default_factory=list)
    source_path: str | None = None


def default_config_paths() -> list[Path]:
    paths = [Path("spendguard.toml")]
    xdg = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    paths.append(Path(xdg) / "spendguard" / "config.toml")
    return paths


def _number(value: object, where: str, minimum: float = 0.0) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{where} must be a number, got {value!r}")
    if value < minimum:
        raise ConfigError(f"{where} must be >= {minimum}, got {value}")
    return float(value)


def _limits(table: object, where: str) -> dict[str, float]:
    if not isinstance(table, dict):
        raise ConfigError(f"{where} must be a table")
    out: dict[str, float] = {}
    for key, value in table.items():
        if key not in PERIODS:
            raise ConfigError(f"{where}.{key}: unknown period (use {', '.join(PERIODS)})")
        limit = _number(value, f"{where}.{key}")
        if limit == 0:
            raise ConfigError(f"{where}.{key} must be greater than 0")
        out[key] = limit
    return out


def local_timezone() -> dt.tzinfo:
    return dt.datetime.now().astimezone().tzinfo or dt.timezone.utc


def parse_config(data: dict, source_path: str | None = None) -> Config:
    tz: dt.tzinfo = local_timezone()
    if "timezone" in data:
        try:
            tz = ZoneInfo(str(data["timezone"]))
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ConfigError(f"timezone {data['timezone']!r} not found") from exc

    cfg = Config(timezone=tz, source_path=source_path)

    if "warn_at" in data:
        cfg.warn_at = _number(data["warn_at"], "warn_at")
        if not 0 < cfg.warn_at <= 1:
            raise ConfigError("warn_at must be between 0 and 1 (for example 0.8)")

    if "budget" in data:
        cfg.limits = _limits(data["budget"], "budget")

    for name, table in (data.get("projects") or {}).items():
        cfg.project_limits[name] = _limits(table, f"projects.{name}")

    for pattern, table in (data.get("pricing") or {}).items():
        if not isinstance(table, dict) or "input" not in table or "output" not in table:
            raise ConfigError(f"pricing.{pattern} needs at least 'input' and 'output' (USD per million tokens)")
        cfg.prices[pattern] = Price(
            input=_number(table["input"], f"pricing.{pattern}.input"),
            output=_number(table["output"], f"pricing.{pattern}.output"),
            cache_read=_number(table["cache_read"], f"pricing.{pattern}.cache_read") if "cache_read" in table else None,
            cache_write=_number(table["cache_write"], f"pricing.{pattern}.cache_write") if "cache_write" in table else None,
        )

    sources = data.get("sources") or {}
    cfg.claude_dirs = [str(p) for p in sources.get("claude", [])]
    cfg.usage_files = [str(p) for p in sources.get("usage", [])]
    return cfg


def load_config(explicit: str | None = None) -> Config:
    """Load the config file. With no file anywhere, return defaults (no budgets)."""
    candidates = [Path(explicit)] if explicit else default_config_paths()
    for path in candidates:
        if path.is_file():
            try:
                with path.open("rb") as fh:
                    data = tomllib.load(fh)
            except tomllib.TOMLDecodeError as exc:
                raise ConfigError(f"{path}: invalid TOML: {exc}") from exc
            try:
                return parse_config(data, str(path))
            except ConfigError as exc:
                raise ConfigError(f"{path}: {exc}") from exc
    if explicit:
        raise ConfigError(f"config file not found: {explicit}")
    return parse_config({})


SAMPLE_CONFIG = """\
# spendguard.toml - run `spendguard check` to compare your usage to these limits.

# Optional. Defaults to this computer's timezone. Day/week/month boundaries use it.
# timezone = "Asia/Kolkata"

# Warn when spend reaches this fraction of a limit.
warn_at = 0.8

# Global limits in USD, for the current day / ISO week (Monday start) / calendar month.
[budget]
daily = 20
weekly = 100
monthly = 300

# Optional per-project limits. The name matches the project folder name.
[projects.my-repo]
monthly = 50

# Prices are USD per 1 million tokens. The built-in values are only STARTER
# estimates. Check your provider's pricing page and edit these.
# The longest matching pattern wins, e.g. "sonnet-4" beats "sonnet".
[pricing.sonnet]
input = 3.0
output = 15.0
# cache_read  defaults to 10% of input, cache_write to 125% of input

[pricing.opus]
input = 15.0
output = 75.0

[pricing.haiku]
input = 1.0
output = 5.0

# Optional. Where to read usage from. Defaults: Claude Code's local log folders.
# [sources]
# claude = ["~/.claude/projects"]
# usage  = ["team-usage.jsonl"]   # normalised JSONL, see README
"""
