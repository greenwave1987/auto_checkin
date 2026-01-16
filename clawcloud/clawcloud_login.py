#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import json
import time
import subprocess
import pyotp
import requests
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)

# 导入自定义模块
from engine.main import ConfigReader, SecretUpdater
from engine.notify import TelegramNotifier

# ================== 基础配置 ==================
GITHUB_LOGIN_URL = "https://github.com/login"
CLAWCLOUD_LOGIN_URL = "https://console.run.claw.cloud/signin" # 假设的 ClawCloud 登录地址
CLAWCLOUD_TEST_URL = "https://console.run.claw.cloud/" # 用于校验登录状态的地址

# ================== 初始化 ==================
config = ConfigReader()
notifier = TelegramNotifier(config)

# 1. 从 Secrets 获取
gh_session_env = os.getenv("GH_SESSION", "{}").strip()
claw_cookies_env = os.getenv("CLAWCLOUD_COOKIES", "{}").strip()

# 2. 从 ConfigReader 获取
gh_info = config.get_value("GH_INFO")
proxy_info = config.get_value("PROXY_INFO")

# 3. 初始化更新器
gh_session_updater = SecretUpdater("GH_SESSION", config_reader=config)
claw_cookies_updater = SecretUpdater("CLAWCLOUD_COOKIES", config_reader=config)

# 解析字典
try:
    all_gh_sessions = json.loads(gh_session_env)
    all_claw_cookies = json.loads(claw_cookies_env)
except:
    all_gh_sessions, all_claw_cookies = {}, {}

# ================== 核心逻辑 ==================

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
        
        print(f"\n{'='*60}\n👤 账号: {username} | 代理: {proxy['server']}")
        
        gost_proc = None
        try:
            # 1. 启动隧道
            gost_proc = subprocess.Popen(["./gost", "-L=:8080", f"-F=socks5://{proxy_str}"], 
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            time.sleep(5)

            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
                context = browser.new_context(proxy={"server": local_proxy})
                page = context.new_page()

                is_logged_in = False

                # --- 🔑 优先级 1：ClawCloud Cookies 直接登录 ---
                user_claw_cookies = all_claw_cookies.get(username)
                if user_claw_cookies:
                    print("尝试：使用 ClawCloud Cookies 直接注入...")
                    context.add_cookies(user_claw_cookies)
                    page.goto(CLAWCLOUD_TEST_URL)
                    if "login" not in page.url.lower():
                        print("✅ ClawCloud Cookies 有效")
                        is_logged_in = True

                # --- 🔑 优先级 2：GH_SESSION GitHub 授权登录 ---
                if not is_logged_in:
                    user_gh_session = all_gh_sessions.get(username)
                    if user_gh_session:
                        print("尝试：注入 GH_SESSION 免密登录 GitHub...")
                        context.add_cookies([
                            {"name": "user_session", "value": user_gh_session, "domain": "github.com", "path": "/"},
                            {"name": "logged_in", "value": "yes", "domain": "github.com", "path": "/"}
                        ])
                        page.goto(GITHUB_LOGIN_URL)
                        # 如果注入后访问登录页跳转到了首页或设置页，说明有效
                        if "login" not in page.url.lower():
                            print("✅ GH_SESSION 有效，正在跳转 ClawCloud...")
                            page.goto(CLAWCLOUD_LOGIN_URL)
                            # 此处通常点击 "Login with GitHub" 按钮
                            is_logged_in = True 

                # --- 🔑 优先级 3：GH_INFO 账号密码 + 2FA 登录 ---
                if not is_logged_in:
                    print("尝试：使用账号密码 + 2FA 登录...")
                    page.goto(GITHUB_LOGIN_URL)
                    page.fill('input[name="login"]', username)
                    page.fill('input[name="password"]', password)
                    page.keyboard.press("Enter")
                    time.sleep(3)

                    otp_selector = 'input#app_totp, input#otp'
                    if page.query_selector(otp_selector):
                        print("🔢 输入 2FA 验证码...")
                        code = pyotp.TOTP(totp_secret.replace(" ", "")).now()
                        page.fill(otp_selector, code)
                        page.keyboard.press("Enter")
                        time.sleep(5)
                    
                    page.goto(CLAWCLOUD_LOGIN_URL) # 登录后跳转至业务平台
                    is_logged_in = True

                # --- 💾 阶段：更新状态 ---
                if is_logged_in:
                    # 提取 GitHub Session
                    gh_cookie = next((c["value"] for c in context.cookies() if c["name"] == "user_session"), None)
                    if gh_cookie:
                        all_gh_sessions[username] = gh_cookie
                    
                    # 提取 ClawCloud 所有的 Cookies (列表形式存储)
                    all_claw_cookies[username] = context.cookies()
                    print(f"🟢 {username} 状态更新完成")

                browser.close()

        except Exception as e:
            print(f"❌ 账号 {username} 异常: {e}")
        finally:
            if gost_proc:
                gost_proc.terminate()

    # --- 📤 阶段：回写 Secrets ---
    print("\n📤 正在回写 Secrets...")
    gh_session_updater.update(json.dumps(all_gh_sessions))
    claw_cookies_updater.update(json.dumps(all_claw_cookies))
    print("🏁 任务结束")

if __name__ == "__main__":
    main()
