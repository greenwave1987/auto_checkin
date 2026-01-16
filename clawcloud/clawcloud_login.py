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

def perform_gh_login(page, username, password, totp_secret):
    """统一执行 GitHub 授权登录"""
    print(f"🔘 [点击] 尝试通过 GitHub 授权...")
    try:
        page.click('button:has-text("GitHub"), [data-provider="github"]', timeout=10000)
    except:
        print("⚠️ 未找到 GitHub 按钮，可能已处于登录中间态")
    
    time.sleep(5)
    if "github.com/login" in page.url:
        print(f"⌨️ [表单] 输入 GitHub 账号密码...")
        page.fill('input[name="login"]', username)
        page.fill('input[name="password"]', password)
        page.keyboard.press("Enter")
        time.sleep(5)

        if "two-factor" in page.url:
            print(f"🔢 [2FA] 输入验证码...")
            code = pyotp.TOTP(totp_secret.replace(" ", "")).now()
            page.locator('input#app_totp, input#otp, input[name="otp"]').first.fill(code)
            page.keyboard.press("Enter")
            # 等待回到 claw 域名
            page.wait_for_url("**/claw.cloud/**", timeout=60000)

def wait_for_console_stable(page):
    """等待页面离开登录态并稳定"""
    print("⏳ [等待] 确认已离开登录页面...")
    try:
        # 确保网址不包含 signin
        page.wait_for_function("() => !window.location.href.includes('signin')", timeout=30000)
        page.wait_for_load_state("networkidle")
        return True
    except:
        return False

def save_state(context, username, current_url):
    """回写最新的 Session 和 Cookie"""
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

                # --- 🚩 第一阶段：主站入口登录 ---
                print(f"🚩 [阶段 1] 访问登录入口...")
                page.goto(CLAW_LOGIN_ENTRY)
                
                # 注入 Session 缓存
                user_gh_session = all_gh_sessions.get(username)
                if user_gh_session:
                    context.add_cookies([{"name": "user_session", "value": user_gh_session, "domain": ".github.com", "path": "/"}])
                
                perform_gh_login(page, username, password, totp_secret)
                
                if wait_for_console_stable(page):
                    print("🔍 [控制台] 正在寻找 App Launchpad 入口...")
                    try:
                        # 定位 <p>App Launchpad</p> 并点击
                        launchpad = page.get_by_text("App Launchpad")
                        launchpad.wait_for(state="visible", timeout=20000)
                        launchpad.click()
                        print("🔘 [点击] 成功进入 App Launchpad")
                        page.wait_for_load_state("networkidle")
                        time.sleep(5)
                        
                        save_state(context, username, page.url)
                        page.screenshot(path=screenshot_p1)
                        notifier.send(title=f"{username}-主控制台进入成功", content=f"🔗 当前 URL: {page.url}", image_path=screenshot_p1)
                    except Exception as e:
                        print(f"⚠️ [警告] 未能点击 Launchpad: {e}")
                else:
                    print("❌ [错误] 阶段 1 登录状态校验失败")
                    continue

                # --- 🚩 第二阶段：跳转日本子站并获取余额 ---
                print(f"🚩 [阶段 2] 跳转目标子站: {TARGET_REGION_URL}")
                page.goto(TARGET_REGION_URL)
                time.sleep(5)

                # 检查是否掉线需要重新 GitHub 授权
                if "signin" in page.url or "login" in page.url:
                    print("⚠️ [重连] 检测到掉线，执行二次登录补丁...")
                    perform_gh_login(page, username, password, totp_secret)
                
                # 等待直到网址不再是登录页
                if wait_for_console_stable(page):
                    print("⌛ [数据] 等待页面缓存加载余额...")
                    time.sleep(10) # 充分等待后台接口返回数据
                    
                    # 精准定位余额
                    balance_text = "N/A"
                    try:
                        # 查找包含 $ 符号的文本，通常在特定的 css 类或结构下
                        # 按照你的描述查找类似 $4.84 的内容
                        balance_element = page.locator('p:has-text("$")').filter(has_not_text="Credit").first
                        balance_element.wait_for(state="visible", timeout=15000)
                        balance_text = balance_element.inner_text()
                        print(f"💰 [成功] 余额获取完成: {balance_text}")
                    except Exception as e:
                        print(f"⚠️ [失败] 无法定位余额元素: {e}")

                    page.screenshot(path=screenshot_p2)
                    save_state(context, username, page.url)
                    
                    notifier.send(
                        title=f"{username}-子站余额检测", 
                        content=f"💵 <b>最终余额:</b> <code>{balance_text}</code>\n📍 区域: 日本(Tokyo)\n🔗 URL: {page.url}", 
                        image_path=screenshot_p2
                    )

                browser.close()

        except Exception as e:
            print(f"💥 [崩溃] {username}: {e}")
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
