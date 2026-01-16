#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import time
import base64
import json
import pyotp
from pathlib import Path
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
from engine.main import ConfigReader, SecretUpdater
from engine.notify import TelegramNotifier  # 改成你的 notify 实现

# ================== 配置 ==================

CONFIG_PASSWORD = os.getenv("CONFIG_PASSWORD", "").strip()
if not CONFIG_PASSWORD:
    raise RuntimeError("❌ 请设置 CONFIG_PASSWORD")

REPO = os.getenv("GITHUB_REPOSITORY")

# ================== 初始化 ==================

config = ConfigReader()
gh_info = config.get_value("GH_INFO")  # 多账号信息列表
secret_updater = SecretUpdater("GT_SESSION", config_reader=config)
tg_notifier = TelegramNotifier(config)

print(f"✅ 配置解密成功，账号数: {len(gh_info)}")

# ================== 工具函数 ==================

def sep():
    print("="*60, flush=True)

def mask_email(email: str) -> str:
    if "@" not in email:
        return email
    name, domain = email.split("@", 1)
    return f"{name[:3]}***{name[-2:]}@{domain}"

def save_screenshot(page, name):
    path = f"{name}.png"
    page.screenshot(path=path)
    print(f"📸 保存截图: {path}")
    return path

def fill_2fa(page, totp_secret, retries=5, interval=2):
    """
    安全等待并填充 GitHub 2FA 页面
    """
    selector = 'input[autocomplete="one-time-code"]'

    for attempt in range(1, retries + 1):
        print(f"[2FA] 尝试第 {attempt}/{retries} 次等待输入框...", flush=True)
        try:
            page.wait_for_selector(selector, timeout=5000)
            locator = page.locator(selector)
            count = locator.count()
            print(f"[2FA] 元素数量: {count}", flush=True)
            if count > 0 and locator.is_enabled():
                code = pyotp.TOTP(totp_secret).now()
                print(f"[2FA] 填充 TOTP 码: {code}", flush=True)
                locator.fill(code)
                page.keyboard.press("Enter")
                time.sleep(2)
                page.wait_for_load_state("networkidle", timeout=15000)
                print("[2FA] 成功填充并提交", flush=True)
                return True
        except PWTimeout:
            print(f"[2FA] 第 {attempt} 次等待超时，{interval} 秒后重试...", flush=True)
            time.sleep(interval)
        except Exception as e:
            print(f"[2FA] 第 {attempt} 次尝试异常: {e}", flush=True)
            time.sleep(interval)
    print("[2FA] 最终失败，未能填充 TOTP", flush=True)
    return False

# ================== 主流程 ==================

def main():
    session_dict = {}  # 按 username 保存 session

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
        context = browser.new_context()
        page = context.new_page()

        print("🌐 浏览器已启动")
        sep()

        for idx, account in enumerate(gh_info):
            username = account.get("username")
            password = account.get("password")
            totp_secret = account.get("2fasecret")
            env_session = os.getenv(f"GH_SESSION_{username}", "").strip()

            print(f"👤 账号 {idx}: {mask_email(username)}")

            cookies_ok = False
            if env_session:
                print("🍪 检测到环境变量 session，尝试注入 cookies")
                context.add_cookies([
                    {"name":"user_session","value":env_session,"domain":"github.com","path":"/"},
                    {"name":"logged_in","value":"yes","domain":"github.com","path":"/"}
                ])
                page.goto("https://github.com/settings/profile", timeout=30000)
                page.wait_for_load_state("domcontentloaded", timeout=30000)
                if "login" not in page.url:
                    print("✅ session 有效，跳过登录")
                    cookies_ok = True
                else:
                    print("⚠️ session 已失效，需要登录")

            if not cookies_ok:
                # 登录流程
                page.goto("https://github.com/login", timeout=30000)
                page.wait_for_load_state("domcontentloaded", timeout=30000)

                try:
                    page.fill('input[name="login"]', username)
                    page.fill('input[name="password"]', password)
                    page.click('input[type="submit"]')
                    time.sleep(3)
                    page.wait_for_load_state("networkidle", timeout=30000)

                    # 2FA
                    if "two-factor" in page.url:
                        print("🔑 检测到两步验证")
                        if totp_secret:
                            ok = fill_2fa(page, totp_secret)
                            if not ok:
                                shot = save_screenshot(page, f"{username}_2fa_failed")
                                tg_notifier.send("❌ GitHub 登录失败", f"账号 {mask_email(username)} 2FA 失败", shot)
                                print(f"❌ 账号失败但继续下一个: 2FA 填充失败")
                                sep()
                                continue
                        else:
                            print("❌ 未提供 2FA 密钥")
                            shot = save_screenshot(page, f"{username}_2fa_missing")
                            tg_notifier.send("❌ GitHub 登录失败", f"账号 {mask_email(username)} 缺少 2FA 密钥", shot)
                            sep()
                            continue

                    if "login" in page.url:
                        print("❌ 登录失败")
                        shot = save_screenshot(page, f"{username}_login_failed")
                        tg_notifier.send("❌ GitHub 登录失败", f"账号 {mask_email(username)} 登录失败", shot)
                        sep()
                        continue

                    print("✅ 登录成功")

                except Exception as e:
                    print(f"❌ 登录异常: {e}")
                    shot = save_screenshot(page, f"{username}_exception")
                    tg_notifier.send("❌ GitHub 登录异常", f"账号 {mask_email(username)} 异常: {e}", shot)
                    sep()
                    continue

            # 获取 session
            new_session = None
            for c in context.cookies():
                if c["name"]=="user_session" and "github.com" in c["domain"]:
                    new_session = c["value"]
                    break

            if new_session:
                session_dict[username] = new_session
                print(f"🍪 获取 session 成功: {new_session[:6]}****{new_session[-4:]}")
            else:
                print(f"❌ 未获取到 session")
                shot = save_screenshot(page, f"{username}_session_failed")
                tg_notifier.send("❌ GitHub Session 获取失败", f"账号 {mask_email(username)} 未获取到 session", shot)

            sep()

        browser.close()
        print("🟢 所有账号处理完成")

    # 更新 Secret
    if session_dict:
        secret_updater.update(session_dict)
        print("🔄 GT_SESSION 更新完成")

# ================== 入口 ==================
if __name__ == "__main__":
    main()
