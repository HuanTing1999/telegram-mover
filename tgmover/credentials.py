"""Resolve an application's Telegram credentials without storing login inputs.

Application IDs identify this software. A distributor may supply an application
credential file; end users can then sign in with only their phone and code.
This module never creates credentials or borrows another application's keys.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass, field
import os
from pathlib import Path
import re
import sys
import tomllib
from typing import Mapping


class CredentialsError(ValueError):
    """Credential setup is missing or invalid; messages contain no values."""


@dataclass(frozen=True, slots=True)
class ApplicationCredentials:
    api_id: int = field(repr=False)
    api_hash: str = field(repr=False)
    source_label: str


@dataclass(frozen=True, slots=True)
class CredentialsStatus:
    ready: bool
    source_label: str
    message: str


def application_directory() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[2]


def bundle_directory() -> Path | None:
    if getattr(sys, "frozen", False) and getattr(sys, "_MEIPASS", None):
        return Path(sys._MEIPASS)
    return None


def _checked(api_id, api_hash, source_label) -> ApplicationCredentials:
    if isinstance(api_id, str) and re.fullmatch(r"[0-9]+", api_id):
        api_id = int(api_id)
    if isinstance(api_id, bool) or not isinstance(api_id, int) or not 1 <= api_id <= 2147483647:
        raise CredentialsError("尚未设置有效的应用 ID（API_ID）。请在“应用设置”填写 Telegram 申请页面提供的正整数 ID。")
    if not isinstance(api_hash, str) or not re.fullmatch(r"[0-9a-fA-F]{32}", api_hash):
        raise CredentialsError("尚未设置有效的应用密钥（API_HASH）。请在“应用设置”填写与应用 ID 配对的 32 位密钥。")
    return ApplicationCredentials(api_id, api_hash, source_label)


def read_application_credentials(path: str | Path) -> ApplicationCredentials:
    """Read only a minimal app file, never a user's full sync configuration."""
    try:
        with Path(path).expanduser().open("rb") as handle:
            data = tomllib.load(handle)
    except (OSError, ValueError, UnicodeError):
        raise CredentialsError("无法读取软件应用凭证文件，请检查文件路径和 TOML 格式。") from None
    values = data.get("telegram")
    if set(data) != {"telegram"} or not isinstance(values, dict) or set(values) != {"api_id", "api_hash"}:
        raise CredentialsError("软件凭证文件只能包含 [telegram] 下的 api_id 和 api_hash，不能包含手机号、验证码、会话或任务设置。")
    return _checked(values["api_id"], values["api_hash"], "软件预置")


def _has_config_value(values: Mapping) -> bool:
    value = values.get("api_id")
    empty_id = value is None or (type(value) is int and value == 0) or (isinstance(value, str) and value in {"", "0"})
    return not empty_id or values.get("api_hash") not in (None, "")


def resolve_credentials(telegram: Mapping, config_path=None) -> ApplicationCredentials:
    """Environment overrides an explicit pair or an application-level default.

    A partially configured user pair never borrows fields from a lower-priority
    application's defaults. Environment variables retain the original explicit
    per-field overrides, including completing a pair kept partly in config.
    Empty GUI defaults permit fallback; empty environment values report errors.
    """
    if not isinstance(telegram, Mapping):
        raise CredentialsError("应用设置格式无效，请重新选择配置文件。")
    environment_id = "API_ID" in os.environ
    environment_hash = "API_HASH" in os.environ
    if environment_id and environment_hash:
        return _checked(os.environ["API_ID"], os.environ["API_HASH"], "环境变量")

    api_id = telegram.get("api_id")
    api_hash = telegram.get("api_hash")
    source_label = "本机配置"
    if not _has_config_value(telegram):
        explicit_path = os.environ.get("TGMOVER_APP_CREDENTIALS")
        external = Path(explicit_path).expanduser() if explicit_path else application_directory() / "app_credentials.toml"
        bundled = bundle_directory()
        if explicit_path is not None or external.is_file():
            defaults = read_application_credentials(external)
            api_id, api_hash = defaults.api_id, defaults.api_hash
            source_label = "软件预置（本机文件）"
        elif bundled is not None and (bundled / "app_credentials.toml").is_file():
            defaults = read_application_credentials(bundled / "app_credentials.toml")
            api_id, api_hash = defaults.api_id, defaults.api_hash
            source_label = "软件内置"
        elif not environment_id and not environment_hash:
            raise CredentialsError(
                "本软件尚未配置应用凭证。请展开“应用设置”完成一次配置；"
                "设置保存后，日常登录只需手机号和验证码。软件不会自动申请或生成有效凭证。"
            )
    if environment_id:
        api_id = os.environ["API_ID"]
    if environment_hash:
        api_hash = os.environ["API_HASH"]
    if environment_id or environment_hash:
        source_label = "环境变量 / " + source_label
    return _checked(api_id, api_hash, source_label)


def credentials_status(config_path: str | Path, *, telegram: Mapping | None = None) -> CredentialsStatus:
    """Report readiness/source, without exposing or copying credential values."""
    try:
        if telegram is None:
            try:
                with Path(config_path).open("rb") as handle:
                    data = tomllib.load(handle)
            except FileNotFoundError:
                data = {}
            except (OSError, ValueError, UnicodeError):
                raise CredentialsError("无法读取应用设置，请检查配置文件。") from None
            telegram = data.get("telegram", {})
        credentials = resolve_credentials(telegram, config_path)
        return CredentialsStatus(True, credentials.source_label,
                                 f"已自动读取{credentials.source_label}中的应用凭证。现在只需输入手机号和验证码。")
    except CredentialsError as exc:
        return CredentialsStatus(False, "尚未配置", str(exc))


def main(argv=None):
    parser = argparse.ArgumentParser(description="Validate app credentials without printing their values.")
    parser.add_argument("--validate-file", required=True)
    parser.add_argument("--stage-file", help="Write only the two validated application fields for an explicit build.")
    args = parser.parse_args(argv)
    try:
        credentials = read_application_credentials(args.validate_file)
        if args.stage_file:
            # Both validated values have restricted numeric/hex alphabets.
            # Serialize afresh so comments and unrelated source data cannot
            # accidentally become embedded in a distributed application.
            Path(args.stage_file).write_text(
                f'[telegram]\napi_id = {credentials.api_id}\napi_hash = "{credentials.api_hash}"\n',
                encoding="utf-8",
            )
    except CredentialsError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except OSError:
        print("Cannot stage the application credential file (values hidden).", file=sys.stderr)
        return 1
    print("Application credential file format is valid (values hidden).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
