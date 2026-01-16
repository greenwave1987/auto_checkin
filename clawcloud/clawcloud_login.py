#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import sys
import time
import re
import requests
import pyotp
from urllib.parse import urlparse
from playwright.sync_api import sync_playwright

# 导入原有类
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)
try:
    from engine.main import ConfigReader, SecretUpdater
except ImportError:
    pass

# ==================== 配置 ====================
LOGIN_ENTRY_URL = "https://console.run.claw.cloud"
SIGNIN_URL = f"{LOGIN_ENTRY_URL}/signin"
DEVICE_VERIFY_WAIT = 30  
TWO_FACTOR_WAIT = 120    

class ClawAutoLogin:
    def __init__(self):
        # --- 获取数据方式不改 ---
        self.config = ConfigReader()
        bot_info_list = self.config.get_value("BOT_INFO")
        self.bot_config = bot_info_list[0] if bot_info_list else {}
        
        gh_info_list = self.config.get_value("GH_INFO")
        self.gh_info = gh_info_list[0] if gh_info_list else {}
        self.proxy_list = self.config.get_value("PROXY_INFO") or []
        
        # --- 更新变量方式不改 ---
        self.session_updater = SecretUpdater("GH_SESSION", config_reader=self.config)
        self.gh_session = os.getenv("GH_SESSION", "").strip()
        
        self.logs = []
        self.n = 0
        self.detected_region = None

    # ==================== 辅助工具函数 ====================
    def log(self, msg, level="INFO"):
        print(f"[{level}] {msg}")
        self.logs.append(msg)

    def shot(self, page, name):
        self.n += 1
        path = f"shot_{self.n}_{name}.png"
        try:
            page.screenshot(path=path)
            return path
        except: return None

    def click(self, page, selectors, name):
        for s in selectors:
            try:
                el = page.locator(s).first
                if el.is_visible(timeout=5000):
                    el.click()
                    return True
            except: continue
        return False

    def detect_region(self, url):
        parsed = urlparse(url)
        if 'claw.cloud' in parsed.netloc:
            self.detected_region = parsed.netloc.split('.')[0]
            self.log(f"检测到区域: {self.detected_region}", "SUCCESS")

    def get_session(self, context):
        cookies = context.cookies()
        return next((c['value'] for c in cookies if c['name'] == 'user_session'), None)

    def save_cookie(self, value):
        # 调用 SecretUpdater 更新变量
        self.session_updater.update(value)
        self.log("Session 已更新回写", "SUCCESS")

    def notify(self, success, reason=""):
        msg = f"ClawCloud 登录{'成功' if success else '失败'}"
        if reason: msg += f": {reason}"
        if self.detected_region: msg += f"\n区域: {self.detected_region}"
        
        token = self.bot_config.get("token")
        chat_id = self.bot_config.get("id")
        if token and chat_id:
            try:
                requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                              data={"chat_id": chat_id, "text": msg})
            except: pass

    def oauth(self, page):
        self.log("正在处理 OAuth 授权...", "STEP")
        try:
            page.click('button[name="authorize"]', timeout=10000)
            time.sleep(5)
        except: pass

    def wait_redirect(self, page):
        try:
            page.wait_for_url(re.compile(r".*claw\.cloud.*"), timeout=60000)
            return True
        except: return False

    def keepalive(self, page):
        self.log("执行保活动作...", "STEP")
        try:
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            time.sleep(2)
        except: pass

    def pick_proxy(self):
        if not self.proxy_list: return None
        p = self.proxy_list[0]
        return f"http://{p.get('username')}:{p.get('password')}@{p.get('server')}:{p.get('port')}"

    # ==================== GitHub 登录核心 (包含2FA) ====================
    def login_github(self, page, context):
        try:
            page.fill('input[name="login"]', self.gh_info.get("username", ""))
            page.fill('input[name="password"]', self.gh_info.get("password", ""))
            page.click('input[type="submit"]')
            time.sleep(5)

            # 处理设备验证
            if "device-verification" in page.url or "verified-device" in page.url:
                self.log("需手机批准登录", "WARN")
                time.sleep(DEVICE_VERIFY_WAIT)

            # 处理 2FA
            if "two-factor" in page.url:
                totp_secret = self.gh_info.get("2fasecret")
                if totp_secret:
                    code = pyotp.TOTP(totp_secret.replace(" ", "")).now()
                    page.fill('input[id="app_totp"], input[name="otp"]', code)
                    page.keyboard.press("Enter")
                    time.sleep(5)
            
            return "github.com" not in page.url or "authorize" in page.url
        except:
            return False

    # ==================== 主流程 (登录部分严格按照要求) ====================
    def run(self):
        with sync_playwright() as p:
            proxy_url = self.pick_proxy()
            browser = p.chromium.launch(headless=True, proxy={"server": proxy_url} if proxy_url else None)
            context = browser.new_context(user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36")
            
            if self.gh_session:
                context.add_cookies([{'name': 'user_session', 'value': self.gh_session, 'domain': 'github.com', 'path': '/'}])

            page = context.new_page()

            try:
                # 1. 访问 ClawCloud 登录入口 (严格执行)
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
                    new = self.get_session(context)
                    if new:
                        self.save_cookie(new)
                    self.notify(True)
                    print("\n✅ 成功！\n")
                    return
                
                # 2. 点击 GitHub (严格执行)
                self.log("步骤2: 点击 GitHub", "STEP")
                if not self.click(page, [
                    'button:has-text("GitHub")',
                    'a:has-text("GitHub")',
                    '[data-provider="github"]'
                ], "GitHub"):
                    self.log("找不到按钮", "ERROR")
                    self.notify(False, "找不到 GitHub 按钮")
                    sys.exit(1)
                
                time.sleep(3)
                page.wait_for_load_state('networkidle', timeout=60000)
                self.shot(page, "点击后")
                
                url = page.url
                self.log(f"当前: {url}")
                
                # 3. GitHub 登录 (严格执行)
                self.log("步骤3: GitHub 认证", "STEP")
                if 'github.com/login' in url or 'github.com/session' in url:
                    if not self.login_github(page, context):
                        self.shot(page, "登录失败")
                        self.notify(False, "GitHub 登录失败")
                        sys.exit(1)
                elif 'github.com/login/oauth/authorize' in url:
                    self.log("Cookie 有效", "SUCCESS")
                    self.oauth(page)
                
                # 4. 等待重定向 (严格执行)
                self.log("步骤4: 等待重定向", "STEP")
                if not self.wait_redirect(page):
                    self.shot(page, "重定向失败")
                    self.notify(False, "重定向失败")
                    sys.exit(1)
                
                self.shot(page, "重定向成功")
                
                # 5. 验证 (严格执行)
                self.log("步骤5: 验证", "STEP")
                current_url = page.url
                if 'claw.cloud' not in current_url or 'signin' in current_url.lower():
                    self.notify(False, "验证失败")
                    sys.exit(1)
                
                if not self.detected_region:
                    self.detect_region(current_url)
                
                # 6. 保活 (严格执行)
                self.keepalive(page)
                
                # 最终 Session 提取
                new_s = self.get_session(context)
                if new_s: self.save_cookie(new_s)
                self.notify(True)

            except Exception as e:
                self.log(f"异常: {str(e)}", "ERROR")
                self.notify(False, str(e))
            finally:
                browser.close()

if __name__ == "__main__":
    ClawAutoLogin().run()
