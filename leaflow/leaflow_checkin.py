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

    def run(self):
        # 1️⃣ 代理启动阶段 (强制最优先，必须看到日志)
        # ------------------------------------------------
        local_proxy = None
        
        # 只要配置列表不为空就执行
        if len(self.proxy_list) > 0:
            p = self.proxy_list[0]
            proxy_str = f"{p.get('username')}:{p.get('password')}@{p.get('server')}:{p.get('port')}"
            
            self.log(f"步骤 0: 准备启动 Gost 隧道 -> {p.get('server')}", "STEP")
            
            try:
                if os.path.exists("./gost"):
                    os.chmod("./gost", 0o755)
                
                # 启动 Gost
                self.gost_proc = subprocess.Popen(
                    ["./gost", "-L=:8080", f"-F=socks5://{proxy_str}"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
                )
                
                self.log("正在等待隧道建立 (5s)...", "INFO")
                time.sleep(5)
                
                # 测试隧道
                local_proxy = "http://127.0.0.1:8080"
                res = requests.get("https://api.ipify.org", 
                                   proxies={"http": local_proxy, "https": local_proxy}, 
                                   timeout=15)
                self.log(f"✅ 隧道就绪，出口 IP: {res.text.strip()}", "SUCCESS")
            except Exception as e:
                self.log(f"❌ 隧道测试失败: {e}", "ERROR")
                if self.gost_proc: self.gost_proc.terminate()
                local_proxy = None # 失败后会尝试直连
        else:
            self.log("未读取到 PROXY_INFO，跳过代理启动", "WARN")

        # 2️⃣ 浏览器执行阶段
        # ------------------------------------------------
        with sync_playwright() as p:
            self.log("正在初始化 Chromium 浏览器...", "INFO")
            browser = p.chromium.launch(
                headless=True,
                args=['--no-sandbox', '--disable-dev-shm-usage'],
                proxy={"server": local_proxy} if local_proxy else None
            )
            context = browser.new_context(
                viewport={'width': 1280, 'height': 800},
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            )
            
            # 注入旧 Session 绕过登录
            if self.gh_session:
                context.add_cookies([{'name': 'user_session', 'value': self.gh_session, 'domain': 'github.com', 'path': '/'}])

            page = context.new_page()

            try:
                # 严格按照你要求的登录检测部分
                self.log("步骤1: 打开 ClawCloud 登录页", "STEP")
                page.goto("https://console.run.claw.cloud/signin", timeout=60000)
                page.wait_for_load_state('networkidle', timeout=30000)
                time.sleep(2)
                self.shot(page, "clawcloud")
                
                current_url = page.url
                self.log(f"当前 URL: {current_url}")
                
                # 检测是否已登录
                if 'signin' not in current_url.lower() and 'claw.cloud' in current_url:
                    self.log("已通过 Cookie 登录！", "SUCCESS")
                else:
                    # 步骤2: 点击 GitHub
                    self.log("步骤2: 点击 GitHub 登录", "STEP")
                    github_selectors = ['button:has-text("GitHub")', '[data-provider="github"]', 'a:has-text("GitHub")']
                    clicked = False
                    for s in github_selectors:
                        if page.locator(s).count() > 0:
                            page.click(s)
                            clicked = True
                            break
                    if not clicked: raise Exception("找不到 GitHub 按钮")
                    
                    time.sleep(3)
                    page.wait_for_load_state('networkidle', timeout=60000)
                    
                    # 步骤3: GitHub 账号登录逻辑
                    if 'github.com/login' in page.url or 'github.com/session' in page.url:
                        self.log("正在执行 GitHub 账号密码填充", "STEP")
                        page.fill('input[name="login"]', self.gh_info.get("username", ""))
                        page.fill('input[name="password"]', self.gh_info.get("password", ""))
                        page.click('input[type="submit"]')
                        time.sleep(5)
                        
                        if "device-verification" in page.url:
                            self.log("需手机确认批准 (30s)...", "WARN")
                            time.sleep(30)
                        
                        if "two-factor" in page.url:
                            totp_key = self.gh_info.get("2fasecret", "").replace(" ", "")
                            if totp_key:
                                code = pyotp.TOTP(totp_key).now()
                                self.log(f"自动填入 TOTP: {code}", "SUCCESS")
                                page.fill('input[id="app_totp"], input[name="otp"]', code)
                                page.keyboard.press("Enter")
                                time.sleep(5)

                    if 'github.com/login/oauth/authorize' in page.url:
                        self.log("处理 OAuth 授权页面", "STEP")
                        page.click('button[name="authorize"]')
                        time.sleep(5)

                # 4️⃣ 等待重定向与区域验证
                self.log("步骤4: 等待最终页面重定向", "STEP")
                page.wait_for_url(re.compile(r".*claw\.cloud.*"), timeout=60000)
                
                self.log("步骤5: 验证登录状态", "STEP")
                if 'claw.cloud' in page.url and 'signin' not in page.url.lower():
                    # 检测区域
                    region = urlparse(page.url).netloc.split('.')[0]
                    self.log(f"登录成功! 区域: {region}", "SUCCESS")
                    
                    # 提取并回写 Session (核心要求)
                    new_cookies = context.cookies()
                    new_val = next((c['value'] for c in new_cookies if c['name'] == 'user_session'), None)
                    if new_val:
                        self.session_updater.update(new_val)
                        self.log("GitHub Session 变量已更新回写", "SUCCESS")
                    
                    # 页面保活
                    page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                else:
                    raise Exception(f"未能成功进入控制台，停留在: {page.url}")

            except Exception as e:
                self.log(f"异常: {str(e)}", "ERROR")
                self.shot(page, "error_detail")
            finally:
                browser.close()
                if self.gost_proc:
                    self.log("正在清理并关闭 Gost 进程", "INFO")
                    self.gost_proc.terminate()

if __name__ == "__main__":
    ClawAutoLogin().run()
