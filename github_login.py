#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import time
import json
import base64
import pyotp
from pathlib import Path
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

from engine.main import ConfigReader, SecretUpdater
from engine.notify import TelegramNotifier

# =========================
# 初始化配置
# =========================
config = ConfigReader()
gh_info = config.get_value("GH_INFO")       # 列表，每个元素包含 username/password/2fasecret/repotoken
secret = SecretUpdater("GT_SESSION", config_reader=config)

tg_notifier = TelegramNotifier(config)

REPO = os.getenv("GITHUB_REPOSITORY")

# =========================
# 工具函数
# =========================
def sep():
    print("=" * 60, flush=True)

def mask_email(email: str) -> str:
    if "@" not in email:
        return email
    name, domain = email.split("@", 1)
    return f"{name[:3]}***{name[-2:]}@{domain}"

def save_screenshot(page, name):
    path = f"{name}.png"
    page.screenshot(path=path)
    return path

def update_session_secret(session_dict):
    """将按 username 保存的 session 字典上传到 GitHub Secret"""
    json_str = json.dumps(session_dict)
    secret.update(json_str)
    print("✅ GT_SESSION 更新完成", flush=True)

def fill_2fa(page, totp_secret, retries=5, interval=2):
    """安全等待并填充 GitHub 2FA 页面"""
    selector = 'input#app_totp'
    for attempt in range(1, retries + 1):
        print(f"[2FA] 第 {attempt}/{retries} 次等待 2FA 输入框...", flush=True)
        try:
            page.wait_for_selector(selector, timeout=5000)
            locator = page.locator(selector)
            count = locator.count()
            print(f"[2FA] 找到 {count} 个输入框", flush=True)
            if count > 0 and locator.is_enabled():
                code = pyotp.TOTP(totp_secret).now()
                print(f"[2FA] 填充 TOTP 码: {code}", flush=True)
                locator.fill(code)
                page.keyboard.press("Enter")
                time.sleep(2)
                page.wait_for_load_state("networkidle", timeout=15000)
                print("[2FA] 成功填充并提交", flush=True)
                return True
        except PWTimeout:
            print(f"[2FA] 第 {attempt} 次等待超时，{interval} 秒后重试...", flush=True)
            time.sleep(interval)
        except Exception as e:
            print(f"[2FA] 第 {attempt} 次异常: {e}", flush=True)
            time.sleep(interval)
    print("[2FA] 最终失败，未能填充 TOTP", flush=True)
    return False

# =========================
# 主流程
# =========================
def main():
    session_dict = {}  # 按 username 存储 session

    print(f"🔐 读取账号数: {len(gh_info)}", flush=True)
    sep()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
        context = browser.new_context()
        page = context.new_page()

        for idx, account in enumerate(gh_info):
            username = account.get("username")
            password = account.get("password")
            totp_secret = account.get("2fasecret")
            repotoken = account.get("repotoken")

            masked = mask_email(username)
            print(f"👤 账号 {idx}: {masked}", flush=True)

            try:
                # -----------------------
                # 注入已有 session（如果存在）
                # -----------------------
                old_sessions = secret.get_value()  # 返回 JSON 字符串
                user_session = ""
                if old_sessions:
                    try:
                        sess_dict = json.loads(old_sessions)
                        user_session = sess_dict.get(username, "")
                        if user_session:
                            print(f"🍪 检测到已保存 session，尝试注入 cookies", flush=True)
                            context.add_cookies([
                                {"name": "user_session", "value": user_session, "domain": "github.com", "path": "/"},
                                {"name": "logged_in", "value": "yes", "domain": "github.com", "path": "/"}
                            ])
                            page.goto("https://github.com/settings/profile", timeout=30000)
                            page.wait_for_load_state("domcontentloaded", timeout=30000)
                            if "login" not in page.url:
                                print("✅ session 有效，跳过登录", flush=True)
                                session_dict[username] = user_session
                                continue
                            else:
                                print("⚠️ session 失效，需重新登录", flush=True)
                    except Exception as e:
                        print(f"⚠️ session 解析异常: {e}", flush=True)

                # -----------------------
                # 登录流程
                # -----------------------
                print("🌐 打开 GitHub 登录页", flush=True)
                page.goto("https://github.com/login", timeout=30000)
                page.wait_for_load_state("domcontentloaded", timeout=30000)

                print("⌨️ 输入用户名和密码", flush=True)
                page.fill('input[name="login"]', username)
                page.fill('input[name="password"]', password)
                page.click('input[type="submit"]')
                time.sleep(3)
                page.wait_for_load_state("networkidle", timeout=30000)

                # -----------------------
                # 2FA
                # -----------------------
                if "two-factor" in page.url or page.locator('input#app_totp').count() > 0:
                    print("🔑 检测到两步验证")
                    if not totp_secret:
                        raise RuntimeError("❌ 缺少 2FA 密钥")
                    ok = fill_2fa(page, totp_secret)
                    if not ok:
                        shot = save_screenshot(page, f"{username}_2fa_failed")
                        tg_notifier.send(f"❌ GitHub 登录失败: {masked}", "2FA 输入框未出现或超时", shot)
                        raise RuntimeError("2FA 输入框超时或未出现")

                # -----------------------
                # 登录成功后获取 session
                # -----------------------
                new_session = None
                for c in context.cookies():
                    if c["name"] == "user_session" and "github.com" in c["domain"]:
                        new_session = c["value"]
                        break
                if not new_session:
                    shot = save_screenshot(page, f"{username}_session_failed")
                    tg_notifier.send(f"❌ GitHub 登录失败: {masked}", "未获取到 session", shot)
                    raise RuntimeError("未获取到新的 user_session")

                print(f"✅ {masked} 登录成功，更新 session", flush=True)
                session_dict[username] = new_session

            except Exception as e:
                print(f"❌ 账号失败但继续下一个: {e}", flush=True)
                sep()
                continue

        # -----------------------
        # 上传 Secret
        # -----------------------
        if session_dict:
            update_session_secret(session_dict)

        browser.close()
        print("🟢 所有账号处理完成", flush=True)

# =========================
# 入口
# =========================
if __name__ == "__main__":
    main()
