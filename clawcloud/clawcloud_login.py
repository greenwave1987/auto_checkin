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
        
        # 1. 获取所有账号 (GH_INFO 是个列表)
        self.accounts = self.config.get_value("GH_INFO") or []
        
        # 2. 获取所有代理 (严格对应)
        raw_proxy = self.config.get_value("PROXY_INFO")
        if isinstance(raw_proxy, dict) and "value" in raw_proxy:
            self.proxy_list = raw_proxy["value"]
        else:
            self.proxy_list = raw_proxy if isinstance(raw_proxy, list) else []

        # 3. TG 配置
        self.bot_info = (self.config.get_value("BOT_INFO") or [{}])[0]
        self.tg_token = self.bot_info.get("token")
        self.tg_chat_id = self.bot_info.get("id")

        self.session_updater = SecretUpdater("GH_SESSION", config_reader=self.config)
        self.gost_proc = None

    def log(self, msg, level="INFO"):
        icons = {"INFO": "ℹ️", "SUCCESS": "✅", "ERROR": "❌", "STEP": "🔹", "WARN": "⚠️"}
        print(f"{icons.get(level, '•')} {msg}")

    def send_tg_photo(self, photo_path, caption):
        """发送图片和文字到 Telegram"""
        if not self.tg_token or not self.tg_chat_id:
            return
        url = f"https://api.telegram.org/bot{self.tg_token}/sendPhoto"
        try:
            with open(photo_path, "rb") as photo:
                requests.post(url, data={"chat_id": self.tg_chat_id, "caption": caption}, files={"photo": photo}, timeout=20)
        except Exception as e:
            self.log(f"发送 TG 失败: {e}", "WARN")

    def stop_gost(self):
        if self.gost_proc:
            try:
                self.gost_proc.terminate()
                self.gost_proc = None
                self.log("Gost 隧道已关闭")
            except: pass

    def start_gost(self, proxy_data):
        """为特定账号启动代理，如果不通则返回 None 触发直连"""
        if not proxy_data:
            self.log("未分配代理，准备直连", "WARN")
            return None

        p_str = f"{proxy_data.get('username')}:{proxy_data.get('password')}@{proxy_data.get('server')}:{proxy_data.get('port')}"
        local_proxy = "http://127.0.0.1:8080"
        
        try:
            if os.path.exists("./gost"): os.chmod("./gost", 0o755)
            self.gost_proc = subprocess.Popen(
                ["./gost", "-L=:8080", f"-F=socks5://{p_str}"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
            time.sleep(5)
            # 测试
            requests.get("https://api.ipify.org", proxies={"http": local_proxy, "https": local_proxy}, timeout=10)
            self.log(f"代理就绪: {proxy_data.get('server')}", "SUCCESS")
            return local_proxy
        except Exception as e:
            self.log(f"代理不通，切换直连模式: {e}", "WARN")
            self.stop_gost()
            return None

    def process_account(self, idx, account):
        """处理单个账号的登录逻辑"""
        username = account.get("username")
        self.log(f"--- 正在处理账号 ({idx+1}/{len(self.accounts)}): {username} ---", "STEP")
        
        # 1. 获取对应的代理 (一对一)
        current_proxy_data = self.proxy_list[idx] if idx < len(self.proxy_list) else None
        local_proxy = self.start_gost(current_proxy_data)

        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=['--no-sandbox', '--disable-dev-shm-usage'],
                proxy={"server": local_proxy} if local_proxy else None
            )
            # 为每个账号建立干净的 context
            context = browser.new_context(user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36")
            page = context.new_page()

            try:
                # 步骤1: 访问 ClawCloud
                self.log("访问登录页...")
                page.goto("https://console.run.claw.cloud/signin", timeout=60000)
                page.wait_for_load_state('networkidle')
                
                # 判断是否已通过 session 登录 (这里账号轮询通常建议全新登录，不注入 session)
                if 'signin' in page.url.lower():
                    # 步骤2: 点击 GitHub
                    page.click('button:has-text("GitHub"), [data-provider="github"]')
                    time.sleep(5)
                    
                    # 步骤3: 登录 GitHub
                    if 'github.com/login' in page.url:
                        self.log("正在通过 GitHub 登录...")
                        page.fill('input[name="login"]', username)
                        page.fill('input[name="password"]', account.get("password", ""))
                        page.click('input[type="submit"]')
                        time.sleep(5)
                        
                        if "two-factor" in page.url:
                            totp = pyotp.TOTP(account.get("2fasecret", "").replace(" ", "")).now()
                            page.fill('input[id="app_totp"], input[name="otp"]', totp)
                            page.keyboard.press("Enter")
                            time.sleep(8)

                    if 'github.com/login/oauth/authorize' in page.url:
                        page.click('button[name="authorize"]')

                # 步骤4: 等待控制台重定向
                page.wait_for_url(re.compile(r".*claw\.cloud.*"), timeout=60000)
                time.sleep(5) # 等待加载完成

                # 步骤5: 获取结果与截图
                final_url = page.url
                if 'claw.cloud' in final_url and 'signin' not in final_url.lower():
                    self.log(f"登录成功! 网址: {final_url}", "SUCCESS")
                    
                    shot_path = f"success_{username}.png"
                    page.screenshot(path=shot_path, full_page=True)
                    
                    # 发送 TG 消息
                    caption = f"✅ ClawCloud 登录成功\n👤 账号: {username}\n🌐 网址: {final_url}\n📍 代理: {current_proxy_data.get('server') if local_proxy else '直连'}"
                    self.send_tg_photo(shot_path, caption)
                    
                    # 如果是主账号（第一个），可以考虑回写 session
                    if idx == 0:
                        new_s = next((c['value'] for c in context.cookies() if c['name'] == 'user_session'), None)
                        if new_s: self.session_updater.update(new_s)
                else:
                    raise Exception(f"停留在了错误页面: {final_url}")

            except Exception as e:
                self.log(f"账号 {username} 执行出错: {e}", "ERROR")
                # 失败也截个图
                err_shot = f"error_{username}.png"
                page.screenshot(path=err_shot)
                self.send_tg_photo(err_shot, f"❌ 账号 {username} 登录失败\n原因: {str(e)[:100]}")
            finally:
                browser.close()
                self.stop_gost()

    def run(self):
        if not self.accounts:
            self.log("没有检测到账号列表", "ERROR")
            return

        for i, acc in enumerate(self.accounts):
            try:
                self.process_account(i, acc)
                time.sleep(3) # 账号间稍微停顿
            except Exception as e:
                self.log(f"轮询异常: {e}", "ERROR")

if __name__ == "__main__":
    ClawAutoLogin().run()
