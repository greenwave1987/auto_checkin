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

# 导入原有类
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)
try:
    from engine.main import ConfigReader, SecretUpdater
except ImportError:
    pass

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
        self.gost_proc = None

    def log(self, msg, level="INFO"):
        icons = {"INFO": "ℹ️", "SUCCESS": "✅", "ERROR": "❌", "WARN": "⚠️", "STEP": "🔹"}
        print(f"{icons.get(level, '•')} {msg}")
        self.logs.append(msg)

    def shot(self, page, name):
        self.n += 1
        path = f"shot_{self.n}_{name}.png"
        try:
            page.screenshot(path=path)
            return path
        except: return None

    # ==================== Gost 隧道与代理测试 ====================
    def setup_proxy(self):
        """启动 Gost 隧道并测试"""
        if not self.proxy_list:
            self.log("未配置代理，尝试直连", "WARN")
            return None
        
        p = self.proxy_list[0]
        proxy_str = f"{p.get('username')}:{p.get('password')}@{p.get('server')}:{p.get('port')}"
        local_proxy = "http://127.0.0.1:8080"

        try:
            self.log(f"步骤 0: 启动 Gost 隧道 (转发至 {p.get('server')})", "STEP")
            # 确保 gost 文件有执行权限
            if os.path.exists("./gost"):
                os.chmod("./gost", 0o755)
            
            self.gost_proc = subprocess.Popen(
                ["./gost", "-L=:8080", f"-F=socks5://{proxy_str}"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
            time.sleep(5)

            # 测试隧道
            res = requests.get("https://api.ipify.org", 
                               proxies={"http": local_proxy, "https": local_proxy}, 
                               timeout=15)
            self.log(f"✅ 隧道就绪，出口 IP: {res.text.strip()}", "SUCCESS")
            return local_proxy
        except Exception as e:
            self.log(f"隧道启动失败: {e}", "ERROR")
            if self.gost_proc:
                self.gost_proc.terminate()
            return None

    # ==================== 辅助函数 ====================
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
        self.session_updater.update(value)
        self.log("GitHub Session 已回写更新", "SUCCESS")

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

    def login_github(self, page, context):
        try:
            page.fill('input[name="login"]', self.gh_info.get("username", ""))
            page.fill('input[name="password"]', self.gh_info.get("password", ""))
            page.click('input[type="submit"]')
            time.sleep(5)

            if "device-verification" in page.url or "verified-device" in page.url:
                self.log("需手机批准登录...", "WARN")
                time.sleep(DEVICE_VERIFY_WAIT)

            if "two-factor" in page.url:
                totp_secret = self.gh_info.get("2fasecret")
                if totp_secret:
                    code = pyotp.TOTP(totp_secret.replace(" ", "")).now()
                    page.fill('input[id="app_totp"], input[name="otp"]', code)
                    page.keyboard.press("Enter")
                    time.sleep(5)
            return "github.com" not in page.url or "authorize" in page.url
        except: return False

    # ==================== 主流程 ====================
    def run(self):
        # 1. 设置代理隧道
        proxy_url = self.setup_proxy()
        
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=['--no-sandbox', '--disable-dev-shm-usage'],
                proxy={"server": proxy_url} if proxy_url else None
            )
            context = browser.new_context(user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36")
            
            if self.gh_session:
                context.add_cookies([{'name': 'user_session', 'value': self.gh_session, 'domain': 'github.com', 'path': '/'}])

            page = context.new_page()

            try:
                # 步骤1: 访问 ClawCloud
                self.log("步骤1: 打开 ClawCloud 登录页", "STEP")
                page.goto(SIGNIN_URL, timeout=60000)
                page.wait_for_load_state('networkidle', timeout=30000)
                time.sleep(2)
                self.shot(page, "clawcloud")
                
                current_url = page.url
                if 'signin' not in current_url.lower() and 'claw.cloud' in current_url:
                    self.log("已自动登录成功！", "SUCCESS")
                    self.detect_region(current_url)
                    new = self.get_session(context)
                    if new: self.save_cookie(new)
                    self.notify(True)
                    return

                # 步骤2: 点击 GitHub
                self.log("步骤2: 点击 GitHub", "STEP")
                if not self.click(page, ['button:has-text("GitHub")', '[data-provider="github"]'], "GitHub"):
                    self.log("找不到 GitHub 按钮", "ERROR")
                    self.notify(False, "找不到按钮")
                    return
                
                time.sleep(3)
                page.wait_for_load_state('networkidle', timeout=60000)

                # 步骤3: GitHub 认证
                self.log("步骤3: GitHub 认证", "STEP")
                url = page.url
                if 'github.com/login' in url:
                    if not self.login_github(page, context):
                        self.notify(False, "GitHub 登录失败")
                        return
                elif 'github.com/login/oauth/authorize' in url:
                    page.click('button[name="authorize"]')
                
                # 步骤4: 等待重定向
                self.log("步骤4: 等待重定向", "STEP")
                page.wait_for_url(re.compile(r".*claw\.cloud.*"), timeout=60000)
                
                # 步骤5: 验证
                self.log("步骤5: 验证", "STEP")
                if 'claw.cloud' in page.url and 'signin' not in page.url.lower():
                    self.detect_region(page.url)
                    # 6. 保活并保存新 Cookie
                    page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                    new_s = self.get_session(context)
                    if new_s: self.save_cookie(new_s)
                    self.notify(True)
                    self.log("全部流程执行完毕", "SUCCESS")

            except Exception as e:
                self.log(f"异常: {e}", "ERROR")
                self.notify(False, str(e))
            finally:
                browser.close()
                if self.gost_proc:
                    self.gost_proc.terminate()

if __name__ == "__main__":
    ClawAutoLogin().run()
