"""User login with a phone/code and Telegram-required 2FA; reuse sessions."""

from __future__ import annotations

import getpass
from typing import Any, Callable

from telethon import errors

from .login_input import normalize_code, normalize_phone


class AuthError(RuntimeError):
    """The interactive user-account login cannot continue."""


async def _password_login(client: Any, password_fn: Callable[[str], str]) -> None:
    for attempt in range(3):
        password = password_fn("Telegram 两步验证密码（输入不显示）：")
        # Leading/trailing whitespace belongs to the password. In particular,
        # a password consisting of spaces is not the same as an empty input.
        if not isinstance(password, str) or password == "":
            if attempt == 2:
                raise AuthError("连续 3 次两步验证密码无效或为空，本次登录停止。")
            continue
        try:
            await client.sign_in(password=password)
            return
        except errors.PasswordHashInvalidError:
            if attempt == 2:
                raise AuthError("连续 3 次两步验证密码无效或为空，本次登录停止。") from None
        finally:
            # No config/log receives this value; retain it only for this RPC.
            password = None


async def login(
    client: Any,
    input_fn: Callable[[str], str] = input,
    code_fn: Callable[[str], str] = getpass.getpass,
    *,
    password_fn: Callable[[str], str] = getpass.getpass,
) -> Any:
    """Connect and authenticate a user, asking for 2FA only when required.

    The caller owns the connection and must disconnect it when finished.
    Telegram chooses whether the code arrives in an existing app or by SMS.
    The optional password stays in memory for authentication and is never saved.
    """
    await client.connect()
    if not await client.is_user_authorized():
        try:
            phone = normalize_phone(input_fn("Telegram 国际手机号（如菲律宾 +63…、美国 +1…、英国 +44…）："))
        except ValueError as exc:
            raise AuthError(str(exc)) from exc

        sent = await client.send_code_request(phone)
        for attempt in range(3):
            raw_code = code_fn("Telegram 验证码：")
            # Telethon treats an empty code as a request to send another code.
            # Count it locally so this tool never silently starts that flow.
            try:
                code = normalize_code(raw_code)
            except ValueError:
                if attempt == 2:
                    raise AuthError("连续 3 次验证码无效或为空，本次登录停止。")
                continue
            try:
                await client.sign_in(
                    phone=phone,
                    code=code,
                    phone_code_hash=sent.phone_code_hash,
                )
                break
            except errors.SessionPasswordNeededError:
                await _password_login(client, password_fn)
                break
            except errors.PhoneCodeExpiredError as exc:
                raise AuthError(
                    "验证码已过期；本次登录停止，未自动重发。请重新运行后申请新验证码。"
                ) from exc
            except (errors.PhoneCodeInvalidError, errors.PhoneCodeEmptyError) as exc:
                if attempt == 2:
                    raise AuthError("连续 3 次验证码无效或为空，本次登录停止。") from exc

    me = await client.get_me()
    if me is None:
        raise AuthError("未能取得已登录的 Telegram 用户信息。")
    if getattr(me, "bot", False):
        raise AuthError("当前 session 属于机器人账号；本工具仅允许 MTProto 用户账号。请更换 session 路径。")
    return me
