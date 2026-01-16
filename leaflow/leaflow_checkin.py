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
        self.config = ConfigReader()
        
        # --- 增强型数据读取 ---
        raw_proxy = self.config.get_value("PROXY_INFO")
        # 兼容 {"value": [...]} 或直接 [...]
        if isinstance(raw_proxy, dict) and "value" in raw_proxy:
            self.proxy_list = raw_proxy["value"]
        else:
            self.proxy_list = raw_proxy or []

        self.bot_info = (self.config.get_value("BOT_INFO") or [{}])[0]
        self.gh_info = (self.config.get_value("GH_INFO") or [{}])[0]
        
        # --- 更新变量方式 ---
        self.session_updater = SecretUpdater("GH_SESSION", config_reader=self.config)
        self.gh_session = os.getenv("GH_SESSION", "").strip()
        
        self.n = 0
        self.gost_proc = None

    def log(self, msg, level="INFO"):
        icons = {"INFO": "ℹ️", "SUCCESS": "✅", "ERROR": "❌", "WARN": "⚠️", "STEP": "🔹"}
        print(f"{icons.get(level, '•')} {msg}")

    def run(self):
        # 强制打印诊断
        self.log(f"诊断：读取到代理数量 = {len(self.proxy_list)}")
        
        local_proxy = "http://127.0.0.1:8080"
        
        # ------------------------------------------------
        # 1️⃣ 启动 Gost 隧道 (强制执行)
        # ------------------------------------------------
        if not self.proxy_list:
            self.log("致命错误：PROXY_INFO 为空，无法启动代理流程", "ERROR")
            # 这里如果不退出，后面 page.goto 必然 ERR_EMPTY_RESPONSE
        else:
            p = self.proxy_list[0]
            p_str = f"{p.get('username')}:{p.get('password')}@{p.get('server')}:{p.get('port')}"
            
            self.log(f"步骤 0: 启动隧道 [gost -L=:8080 -F=socks5://{p.get('server')}]", "STEP")
            
            try:
                if os.path.exists("./gost"):
                    os.chmod("./gost", 0o755)
                
                # 显式启动进程
                self.gost_proc = subprocess.Popen(
                    ["./gost", "-L=:8080", f"-F=socks5://{p_str}"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
                )
                
                self.log("正在验证隧道可用性 (5s)...", "INFO")
                time.sleep(5)
                
                # 测试隧道
                res = requests.get("https://api.ipify.org", 
                                   proxies={"http": local_proxy, "https": local_proxy}, 
                                   timeout=15)
                self.log(f"✅ 隧道就绪，出口 IP: {res.text.strip()}", "SUCCESS")
            except Exception as e:
                self.log(f"❌ 隧道建立失败: {e}", "ERROR")
                # 即使失败也记录下来，方便调试

        # ------------------------------------------------
        # 2️⃣ 启动浏览器 (带上代理参数)
        # ------------------------------------------------
        with sync_playwright() as p:
            self.log("初始化 Chromium 浏览器...", "INFO")
            browser = p.chromium.launch(
                headless=True,
                args=['--no-sandbox', '--disable-dev-shm-usage'],
                proxy={"server": local_proxy} # 强制使用 gost 监听的 8080
            )
            context = browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            )
            
            if self.gh_session:
                context.add_cookies([{'name': 'user_session', 'value': self.gh_session, 'domain': 'github.com', 'path': '/'}])

            page = context.new_page()

            try:
                # 严格按照要求的登录部分
                self.log("步骤1: 打开 ClawCloud 登录页", "STEP")
                page.goto("https://console.run.claw.cloud/signin", timeout=60000)
                page.wait_for_load_state('networkidle', timeout=30000)
                time.sleep(2)
                
                cur_url = page.url
                self.log(f"当前 URL: {cur_url}")
                
                if 'signin' not in cur_url.lower() and 'claw.cloud' in cur_url:
                    self.log("Session 有效，已进入控制台", "SUCCESS")
                else:
                    self.log("步骤2: 点击 GitHub 登录", "STEP")
                    page.click('button:has-text("GitHub"), [data-provider="github"]', timeout=15000)
                    time.sleep(5)
                    
                    if 'github.com/login' in page.url:
                        self.log("步骤3: 填充 GitHub 认证信息", "STEP")
                        page.fill('input[name="login"]', self.gh_info.get("username", ""))
                        page.fill('input[name="password"]', self.gh_info.get("password", ""))
                        page.click('input[type="submit"]')
                        time.sleep(5)
                        
                        if "two-factor" in page.url:
                            secret = self.gh_info.get("2fasecret", "").replace(" ", "")
                            if secret:
                                code = pyotp.TOTP(secret).now()
                                self.log(f"自动填入 2FA 码: {code}", "SUCCESS")
                                page.fill('input[id="app_totp"], input[name="otp"]', code)
                                page.keyboard.press("Enter")
                                time.sleep(5)

                    if 'github.com/login/oauth/authorize' in page.url:
                        self.log("执行 OAuth 授权点击", "STEP")
                        page.click('button[name="authorize"]')
                        time.sleep(5)

                # 4. 验证重定向
                self.log("步骤4: 等待最终页面", "STEP")
                page.wait_for_url(re.compile(r".*claw\.cloud.*"), timeout=60000)
                
                if 'claw.cloud' in page.url and 'signin' not in page.url.lower():
                    self.log("登录验证完成", "SUCCESS")
                    # 提取并保存新 Cookie
                    new_cookies = context.cookies()
                    new_s = next((c['value'] for c in new_cookies if c['name'] == 'user_session'), None)
                    if new_s:
                        self.session_updater.update(new_s)
                        self.log("GH_SESSION 已同步至 Secrets", "SUCCESS")
                    
                    page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                else:
                    raise Exception(f"未进入主页, 当前: {page.url}")

            except Exception as e:
                self.log(f"运行异常: {e}", "ERROR")
            finally:
                browser.close()
                if self.gost_proc:
                    self.log("清理 Gost 隧道进程")
                    self.gost_proc.terminate()

if __name__ == "__main__":
    ClawAutoLogin().run()
