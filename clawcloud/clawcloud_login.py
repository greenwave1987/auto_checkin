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

# 保持你项目原有的导入方式
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)
try:
    from engine.main import ConfigReader, SecretUpdater
except ImportError:
    # 模拟类供本地调试（生产环境会走上面的 import）
    class ConfigReader:
        def get_value(self, key): return []
    class SecretUpdater:
        def __init__(self, *args, **kwargs): pass
        def update(self, val): print(f"模拟更新变量: {val}")

# ==================== 配置 ====================
LOGIN_ENTRY_URL = "https://console.run.claw.cloud"
SIGNIN_URL = f"{LOGIN_ENTRY_URL}/signin"
DEVICE_VERIFY_WAIT = 30  
TWO_FACTOR_WAIT = 120    

class Telegram:
    """Telegram 通知与交互模块"""
    def __init__(self, bot_config):
        self.token = bot_config.get("token")
        self.chat_id = bot_config.get("id")
        self.ok = bool(self.token and self.chat_id)
    
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

class ClawAutoLogin:
    def __init__(self):
        # 1. 保持原有的配置读取方式
        self.config = ConfigReader()
        
        # 获取机器人信息 (第一组)
        bot_info_list = self.config.get_value("BOT_INFO")
        self.tg = Telegram(bot_info_list[0] if bot_info_list else {})

        # 获取 GitHub 信息 (第一组)
        gh_info_list = self.config.get_value("GH_INFO")
        self.gh_info = gh_info_list[0] if gh_info_list else {}
        
        # 获取代理信息
        self.proxy_list = self.config.get_value("PROXY_INFO") or []
        
        # 2. 保持原有的变量更新方式 (SecretUpdater)
        self.session_updater = SecretUpdater("GH_SESSION", config_reader=self.config)
        self.gh_session = os.getenv("GH_SESSION", "").strip()
        
        self.logs = []
        self.n = 0

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

    def pick_available_proxy(self):
        if not self.proxy_list: return None
        for p in self.proxy_list:
            server, port = p.get('server'), p.get('port')
            user, pwd = p.get('username'), p.get('password')
            proxy_url = f"http://{user}:{pwd}@{server}:{port}"
            try:
                resp = requests.get("https://myip.ipip.net", proxies={"http": proxy_url, "https": proxy_url}, timeout=10)
                if resp.status_code == 200:
                    self.log(f"使用代理: {server}:{port}", "SUCCESS")
                    return proxy_url
            except: continue
        return None

    def handle_2fa(self, page):
        """处理 GitHub 2FA"""
        totp_secret = self.gh_info.get("2fasecret")
        code = None
        
        if totp_secret:
            try:
                code = pyotp.TOTP(totp_secret.replace(" ", "")).now()
                self.log("自动计算 TOTP 成功", "SUCCESS")
            except Exception as e:
                self.log(f"TOTP 计算失败: {e}", "WARN")
            
        if not code:
            self.log("需要手动输入 2FA，请在 TG 回复 /code", "WARN")
            self.tg.photo(self.shot(page, "2fa_wait"), "检测到 2FA，请在 120s 内回复 /code xxxxxx")
            code = self.tg.wait_code(TWO_FACTOR_WAIT)
            
        if code:
            # 兼容多种可能的验证码输入框
            selectors = ['input[name="app_otp"]', 'input#app_totp', 'input[name="otp"]', 'input#otp']
            for s in selectors:
                el = page.locator(s).first
                if el.is_visible(timeout=3000):
                    el.fill(code)
                    page.keyboard.press("Enter")
                    time.sleep(5)
                    return True
        return False

    def run(self):
        start_ts = time.time()
        with sync_playwright() as p:
            proxy_url = self.pick_available_proxy()
            
            browser = p.chromium.launch(
                headless=True, 
                args=['--no-sandbox', '--disable-dev-shm-usage'],
                proxy={"server": proxy_url} if proxy_url else None
            )
            
            context = browser.new_context(
                viewport={'width': 1280, 'height': 800},
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            )
            
            # 注入旧的 GitHub Session 以尝试跳过登录
            if self.gh_session:
                context.add_cookies([{'name': 'user_session', 'value': self.gh_session, 'domain': 'github.com', 'path': '/'}])

            page = context.new_page()
            page.set_default_timeout(60000)

            try:
                self.log("正在访问 Claw 登录页...")
                page.goto(SIGNIN_URL, wait_until="domcontentloaded")
                time.sleep(5)

                # 1. 如果还在登录页，点击 GitHub 登录
                if "signin" in page.url:
                    self.log("点击 GitHub 登录按钮", "STEP")
                    page.click('button:has-text("GitHub"), [data-provider="github"]', timeout=15000)
                    time.sleep(5)

                # 2. 处理 GitHub 账号密码输入
                if "github.com/login" in page.url:
                    self.log("填充 GitHub 表单", "STEP")
                    page.fill('input[name="login"]', self.gh_info.get("username", ""))
                    page.fill('input[name="password"]', self.gh_info.get("password", ""))
                    page.click('input[type="submit"]')
                    time.sleep(5)

                # 3. 处理移动端设备验证 (批准数字)
                if "device-verification" in page.url or "verified-device" in page.url:
                    self.log(f"检测到设备验证，请在手机 GitHub App 批准", "WARN")
                    self.tg.photo(self.shot(page, "device_verify"), "请在手机端批准登录")
                    # 等待批准
                    time.sleep(DEVICE_VERIFY_WAIT)

                # 4. 处理 2FA 验证码
                if "two-factor" in page.url:
                    self.handle_2fa(page)

                # 5. 处理 OAuth 授权页面
                if "oauth/authorize" in page.url:
                    self.log("点击 OAuth 授权", "STEP")
                    page.click('button[name="authorize"]')
                    time.sleep(5)

                # 6. 等待并确认是否进入了控制台
                page.wait_for_url(re.compile(r".*claw\.cloud.*"), timeout=60000)
                parsed = urlparse(page.url)
                
                if '.console.claw.cloud' in parsed.netloc:
                    region = parsed.netloc.split('.')[0]
                    self.log(f"登录成功! 当前区域: {region}", "SUCCESS")
                    
                    # 7. 提取并更新 Session Cookie
                    new_cookies = context.cookies()
                    new_session = next((c['value'] for c in new_cookies if c['name'] == 'user_session'), None)
                    if new_session and new_session != self.gh_session:
                        self.session_updater.update(new_session)
                        self.log("GitHub Session 已回写更新", "SUCCESS")
                    
                    duration = time.time() - start_ts
                    self.tg.send(f"✅ <b>ClawCloud 登录成功</b>\n<b>用户:</b> {self.gh_info.get('username')}\n<b>区域:</b> {region}\n<b>耗时:</b> {duration:.1f}s")
                else:
                    raise Exception(f"未预期的页面地址: {page.url}")

            except Exception as e:
                self.log(f"运行失败: {str(e)}", "ERROR")
                shot_path = self.shot(page, "error")
                if shot_path:
                    self.tg.photo(shot_path, f"❌ 任务失败: {str(e)[:150]}")
                else:
                    self.tg.send(f"❌ 任务失败 (无法截图): {str(e)[:150]}")
            finally:
                browser.close()

if __name__ == "__main__":
    ClawAutoLogin().run()
