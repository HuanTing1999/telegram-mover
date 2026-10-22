"""Read local TOML configuration without storing account login secrets."""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .limits import LimitSettings
from .filters import FilterConfigError, FilterSettings, validate_filter_settings
from .credentials import CredentialsError, resolve_credentials


MODES = frozenset({"download_only", "forward", "reupload", "download_then_forward"})


class ConfigError(ValueError):
    """A configuration value is missing, unsupported, or unsafe."""


@dataclass(frozen=True, slots=True)
class Config:
    api_id: int
    api_hash: str
    source: str | int
    target: str | int | None
    mode: str
    comment_mode: str
    sync_comments: bool
    session: Path
    database: Path
    download_dir: Path
    start_id: int
    poll_interval: float
    settle_seconds: float
    max_flood_wait: int
    send_delay: float
    limits: LimitSettings = field(default_factory=LimitSettings)
    filters: FilterSettings = field(default_factory=FilterSettings)
    source_topic_id: int = 0
    target_topic_id: int = 0
    auto_cleanup_forwarded: bool = False
    download_connections: int = 4


_ALLOWED = {
    "telegram": {"api_id", "api_hash", "session"},
    "sync": {
        "source", "target", "mode", "comment_mode", "sync_comments", "start_id",
        "poll_interval", "settle_seconds", "max_flood_wait", "send_delay",
        "source_topic_id", "target_topic_id",
    },
    "storage": {"database", "download_dir", "auto_cleanup_forwarded", "download_connections"},
    "limits": {
        "enabled", "min_send_interval", "min_request_interval", "max_messages_per_minute",
        "max_messages_per_hour", "max_messages_per_day", "max_messages_per_run",
        "max_runtime_minutes", "max_auto_wait", "retry_wait_errors", "wait_padding",
        "active_start", "active_end", "timezone",
    },
    "filters": {
        "enabled", "keywords", "regex_patterns", "blocked_domains", "allowed_keywords", "allowed_domains",
        "block_all_links", "block_buttons", "apply_to_comments", "apply_to_downloads", "blocked_message_keys",
    },
}


def _integer(value: Any, name: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool):
        raise ConfigError(f"{name} must be an integer >= {minimum}.")
    if isinstance(value, str) and re.fullmatch(r"[0-9]+", value):
        value = int(value)
    if not isinstance(value, int) or value < minimum:
        raise ConfigError(f"{name} must be an integer >= {minimum}.")
    return value


def _number(value: Any, name: str, *, minimum: float = 0.0) -> float:
    import math

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{name} must be a number >= {minimum}.")
    value = float(value)
    if not math.isfinite(value) or value < minimum:
        raise ConfigError(f"{name} must be a finite number >= {minimum}.")
    return value


def _topic_id(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 2147483647:
        raise ConfigError(f"{name} 必须是 0 到 2147483647 之间的整数；0 表示整个源群或目标默认位置。")
    return value


def _peer(value: Any, name: str, *, required: bool) -> str | int | None:
    if value is None and not required:
        return None
    if isinstance(value, int) and not isinstance(value, bool) and value != 0:
        return value
    if isinstance(value, str) and value.strip():
        return value.strip()
    suffix = " is required" if required else " must be a channel/group name or ID"
    raise ConfigError(f"sync.{name}{suffix}.")


def _path(value: Any, name: str, base: Path) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{name} must be a non-empty filesystem path.")
    result = Path(value).expanduser()
    return (result if result.is_absolute() else base / result).resolve()


def _validate_keys(data: dict[str, Any]) -> None:
    for section, values in data.items():
        if section not in _ALLOWED:
            raise ConfigError(f"Unsupported configuration section: {section}.")
        if not isinstance(values, dict):
            raise ConfigError(f"[{section}] must be a TOML table.")
        for key in values:
            normalized = key.casefold().replace("-", "_")
            if normalized in {"bot_token", "token", "phone", "phone_number", "password", "code"}:
                raise ConfigError(
                    f"{section}.{key} is not accepted: login is interactive phone + code; "
                    "do not store login details or bot tokens in configuration."
                )
            if key not in _ALLOWED[section]:
                raise ConfigError(f"Unsupported configuration key: {section}.{key}.")


def _limits(values: dict[str, Any]) -> LimitSettings:
    defaults = LimitSettings()
    validated = {}
    enabled = values.get("enabled", defaults.enabled)
    if not isinstance(enabled, bool):
        raise ConfigError("limits.enabled must be true or false.")
    validated["enabled"] = enabled
    for name in ("max_messages_per_minute", "max_messages_per_hour", "max_messages_per_day",
                 "max_messages_per_run", "retry_wait_errors"):
        validated[name] = _integer(values.get(name, getattr(defaults, name)), f"limits.{name}")
    if validated["retry_wait_errors"] > 10:
        raise ConfigError("limits.retry_wait_errors must be between 0 and 10.")
    for name in ("min_send_interval", "min_request_interval", "max_runtime_minutes",
                 "max_auto_wait", "wait_padding"):
        validated[name] = _number(values.get(name, getattr(defaults, name)), f"limits.{name}")
    for name in ("active_start", "active_end"):
        value = values.get(name, getattr(defaults, name))
        if not isinstance(value, str) or not re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", value):
            raise ConfigError(f"limits.{name} must be a time in HH:MM format.")
        validated[name] = value
    zone = values.get("timezone", defaults.timezone)
    if not isinstance(zone, str) or not re.fullmatch(r"[+-](?:0[0-9]|1[0-4]):[0-5][0-9]", zone):
        raise ConfigError("limits.timezone must be a fixed UTC offset such as '+08:00'.")
    if int(zone[1:3]) == 14 and zone[4:] != "00":
        raise ConfigError("limits.timezone must be within -14:00 and +14:00.")
    validated["timezone"] = zone
    return LimitSettings(**validated)


def _filters(values: dict[str, Any]) -> FilterSettings:
    defaults = FilterSettings()
    validated = {}
    for name in ("enabled", "block_all_links", "block_buttons", "apply_to_comments", "apply_to_downloads"):
        value = values.get(name, getattr(defaults, name))
        if not isinstance(value, bool):
            raise ConfigError(f"filters.{name} must be true or false.")
        validated[name] = value
    for name in ("keywords", "regex_patterns", "blocked_domains", "allowed_keywords", "allowed_domains", "blocked_message_keys"):
        value = values.get(name, getattr(defaults, name))
        if name in values and not isinstance(value, list):
            raise ConfigError(f"filters.{name} must be a TOML array of strings.")
        validated[name] = tuple(value)
    try:
        return validate_filter_settings(FilterSettings(**validated))
    except FilterConfigError as exc:
        raise ConfigError(str(exc)) from exc


def load_config(path: str | Path, *, account_only: bool = False) -> Config:
    """Load TOML; API_ID/API_HASH environment variables override TOML values.

    Relative storage/session paths are anchored to the configuration directory,
    independent of the process's working directory. The session contains the
    Telethon account authorization; this module never accepts a phone or code.
    """
    path = Path(path).expanduser().resolve()
    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"Cannot read TOML configuration {path}: {exc}") from exc
    _validate_keys(data)
    telegram = data.get("telegram", {})
    sync = data.get("sync", {})
    storage = data.get("storage", {})
    auto_cleanup_forwarded = storage.get("auto_cleanup_forwarded", False)
    if not isinstance(auto_cleanup_forwarded, bool):
        raise ConfigError("storage.auto_cleanup_forwarded 必须是 true 或 false。")
    download_connections = storage.get("download_connections", 4)
    if isinstance(download_connections, bool) or not isinstance(download_connections, int) or not 1 <= download_connections <= 8:
        raise ConfigError("storage.download_connections 必须是 1 到 8 之间的整数。")

    try:
        credentials = resolve_credentials(telegram, path)
    except CredentialsError as exc:
        raise ConfigError(str(exc)) from None
    mode = sync.get("mode", "reupload")
    comment_mode = sync.get("comment_mode", "download_only" if mode == "download_only" else "reupload")
    if not isinstance(mode, str) or mode not in MODES:
        raise ConfigError(f"sync.mode must be one of: {', '.join(sorted(MODES))}.")
    if not isinstance(comment_mode, str) or comment_mode not in MODES:
        raise ConfigError(f"sync.comment_mode must be one of: {', '.join(sorted(MODES))}.")
    if mode == "download_only" and comment_mode != "download_only":
        raise ConfigError("download_only mode requires comment_mode = 'download_only'.")
    sync_comments = sync.get("sync_comments", True)
    if not isinstance(sync_comments, bool):
        raise ConfigError("sync.sync_comments must be true or false.")

    source_value, target_value = sync.get("source"), sync.get("target")
    if account_only:
        # Login can happen before a task is configured. These placeholders must
        # never weaken the regular task-loading path's required-peer checks.
        if source_value is None or (isinstance(source_value, str) and not source_value.strip()):
            source_value = "@source_channel"
        if target_value is None or (isinstance(target_value, str) and not target_value.strip()):
            target_value = "@target_channel"

    return Config(
        api_id=credentials.api_id,
        api_hash=credentials.api_hash,
        source=_peer(source_value, "source", required=True),
        target=_peer(target_value, "target", required=mode != "download_only"),
        mode=mode,
        comment_mode=comment_mode,
        sync_comments=sync_comments,
        session=_path(telegram.get("session", "data/telegram"), "telegram.session", path.parent),
        database=_path(storage.get("database", "data/state.sqlite3"), "storage.database", path.parent),
        download_dir=_path(storage.get("download_dir", "downloads"), "storage.download_dir", path.parent),
        start_id=_integer(sync.get("start_id", 0), "sync.start_id"),
        poll_interval=_number(sync.get("poll_interval", 0), "sync.poll_interval"),
        settle_seconds=_number(sync.get("settle_seconds", 3), "sync.settle_seconds", minimum=2),
        max_flood_wait=_integer(sync.get("max_flood_wait", 300), "sync.max_flood_wait"),
        send_delay=_number(sync.get("send_delay", 0.5), "sync.send_delay"),
        limits=_limits(data.get("limits", {})),
        filters=_filters(data.get("filters", {})),
        source_topic_id=_topic_id(sync.get("source_topic_id", 0), "sync.source_topic_id"),
        target_topic_id=_topic_id(sync.get("target_topic_id", 0), "sync.target_topic_id"),
        auto_cleanup_forwarded=auto_cleanup_forwarded,
        download_connections=download_connections,
    )
