#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import sys
import os
import json
import time
import subprocess
import pyotp
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
        if host.endswith('.console.claw.cloud'):
            region = host.replace('.console.claw.cloud', '')
            if region and region != 'console':
                return region, f"https://{host}"
        if "ap-northeast-1" in host:
            return "日本 (Tokyo)", f"https://{host}"
        return "主站/其他", f"{parsed.scheme}://{parsed.netloc}"
    except:
        return "未知", url

def wait_device_verification(page, username):
    """处理 GitHub 设备验证"""
    print(f"📡 [通知] 正在发送设备验证提醒到 TG...")
    msg = f"⚠️ <b>设备验证需确认</b>\n账号: <code>{username}</code>\n请检查邮箱或 GitHub App 批准登录。"
    notifier.send(title="GitHub 设备验证", content=msg)
    
    print(f"⏳ [等待] 需要设备验证，每10秒检查一次状态，总共等待 {DEVICE_VERIFY_WAIT} 秒...")
    for i in range(DEVICE_VERIFY_WAIT):
        time.sleep(1)
        if "verified-device" not in page.url and "device-verification" not in page.url:
            print("✅ [验证] 设备验证已通过！")
            return True
        if (i + 1) % 10 == 0:
            print(f"🔄 [重试] 等待中，尝试刷新检测状态...")
            try: page.reload()
            except: pass
    return False

# ================== 核心逻辑 ==================

def main():
    if not gh_info:
        print("❌ [错误] 未获取到账号信息")
        return

    print(f"🚀 [启动] 处理 {len(gh_info)} 个账号")

    for idx, (account, proxy) in enumerate(zip(gh_info, proxy_info)):
        username = account["username"]
        password = account["password"]
        totp_secret = account.get("2fasecret", "")
        
        proxy_str = f"{proxy['username']}:{proxy['password']}@{proxy['server']}:{proxy['port']}"
        local_proxy = "http://127.0.0.1:8080"
        
        print(f"\n{'='*20} 👤 ({idx+1}/{len(gh_info)}) {username} {'='*20}")
        
        gost_proc = None
        screenshot_path = f"screenshot_{username}.png"

        try:
            # 1. 代理启动
            print(f"🔌 [代理] 启动隧道: {proxy['server']}...")
            gost_proc = subprocess.Popen(["./gost", "-L=:8080", f"-F=socks5://{proxy_str}"], 
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            time.sleep(5)

            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
                context = browser.new_context(proxy={"server": local_proxy}, viewport={'width': 1280, 'height': 800})
                page = context.new_page()

                is_logged_in = False

                # --- 步骤 1: 注入 Claw Cookies ---
                user_claw_cookies = all_claw_cookies.get(username)
                if user_claw_cookies:
                    print(f"🧪 [Cookie] 注入现有 Claw 缓存...")
                    context.add_cookies(user_claw_cookies)
                    page.goto("https://console.run.claw.cloud/", timeout=45000)
                    page.wait_for_load_state("networkidle")
                    if "signin" not in page.url:
                        print(f"✅ [成功] 缓存登录有效")
                        is_logged_in = True

                # --- 步骤 2: 执行登录流程 ---
                if not is_logged_in:
                    print(f"🔑 [登录] 准备执行 GitHub 登录...")
                    user_gh_session = all_gh_sessions.get(username)
                    page.goto(CLAW_LOGIN_ENTRY)
                    
                    if user_gh_session:
                        print(f"🧪 [Cookie] 注入 GitHub Session...")
                        context.add_cookies([
                            {"name": "user_session", "value": user_gh_session, "domain": ".github.com", "path": "/"},
                            {"name": "logged_in", "value": "yes", "domain": ".github.com", "path": "/"}
                        ])
                    
                    page.click('button:has-text("GitHub"), [data-provider="github"]')
                    time.sleep(5)

                    if "github.com/login" in page.url:
                        print(f"⌨️ [表单] 输入账号密码...")
                        page.fill('input[name="login"]', username)
                        page.fill('input[name="password"]', password)
                        page.keyboard.press("Enter")
                        time.sleep(5)

                        if "device-verification" in page.url:
                            wait_device_verification(page, username)

                        if "two-factor" in page.url:
                            print(f"🔢 [2FA] 输入验证码...")
                            code = pyotp.TOTP(totp_secret.replace(" ", "")).now()
                            page.locator('input#app_totp, input#otp, input[name="otp"]').first.fill(code)
                            page.keyboard.press("Enter")
                            page.wait_for_url("**/claw.cloud/**", timeout=60000)
                        
                    is_logged_in = "claw.cloud" in page.url and "signin" not in page.url

                # --- 步骤 3: 目标页面跳转与二次登录检测 ---
                if is_logged_in:
                    print(f"🚀 [跳转] 访问目标区域: {TARGET_REGION_URL}")
                    try:
                        page.goto(TARGET_REGION_URL, timeout=30000)
                        page.wait_for_load_state("networkidle")

                        # 检测是否掉回登录页
                        if "signin" in page.url or "login" in page.url:
                            print(f"⚠️ [检测] 区域跳转后会话失效，尝试二次登录...")
                            page.click('button:has-text("GitHub"), [data-provider="github"]')
                            time.sleep(5)
                            
                            # 再次检测 GH Session 是否依然有效
                            if "github.com/login" in page.url:
                                print("❌ [失败] GitHub Session 彻底失效，无法二次登录")
                            else:
                                print("✅ [重连] 二次登录成功")
                                page.wait_for_load_state("networkidle")
                    except Exception as e:
                        print(f"⚠️ [异常] 跳转过程出错: {e}")

                    # 稳定等待
                    time.sleep(10)
                    region, current_url = detect_region(page.url)

                    # --- 步骤 4: 截图与通知 ---
                    print(f"📸 [截图] 捕获当前页面...")
                    page.screenshot(path=screenshot_path)
                    
                    title = f"{username}-自动检测区域: {region}"
                    content = f"🔗 <b>URL:</b> {page.url}\n🕒 <b>时间:</b> {time.strftime('%Y-%m-%d %H:%M:%S')}"
                    notifier.send(title=title, content=content, image_path=screenshot_path)

                    # --- 步骤 5: 更新并回写 Cookies ---
                    print(f"💾 [保存] 更新最新的 Session/Cookies...")
                    
                    # 更新 GitHub Session
                    gh_cookies = context.cookies("https://github.com")
                    gh_val = next((c["value"] for c in gh_cookies if c["name"] == "user_session"), None)
                    if gh_val: all_gh_sessions[username] = gh_val
                    
                    # 更新 Claw Cookies (使用当前所在页面的域)
                    all_claw_cookies[username] = context.cookies(page.url)
                    
                    print(f"🟢 [完成] {username} 成功")
                else:
                    print(f"❌ [失败] {username} 登录未成功")
                    notifier.send(title="登录失败", content=f"账号: {username} 未进入控制台")

                browser.close()

        except Exception as e:
            print(f"💥 [崩溃] {username}: {e}")
            notifier.send(title="异常提醒", content=f"账号: {username}\n错误: {str(e)[:100]}")
        finally:
            if gost_proc: gost_proc.terminate()
            if os.path.exists(screenshot_path): os.remove(screenshot_path)

    # 回写 Secrets
    print("\n📤 [同步] 回写 Secrets...")
    gh_session_updater.update(json.dumps(all_gh_sessions))
    claw_cookies_updater.update(json.dumps(all_claw_cookies))
    print("🏁 任务结束")

if __name__ == "__main__":
    main()
