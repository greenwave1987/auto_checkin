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

# 导入你项目原有的读取类和 Secret 更新器
# 假设目录结构保持不变
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)
try:
    from engine.main import ConfigReader, SecretUpdater
except ImportError:
    # 如果在本地测试没有这些类，可以根据需要 Mock 或确保路径正确
    pass

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
        
        # 2. 读取 TG 第一组配置 (你的核心需求)
        bot_info_list = self.config.get_value("BOT_INFO")
        if bot_info_list and len(bot_info_list) > 0:
            self.tg = Telegram(bot_info_list[0])
        else:
            print("❌ Config 中未找到有效 BOT_INFO")
            sys.exit(1)

        # 3. 读取 Github 和 代理信息
        # 假设取第一组账号
        gh_info_list = self.config.get_value("GH_INFO")
        self.gh_info = gh_info_list[0] if gh_info_list else {}
        self.proxy_list = self.config.get_value("PROXY_INFO") or []
        
        # 4. 初始化 Secret 更新器 (用于回写 Session)
        self.session_updater = SecretUpdater("GH_SESSION", config_reader=self.config)
        # 尝试从环境变量获取现有 Session
        self.gh_session = os.getenv("GH_SESSION", "").strip()
        
        self.shots = []
        self.logs = []
        self.detected_region = None
        self.n = 0

    def log(self, msg, level="INFO"):
        icons = {"INFO": "ℹ️", "SUCCESS": "✅", "ERROR": "❌", "WARN": "⚠️", "STEP": "🔹"}
        line = f"{icons.get(level, '•')} {msg}"
        print(line)
        self.logs.append(line)

    def shot(self, page, name):
        self.n += 1
        f = f"shot_{self.n}_{name}.png"
        try:
            page.screenshot(path=f)
            self.shots.append(f)
        except: pass
        return f

    def pick_available_proxy(self):
        """轮询代理列表并返回第一个可用的代理 URL"""
        if not self.proxy_list:
            self.log("未配置代理信息，尝试直连")
            return None
        
        for p in self.proxy_list:
            # 兼容多种格式，构建标准 proxy url
            server = p.get('server')
            port = p.get('port')
            user = p.get('username')
            pwd = p.get('password')
            proxy_url = f"http://{user}:{pwd}@{server}:{port}"
            
            self.log(f"测试代理: {server}:{port}...")
            try:
                resp = requests.get("https://myip.ipip.net", 
                                    proxies={"http": proxy_url, "https": proxy_url}, 
                                    timeout=10)
                if resp.status_code == 200:
                    self.log(f"代理可用: {resp.text.strip()}", "SUCCESS")
                    return proxy_url
            except Exception:
                continue
        self.log("所有代理均不可用，将尝试直连", "WARN")
        return None

    def handle_2fa(self, page):
        """处理 2FA: 优先计算 TOTP，失败则求助 TG"""
        totp_secret = self.gh_info.get("2fasecret") or os.getenv("GH_2FA_SECRET")
        code = None
        
        if totp_secret:
            try:
                code = pyotp.TOTP(totp_secret.replace(" ", "")).now()
                self.log(f"自动计算 TOTP 成功", "SUCCESS")
            except: pass
            
        if not code:
            self.log("需要手动 2FA，已发送通知至 Telegram", "WARN")
            self.tg.photo(self.shot(page, "2fa_wait"), "请在 120 秒内回复 /code xxxxxx")
            code = self.tg.wait_code(TWO_FACTOR_WAIT)
            
        if code:
            selectors = ['input[name="app_otp"]', 'input#app_totp', 'input[name="otp"]']
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
            
            # 注入旧 Session 以绕过登录
            if self.gh_session:
                context.add_cookies([{'name': 'user_session', 'value': self.gh_session, 'domain': 'github.com', 'path': '/'}])

            page = context.new_page()
            page.set_default_timeout(60000) # 设置全局超时

            try:
                self.log(f"步骤1: 访问 Claw 登录页")
                # 使用 domcontentloaded 提高在慢速代理下的成功率
                page.goto(SIGNIN_URL, wait_until="domcontentloaded")
                time.sleep(3)

                # 判断登录状态
                if "signin" in page.url:
                    self.log("点击 GitHub 登录按钮", "STEP")
                    page.click('button:has-text("GitHub"), [data-provider="github"]', timeout=15000)
                    time.sleep(5)

                # 处理 GitHub 登录表单
                if "github.com/login" in page.url:
                    self.log("填充 GitHub 表单", "STEP")
                    page.fill('input[name="login"]', self.gh_info.get("username", ""))
                    page.fill('input[name="password"]', self.gh_info.get("password", ""))
                    page.click('input[type="submit"]')
                    time.sleep(5)

                # 处理设备验证 (批准数字)
                if "device-verification" in page.url or "verified-device" in page.url:
                    self.log(f"检测到设备验证，等待 {DEVICE_VERIFY_WAIT} 秒...", "WARN")
                    self.tg.photo(self.shot(page, "device_verify"), f"请在手机端批准登录")
                    time.sleep(DEVICE_VERIFY_WAIT)

                # 处理 2FA
                if "two-factor" in page.url:
                    self.handle_2fa(page)

                # 处理 OAuth 授权
                if "oauth/authorize" in page.url:
                    self.log("点击 OAuth 授权", "STEP")
                    page.click('button[name="authorize"]')
                    time.sleep(5)

                # 等待最终跳转回 Claw 并检测区域
                page.wait_for_url(re.compile(r".*claw\.cloud.*"), timeout=60000)
                parsed = urlparse(page.url)
                if '.console.claw.cloud' in parsed.netloc:
                    self.detected_region = parsed.netloc.split('.')[0]
                    self.log(f"成功进入 Claw 区域控制台: {self.detected_region}", "SUCCESS")

                # 提取并回写新的 Session Cookie
                new_cookies = context.cookies()
                new_session = next((c['value'] for c in new_cookies if c['name'] == 'user_session'), None)
                if new_session:
                    self.session_updater.update(new_session)
                    self.log("GitHub Session 已回写更新", "SUCCESS")

                # 任务完成通知
                duration = time.time() - start_ts
                self.tg.send(f"✅ <b>ClawCloud 登录成功</b>\n<b>用户:</b> {self.gh_info.get('username')}\n<b>区域:</b> {self.detected_region}\n<b>耗时:</b> {duration:.1f}s")

            except Exception as e:
                self.log(f"运行失败: {str(e)}", "ERROR")
                # 出错时强制截图并发送
                try:
                    error_shot = self.shot(page, "error_final")
                    self.tg.photo(error_shot, f"❌ 任务失败: {str(e)[:150]}")
                except:
                    self.tg.send(f"❌ 任务失败 (截图失败): {str(e)[:150]}")
            finally:
                browser.close()

if __name__ == "__main__":
    AutoLogin().run()
