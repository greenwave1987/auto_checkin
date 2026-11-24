#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import json
import time
import base64
import hashlib
import hmac
import struct
from pathlib import Path
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError
from engine.main import ConfigReader, SecretUpdater
from engine.notify import TelegramNotifier

# ================== 基础配置 ==================
GITHUB_LOGIN_URL = "https://github.com/login"
GITHUB_TEST_URL = "https://github.com/settings/profile"
SESSION_SECRET_NAME = "GH_SESSION"
WAIT_SECONDS = 30

# ================== 初始化 ==================
config = ConfigReader()
gh_info = config.get_value("GH_INFO")  
notifier = TelegramNotifier(config)
secret = SecretUpdater(SESSION_SECRET_NAME, config_reader=config)

sess_dict = {}
env_sess = os.getenv("GH_SESSION", "").strip()
if env_sess:
    try:
        sess_dict = json.loads(env_sess)
        print(f"ℹ️ 已读取 GH_SESSION 字典: {list(sess_dict.keys())}", flush=True)
    except Exception as e:
        print(f"⚠️ GH_SESSION 解析异常: {e}", flush=True)

def sep():
    print("=" * 60, flush=True)

def mask_user(username: str) -> str:
    if len(username) <= 2: return username
    return username[:2] + "***" + username[-1]

def save_screenshot(page, name):
    path = f"{name}.png"
    page.screenshot(path=path)
    return path

def update_secret():
    secret.update(json.dumps(sess_dict))
    print(f"✅ GH_SESSION 已更新: {list(sess_dict.keys())}", flush=True)

def generate_totp(secret_text: str) -> str:
    """
    生成 GitHub 默认格式的 TOTP 验证码：
    Base32 密钥、HMAC-SHA1、6 位数字、30 秒时间步长。
    不调用 PyOTP 的 112 bit 长度校验，因此兼容 GitHub 提供的 16 位旧格式密钥。
    """
    normalized = "".join(str(secret_text).split()).replace("-", "").upper().rstrip("=")
    if not normalized:
        raise ValueError("未配置两步验证密钥")
    padding = "=" * (-len(normalized) % 8)
    try:
        key = base64.b32decode(normalized + padding, casefold=True)
    except Exception as e:
        raise ValueError(f"两步验证密钥不是有效的 Base32 格式: {e}") from e
    if not key:
        raise ValueError("两步验证密钥解码后为空")
    counter = int(time.time() // 30)
    message = struct.pack(">Q", counter)
    digest = hmac.new(key, message, hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    binary = (
        ((digest[offset] & 0x7F) << 24)
        | ((digest[offset + 1] & 0xFF) << 16)
        | ((digest[offset + 2] & 0xFF) << 8)
        | (digest[offset + 3] & 0xFF)
    )
    return str(binary % 1_000_000).zfill(6)

# ================== 主流程 ==================
def main():
    print("🔐 配置解密成功", flush=True)
    print(f"ℹ️ 读取 GH_INFO: {len(gh_info)} 个账号", flush=True)
    sep()

    with sync_playwright() as p:
        # 启动浏览器时增加反爬参数
        browser = p.chromium.launch(
            headless=True, 
            args=["--no-sandbox", "--disable-blink-features=AutomationControlled"]
        )

        session_updated = False

        for idx, account in enumerate(gh_info):
            username = account["username"]
            password = account["password"]
            totp_secret = account.get("2fasecret", "")

            print(f"👤 准备处理账号 {idx}: {mask_user(username)}", flush=True)

            # --- ✨ 环境清理与隔离核心步骤 ---
            # 为每个账号创建完全独立的 Context，模拟不同的浏览器指纹特征
            context = browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
                viewport={'width': 1280, 'height': 720}
            )
            page = context.new_page()
            
            # 注入隔离脚本，防止检测 WebDriver
            page.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
            # -------------------------------

            # ================== 优先使用已有 session ==================
            user_session = sess_dict.get(username, "")
            cookies_ok = False

            if user_session:
                print(f"🍪 注入账号 {mask_user(username)} 的独立 Cookies", flush=True)
                context.add_cookies([
                    {"name": "user_session", "value": user_session, "domain": "github.com", "path": "/"},
                    {"name": "logged_in", "value": "yes", "domain": "github.com", "path": "/"}
                ])
                try:
                    page.goto(GITHUB_TEST_URL, timeout=30000)
                    if "login" not in page.url:
                        print("✅ Session 有效", flush=True)
                        cookies_ok = True
                except:
                    pass

            # ================== 登录流程 ==================
            if not cookies_ok:
                print("🔐 执行全新登录", flush=True)
                try:
                    page.goto(GITHUB_LOGIN_URL, timeout=30000)
                    page.fill('input[name="login"]', username)
                    page.fill('input[name="password"]', password)
                    page.keyboard.press("Enter")
                    
                    time.sleep(5) # 留出页面响应时间
                    
                    # --- 2FA 处理 ---
                    otp_selector = 'input#app_totp, input#otp, input[name="otp"]'
                    if "two-factor" in page.url or page.query_selector(otp_selector):
                        print("🔑 处理两步验证", flush=True)
                        otp_input = page.wait_for_selector(otp_selector, timeout=15000)
                        if totp_secret:
                            code = generate_totp(totp_secret)
                            print("🔢 已生成两步验证码，正在提交", flush=True)
                            otp_input.fill(code)
                            page.keyboard.press("Enter")
                            time.sleep(5)
                        else:
                            raise ValueError("账号触发两步验证，但 GH_INFO 未配置 2fasecret")
                    
                    # 校验
                    page.goto(GITHUB_TEST_URL, timeout=30000)
                    if "login" in page.url:
                        print(f"❌ {username} 登录失败", flush=True)
                        save_screenshot(page, f"{username}_failed")
                        context.close() # 失败也要关闭当前 Context
                        continue
                except Exception as e:
                    print(f"❌ 运行异常: {e}", flush=True)
                    context.close()
                    continue

            # ================== 提取并更新 Session ==================
            new_session = next((c["value"] for c in context.cookies() if c["name"] == "user_session"), None)
            if new_session:
                sess_dict[username] = new_session
                print(f"🟢 {username} 处理成功", flush=True)
            
            # --- ✨ 彻底清理：关闭上下文 ---
            # 这会销毁该账号所有的缓存、临时文件和内存中的 Cookie
            context.close() 
            print(f"🧹 环境已清理，准备下一个账号...", flush=True)
            sep()

        # 仅在至少一个账号成功获取 Session 时回写，避免全部登录失败仍提示更新成功
        if session_updated:
            update_secret()
        else:
            print("⚠️ 本次没有成功获取任何 Session，跳过 GH_SESSION 回写", flush=True)
        browser.close()

if __name__ == "__main__":
    main()
