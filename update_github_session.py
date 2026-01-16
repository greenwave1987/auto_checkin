#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import json
import time
import pyotp
from pathlib import Path
from playwright.sync_api import sync_playwright

from engine.config_reader import ConfigReader
from engine.main import SecretUpdater
from engine.notify import send_notify

# ================= 基础配置 =================
SESSION_SECRET = "GT_SESSION"
MATRIX_INDEX = int(os.getenv("MATRIX_INDEX", "0"))

# ================= 读取加密配置 =================
config = ConfigReader()  # 自动读取 engine/config.enc 并解密
GH_INFO = config.get_value("GH_INFO")

ACCOUNT = GH_INFO[MATRIX_INDEX]
USERNAME = ACCOUNT["username"]
PASSWORD = ACCOUNT["password"]
TOTP_SECRET = ACCOUNT.get("2fasecret")

MASKED = USERNAME[:2] + "***" + USERNAME[-2:]

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
    context.add_cookies([{
        "name": "user_session",
        "value": session_value,
        "domain": "github.com",
        "path": "/"
    }])
    page.goto("https://github.com/settings/profile")
    page.wait_for_load_state("domcontentloaded")
    return "login" not in page.url

def github_login(page):
    page.goto("https://github.com/login")
    page.fill('input[name="login"]', USERNAME)
    page.fill('input[name="password"]', PASSWORD)
    page.click('input[type="submit"]')
    time.sleep(2)
    page.wait_for_load_state("networkidle")

    if "two-factor" in page.url and TOTP_SECRET:
        code = pyotp.TOTP(TOTP_SECRET).now()
        page.fill('input[autocomplete="one-time-code"]', code)
        page.keyboard.press("Enter")
        time.sleep(2)
        page.wait_for_load_state("networkidle")

    if "login" in page.url:
        raise RuntimeError("GitHub 登录失败")

# ================= 主流程 =================
def main():
    print(f"👤 Matrix[{MATRIX_INDEX}] 账号: {MASKED}", flush=True)

    # 读取已有 session dict
    raw = os.getenv(SESSION_SECRET)
    session_map = json.loads(raw) if raw else {}

    secret = SecretUpdater(SESSION_SECRET, config_reader=config)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
        context = browser.new_context()
        page = context.new_page()

        try:
            need_login = True

            if USERNAME in session_map:
                print("🍪 校验已有 session", flush=True)
                if validate_session(context, page, session_map[USERNAME]):
                    print("✅ session 有效，跳过登录", flush=True)
                    need_login = False
                else:
                    print("⚠️ session 失效，需要重新登录", flush=True)

            if need_login:
                github_login(page)
                session = extract_session(context)
                if not session:
                    raise RuntimeError("未获取 session")

                session_map[USERNAME] = session
                secret.update(json.dumps(session_map, ensure_ascii=False))
                print("✅ 登录成功 & Session 已更新", flush=True)

        except Exception as e:
            shot = screenshot(page, f"login_failed_{MATRIX_INDEX}")
            send_notify(
                f"❌ GitHub 登录失败",
                f"{MASKED}\n原因: {e}",
                shot
            )
            print(f"❌ 失败但不中断其他账号: {e}", flush=True)

        finally:
            context.close()
            browser.close()

if __name__ == "__main__":
    main()
