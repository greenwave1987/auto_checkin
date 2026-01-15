# -*- coding: utf-8 -*-
import builtins
import re

# 保存原始 print
_original_print = builtins.print


def _mask_value(val: str) -> str:
    """保留前三 + 后两位"""
    if not val or len(val) <= 3:
        return val
    if len(val) <= 5:
        return val[0] + "***" + val[-1]
    return val[:3] + "***" + val[-2:]


def _mask_email(email: str) -> str:
    """
    邮箱脱敏：
    username@example.com -> use***me@example.com
    """
    try:
        name, domain = email.split("@", 1)
        return f"{_mask_value(name)}@{domain}"
    except Exception:
        return _mask_value(email)


def desensitize_text(text: str) -> str:
    """统一脱敏入口（print / Telegram 共用）"""
    if not isinstance(text, str):
        return text

    # 📧 邮箱
    email_pattern = re.compile(
        r'\b[a-zA-Z0-9._%+-]{3,}@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}\b'
    )

    # 📱 手机号（11 位）
    phone_pattern = re.compile(r'\b1\d{10}\b')

    text = email_pattern.sub(
        lambda m: _mask_email(m.group(0)),
        text
    )

    text = phone_pattern.sub(
        lambda m: _mask_value(m.group(0)),
        text
    )

    return text


def safe_print(*args, **kwargs):
    masked = []
    for arg in args:
        if isinstance(arg, str):
            masked.append(desensitize_text(arg))
        else:
            masked.append(arg)
    _original_print(*masked, **kwargs)


def enable_safe_print():
    """全局接管 print"""
    builtins.print = safe_print
    _original_print("🔐 [SafePrint] 全局日志脱敏已启用")


def disable_safe_print():
    """恢复原始 print"""
    builtins.print = _original_print
    _original_print("🔓 [SafePrint] 已恢复原始 print")
