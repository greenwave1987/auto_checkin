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
        if host.endswith('.console.claw.cloud'):
            region = host.replace('.console.claw.cloud', '')
            if region and region != 'console':
                return region, f"https://{host}"
        return "主站", f"{parsed.scheme}://{parsed.netloc}"
    except:
        return "未知", "https://console.run.claw.cloud"

def wait_device_verification(page, username):
    """处理 GitHub 设备验证"""
    print(f"📡 [通知] 正在发送设备验证提醒到 TG...")
    msg = f"⚠️ <b>设备验证需确认</b>\n账号: <code>{username}</code>\n请检查邮箱或 GitHub App 批准登录。"
    notifier.send(title="GitHub 设备验证", content=msg)
    
    print(f"⏳ [等待] 需要设备验证，每10秒检查一次状态，总共等待 {DEVICE_VERIFY_WAIT} 秒...")
    for i in range(DEVICE_VERIFY_WAIT):
        time.sleep(1)
        if "verified-device" not in page.url and "device-verification" not in page.url:
            print("✅ [验证] 设备验证已在网页端通过！")
            return True
        if (i + 1) % 10 == 0:
            print(f"🔄 [重试] 已等待 {i+1} 秒，尝试刷新页面检测状态...")
            try: page.reload()
            except: pass
    print("❌ [超时] 设备验证未能在规定时间内完成。")
    return False

# ================== 核心逻辑 ==================

def main():
    if not gh_info:
        print("❌ [错误] 未获取到账号信息，请检查配置文件。")
        return

    print(f"🚀 [启动] 开始处理共 {len(gh_info)} 个账号...")

    for idx, (account, proxy) in enumerate(zip(gh_info, proxy_info)):
        username = account["username"]
        password = account["password"]
        totp_secret = account.get("2fasecret", "")
        
        proxy_str = f"{proxy['username']}:{proxy['password']}@{proxy['server']}:{proxy['port']}"
        local_proxy = "http://127.0.0.1:8080"
        
        print(f"\n{'='*20} 👤 账号 ({idx+1}/{len(gh_info)}): {username} {'='*20}")
        
        gost_proc = None
        screenshot_path = f"screenshot_{username}.png"

        try:
            print(f"🔌 [代理] 正在启动 Gost 隧道连接: {proxy['server']}...")
            gost_proc = subprocess.Popen(["./gost", "-L=:8080", f"-F=socks5://{proxy_str}"], 
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            time.sleep(5) # 等待代理稳定

            with sync_playwright() as p:
                print(f"🌐 [浏览器] 启动 Chromium 无头模式...")
                browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
                context = browser.new_context(proxy={"server": local_proxy}, viewport={'width': 1280, 'height': 800})
                page = context.new_page()

                is_logged_in = False

                # --- 🔑 步骤 1: 尝试注入 ClawCloud Cookies ---
                user_claw_cookies = all_claw_cookies.get(username)
                if user_claw_cookies:
                    print(f"🧪 [Cookie] 发现现有 Claw 缓存，尝试直接注入...")
                    context.add_cookies(user_claw_cookies)
                    print(f"🛰️ [访问] 正在跳转主控制台...")
                    page.goto("https://console.run.claw.cloud/", timeout=45000)
                    page.wait_for_load_state("networkidle")
                    
                    if "signin" not in page.url:
                        print(f"✅ [成功] 缓存有效，跳过登录流程。当前 URL: {page.url}")
                        is_logged_in = True
                    else:
                        print(f"⚠️ [失效] Claw 缓存已过期。")

                # --- 🔑 步骤 2: 执行登录流程 ---
                if not is_logged_in:
                    print(f"🔑 [登录] 开始执行 GitHub 登录流程...")
                    user_gh_session = all_gh_sessions.get(username)
                    page.goto(CLAW_LOGIN_ENTRY)
                    
                    if user_gh_session:
                        print(f"🧪 [Cookie] 注入 GitHub Session 缓存...")
                        context.add_cookies([
                            {"name": "user_session", "value": user_gh_session, "domain": ".github.com", "path": "/"},
                            {"name": "logged_in", "value": "yes", "domain": ".github.com", "path": "/"}
                        ])
                    
                    print(f"🔘 [点击] 正在点击 GitHub 登录按钮...")
                    page.click('button:has-text("GitHub"), [data-provider="github"]')
                    time.sleep(5)

                    if "github.com/login" in page.url:
                        print(f"⌨️ [表单] Session 缺失，正在输入账号密码...")
                        page.fill('input[name="login"]', username)
                        page.fill('input[name="password"]', password)
                        page.keyboard.press("Enter")
                        time.sleep(5)

                        if "device-verification" in page.url:
                            wait_device_verification(page, username)

                        if "two-factor" in page.url:
                            print(f"🔢 [2FA] 检测到二次验证，正在生成验证码...")
                            code = pyotp.TOTP(totp_secret.replace(" ", "")).now()
                            print(f"🔢 [2FA] 输入验证码: {code}")
                            otp_input = page.locator('input#app_totp, input#otp, input[name="otp"]').first
                            otp_input.fill(code)
                            page.keyboard.press("Enter")
                            print(f"⏳ [等待] 正在等待 ClawCloud 授权跳转...")
                            page.wait_for_url("**/claw.cloud/**", timeout=60000)
                        
                    is_logged_in = "claw.cloud" in page.url and "signin" not in page.url

                # --- 📡 检测区域并发送通知 ---
                if is_logged_in:
                    print(f"🔍 [检测] 正在分析当前分配的节点区域...")
                    region, base_url = detect_region(page.url)
                    print(f"📍 [结果] 账号名-自动检测区域: {region} | 基础 URL: {base_url}")
                    
                    print(f"📸 [截图] 正在访问应用列表页面并准备截图...")
                    try:
                        page.goto(f"{base_url}/apps", timeout=30000)
                        page.wait_for_load_state("networkidle")
                        time.sleep(3) # 稍微多等一会儿确保实例列表加载出来
                    except Exception as e:
                        print(f"⚠️ [忽略] 跳转应用页失败 (可能无实例): {e}")

                    page.screenshot(path=screenshot_path)
                    print(f"🖼️ [截图] 已保存至: {screenshot_path}")

                    print(f"📤 [通知] 正在准备发送 Telegram 通知...")
                    title = f"{username}-自动检测区域: {region}"
                    content = f"🔗 <b>URL:</b> {base_url}\n🕒 <b>检测时间:</b> {time.strftime('%Y-%m-%d %H:%M:%S')}"
                    
                    notifier.send(title=title, content=content, image_path=screenshot_path)
                    print(f"✅ [通知] Telegram 消息发送指令已下达。")

                    # --- 💾 步骤 5: 更新本地状态和 Cookies ---
                    print(f"💾 [保存] 正在捕获并更新最新的 Session/Cookies...")
                    gh_cookies = context.cookies("https://github.com")
                    gh_val = next((c["value"] for c in gh_cookies if c["name"] == "user_session"), None)
                    if gh_val: 
                        all_gh_sessions[username] = gh_val
                        print(f"📝 [更新] GitHub Session 已缓存。")
                    
                    all_claw_cookies[username] = context.cookies(base_url)
                    print(f"🟢 [完成] {username} 任务全部执行成功。")
                else:
                    print(f"❌ [失败] {username} 未能成功登录 ClawCloud。")
                    notifier.send(title="登录失败", content=f"账号: {username} 无法进入控制台。")

                browser.close()

        except Exception as e:
            print(f"💥 [异常] 账号 {username} 运行过程中崩溃: {e}")
            notifier.send(title="异常提醒", content=f"账号: {username}\n错误: {str(e)[:150]}")
        finally:
            if gost_proc: 
                print(f"🛑 [代理] 正在关闭 Gost 进程...")
                gost_proc.terminate()
            if os.path.exists(screenshot_path): 
                os.remove(screenshot_path)
                print(f"🧹 [清理] 临时截图文件已删除。")

    # --- 📤 回写 Secrets ---
    print(f"\n{'='*50}\n📤 [同步] 正在将最新的 Sessions 回写至 Secrets...")
    gh_session_updater.update(json.dumps(all_gh_sessions))
    claw_cookies_updater.update(json.dumps(all_claw_cookies))
    print("🏁 [结束] 自动化流程执行完毕。")

if __name__ == "__main__":
    main()
