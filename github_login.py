#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import json
import time
import base64
import pyotp
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
gh_info = config.get_value("GH_INFO")  # 列表
notifier = TelegramNotifier(config)
secret = SecretUpdater(SESSION_SECRET_NAME, config_reader=config)

# ================== 读取 GH_SESSION 字典 ==================
sess_dict = {}
env_sess = os.getenv("GH_SESSION", "").strip()
if env_sess:
    try:
        sess_dict = json.loads(env_sess)
        print(f"ℹ️ 已读取 GH_SESSION 字典: {list(sess_dict.keys())}", flush=True)
    except Exception as e:
        print(f"⚠️ GH_SESSION 解析异常: {e}", flush=True)

# ================== 工具函数 ==================
def sep():
    print("=" * 60, flush=True)

def mask_user(username: str) -> str:
    if len(username) <= 2:
        return username
    return username[:2] + "***" + username[-1]

def save_screenshot(page, name):
    path = f"{name}.png"
    page.screenshot(path=path)
    return path

def update_secret():
    secret.update(json.dumps(sess_dict))
    print(f"✅ GH_SESSION 已更新: {list(sess_dict.keys())}", flush=True)

# ================== 主流程 ==================
def main():
    print("🔐 配置解密成功", flush=True)
    print(f"ℹ️ 读取 GH_INFO: {len(gh_info)} 个账号", flush=True)
    sep()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
        context = browser.new_context()
        page = context.new_page()

        for idx, account in enumerate(gh_info):
            username = account["username"]
            password = account["password"]
            totp_secret = account.get("2fasecret", "")

            print(f"👤 账号 {idx}: {mask_user(username)}", flush=True)

            # ================== 优先使用已有 session ==================
            user_session = sess_dict.get(username, "")
            cookies_ok = False

            if user_session:
                print("🍪 检测到已有 session，尝试注入 cookies", flush=True)
                context.add_cookies([
                    {"name": "user_session", "value": user_session, "domain": "github.com", "path": "/"},
                    {"name": "logged_in", "value": "yes", "domain": "github.com", "path": "/"}
                ])
                try:
                    page.goto(GITHUB_TEST_URL, timeout=30000)
                    page.wait_for_load_state("domcontentloaded", timeout=30000)
                    if "login" not in page.url:
                        print("✅ session 有效，跳过登录", flush=True)
                        cookies_ok = True
                    else:
                        print("⚠️ session 无效，需要重新登录", flush=True)
                except PlaywrightTimeoutError:
                    print("⚠️ session 校验超时，需要重新登录", flush=True)

            # ================== 登录流程 (修改部分开始) ==================
            if not cookies_ok:
                print("🔐 GitHub 登录", flush=True)
                try:
                    page.goto(GITHUB_LOGIN_URL, timeout=30000)
                    page.wait_for_selector('input[name="login"]')
                    page.fill('input[name="login"]', username)
                    page.fill('input[name="password"]', password)
                    # 使用 Enter 键提交，比直接点击按钮更稳定
                    page.keyboard.press("Enter")
                    
                    # 等待可能的 2FA 跳转或页面响应
                    time.sleep(5)
                    page.wait_for_load_state("networkidle", timeout=30000)
                except PlaywrightTimeoutError:
                    print(f"❌ {username} 登录操作响应超时", flush=True)
                    shot = save_screenshot(page, f"{username}_login_failed")
                    notifier.send("GitHub 登录失败", f"{username} 登录超时", shot)
                    continue

                # ================== 二次验证 (修改部分) ==================
                # 兼容多种可能的 2FA 选择器：input#app_totp, input#otp, input[name='otp']
                otp_selector = 'input#app_totp, input#otp, input[name="otp"]'
                if "two-factor" in page.url or page.query_selector(otp_selector):
                    print("🔑 检测到两步验证", flush=True)
                    try:
                        # 等待验证码输入框出现并聚焦
                        otp_input = page.wait_for_selector(otp_selector, timeout=15000)
                        if totp_secret:
                            # 移除密钥中的空格并生成最新 code
                            clean_secret = totp_secret.replace(" ", "")
                            code = pyotp.TOTP(clean_secret).now()
                            print(f"🔢 自动填入 2FA 验证码", flush=True)
                            
                            otp_input.focus()
                            otp_input.fill(code)
                            # 填入后通常会自动提交，保险起见补一个回车
                            page.keyboard.press("Enter")
                            
                            time.sleep(3)
                            page.wait_for_load_state("networkidle", timeout=30000)
                        else:
                            print("❌ 未配置 2FA 密钥", flush=True)
                            shot = save_screenshot(page, f"{username}_2fa_missing")
                            notifier.send("GitHub 登录失败", f"{username} 缺少 2FA 密钥", shot)
                            continue
                    except PlaywrightTimeoutError:
                        print(f"❌ {username} 2FA 输入框未能在规定时间内加载", flush=True)
                        shot = save_screenshot(page, f"{username}_2fa_timeout")
                        notifier.send("GitHub 登录失败", f"{username} 2FA 输入框未出现", shot)
                        continue
                # ================== 登录流程 (修改部分结束) ==================

                # 校验是否登录成功
                page.goto(GITHUB_TEST_URL, timeout=30000)
                page.wait_for_load_state("domcontentloaded", timeout=30000)
                if "login" in page.url:
                    print(f"❌ {username} 登录状态校验失败", flush=True)
                    shot = save_screenshot(page, f"{username}_login_verify_failed")
                    notifier.send("GitHub 登录失败", f"{username} 最终登录校验失败", shot)
                    continue

            # ================== 获取新的 session ==================
            new_session = None
            for c in context.cookies():
                if c["name"] == "user_session" and "github.com" in c["domain"]:
                    new_session = c["value"]
                    break

            if new_session:
                sess_dict[username] = new_session
                print(f"🟢 {username} 登录成功，session 已更新", flush=True)
            else:
                print(f"❌ {username} 未获取到新的 session", flush=True)
                shot = save_screenshot(page, f"{username}_session_failed")
                notifier.send("GitHub session 获取失败", f"{username} 未获取到 session", shot)

        # ================== 全部账号处理完成 ==================
        update_secret()
        browser.close()
        print("🟢 所有账号处理完成", flush=True)

# ================== 入口 ==================
if __name__ == "__main__":
    main()
