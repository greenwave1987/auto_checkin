#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import json
import time
import base64
import pyotp
from playwright.sync_api import sync_playwright
from engine.notify import send_notify
from engine.main import SecretUpdater, ConfigReader

# ================== 基础配置 ==================
GITHUB_LOGIN_URL = "https://github.com/login"
GITHUB_TEST_URL = "https://github.com/settings/profile"
SESSION_SECRET_NAME = "GH_SESSION"

# ================== 初始化 ==================
config = ConfigReader()
gh_info = config.get_value("GH_INFO")  # 列表
secret = SecretUpdater(SESSION_SECRET_NAME, config_reader=config)

# GH_SESSION 字典从环境变量获取
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

            masked = mask_user(username)
            print(f"👤 账号 {idx}: {masked}", flush=True)

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
                except Exception:
                    print("⚠️ session 校验超时，需要重新登录", flush=True)

            # ================== 登录流程（只填用户名密码） ==================
            if not cookies_ok:
                print("🔐 GitHub 登录", flush=True)
                try:
                    page.goto(GITHUB_LOGIN_URL, timeout=30000)
                    page.wait_for_load_state("domcontentloaded", timeout=30000)

                    page.fill('input[name="login"]', username)
                    page.fill('input[name="password"]', password)
                    page.click('input[type="submit"]')
                    time.sleep(3)
                    page.wait_for_load_state("networkidle", timeout=30000)
                except Exception as e:
                    print(f"❌ 登录失败: {e}", flush=True)
                    shot = save_screenshot(page, f"{username}_login_failed")
                    send_notify("❌ GitHub 登录失败", f"{masked} 登录页面加载失败", shot)
                    continue

                # ================== 二次验证（严格按单账号脚本） ==================
                if
