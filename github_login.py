#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import time
import json
import pyotp
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

from engine.main import ConfigReader, SecretUpdater
from engine.notify import TelegramNotifier

# ================== 常量 ==================
GITHUB_LOGIN_URL = "https://github.com/login"
GITHUB_TEST_URL = "https://github.com/settings/profile"
SESSION_SECRET_NAME = "GT_SESSION"

# ================== 工具函数 ==================
def sep():
    print("=" * 60, flush=True)

def mask_user(u: str) -> str:
    return u[:2] + "***" + u[-2:] if len(u) > 4 else "***"

def save_screenshot(page, name):
    path = f"{name}.png"
    page.screenshot(path=path)
    return path

# ================== 2FA 填充逻辑 ==================
def fill_2fa(page, totp_secret, retries=3, interval=2):
    for attempt in range(retries):
        try:
            locator = page.locator('input[autocomplete="one-time-code"]')
            if locator.is_visible(timeout=5000):
                code = pyotp.TOTP(totp_secret).now()
                page.fill('input[autocomplete="one-time-code"]', code)
                page.keyboard.press("Enter")
                time.sleep(2)
                page.wait_for_load_state("networkidle", timeout=15000)
                return True
        except PWTimeout:
            print(f"⚠️ 2FA 输入框未出现，重试 {attempt+1}/{retries}")
            time.sleep(interval)
    return False

# ================== 主流程 ==================
def main():
    # ---------- 读取配置 ----------
    config = ConfigReader()
    gh_list = config.get_value("GH_INFO")
    notifier = TelegramNotifier(config)
    secret = SecretUpdater(SESSION_SECRET_NAME, config_reader=config)

    print(f"🔐 读取账号数: {len(gh_list)}", flush=True)
    sep()

    all_sessions = {}

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox"])

        for idx, gh in enumerate(gh_list):
            username = gh.get("username")
            password = gh.get("password")
            totp_secret = gh.get("2fasecret")

            masked = mask_user(username)
            print(f"👤 账号 {idx}: {masked}", flush=True)

            context = browser.new_context()
            page = context.new_page()

            try:
                # ================== 阶段一：cookies 校验 ==================
                cookies_ok = False
                existing_sessions = os.getenv(SESSION_SECRET_NAME, "")
                if existing_sessions:
                    try:
                        data = json.loads(existing_sessions)
                        old_session = data.get(username)
                        if old_session:
                            context.add_cookies([
                                {"name": "user_session", "value": old_session, "domain": "github.com", "path": "/"},
                                {"name": "logged_in", "value": "yes", "domain": "github.com", "path": "/"}
                            ])
                            page.goto(GITHUB_TEST_URL, timeout=30000)
                            page.wait_for_load_state("domcontentloaded", timeout=30000)
                            if "login" not in page.url:
                                print("✅ cookies 有效，跳过登录", flush=True)
                                cookies_ok = True
                                all_sessions[username] = old_session
                    except Exception:
                        pass

                # ================== 阶段二：登录 ==================
                if not cookies_ok:
                    page.goto(GITHUB_LOGIN_URL, timeout=30000)
                    page.wait_for_load_state("domcontentloaded", timeout=30000)

                    page.fill('input[name="login"]', username)
                    page.fill('input[name="password"]', password)
                    page.click('input[type="submit"]')

                    time.sleep(3)
                    page.wait_for_load_state("networkidle", timeout=30000)

                    # ---------- 2FA ----------
                    if "two-factor" in page.url:
                        print("🔑 检测到两步验证", flush=True)
                        if not totp_secret:
                            raise RuntimeError("缺少 2FA 密钥")

                        ok = fill_2fa(page, totp_secret)
                        if not ok:
                            raise RuntimeError("2FA 输入框超时或未出现")

                    if "login" in page.url:
                        raise RuntimeError("登录失败，仍停留在 login")

                # ================== 阶段三：获取 session ==================
                new_session = None
                for c in context.cookies():
                    if c["name"] == "user_session" and "github.com" in c["domain"]:
                        new_session = c["value"]
                        break

                if not new_session:
                    raise RuntimeError("未获取到 user_session")

                all_sessions[username] = new_session
                print("🍪 Session 获取成功", flush=True)

            except Exception as e:
                print(f"❌ 账号失败但继续下一个: {e}", flush=True)
                shot = save_screenshot(page, f"login_failed_{idx}")
                notifier.send(
                    "❌ GitHub 登录失败",
                    f"账号：{masked}\n错误：{e}",
                    shot
                )

            finally:
                context.close()

        browser.close()

    # ================== 更新 Secret ==================
    if all_sessions:
        secret.update(json.dumps(all_sessions, ensure_ascii=False))
        notifier.send(
            "✅ GitHub Session 更新完成",
            f"成功更新账号数：{len(all_sessions)}"
        )

    print("🟢 所有账号处理完成", flush=True)

# ================== 入口 ==================
if __name__ == "__main__":
    main()
