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
        # --- 获取数据方式 ---
        self.config = ConfigReader()
        bot_info_list = self.config.get_value("BOT_INFO")
        self.bot_config = bot_info_list[0] if bot_info_list else {}
        
        gh_info_list = self.config.get_value("GH_INFO")
        self.gh_info = gh_info_list[0] if gh_info_list else {}
        self.proxy_list = self.config.get_value("PROXY_INFO") or []
        
        # --- 更新变量方式 ---
        self.session_updater = SecretUpdater("GH_SESSION", config_reader=self.config)
        self.gh_session = os.getenv("GH_SESSION", "").strip()
        
        self.logs = []
        self.n = 0
        self.detected_region = None
        self.gost_proc = None

    def log(self, msg, level="INFO"):
        icons = {"INFO": "ℹ️", "SUCCESS": "✅", "ERROR": "❌", "WARN": "⚠️", "STEP": "🔹"}
        line = f"{icons.get(level, '•')} {msg}"
        print(line)
        self.logs.append(line)

    def shot(self, page, name):
        self.n += 1
        path = f"shot_{self.n}_{name}.png"
        try:
            page.screenshot(path=path)
            return path
        except: return None

    # ==================== Gost 代理核心逻辑 ====================
    def setup_proxy(self):
        """严格按照你提供的启动和测试 Gost 隧道逻辑"""
        if not self.proxy_list:
            self.log("未检测到代理配置，跳过代理步骤", "WARN")
            return None
        
        p = self.proxy_list[0]
        # 构造 Gost 需要的 Socks5 认证字符串
        proxy_str = f"{p.get('username')}:{p.get('password')}@{p.get('server')}:{p.get('port')}"
        local_proxy = "http://127.0.0.1:8080"

        self.log(f"步骤 0: 启动 Gost 隧道转发 -> {p.get('server')}", "STEP")
        
        try:
            # 1️⃣ 启动 Gost 隧道
            if os.path.exists("./gost"):
                os.chmod("./gost", 0o755)
            
            self.gost_proc = subprocess.Popen(
                ["./gost", "-L=:8080", f"-F=socks5://{proxy_str}"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
            
            # 关键：等待 Gost 进程就绪
            time.sleep(5)

            # 2️⃣ 测试隧道是否可用
            res = requests.get("https://api.ipify.org", 
                               proxies={"http": local_proxy, "https": local_proxy}, 
                               timeout=15)
            self.log(f"✅ 隧道就绪，出口 IP: {res.text.strip()}", "SUCCESS")
            return local_proxy
        except Exception as e:
            self.log(f"❌ 隧道测试失败: {str(e)}", "ERROR")
            if self.gost_proc:
                self.gost_proc.terminate()
            return None

    # ==================== 登录辅助函数 ====================
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
            self.log(f"当前区域控制台: {self.detected_region}", "SUCCESS")

    def get_session(self, context):
        cookies = context.cookies()
        return next((c['value'] for c in cookies if c['name'] == 'user_session'), None)

    def save_cookie(self, value):
        self.session_updater.update(value)
        self.log("GitHub Session 已回写更新", "SUCCESS")

    def login_github(self, page, context):
        try:
            page.fill('input[name="login"]', self.gh_info.get("username", ""))
            page.fill('input[name="password"]', self.gh_info.get("password", ""))
            page.click('input[type="submit"]')
            time.sleep(5)
            
            if "device-verification" in page.url:
                self.log("需手机批准 (30s)...", "WARN")
                time.sleep(30)
            
            if "two-factor" in page.url:
                totp_secret = self.gh_info.get("2fasecret")
                if totp_secret:
                    code = pyotp.TOTP(totp_secret.replace(" ", "")).now()
                    self.log(f"填入验证码: {code}", "SUCCESS")
                    page.fill('input[id="app_totp"], input[name="otp"]', code)
                    page.keyboard.press("Enter")
                    time.sleep(5)
            return True
        except: return False

    # ==================== 主流程 ====================
    def run(self):
        # !!! 确保在启动浏览器前调用 setup_proxy !!!
        local_proxy_url = self.setup_proxy()
        
        with sync_playwright() as p:
            # 只有隧道测试成功，这里才会带上代理
            browser = p.chromium.launch(
                headless=True,
                args=['--no-sandbox', '--disable-dev-shm-usage'],
                proxy={"server": local_proxy_url} if local_proxy_url else None
            )
            
            context = browser.new_context(
                viewport={'width': 1280, 'height': 800},
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            )
            
            # 注入旧 Cookie
            if self.gh_session:
                context.add_cookies([{'name': 'user_session', 'value': self.gh_session, 'domain': 'github.com', 'path': '/'}])

            page = context.new_page()

            try:
                # 1. 访问 ClawCloud
                self.log("步骤1: 打开 ClawCloud 登录页", "STEP")
                page.goto("https://console.run.claw.cloud/signin", timeout=60000)
                page.wait_for_load_state('networkidle', timeout=30000)
                time.sleep(2)
                self.shot(page, "clawcloud")
                
                # 检查是否已登录
                current_url = page.url
                if 'signin' not in current_url.lower() and 'claw.cloud' in current_url:
                    self.log("检测到已登录状态", "SUCCESS")
                    self.detect_region(current_url)
                else:
                    # 2. 点击 GitHub
                    self.log("步骤2: 点击 GitHub 登录按钮", "STEP")
                    self.click(page, ['button:has-text("GitHub")', '[data-provider="github"]'], "GitHub")
                    time.sleep(5)
                    
                    # 3. GitHub 认证
                    if 'github.com/login' in page.url:
                        self.log("步骤3: 执行 GitHub 登录流程", "STEP")
                        self.login_github(page, context)
                    elif 'github.com/login/oauth/authorize' in page.url:
                        self.log("步骤3: 执行 OAuth 授权", "STEP")
                        page.click('button[name="authorize"]')
                
                # 4. 等待并验证
                self.log("步骤4: 等待最终重定向", "STEP")
                page.wait_for_url(re.compile(r".*claw\.cloud.*"), timeout=60000)
                
                if 'claw.cloud' in page.url and 'signin' not in page.url.lower():
                    self.log("登录成功，正在更新 Session...", "SUCCESS")
                    self.detect_region(page.url)
                    # 保活并更新 Cookie
                    page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                    new_s = self.get_session(context)
                    if new_s: self.save_cookie(new_s)
                else:
                    raise Exception(f"未能进入控制台，当前 URL: {page.url}")

            except Exception as e:
                self.log(f"异常: {str(e)}", "ERROR")
            finally:
                browser.close()
                if self.gost_proc:
                    self.log("正在关闭 Gost 隧道...", "INFO")
                    self.gost_proc.terminate()

if __name__ == "__main__":
    ClawAutoLogin().run()
