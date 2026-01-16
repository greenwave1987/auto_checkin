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
        
        # --- 核心修正：精准解析 PROXY_INFO ---
        raw_proxy = self.config.get_value("PROXY_INFO")
        self.proxy_list = []
        
        # 如果返回的是字典且包含 value 键
        if isinstance(raw_proxy, dict) and "value" in raw_proxy:
            self.proxy_list = raw_proxy["value"]
        # 如果直接返回的是列表
        elif isinstance(raw_proxy, list):
            self.proxy_list = raw_proxy
            
        self.bot_info = (self.config.get_value("BOT_INFO") or [{}])[0]
        self.gh_info = (self.config.get_value("GH_INFO") or [{}])[0]
        self.session_updater = SecretUpdater("GH_SESSION", config_reader=self.config)
        self.gh_session = os.getenv("GH_SESSION", "").strip()
        self.gost_proc = None

    def log(self, msg, level="INFO"):
        icons = {"INFO": "ℹ️", "SUCCESS": "✅", "ERROR": "❌", "STEP": "🔹"}
        print(f"{icons.get(level, '•')} {msg}")

    def run(self):
        # 1. 强制打印配置诊断
        print(f"DEBUG: 原始代理数据类型: {type(self.config.get_value('PROXY_INFO'))}")
        self.log(f"实际解析到的代理数量: {len(self.proxy_list)}")

        # 2. 启动 Gost 隧道 (强制前置)
        local_proxy = "http://127.0.0.1:8080"
        
        if not self.proxy_list:
            self.log("致命错误：没有读取到有效的代理列表，脚本终止防止直连报错", "ERROR")
            sys.exit(1) # 强制退出，不再往下走

        # 提取第一个代理
        p = self.proxy_list[0]
        p_str = f"{p.get('username')}:{p.get('password')}@{p.get('server')}:{p.get('port')}"
        
        self.log(f"步骤 0: 启动 Gost 隧道 -> {p.get('server')}", "STEP")
        try:
            if os.path.exists("./gost"):
                os.chmod("./gost", 0o755)
            
            # 显式启动进程
            self.gost_proc = subprocess.Popen(
                ["./gost", "-L=:8080", f"-F=socks5://{p_str}"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
            
            self.log("等待隧道就绪...", "INFO")
            time.sleep(5)
            
            # 测试出口
            res = requests.get("https://api.ipify.org", 
                               proxies={"http": local_proxy, "https": local_proxy}, 
                               timeout=15)
            self.log(f"隧道出口测试成功: {res.text.strip()}", "SUCCESS")
        except Exception as e:
            self.log(f"隧道启动失败: {e}", "ERROR")
            if self.gost_proc: self.gost_proc.terminate()
            sys.exit(1)

        # 3. 启动浏览器
        with sync_playwright() as p:
            self.log("启动 Playwright (使用代理 127.0.0.1:8080)...", "INFO")
            browser = p.chromium.launch(
                headless=True,
                args=['--no-sandbox', '--disable-dev-shm-usage'],
                proxy={"server": local_proxy} 
            )
            context = browser.new_context(user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36")
            
            if self.gh_session:
                context.add_cookies([{'name': 'user_session', 'value': self.gh_session, 'domain': 'github.com', 'path': '/'}])

            page = context.new_page()

            try:
                # 步骤1: 访问
                self.log("步骤1: 打开 ClawCloud 登录页", "STEP")
                page.goto("https://console.run.claw.cloud/signin", timeout=60000)
                page.wait_for_load_state('networkidle')
                
                # 严格按照你要求的逻辑
                if 'signin' not in page.url.lower() and 'claw.cloud' in page.url:
                    self.log("已自动登录", "SUCCESS")
                else:
                    self.log("步骤2: 点击 GitHub", "STEP")
                    page.click('button:has-text("GitHub"), [data-provider="github"]')
                    time.sleep(5)
                    
                    if 'github.com/login' in page.url:
                        self.log("步骤3: GitHub 认证", "STEP")
                        page.fill('input[name="login"]', self.gh_info.get("username", ""))
                        page.fill('input[name="password"]', self.gh_info.get("password", ""))
                        page.click('input[type="submit"]')
                        time.sleep(5)
                        
                        if "two-factor" in page.url:
                            code = pyotp.TOTP(self.gh_info.get("2fasecret", "").replace(" ", "")).now()
                            page.fill('input[id="app_totp"], input[name="otp"]', code)
                            page.keyboard.press("Enter")
                            time.sleep(5)

                    if 'github.com/login/oauth/authorize' in page.url:
                        page.click('button[name="authorize"]')

                # 验证与收尾
                page.wait_for_url(re.compile(r".*claw\.cloud.*"), timeout=60000)
                if 'claw.cloud' in page.url and 'signin' not in page.url.lower():
                    self.log("最终验证成功", "SUCCESS")
                    new_cookies = context.cookies()
                    new_s = next((c['value'] for c in new_cookies if c['name'] == 'user_session'), None)
                    if new_s:
                        self.session_updater.update(new_s)
                        self.log("GH_SESSION 已同步更新", "SUCCESS")
                else:
                    raise Exception("未能进入控制台")

            except Exception as e:
                self.log(f"运行异常: {e}", "ERROR")
            finally:
                browser.close()
                if self.gost_proc:
                    self.gost_proc.terminate()

if __name__ == "__main__":
    ClawAutoLogin().run()
