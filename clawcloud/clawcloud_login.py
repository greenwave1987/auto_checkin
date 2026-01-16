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
USE_PROXY = True  # 是否使用代理
CLAW_LOGIN_ENTRY = "https://console.run.claw.cloud/signin"
TARGET_REGION_URL = "https://ap-northeast-1.run.claw.cloud"
WAIT_MAX_TIMEOUT = 120000  # 120 秒超时

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
    print(f"🔘 [点击] 尝试通过 GitHub 授权登录...")
    try:
        # 寻找 GitHub 登录按钮
        gh_btn = page.locator('button:has-text("GitHub"), [data-provider="github"]').first
        gh_btn.wait_for(state="visible", timeout=15000)
        gh_btn.click()
    except:
        print("⚠️ 未发现登录按钮，可能已在跳转中...")
    
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
            # --- 代理处理 ---
            browser_proxy = None
            if USE_PROXY:
                proxy_str = f"{proxy['username']}:{proxy['password']}@{proxy['server']}:{proxy['port']}"
                gost_proc = subprocess.Popen(["./gost", "-L=:8080", f"-F=socks5://{proxy_str}"], 
                                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                time.sleep(5)
                browser_proxy = {"server": "http://127.0.0.1:8080"}

            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
                context = browser.new_context(proxy=browser_proxy, viewport={'width': 1280, 'height': 800})
                page = context.new_page()

                # --- 🚩 阶段 1：主站登录并点击 Launchpad ---
                print(f"🚩 [阶段 1] 访问登录入口: {CLAW_LOGIN_ENTRY}")
                page.goto(CLAW_LOGIN_ENTRY)
                
                # 注入 GitHub 会话
                user_gh_session = all_gh_sessions.get(username)
                if user_gh_session:
                    context.add_cookies([{"name": "user_session", "value": user_gh_session, "domain": ".github.com", "path": "/"}])
                
                perform_gh_login(page, username, password, totp_secret)
                
                # 等待直到退出登录 URL (120s)
                try:
                    page.wait_for_function("() => !window.location.href.includes('signin')", timeout=WAIT_MAX_TIMEOUT)
                except:
                    print("⚠️ [警告] 阶段 1 离开登录页超时")

                # 点击 App Launchpad 进入真实控制台
                launchpad_success = False
                print(f"🔍 [控制台] 寻找 'App Launchpad' 入口 (120s)...")
                try:
                    # 使用更加稳健的定位方式
                    lp_btn = page.locator('p:has-text("App Launchpad"), div:has-text("App Launchpad")').last
                    lp_btn.wait_for(state="visible", timeout=WAIT_MAX_TIMEOUT)
                    lp_btn.click()
                    launchpad_success = True
                    print("✅ [点击] 成功进入 App Launchpad")
                    page.wait_for_load_state("networkidle")
                except Exception as e:
                    print(f"❌ [失败] 未能点击 Launchpad: {e}")

                # 截图并发送 P1 消息
                page.screenshot(path=screenshot_p1)
                save_state(context, username, page.url)
                notifier.send(
                    title=f"{username}-阶段1-点击进入", 
                    content=f"📍 网址: {page.url}\n💬 点击状态: {'成功' if launchpad_success else '失败'}", 
                    image_path=screenshot_p1
                )

                # --- 🚩 阶段 2：跳转日本站抓余额 ---
                print(f"🚩 [阶段 2] 跳转目标日本子站...")
                page.goto(TARGET_REGION_URL)
                time.sleep(5)

                if "signin" in page.url or "login" in page.url:
                    print("⚠️ [检测] 掉线，尝试补丁登录...")
                    perform_gh_login(page, username, password, totp_secret)
                
                try:
                    page.wait_for_function("() => !window.location.href.includes('signin')", timeout=WAIT_MAX_TIMEOUT)
                    page.wait_for_load_state("networkidle")
                except:
                    print("⚠️ [警告] 阶段 2 状态校验超时")

                # 深度缓存等待并抓取余额
                time.sleep(15) 
                balance_text = "N/A"
                try:
                    # 排除 Landing Page 的 "$5 Credit" 干扰，寻找纯数字金额
                    # 逻辑：寻找包含 $ 符号，且父级或自身不包含 "Benefit" 或 "Credit" 的 P 标签
                    balance_els = page.locator('p:has-text("$")')
                    count = balance_els.count()
                    for i in range(count):
                        txt = balance_els.nth(i).inner_text()
                        if "Credit" not in txt and "Benefit" not in txt:
                            balance_text = txt
                            break
                    print(f"💰 [成功] 最终抓取余额: {balance_text}")
                except Exception as e:
                    print(f"❌ [失败] 无法提取余额: {e}")

                # 截图并发送 P2 最终消息
                page.screenshot(path=screenshot_p2)
                save_state(context, username, page.url)
                notifier.send(
                    title=f"{username}-阶段2-余额检测", 
                    content=f"💵 余额: {balance_text}\n📍 最终网址: {page.url}", 
                    image_path=screenshot_p2
                )

                browser.close()

        except Exception as e:
            print(f"💥 [严重异常] {username}: {e}")
            notifier.send(title=f"{username}-运行异常", content=f"错误: {str(e)[:200]}")
        finally:
            if gost_proc: gost_proc.terminate()
            for f in [screenshot_p1, screenshot_p2]:
                if os.path.exists(f): os.remove(f)

    # 同步状态回写
    gh_session_updater.update(json.dumps(all_gh_sessions))
    claw_cookies_updater.update(json.dumps(all_claw_cookies))

if __name__ == "__main__":
    main()
