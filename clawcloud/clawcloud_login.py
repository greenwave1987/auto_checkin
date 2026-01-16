#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import json
import time
import subprocess
import pyotp
import requests
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError
from engine.main import ConfigReader, SecretUpdater
from engine.notify import TelegramNotifier

# ================== 基础配置 ==================
GITHUB_LOGIN_URL = "https://github.com/login"
GITHUB_TEST_URL = "https://github.com/settings/profile"
SESSION_SECRET_NAME = "GH_SESSION"

# ================== 初始化 ==================
config = ConfigReader()
gh_info = config.get_value("GH_INFO")  # 账号列表
proxy_info = config.get_value("PROXY_INFO")  # 代理列表
notifier = TelegramNotifier(config)
secret_updater = SecretUpdater(SESSION_SECRET_NAME, config_reader=config)

# 读取已有 Session
env_sess = os.getenv("GH_SESSION", "").strip()
sess_dict = json.loads(env_sess) if env_sess else {}

def save_screenshot(page, name):
    path = f"{name}.png"
    page.screenshot(path=path)
    return path

# ================== 主流程 ==================
def main():
    print(f"🚀 开始处理 {len(gh_info)} 个 GitHub 账号", flush=True)

    # 使用 zip 确保账号和代理一一对应
    for idx, (account, proxy) in enumerate(zip(gh_info, proxy_info)):
        username = account["username"]
        password = account["password"]
        totp_secret = account.get("2fasecret", "")
        
        # 构造 Gost 代理字符串
        proxy_str = f"{proxy['username']}:{proxy['password']}@{proxy['server']}:{proxy['port']}"
        local_proxy = "http://127.0.0.1:8080"
        
        print(f"\n{'='*40}")
        print(f"👤 账号 [{idx}]: {username}")
        print(f"🌐 隧道: {proxy['server']}:{proxy['port']}")
        
        gost_proc = None
        try:
            # 1️⃣ 启动 Gost 隧道 (隔离第一步：物理链路隔离)
            gost_proc = subprocess.Popen(
                ["./gost", "-L=:8080", f"-F=socks5://{proxy_str}"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
            time.sleep(5) 

            # 2️⃣ 测试隧道 IP
            res = requests.get("https://api.ipify.org", proxies={"http": local_proxy, "https": local_proxy}, timeout=15)
            print(f"✅ 隧道就绪，出口 IP: {res.text.strip()}")

            # 3️⃣ 启动 Playwright (隔离第二步：环境指纹隔离)
            with sync_playwright() as p:
                browser = p.chromium.launch(
                    headless=True, 
                    args=["--no-sandbox", "--disable-blink-features=AutomationControlled"]
                )
                # 创建完全独立的上下文，并注入本地隧道代理
                context = browser.new_context(
                    proxy={"server": local_proxy},
                    user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                )
                page = context.new_page()
                page.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")

                # --- 尝试注入已有 Session ---
                user_session = sess_dict.get(username, "")
                login_needed = True

                if user_session:
                    print("🍪 注入已有 Session 测试...")
                    context.add_cookies([
                        {"name": "user_session", "value": user_session, "domain": "github.com", "path": "/"},
                        {"name": "logged_in", "value": "yes", "domain": "github.com", "path": "/"}
                    ])
                    page.goto(GITHUB_TEST_URL, timeout=30000)
                    if "login" not in page.url:
                        print("✅ Session 依然有效，跳过登录")
                        login_needed = False

                # --- 执行登录流程 ---
                if login_needed:
                    print("🔐 执行账号密码登录...")
                    page.goto(GITHUB_LOGIN_URL, timeout=30000)
                    page.fill('input[name="login"]', username)
                    page.fill('input[name="password"]', password)
                    page.keyboard.press("Enter")
                    
                    time.sleep(5)

                    # 处理 2FA
                    otp_selector = 'input#app_totp, input#otp, input[name="otp"]'
                    if "two-factor" in page.url or page.query_selector(otp_selector):
                        print("🔑 处理两步验证...")
                        if totp_secret:
                            code = pyotp.TOTP(totp_secret.replace(" ", "")).now()
                            page.wait_for_selector(otp_selector).fill(code)
                            page.keyboard.press("Enter")
                            time.sleep(5)
                        else:
                            raise Exception("缺失 2FA 密钥")

                    # 最终校验
                    page.goto(GITHUB_TEST_URL, timeout=30000)
                    if "login" in page.url:
                        raise Exception("登录校验失败，未能进入个人设置页")

                # 4️⃣ 提取并保存新 Session
                new_session = next((c["value"] for c in context.cookies() if c["name"] == "user_session"), None)
                if new_session:
                    sess_dict[username] = new_session
                    print(f"🟢 {username} 处理成功")
                
                browser.close()

        except Exception as e:
            print(f"❌ 账号 {username} 异常: {e}")
            # 异常时可以截图通知
            # shot = save_screenshot(page, f"err_{username}")
            # notifier.send("GitHub 异常", f"账号 {username}: {str(e)}", shot)
        
        finally:
            # 5️⃣ 彻底清理环境 (隔离第三步：资源释放)
            if gost_proc:
                gost_proc.terminate()
                gost_proc.wait()
            print(f"🧹 隧道已关闭，账号 {username} 处理完毕。")

    # 全部账号处理完后，一次性更新 Secret
    secret_updater.update(json.dumps(sess_dict))
    print("\n✨ 所有账号 Session 已同步至 GitHub Secrets。")

if __name__ == "__main__":
    main()
