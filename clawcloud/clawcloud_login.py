#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import sys
import os
import json
import time
import subprocess
import pyotp
import re
from urllib.parse import urlparse
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)

from engine.main import ConfigReader, SecretUpdater
from engine.notify import TelegramNotifier

# ================== 基础配置 ==================
USE_PROXY = False  # <--- 在这里控制是否使用代理: True 使用, False 直连
CLAW_LOGIN_ENTRY = "https://console.run.claw.cloud/signin"
TARGET_REGION_URL = "https://ap-northeast-1.run.claw.cloud"
WAIT_MAX_TIMEOUT = 120000  # 120 秒

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

# ================== 工具函数 ==================

def perform_gh_login(page, username, password, totp_secret):
    """GitHub 授权逻辑"""
    print(f"🔘 [点击] 尝试通过 GitHub 按钮登录...")
    try:
        page.click('button:has-text("GitHub"), [data-provider="github"]', timeout=15000)
    except:
        print("⚠️ 未发现 GitHub 按钮，可能已进入跳转流")
    
    time.sleep(5)
    if "github.com/login" in page.url:
        page.fill('input[name="login"]', username)
        page.fill('input[name="password"]', password)
        page.keyboard.press("Enter")
        time.sleep(8)
        if "two-factor" in page.url:
            code = pyotp.TOTP(totp_secret.replace(" ", "")).now()
            page.locator('input#app_totp, input#otp, input[name="otp"]').first.fill(code)
            page.keyboard.press("Enter")
            page.wait_for_url("**/claw.cloud/**", timeout=60000)

def save_state(context, username, current_url):
    gh_cookies = context.cookies("https://github.com")
    gh_val = next((c["value"] for c in gh_cookies if c["name"] == "user_session"), None)
    if gh_val: all_gh_sessions[username] = gh_val
    all_claw_cookies[username] = context.cookies(current_url)

# ================== 核心逻辑 ==================

def main():
    if not gh_info: return

    for idx, (account, proxy) in enumerate(zip(gh_info, proxy_info)):
        username = account["username"]
        password = account["password"]
        totp_secret = account.get("2fasecret", "")
        
        print(f"\n{'='*20} 👤 账号: {username} {'='*20}")
        gost_proc = None
        screenshot_p1 = f"p1_{username}.png"
        screenshot_p2 = f"p2_{username}.png"

        try:
            # --- 代理控制 ---
            browser_proxy = None
            if USE_PROXY:
                proxy_str = f"{proxy['username']}:{proxy['password']}@{proxy['server']}:{proxy['port']}"
                local_proxy = "http://127.0.0.1:8080"
                print(f"🔌 [代理] 启动 Gost 隧道: {proxy['server']}...")
                gost_proc = subprocess.Popen(["./gost", "-L=:8080", f"-F=socks5://{proxy_str}"], 
                                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                time.sleep(5)
                browser_proxy = {"server": local_proxy}
            else:
                print("🌐 [直连] 当前未启用代理变量。")

            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
                # 根据 USE_PROXY 变量注入代理配置
                context = browser.new_context(proxy=browser_proxy, viewport={'width': 1280, 'height': 800})
                page = context.new_page()

                # --- 🚩 阶段 1：主站登录 ---
                print(f"🚩 [阶段 1] 登录主站: {CLAW_LOGIN_ENTRY}")
                page.goto(CLAW_LOGIN_ENTRY)
                
                user_gh_session = all_gh_sessions.get(username)
                if user_gh_session:
                    context.add_cookies([{"name": "user_session", "value": user_gh_session, "domain": ".github.com", "path": "/"}])
                
                perform_gh_login(page, username, password, totp_secret)
                
                # 等待离开登录页 (120s)
                try:
                    page.wait_for_function("() => !window.location.href.includes('signin')", timeout=WAIT_MAX_TIMEOUT)
                except:
                    print("⚠️ [警告] 阶段 1 离开登录页超时")

                # 寻找 Launchpad
                launchpad_success = False
                print(f"🔍 [控制台] 寻找 Launchpad 入口 (限时120s)...")
                try:
                    target = page.get_by_text("App Launchpad")
                    target.wait_for(state="visible", timeout=WAIT_MAX_TIMEOUT)
                    target.click()
                    launchpad_success = True
                    print("✅ [点击] 成功进入 Launchpad")
                    page.wait_for_load_state("networkidle")
                except Exception as e:
                    print(f"❌ [失败] 未能点击 Launchpad: {e}")

                # 阶段 1 强制截图与消息
                page.screenshot(path=screenshot_p1)
                save_state(context, username, page.url)
                status_text = "成功" if launchpad_success else "失败/超时"
                notifier.send(
                    title=f"{username}-阶段1-{status_text}", 
                    content=f"📍 当前网址: {page.url}\n💬 备注: 阶段1入口寻找完毕。", 
                    image_path=screenshot_p1
                )

                # --- 🚩 阶段 2：日本子站 ---
                print(f"🚩 [阶段 2] 访问日本区域 (120s 监控)...")
                page.goto(TARGET_REGION_URL)
                time.sleep(5)

                if "signin" in page.url or "login" in page.url:
                    perform_gh_login(page, username, password, totp_secret)
                
                try:
                    page.wait_for_function("() => !window.location.href.includes('signin')", timeout=WAIT_MAX_TIMEOUT)
                except:
                    print("⚠️ [警告] 阶段 2 离开登录页超时")

                # 寻找余额
                time.sleep(15) 
                balance_text = "未获取到"
                try:
                    # 查找包含 $ 符号且不含 Credit 的数值
                    balance_el = page.locator('p:has-text("$")').filter(has_not_text="Credit").first
                    balance_el.wait_for(state="visible", timeout=30000)
                    balance_text = balance_el.inner_text()
                except:
                    pass

                # 阶段 2 强制截图与消息
                page.screenshot(path=screenshot_p2)
                save_state(context, username, page.url)
                notifier.send(
                    title=f"{username}-阶段2-最终状态", 
                    content=f"💵 余额: {balance_text}\n📍 最终网址: {page.url}", 
                    image_path=screenshot_p2
                )

                browser.close()

        except Exception as e:
            print(f"💥 [严重异常] {username}: {e}")
            notifier.send(title=f"{username}-运行异常", content=f"错误: {str(e)}\n网址: {page.url if 'page' in locals() else '未知'}")
        finally:
            if gost_proc: gost_proc.terminate()
            for f in [screenshot_p1, screenshot_p2]:
                if os.path.exists(f): os.remove(f)

    # 回写 Secrets
    gh_session_updater.update(json.dumps(all_gh_sessions))
    claw_cookies_updater.update(json.dumps(all_claw_cookies))

if __name__ == "__main__":
    main()
