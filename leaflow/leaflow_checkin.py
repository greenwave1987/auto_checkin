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
        self.bot_info = (self.config.get_value("BOT_INFO") or [{}])[0]
        self.gh_info = (self.config.get_value("GH_INFO") or [{}])[0]
        self.proxy_list = self.config.get_value("PROXY_INFO") or []
        
        # --- 更新变量方式 ---
        self.session_updater = SecretUpdater("GH_SESSION", config_reader=self.config)
        self.gh_session = os.getenv("GH_SESSION", "").strip()
        
        self.n = 0
        self.detected_region = None
        self.gost_proc = None

    def log(self, msg, level="INFO"):
        icons = {"INFO": "ℹ️", "SUCCESS": "✅", "ERROR": "❌", "WARN": "⚠️", "STEP": "🔹"}
        print(f"{icons.get(level, '•')} {msg}")

    def shot(self, page, name):
        self.n += 1
        path = f"shot_{self.n}_{name}.png"
        try:
            page.screenshot(path=path)
            return path
        except: return None

    # ==================== 执行流程 ====================
    def run(self):
        # 1️⃣ 启动并测试 Gost 隧道 (强制放在最前面)
        # ------------------------------------------------
        local_proxy = None
        if self.proxy_list:
            p = self.proxy_list[0]
            p_str = f"{p.get('username')}:{p.get('password')}@{p.get('server')}:{p.get('port')}"
            
            self.log(f"启动 Gost 隧道: 127.0.0.1:8080 -> {p.get('server')}", "STEP")
            try:
                if os.path.exists("./gost"):
                    os.chmod("./gost", 0o755)
                
                # 严格按照你提供的方法启动
                self.gost_proc = subprocess.Popen(
                    ["./gost", "-L=:8080", f"-F=socks5://{p_str}"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
                )
                time.sleep(5)
                
                # 测试隧道
                local_proxy = "http://127.0.0.1:8080"
                res = requests.get("https://api.ipify.org", 
                                   proxies={"http": local_proxy, "https": local_proxy}, 
                                   timeout=15)
                self.log(f"隧道就绪，出口 IP: {res.text.strip()}", "SUCCESS")
            except Exception as e:
                self.log(f"隧道启动或测试失败: {e}", "ERROR")
                if self.gost_proc: self.gost_proc.terminate()
                local_proxy = None # 失败则尝试直连或报错

        # 2️⃣ 启动浏览器
        # ------------------------------------------------
        with sync_playwright() as p:
            self.log("启动浏览器...", "INFO")
            browser = p.chromium.launch(
                headless=True,
                args=['--no-sandbox', '--disable-dev-shm-usage'],
                proxy={"server": local_proxy} if local_proxy else None
            )
            context = browser.new_context(
                viewport={'width': 1280, 'height': 800},
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            )
            
            if self.gh_session:
                context.add_cookies([{'name': 'user_session', 'value': self.gh_session, 'domain': 'github.com', 'path': '/'}])

            page = context.new_page()

            try:
                # 3️⃣ 访问与登录 (严格执行你提供的流程)
                # ------------------------------------------------
                self.log("步骤1: 打开 ClawCloud 登录页", "STEP")
                page.goto("https://console.run.claw.cloud/signin", timeout=60000)
                page.wait_for_load_state('networkidle', timeout=30000)
                time.sleep(2)
                self.shot(page, "clawcloud")
                
                cur_url = page.url
                self.log(f"当前 URL: {cur_url}")
                
                if 'signin' not in cur_url.lower() and 'claw.cloud' in cur_url:
                    self.log("已通过 Session 登录！", "SUCCESS")
                else:
                    self.log("步骤2: 点击 GitHub", "STEP")
                    btns = ['button:has-text("GitHub")', 'a:has-text("GitHub")', '[data-provider="github"]']
                    clicked = False
                    for s in btns:
                        if page.locator(s).count() > 0:
                            page.click(s)
                            clicked = True
                            break
                    if not clicked: 
                        raise Exception("找不到 GitHub 按钮")
                    
                    time.sleep(5)
                    page.wait_for_load_state('networkidle', timeout=60000)
                    
                    if 'github.com/login' in page.url:
                        self.log("步骤3: GitHub 账号登录", "STEP")
                        page.fill('input[name="login"]', self.gh_info.get("username", ""))
                        page.fill('input[name="password"]', self.gh_info.get("password", ""))
                        page.click('input[type="submit"]')
                        time.sleep(5)
                        
                        if "device-verification" in page.url:
                            self.log("需手机批准，等待 30s...", "WARN")
                            time.sleep(30)
                        
                        if "two-factor" in page.url:
                            secret = self.gh_info.get("2fasecret", "").replace(" ", "")
                            if secret:
                                code = pyotp.TOTP(secret).now()
                                self.log(f"填入 2FA 码: {code}", "SUCCESS")
                                page.fill('input[id="app_totp"], input[name="otp"]', code)
                                page.keyboard.press("Enter")
                                time.sleep(5)

                    if 'github.com/login/oauth/authorize' in page.url:
                        self.log("步骤3: OAuth 授权", "STEP")
                        page.click('button[name="authorize"]')
                        time.sleep(5)

                # 4️⃣ 验证与收尾
                # ------------------------------------------------
                self.log("步骤4: 等待重定向", "STEP")
                page.wait_for_url(re.compile(r".*claw\.cloud.*"), timeout=60000)
                
                self.log("步骤5: 验证", "STEP")
                if 'claw.cloud' in page.url and 'signin' not in page.url.lower():
                    # 检测区域
                    parsed = urlparse(page.url)
                    self.detected_region = parsed.netloc.split('.')[0]
                    self.log(f"验证成功，区域: {self.detected_region}", "SUCCESS")
                    
                    # 保存新 Cookie
                    new_cookies = context.cookies()
                    new_s = next((c['value'] for c in new_cookies if c['name'] == 'user_session'), None)
                    if new_s:
                        self.session_updater.update(new_s)
                        self.log("GitHub Session 已回写更新", "SUCCESS")
                    
                    # 保活
                    page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                else:
                    raise Exception(f"验证失败，停留在: {page.url}")

            except Exception as e:
                self.log(f"异常: {e}", "ERROR")
                self.shot(page, "error_final")
            finally:
                browser.close()
                if self.gost_proc:
                    self.log("关闭 Gost 隧道", "INFO")
                    self.gost_proc.terminate()

if __name__ == "__main__":
    ClawAutoLogin().run()
