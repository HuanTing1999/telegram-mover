"""Shared international phone/code input handling; never infer a country."""

from __future__ import annotations

import re
import unicodedata


_DASHES = frozenset("-‐‑‒–—―−﹘﹣－")


def _clean(value: str, *, phone: bool) -> str:
    if not isinstance(value, str):
        raise ValueError("手机号必须是文本。" if phone else "验证码必须是文本。")
    result = []
    for character in unicodedata.normalize("NFKC", value):
        if character.isspace() or character in _DASHES or (phone and character in "()"):
            continue
        if character.isdecimal():
            result.append(str(unicodedata.decimal(character)))
        else:
            result.append(character)
    return "".join(result)


def normalize_phone(value: str) -> str:
    """Return +digits, accepting formatting and an explicit 00 country prefix.

    Users must supply their own international country calling code. A national
    number is never silently assigned a country or prefixed with +86.
    """
    phone = _clean(value, phone=True)
    if phone.startswith("00"):
        phone = "+" + phone[2:]
    if not re.fullmatch(r"\+[1-9][0-9]{6,14}", phone):
        raise ValueError(
            "手机号格式无效：请填写 + 或 00 开头的国际区号和号码，共 7 至 15 位数字；"
            "支持所有国家和地区，例如菲律宾 +63、美国 +1、英国 +44，不会自动添加 +86。"
        )
    return phone


def normalize_code(value: str) -> str:
    """Return ASCII digits after removing accepted whitespace and separators."""
    code = _clean(value, phone=False)
    if not code:
        raise ValueError("请输入 Telegram 验证码。")
    if not re.fullmatch(r"[0-9]+", code):
        raise ValueError("验证码格式无效：只能输入数字，可含空格或连字符；请复制验证码本身。")
    return code
