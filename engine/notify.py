# notify.py
# -*- coding: utf-8 -*-

"""
通知依赖模块（Telegram）
- 自动读取 GitHub Actions / 系统环境变量
- 支持文字
- 支持图片
"""

import os
import requests
from safe_print import desensitize_text

# =========================
# 环境变量读取
# =========================

TG_BOT_TOKEN = "8525533877:AAGJDqO5TmqtJatwW-tZoDcc8LPtLVVcD8Y"
TG_CHAT_ID = 1966630851


def _check_env():
    print("🔍 检查 Telegram 环境变量")
    if not TG_BOT_TOKEN:
        print("❌ 未检测到 TG_BOT_TOKEN")
        return False
    if not TG_CHAT_ID:
        print("❌ 未检测到 TG_CHAT_ID")
        return False
    print("✅ Telegram 环境变量正常")
    return True


# =========================
# Telegram 文字
# =========================

def send_telegram_text(text):
    if not _check_env():
        return False

    print("📨 [TG] 发送文字通知")

    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TG_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }

    try:
        r = requests.post(url, data=payload, timeout=30)
        print(f"⬅️ [TG] HTTP {r.status_code}")
        if not r.ok:
            print(f"❌ [TG] 失败响应: {r.text}")
        return r.ok
    except Exception as e:
        print(f"💥 [TG] 异常: {e}")
        return False


# =========================
# Telegram 图片
# =========================

def send_telegram_image(image_path, caption=None):
    if not _check_env():
        return False

    print(f"🖼️ [TG] 发送图片: {image_path}")

    if not os.path.exists(image_path):
        print("❌ 图片文件不存在")
        return False

    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendPhoto"

    data = {
        "chat_id": TG_CHAT_ID,
    }
    if caption:
        data["caption"] = caption

    try:
        with open(image_path, "rb") as f:
            files = {"photo": f}
            r = requests.post(url, data=data, files=files, timeout=60)

        print(f"⬅️ [TG] HTTP {r.status_code}")
        if not r.ok:
            print(f"❌ [TG] 失败响应: {r.text}")
        return r.ok
    except Exception as e:
        print(f"💥 [TG] 异常: {e}")
        return False


# =========================
# 统一通知入口（推荐）
# =========================

def send_notify(title, content, image_path=None):
    """
    统一通知入口
    """
    print("🔔 开始发送通知")

    message = f"<b>{title}</b>\n\n{content}"
    message = desensitize_text(message)
    ok_text = send_telegram_text(message)

    ok_img = True
    if image_path:
        title = desensitize_text(title)
        ok_img = send_telegram_image(image_path, caption=title)

    return ok_text and ok_img
