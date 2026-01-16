#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import json
import time
import pyotp
from playwright.sync_api import sync_playwright
from engine.config_reader import ConfigReader
from engine.main import SecretUpdater
from engine.notify import TelegramNotifier

# ================= 基础配置 =================
SESSION_SECRET = "GT_SESSION"

# ================= 读取加密配置 =================
config = ConfigReader()
GH_INFO = config.get_value("GH_INFO")  # 列表

# 初始化 session SecretUpdater
secret = SecretUpdater(SESSION_SECRET, config_reader=config)

# 初始化 Telegram 通知器
notifier = TelegramNotifier(config)

# 读取已有 session dict
raw = os.getenv(SESSION_SECRET)
session_map = json.loads(raw) if raw else {}

# ================= 工具函数 =================
def screenshot(page, name):
    path = f"{name}.png"
    page.screenshot(path=path)
    return path

def extract_session(context):
    for c in context.cookies():
        if c["name"] == "user_session":
            return c["value"]
    return None

def validate_session(context, page, session_value):
    context.clear_cookies()
    context.add_cookies([{
        "name": "user_session",
        "value": session_value,
        "domain": "github.com",
        "path": "/"
    }])
    page.goto("https://github.com/settings/profile")
    page.wait_for_load_state("domcontentloaded")
    return "login" not in page.url

def github_login(page, username, password, totp_secret=None):
    page.goto("https://github.com/login")
    page.fill('input[name="login"]', username)
    page.fill('input[name="password"]', password)
    page.click('input[type="submit"]')
    time.sleep(2)
    page.wait_for_load_state("networkidle")

    if "two-factor" in page.url and totp_secret:
        code = pyotp.TOTP(totp_secret).now()
        page.fill('input[autocomplete="one-time-code"]', code)
        page.keyboard.press("Enter")
        time.sleep(2)
        page.wait_for_load_state("networkidle")

    if "login" in page.url:
        raise RuntimeError("GitHub 登录失败")

# ================= 主流程 =================
def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
        context = browser.new_context()
        page = context.new_page()

        for idx, account in enumerate(GH_INFO):
            username = account["username"]
            password = account["password"]
            totp = account.get("2fasecret")
            masked = username[:2] + "***" + username[-2:]

            print(f"👤 账号 {idx}: {masked}", flush=True)

            try:
                need_login = True

                # 检查已有 session
                if username in session_map:
                    print("🍪 校验已有 session", flush=True)
                    if validate_session(context, page, session_map[username]):
                        print("✅ session 有效，跳过登录", flush=True)
                        need_login = False
                    else:
                        print("⚠️ session 失效，需要重新登录", flush=True)

                if need_login:
                    github_login(page, username, password, totp)
                    session = extract_session(context)
                    if not session:
                        raise RuntimeError("未获取 session")

                    session_map[username] = session
                    # 更新 Secret
                    secret.update(json.dumps(session_map, ensure_ascii=False))
                    print("✅ 登录成功 & Session 已更新", flush=True)

            except Exception as e:
                shot = screenshot(page, f"login_failed_{idx}")
                notifier.send(
                    title="❌ GitHub 登录失败",
                    content=f"{masked}\n原因: {e}",
                    image_path=shot
                )
                print(f"❌ 账号失败但继续下一个: {e}", flush=True)

        context.close()
        browser.close()
        print("🟢 所有账号处理完成", flush=True)

if __name__ == "__main__":
    main()
