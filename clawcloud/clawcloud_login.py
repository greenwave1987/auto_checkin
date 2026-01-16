#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import sys
import os
import json
import time
import subprocess
import pyotp
import re  # 导入正则
from urllib.parse import urlparse
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)

from engine.main import ConfigReader, SecretUpdater
from engine.notify import TelegramNotifier

# ================== 基础配置 ==================
CLAW_LOGIN_ENTRY = "https://console.run.claw.cloud/signin"
TARGET_REGION_URL = "https://ap-northeast-1.run.claw.cloud"
DEVICE_VERIFY_WAIT = 30 

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

def detect_region(url):
    """从 URL 中检测区域信息"""
    try:
        parsed = urlparse(url)
        host = parsed.netloc
        if "ap-northeast-1" in host: return "日本 (Tokyo)"
        if "ap-southeast-1" in host: return "新加坡"
        return "主控制台"
    except:
        return "未知区域"

def get_balance(page):
    """抓取页面上的余额信息"""
    try:
        # 使用 css class 和文本特征定位
        selector = 'p.chakra-text:has-text("$")'
        page.wait_for_selector(selector, timeout=10000)
        text = page.locator(selector).first.inner_text()
        return text.strip()
    except:
        return "N/A"

def perform_gh_login(page, username, password, totp_secret):
    """统一的 GitHub 登录逻辑"""
    print(f"🔘 [点击] 正在通过 GitHub 按钮登录...")
    page.click('button:has-text("GitHub"), [data-provider="github"]')
    time.sleep(5)

    if "github.com/login" in page.url:
        print(f"⌨️ [表单] 输入 GitHub 凭据...")
        page.fill('input[name="login"]', username)
        page.fill('input[name="password"]', password)
        page.keyboard.press("Enter")
        time.sleep(5)

        if "two-factor" in page.url:
            print(f"🔢 [2FA] 输入验证码...")
            code = pyotp.TOTP(totp_secret.replace(" ", "")).now()
            page.locator('input#app_totp, input#otp, input[name="otp"]').first.fill(code)
            page.keyboard.press("Enter")
            page.wait_for_url("**/claw.cloud/**", timeout=60000)
    return

def save_state(context, username, current_url):
    """保存状态"""
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
        proxy_str = f"{proxy['username']}:{proxy['password']}@{proxy['server']}:{proxy['port']}"
        local_proxy = "http://127.0.0.1:8080"
        
        print(f"\n{'='*20} 👤 账号: {username} {'='*20}")
        gost_proc = None
        screenshot_p1 = f"p1_{username}.png"
        screenshot_p2 = f"p2_{username}.png"

        try:
            gost_proc = subprocess.Popen(["./gost", "-L=:8080", f"-F=socks5://{proxy_str}"], 
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            time.sleep(5)

            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
                context = browser.new_context(proxy={"server": local_proxy}, viewport={'width': 1280, 'height': 800})
                page = context.new_page()

                # --- 🚩 第一阶段：主站登录 ---
                print(f"🚩 [阶段 1] 登录主站...")
                page.goto(CLAW_LOGIN_ENTRY)
                
                user_gh_session = all_gh_sessions.get(username)
                if user_gh_session:
                    context.add_cookies([{"name": "user_session", "value": user_gh_session, "domain": ".github.com", "path": "/"}])
                
                perform_gh_login(page, username, password, totp_secret)
                page.wait_for_load_state("networkidle")

                if "signin" not in page.url:
                    balance = get_balance(page)
                    print(f"✅ [成功] 登录主站，余额: {balance}")
                    
                    save_state(context, username, page.url)
                    page.screenshot(path=screenshot_p1)
                    notifier.send(
                        title=f"{username}-主站登录成功", 
                        content=f"💰 <b>账户余额:</b> <code>{balance}</code>\n📍 区域: {detect_region(page.url)}\n🔗 URL: {page.url}", 
                        image_path=screenshot_p1
                    )
                else:
                    print("❌ [错误] 阶段 1 失败")
                    continue

                # --- 🚩 第二阶段：日本站跳转 ---
                print(f"🚩 [阶段 2] 跳转日本区域...")
                page.goto(TARGET_REGION_URL)
                page.wait_for_load_state("networkidle")
                time.sleep(5)

                if "signin" in page.url or "login" in page.url:
                    print("⚠️ [警告] 发生掉线，重新登录补丁...")
                    perform_gh_login(page, username, password, totp_secret)
                    page.wait_for_url(f"**{urlparse(TARGET_REGION_URL).netloc}/**", timeout=30000)

                # 最终页面确认
                page.wait_for_load_state("networkidle")
                time.sleep(5)
                
                final_balance = get_balance(page)
                page.screenshot(path=screenshot_p2)
                save_state(context, username, page.url)
                
                notifier.send(
                    title=f"{username}-日本站跳转结果", 
                    content=f"💰 <b>账户余额:</b> <code>{final_balance}</code>\n📍 区域: {detect_region(page.url)}\n🔗 URL: {page.url}", 
                    image_path=screenshot_p2
                )

                browser.close()

        except Exception as e:
            print(f"💥 [异常] {username}: {e}")
            notifier.send(title=f"{username} 异常", content=str(e)[:100])
        finally:
            if gost_proc: gost_proc.terminate()
            for f in [screenshot_p1, screenshot_p2]:
                if os.path.exists(f): os.remove(f)

    # 回写 Secrets
    gh_session_updater.update(json.dumps(all_gh_sessions))
    claw_cookies_updater.update(json.dumps(all_claw_cookies))

if __name__ == "__main__":
    main()
