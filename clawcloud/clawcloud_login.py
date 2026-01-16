#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import sys
import os
import json
import time
import subprocess
import pyotp
import requests
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)

from engine.main import ConfigReader, SecretUpdater
from engine.notify import TelegramNotifier

# ================== 基础配置 ==================
CLAW_SIGNIN_URL = "https://console.run.claw.cloud/signin"
CLAW_CONSOLE_URL = "https://console.run.claw.cloud/"

# ================== 初始化 ==================
config = ConfigReader()
notifier = TelegramNotifier(config)

gh_session_env = os.getenv("GH_SESSION", "{}").strip()
claw_cookies_env = os.getenv("CLAWCLOUD_COOKIES", "{}").strip()

gh_info = config.get_value("GH_INFO")
proxy_info = config.get_value("PROXY_INFO")

gh_session_updater = SecretUpdater("GH_SESSION", config_reader=config)
claw_cookies_updater = SecretUpdater("CLAWCLOUD_COOKIES", config_reader=config)

try:
    all_gh_sessions = json.loads(gh_session_env)
    all_claw_cookies = json.loads(claw_cookies_env)
except:
    all_gh_sessions, all_claw_cookies = {}, {}

def main():
    if not gh_info:
        print("❌ 未获取到账号信息")
        return

    for idx, (account, proxy) in enumerate(zip(gh_info, proxy_info)):
        username = account["username"]
        password = account["password"]
        totp_secret = account.get("2fasecret", "")
        
        proxy_str = f"{proxy['username']}:{proxy['password']}@{proxy['server']}:{proxy['port']}"
        local_proxy = "http://127.0.0.1:8080"
        
        print(f"\n{'='*60}\n👤 账号: {username}")
        
        gost_proc = None
        try:
            gost_proc = subprocess.Popen(["./gost", "-L=:8080", f"-F=socks5://{proxy_str}"], 
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            time.sleep(5)

            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
                context = browser.new_context(proxy={"server": local_proxy})
                page = context.new_page()

                # --- 🔑 步骤 1：注入 ClawCloud Cookie 验证 ---
                is_logged_in = False
                user_claw_cookies = all_claw_cookies.get(username)
                if user_claw_cookies:
                    print("🧪 尝试：注入 ClawCloud Cookies...")
                    context.add_cookies(user_claw_cookies)
                    page.goto(CLAW_CONSOLE_URL, timeout=40000)
                    if "signin" not in page.url:
                        print("✅ ClawCloud Cookie 有效")
                        is_logged_in = True

                # --- 🔑 步骤 2：若失败，注入 GH_SESSION 并通过 GitHub 登录按钮验证 ---
                if not is_logged_in:
                    user_gh_session = all_gh_sessions.get(username)
                    print(f"🧪 尝试：通过 GitHub Session 登录...")
                    page.goto(CLAW_SIGNIN_URL)
                    
                    if user_gh_session:
                        context.add_cookies([
                            {"name": "user_session", "value": user_gh_session, "domain": ".github.com", "path": "/"},
                            {"name": "logged_in", "value": "yes", "domain": ".github.com", "path": "/"}
                        ])
                    
                    # 点击 "Continue with GitHub" 按钮
                    page.click('button:has-text("GitHub"), [href*="github.com/login/oauth"]')
                    page.wait_for_load_state("networkidle")
                    time.sleep(5)

                    # 判断是否直接登录成功（URL 跳转回 console.claw.cloud）
                    if "console.run.claw.cloud" in page.url and "signin" not in page.url:
                        print("✅ GH_SESSION 有效，自动跳转成功")
                        is_logged_in = True
                    else:
                        # --- 🔑 步骤 3：若 GH_SESSION 无效，走账号密码登录 ---
                        print("⚠️ GH_SESSION 失效或不存在，开始手动登录 GitHub...")
                        if "github.com/login" in page.url:
                            page.fill('input[name="login"]', username)
                            page.fill('input[name="password"]', password)
                            page.keyboard.press("Enter")
                            time.sleep(3)

                            # 处理 2FA
                            otp_selector = 'input#app_totp, input#otp'
                            if page.query_selector(otp_selector):
                                print("🔢 输入 GitHub 2FA 验证码...")
                                code = pyotp.TOTP(totp_secret.replace(" ", "")).now()
                                page.fill(otp_selector, code)
                                page.keyboard.press("Enter")
                                # 等待 GitHub 授权跳转回 ClawCloud
                                page.wait_for_url("**/console.run.claw.cloud/**", timeout=60000)
                            
                            is_logged_in = True

                # --- 💾 提取阶段：更新 Session 和 Cookie ---
                if is_logged_in:
                    # 1. 提取 GitHub Session (从 github.com 域名下找)
                    gh_cookies = context.cookies("https://github.com")
                    gh_sess_val = next((c["value"] for c in gh_cookies if c["name"] == "user_session"), None)
                    if gh_sess_val:
                        all_gh_sessions[username] = gh_sess_val
                        print("📝 已获取最新的 GH_SESSION")

                    # 2. 提取 ClawCloud 全量 Cookies
                    all_claw_cookies[username] = context.cookies("https://console.run.claw.cloud")
                    print(f"🟢 {username} 登录成功，Cookie 已提取")

                browser.close()

        except Exception as e:
            print(f"❌ 账号 {username} 执行异常: {e}")
        finally:
            if gost_proc:
                gost_proc.terminate()

    # --- 📤 回写 ---
    print("\n📤 同步数据至 Secrets...")
    gh_session_updater.update(json.dumps(all_gh_sessions))
    claw_cookies_updater.update(json.dumps(all_claw_cookies))
    print("🏁 任务结束")

if __name__ == "__main__":
    main()
