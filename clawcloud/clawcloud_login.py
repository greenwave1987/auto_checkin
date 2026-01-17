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
USE_PROXY = False  # 是否使用代理总开关
DEBUG_MODE = False # 调试模式

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

    def log(self, msg, level="INFO"):
        icons = {"INFO": "ℹ️", "SUCCESS": "✅", "ERROR": "❌", "STEP": "🔹", "WARN": "⚠️", "BLOCK": "🚫"}
        print(f"{icons.get(level, '•')} {msg}")

    def send_tg_photo(self, photo_path, caption):
        if not self.tg_token or not self.tg_chat_id: return
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
        if not USE_PROXY or not proxy_data:
            self.log("代理已禁用或未分配，准备直连", "WARN")
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
            # 增加拨测确认出口 IP
            res = requests.get("https://api.ipify.org", proxies={"http": local_proxy, "https": local_proxy}, timeout=10)
            self.log(f"代理就绪: {proxy_data.get('server')} (出口 IP: {res.text.strip()})", "SUCCESS")
            return local_proxy
        except Exception as e:
            self.log(f"代理验证失败，尝试直连: {e}", "WARN")
            self.stop_gost()
            return None

    def check_interception(self, page):
        """核心拦截检测逻辑"""
        content = page.content()
        # 常见拦截关键字
        blocks = {
            "Region not available": "地区不可用 (Claw 封禁了该 IP 段)",
            "Access Denied": "访问被拒绝 (WAF 拦截)",
            "Cloudflare": "触发 Cloudflare 验证码",
            "Verify you are human": "触发人机验证"
        }
        for key, val in blocks.items():
            if key.lower() in content.lower():
                return val
        return None

    def process_account(self, idx, account):
        username = account.get("username")
        self.log(f"--- 正在处理账号 ({idx+1}/{len(self.accounts)}): {username} ---", "STEP")
        
        current_proxy_data = self.proxy_list[idx] if idx < len(self.proxy_list) else None
        local_proxy = self.start_gost(current_proxy_data)

        with sync_playwright() as p:
            # 防检测启动参数
            browser = p.chromium.launch(
                headless=True,
                args=[
                    '--no-sandbox',
                    '--disable-blink-features=AutomationControlled', # 隐藏自动化标志
                    '--disable-infobars',
                    '--window-size=1920,1080'
                ],
                proxy={"server": local_proxy} if local_proxy else None
            )
            
            # 设置伪装上下文
            context = browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                viewport={'width': 1920, 'height': 1080},
                locale="en-US",      # 强制英文环境减少被拒概率
                timezone_id="UTC"    # 匹配出口 IP 时区 (或统一用 UTC)
            )
            
            # 注入额外脚本隐藏 WebDriver 属性
            context.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
            
            page = context.new_page()

            try:
                self.log("访问登录页...")
                page.goto("https://console.run.claw.cloud/signin", timeout=60000, wait_until="networkidle")
                
                # 确认是否被拦截
                block_reason = self.check_interception(page)
                if block_reason:
                    self.log(f"拦截警告: {block_reason}", "BLOCK")
                    shot_path = f"blocked_{username}.png"
                    page.screenshot(path=shot_path)
                    self.send_tg_photo(shot_path, f"🚫 账号 {username} 被拦截\n原因: {block_reason}\nIP: {current_proxy_data.get('server') if local_proxy else '直连机房'}")
                    return # 终止当前账号

                # 登录逻辑
                if 'signin' in page.url.lower():
                    self.log("点击 GitHub 登录...")
                    page.click('button:has-text("GitHub"), [data-provider="github"]', timeout=15000)
                    time.sleep(5)
                    
                    if 'github.com/login' in page.url:
                        self.log("输入 GitHub 凭据...")
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

                # 等待控制台
                page.wait_for_url(re.compile(r".*claw\.cloud.*"), timeout=60000)
                time.sleep(5) 

                final_url = page.url
                if 'claw.cloud' in final_url and 'signin' not in final_url.lower():
                    self.log(f"登录成功! 网址: {final_url}", "SUCCESS")
                    shot_path = f"success_{username}.png"
                    page.screenshot(path=shot_path, full_page=True)
                    caption = f"✅ ClawCloud 登录成功\n👤 账号: {username}\n📍 代理: {current_proxy_data.get('server') if local_proxy else '直连'}"
                    self.send_tg_photo(shot_path, caption)
                    
                    if idx == 0:
                        new_s = next((c['value'] for c in context.cookies() if c['name'] == 'user_session'), None)
                        if new_s: self.session_updater.update(new_s)
                else:
                    raise Exception(f"停留在了非控制台页面: {final_url}")

            except Exception as e:
                self.log(f"执行出错: {e}", "ERROR")
                err_shot = f"error_{username}.png"
                page.screenshot(path=err_shot)
                self.send_tg_photo(err_shot, f"❌ 账号 {username} 出错\n{str(e)[:100]}")
            finally:
                browser.close()
                self.stop_gost()

    def run(self):
        if not self.accounts:
            self.log("没有检测到账号列表", "ERROR")
            return
        for i, acc in enumerate(self.accounts):
            self.process_account(i, acc)
            time.sleep(5)

if __name__ == "__main__":
    ClawAutoLogin().run()
