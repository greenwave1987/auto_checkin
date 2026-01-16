#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import sys
import time
import base64
import re
import requests
import pyotp
from urllib.parse import urlparse
from playwright.sync_api import sync_playwright
from requests.exceptions import RequestException

# 导入你项目原有的读取类
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)
from engine.main import ConfigReader, SecretUpdater

# ==================== 配置 ====================
LOGIN_ENTRY_URL = "https://console.run.claw.cloud"
SIGNIN_URL = f"{LOGIN_ENTRY_URL}/signin"
DEVICE_VERIFY_WAIT = 30  
TWO_FACTOR_WAIT = 120    

class Telegram:
    """Telegram 通知与交互模块 - 使用 Config 第一组配置"""
    def __init__(self, bot_config):
        self.token = bot_config.get("token")
        self.chat_id = bot_config.get("id")
        self.ok = bool(self.token and self.chat_id)
        if self.ok:
            print(f"✅ TG Bot 已就绪 (ID: {self.chat_id})")
    
    def send(self, msg):
        if not self.ok: return
        try:
            requests.post(f"https://api.telegram.org/bot{self.token}/sendMessage",
                          data={"chat_id": self.chat_id, "text": msg, "parse_mode": "HTML"}, timeout=30)
        except: pass
    
    def photo(self, path, caption=""):
        if not self.ok or not os.path.exists(path): return
        try:
            with open(path, 'rb') as f:
                requests.post(f"https://api.telegram.org/bot{self.token}/sendPhoto",
                              data={"chat_id": self.chat_id, "caption": caption[:1024]},
                              files={"photo": f}, timeout=60)
        except: pass

    def wait_code(self, timeout=120):
        """等待用户在 TG 发送 /code 123456"""
        if not self.ok: return None
        offset = 0
        deadline = time.time() + timeout
        pattern = re.compile(r"^/code\s+(\d{6,8})$")
        while time.time() < deadline:
            try:
                r = requests.get(f"https://api.telegram.org/bot{self.token}/getUpdates",
                                 params={"timeout": 20, "offset": offset}, timeout=30)
                data = r.json()
                if data.get("ok") and data.get("result"):
                    for upd in data["result"]:
                        offset = upd["update_id"] + 1
                        msg = upd.get("message", {})
                        if str(msg.get("chat", {}).get("id")) == str(self.chat_id):
                            text = (msg.get("text") or "").strip()
                            m = pattern.match(text)
                            if m: return m.group(1)
            except: pass
            time.sleep(2)
        return None

class AutoLogin:
    def __init__(self):
        # 1. 初始化配置读取
        self.config = ConfigReader()
        
        # 2. 读取 TG 第一组配置
        bot_info_list = self.config.get_value("BOT_INFO")
        if bot_info_list and len(bot_info_list) > 0:
            self.tg = Telegram(bot_info_list[0])
        else:
            print("❌ Config 中未找到 BOT_INFO")
            sys.exit(1)

        # 3. 读取用户和代理（按你原有脚本逻辑）
        self.gh_info = self.config.get_value("GH_INFO")[0] # 取第一个账号
        self.proxy_info = self.config.get_value("PROXY_INFO")
        
        # 4. Secret 更新器
        self.session_updater = SecretUpdater("GH_SESSION", config_reader=self.config)
        self.gh_session = os.getenv("GH_SESSION", "").strip()
        
        self.shots = []
        self.logs = []
        self.detected_region = None
        self.region_base_url = None

    def log(self, msg, level="INFO"):
        icon = {"INFO": "ℹ️", "SUCCESS": "✅", "ERROR": "❌", "WARN": "⚠️"}.get(level, "•")
        print(f"{icon} {msg}")
        self.logs.append(f"{icon} {msg}")

    def shot(self, page, name):
        f = f"shot_{len(self.shots)}.png"
        page.screenshot(path=f); self.shots.append(f)
        return f

    def pick_available_proxy(self):
        """轮询代理列表并返回第一个可用的"""
        if not self.proxy_info: return None
        for p in self.proxy_info:
            proxy_url = f"http://{p['username']}:{p['password']}@{p['server']}:{p['port']}"
            try:
                r = requests.get("https://myip.ipip.net", proxies={"http": proxy_url, "https": proxy_url}, timeout=8)
                if r.status_code == 200:
                    self.log(f"代理可用: {r.text.strip()}", "SUCCESS")
                    return proxy_url
            except: continue
        return None

    def handle_2fa(self, page):
        """2FA 逻辑：计算 -> TG 索要"""
        shot = self.shot(page, "2fa_wait")
        code = None
        
        # 尝试自动计算
        totp_secret = self.gh_info.get("2fasecret")
        if totp_secret:
            code = pyotp.TOTP(totp_secret.replace(" ", "")).now()
            self.log(f"自动生成 TOTP: {code}")
        
        # 自动失败则求助 TG
        if not code:
            self.tg.photo(shot, "需要 2FA 验证，请在 TG 回复 /code xxxxxx")
            code = self.tg.wait_code(TWO_FACTOR_WAIT)

        if code:
            for sel in ['input[name="app_otp"]', 'input#app_totp', 'input[name="otp"]']:
                el = page.locator(sel).first
                if el.is_visible(timeout=3000):
                    el.fill(code)
                    page.keyboard.press("Enter")
                    time.sleep(5)
                    return True
        return False

    def run(self):
        with sync_playwright() as p:
            proxy = self.pick_available_proxy()
            browser = p.chromium.launch(headless=True, args=['--no-sandbox'], proxy={"server": proxy} if proxy else None)
            context = browser.new_context(viewport={'width': 1280, 'height': 800})
            
            # 注入 Session
            if self.gh_session:
                context.add_cookies([{'name': 'user_session', 'value': self.gh_session, 'domain': 'github.com', 'path': '/'}])

            page = context.new_page()
            try:
                self.log(f"正在访问 Claw: {SIGNIN_URL}")
                page.goto(SIGNIN_URL, timeout=60000)
                
                # 流程：判断是否需登录 -> 点击 GitHub -> 处理 GitHub 表单 -> 处理 2FA -> 授权 -> 区域检测
                if "signin" in page.url:
                    page.click('button:has-text("GitHub"), [data-provider="github"]', timeout=10000)
                    time.sleep(5)

                if "github.com/login" in page.url:
                    page.fill('input[name="login"]', self.gh_info["username"])
                    page.fill('input[name="password"]', self.gh_info["password"])
                    page.click('input[type="submit"]')
                    time.sleep(5)

                if "two-factor" in page.url:
                    self.handle_2fa(page)

                if "oauth/authorize" in page.url:
                    page.click('button[name="authorize"]')
                    time.sleep(5)

                # 等待重定向回 claw 并检测 URL
                page.wait_for_url(re.compile(r".*claw\.cloud.*"), timeout=60000)
                parsed = urlparse(page.url)
                if '.console.claw.cloud' in parsed.netloc:
                    self.detected_region = parsed.netloc.split('.')[0]
                    self.log(f"成功进入区域: {self.detected_region}", "SUCCESS")

                # 更新 Session
                new_s = next((c['value'] for c in context.cookies() if c['name'] == 'user_session'), None)
                if new_s:
                    self.session_updater.update(new_s)
                    self.log("GH_SESSION 已回写更新", "SUCCESS")

                self.tg.send(f"✅ <b>ClawCloud 登录成功</b>\n用户: {self.gh_info['username']}\n区域: {self.detected_region}")

            except Exception as e:
                self.log(f"致命错误: {e}", "ERROR")
                self.tg.photo(self.shot(page, "crash"), f"❌ 任务失败: {str(e)[:100]}")
            finally:
                browser.close()

if __name__ == "__main__":
    AutoLogin().run()
