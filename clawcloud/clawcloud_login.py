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
DEVICE_VERIFY_WAIT = 30 

# ================== 初始化 ==================
config = ConfigReader()
notifier = TelegramNotifier(config) # 使用您已有的通知类

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
        if host.endswith('.console.claw.cloud'):
            region = host.replace('.console.claw.cloud', '')
            if region and region != 'console':
                return region, f"https://{host}"
        return "主站", f"{parsed.scheme}://{parsed.netloc}"
    except:
        return "未知", "https://console.run.claw.cloud"

def wait_device_verification(page, username):
    """处理 GitHub 设备验证 并发送 TG 通知提示手动确认"""
    msg = f"⚠️ <b>设备验证需确认</b>\n账号: <code>{username}</code>\n请检查邮箱或 GitHub App 批准登录。"
    notifier.send_message(msg) # 发送文字提醒
    
    print(f"⚠️ 需要设备验证，等待 {DEVICE_VERIFY_WAIT} 秒...")
    for i in range(DEVICE_VERIFY_WAIT):
        time.sleep(1)
        if "verified-device" not in page.url and "device-verification" not in page.url:
            print("✅ 设备验证通过")
            return True
        if i % 10 == 0:
            try: page.reload()
            except: pass
    return False

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
        screenshot_path = f"screenshot_{username}.png"

        try:
            gost_proc = subprocess.Popen(["./gost", "-L=:8080", f"-F=socks5://{proxy_str}"], 
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            time.sleep(5)

            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
                context = browser.new_context(proxy={"server": local_proxy}, viewport={'width': 1280, 'height': 800})
                page = context.new_page()

                is_logged_in = False

                # --- 🔑 步骤 1: 尝试注入 ClawCloud Cookies ---
                user_claw_cookies = all_claw_cookies.get(username)
                if user_claw_cookies:
                    print("🧪 尝试：注入 ClawCloud Cookies...")
                    context.add_cookies(user_claw_cookies)
                    page.goto("https://console.run.claw.cloud/", timeout=45000)
                    page.wait_for_load_state("networkidle")
                    if "signin" not in page.url:
                        print("✅ ClawCloud Cookies 有效")
                        is_logged_in = True

                # --- 🔑 步骤 2: 执行登录流程 ---
                if not is_logged_in:
                    user_gh_session = all_gh_sessions.get(username)
                    page.goto(CLAW_LOGIN_ENTRY)
                    
                    if user_gh_session:
                        print("🧪 尝试：注入 GH_SESSION...")
                        context.add_cookies([
                            {"name": "user_session", "value": user_gh_session, "domain": ".github.com", "path": "/"},
                            {"name": "logged_in", "value": "yes", "domain": ".github.com", "path": "/"}
                        ])
                    
                    print("🔘 点击 GitHub 登录按钮...")
                    page.click('button:has-text("GitHub"), [data-provider="github"]')
                    time.sleep(5)

                    # 检查是否跳转到 GH 登录页
                    if "github.com/login" in page.url:
                        print("⚠️ Session 失效，执行账号密码登录...")
                        page.fill('input[name="login"]', username)
                        page.fill('input[name="password"]', password)
                        page.keyboard.press("Enter")
                        time.sleep(5)

                        if "device-verification" in page.url:
                            wait_device_verification(page, username)

                        if "two-factor" in page.url:
                            print("🔢 输入 2FA 验证码...")
                            code = pyotp.TOTP(totp_secret.replace(" ", "")).now()
                            otp_input = page.locator('input#app_totp, input#otp, input[name="otp"]').first
                            otp_input.fill(code)
                            page.keyboard.press("Enter")
                            page.wait_for_url("**/claw.cloud/**", timeout=60000)
                        
                    is_logged_in = "claw.cloud" in page.url and "signin" not in page.url

                # --- 📡 检测区域并发送通知 ---
                if is_logged_in:
                    # 1. 检测区域
                    region, base_url = detect_region(page.url)
                    print(f"📍 区域: {region} | URL: {base_url}")
                    
                    # 2. 访问应用列表（保活）
                    page.goto(f"{base_url}/apps")
                    page.wait_for_load_state("networkidle")
                    time.sleep(2)

                    # 3. 截图
                    page.screenshot(path=screenshot_path)

                    # 4. 组装并发送 TG 通知
                    tg_msg = (
                        f"<b>🤖 ClawCloud 自动登录成功</b>\n"
                        f"👤 账号: <code>{username}</code>\n"
                        f"📍 区域: <b>{region}</b>\n"
                        f"🔗 URL: {base_url}"
                    )
                    # 发送图片+文字通知
                    notifier.send_photo(screenshot_path, caption=tg_msg)

                    # 5. 更新本地状态和 Cookies
                    gh_cookies = context.cookies("https://github.com")
                    gh_val = next((c["value"] for c in gh_cookies if c["name"] == "user_session"), None)
                    if gh_val: all_gh_sessions[username] = gh_val
                    all_claw_cookies[username] = context.cookies(base_url)
                    
                    print(f"🟢 {username} 状态更新完成")
                else:
                    notifier.send_message(f"❌ <b>ClawCloud 登录失败</b>\n账号: {username}")

                browser.close()

        except Exception as e:
            print(f"❌ 账号 {username} 异常: {e}")
            notifier.send_message(f"❌ <b>账号 {username} 运行异常</b>\n错误: {str(e)[:100]}")
        finally:
            if gost_proc: gost_proc.terminate()
            if os.path.exists(screenshot_path): os.remove(screenshot_path) # 清理截图文件

    # --- 📤 回写 Secrets ---
    print("\n📤 正在回写 Secrets...")
    gh_session_updater.update(json.dumps(all_gh_sessions))
    claw_cookies_updater.update(json.dumps(all_claw_cookies))
    print("🏁 任务结束")

if __name__ == "__main__":
    main()
