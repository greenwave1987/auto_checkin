#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import sys
import time
import re
import requests
import pyotp
import subprocess
from urllib.parse import urlparse
from playwright.sync_api import sync_playwright

# 策略配置
USE_PROXY = True
SIGNIN_URL = "https://console.run.claw.cloud/signin"
STATUS_FAIL = "FAIL"

# 导入原有类 (获取参数方式严禁改动)
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)
try:
    from engine.main import ConfigReader, SecretUpdater
except ImportError:
    pass

class ClawAutoLogin:
    def __init__(self):
        # --- 保持基准脚本获取参数方式 ---
        self.config = ConfigReader()
        self.accounts = self.config.get_value("GH_INFO") or []
        
        raw_proxy = self.config.get_value("PROXY_INFO")
        if isinstance(raw_proxy, dict) and "value" in raw_proxy:
            self.proxy_list = raw_proxy["value"]
        else:
            self.proxy_list = raw_proxy if isinstance(raw_proxy, list) else []

        self.bot_info = (self.config.get_value("BOT_INFO") or [{}])[0]
        self.tg_token = self.bot_info.get("token")
        self.tg_chat_id = self.bot_info.get("id")

        self.session_updater = SecretUpdater("GH_SESSION", config_reader=self.config)
        self.gost_proc = None
        self.detected_region = None

    def log(self, msg, level="INFO"):
        icons = {"INFO": "ℹ️", "SUCCESS": "✅", "ERROR": "❌", "STEP": "🔹", "WARN": "⚠️", "BLOCK": "🚫"}
        print(f"{icons.get(level, '•')} {msg}")

    def shot(self, page, name):
        f = f"screenshot_{name}_{int(time.time())}.png"
        try:
            page.screenshot(path=f)
            return f
        except: return None

    # --- 保持基准脚本代理逻辑 ---
    def stop_gost(self):
        if self.gost_proc:
            try:
                self.gost_proc.terminate()
                self.gost_proc = None
            except: pass

    def start_gost(self, proxy_data):
        if not USE_PROXY or not proxy_data: return None
        p_str = f"{proxy_data.get('username')}:{proxy_data.get('password')}@{proxy_data.get('server')}:{proxy_data.get('port')}"
        local_proxy = "http://127.0.0.1:8080"
        try:
            if os.path.exists("./gost"): os.chmod("./gost", 0o755)
            self.gost_proc = subprocess.Popen(
                ["./gost", "-L=:8080", f"-F=socks5://{p_str}"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
            time.sleep(5)
            return local_proxy
        except:
            self.stop_gost()
            return None

    # --- 辅助业务逻辑 ---
    def detect_region(self, url):
        parsed = urlparse(url)
        host = parsed.netloc
        if host.endswith('.console.claw.cloud'):
            self.detected_region = host.split('.')[0]
            self.log(f"检测到区域: {self.detected_region}", "SUCCESS")

    def click(self, page, selectors, desc=""):
        for s in selectors:
            try:
                el = page.locator(s).first
                if el.is_visible(timeout=5000):
                    el.click()
                    return True
            except: continue
        return False

    def login_github(self, page, context, account):
        """GitHub 登录逻辑"""
        try:
            page.fill('input[name="login"]', account.get("username"))
            page.fill('input[name="password"]', account.get("password", ""))
            page.click('input[type="submit"]')
            time.sleep(5)
            if "two-factor" in page.url:
                totp = pyotp.TOTP(account.get("2fasecret", "").replace(" ", "")).now()
                page.fill('input[id="app_totp"], input[name="otp"]', totp)
                page.keyboard.press("Enter")
                time.sleep(8)
            return True
        except: return False

    def oauth(self, page):
        try: page.click('button[name="authorize"]', timeout=10000)
        except: pass

    def wait_redirect(self, page):
        try:
            page.wait_for_url(re.compile(r".*claw\.cloud.*"), timeout=60000)
            return True
        except: return False

    def keepalive(self, page):
        self.log("正在执行保活操作...", "STEP")
        # 模拟点击或访问控制面板
        page.goto("https://console.run.claw.cloud/dashboard", timeout=30000)
        time.sleep(3)

    def process_account(self, idx, account):
        username = account.get("username")
        self.log(f"--- 处理账号: {username} ---", "STEP")
        
        current_proxy_data = self.proxy_list[idx] if idx < len(self.proxy_list) else None
        local_proxy = self.start_gost(current_proxy_data)

        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=['--no-sandbox', '--disable-blink-features=AutomationControlled'],
                proxy={"server": local_proxy} if local_proxy else None
            )
            context = browser.new_context(viewport={'width': 1920, 'height': 1080})
            page = context.new_page()

            try:
                # ====== 1. 访问 ClawCloud 登录入口 ======
                self.log("步骤1: 打开 ClawCloud 登录页", "STEP")
                page.goto(SIGNIN_URL, timeout=60000)
                page.wait_for_load_state('networkidle', timeout=30000)
                time.sleep(2)
                self.shot(page, "clawcloud")
                
                current_url = page.url
                self.log(f"当前 URL: {current_url}")
                
                if 'signin' not in current_url.lower() and 'claw.cloud' in current_url:
                    self.log("已登录！", "SUCCESS")
                    self.detect_region(current_url)
                    self.keepalive(page)
                    # 提取并保存新 Cookie (仅首个账号更新)
                    if idx == 0:
                        new_s = next((c['value'] for c in context.cookies() if c['name'] == 'user_session'), None)
                        if new_s: self.session_updater.update(new_s)
                    return

                # ====== 2. 点击 GitHub ======
                self.log("步骤2: 点击 GitHub", "STEP")
                if not self.click(page, [
                    'button:has-text("GitHub")',
                    'a:has-text("GitHub")',
                    '[data-provider="github"]'
                ], "GitHub"):
                    self.log("找不到按钮", "ERROR")
                    return
                
                time.sleep(3)
                page.wait_for_load_state('networkidle', timeout=60000)
                self.shot(page, "clicked_github")
                
                url = page.url
                self.log(f"当前: {url}")

                # ====== 3. GitHub 认证 ======
                self.log("步骤3: GitHub 认证", "STEP")
                if 'github.com/login' in url or 'github.com/session' in url:
                    if not self.login_github(page, context, account):
                        self.shot(page, "login_fail")
                        return
                elif 'github.com/login/oauth/authorize' in url:
                    self.log("Cookie 有效", "SUCCESS")
                    self.oauth(page)

                # ====== 4. 等待重定向 ======
                self.log("步骤4: 等待重定向", "STEP")
                if not self.wait_redirect(page):
                    self.shot(page, "redirect_fail")
                    return
                self.shot(page, "redirect_success")

                # ====== 5. 验证 ======
                self.log("步骤5: 验证", "STEP")
                current_url = page.url
                if 'claw.cloud' not in current_url or 'signin' in current_url.lower():
                    self.log("验证失败", "ERROR")
                    return
                
                if not self.detected_region:
                    self.detect_region(current_url)

                # ====== 6. 保活 ======
                self.keepalive(page)
                self.log(f"账号 {username} 任务完成", "SUCCESS")

                # 更新 Session (仅首个账号)
                if idx == 0:
                    new_s = next((c['value'] for c in context.cookies() if c['name'] == 'user_session'), None)
                    if new_s: self.session_updater.update(new_s)

            except Exception as e:
                self.log(f"异常: {e}", "ERROR")
            finally:
                browser.close()
                self.stop_gost()

    def run(self):
        for i, acc in enumerate(self.accounts):
            self.process_account(i, acc)
            time.sleep(5)

if __name__ == "__main__":
    ClawAutoLogin().run()
