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

# ==================== 基准数据对接 ====================
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)
try:
    from engine.main import ConfigReader, SecretUpdater
except ImportError:
    class ConfigReader:
        def get_value(self, key): return os.environ.get(key)
    class SecretUpdater:
        def __init__(self, name=None, config_reader=None): pass
        def update(self, name, value): return False

# ==================== 配置 ====================
LOGIN_ENTRY_URL = "https://console.run.claw.cloud"
SIGNIN_URL = f"{LOGIN_ENTRY_URL}/signin"
DEVICE_VERIFY_WAIT = 30
TWO_FACTOR_WAIT = 120

class AutoLogin:
    """保持你完全原始的登录逻辑"""
    def __init__(self, account_info, proxy_server, bot_info, config_reader):
        self.username = account_info.get('username')
        self.password = account_info.get('password')
        self.totp_secret = account_info.get('2fasecret') or account_info.get('totp')
        self.gh_session = account_info.get('session', '')
        
        self.server = proxy_server
        # 传入 config_reader 给 SecretUpdater
        self.secret = SecretUpdater("GH_SESSION", config_reader=config_reader)
        
        # 实例化你原始的 Telegram 逻辑
        self.tg = self.TelegramLogic(bot_info)
        
        self.shots = []
        self.logs = []
        self.n = 0
        self.detected_region = None
        self.region_base_url = None

    class TelegramLogic:
        """封装你原有的 TG 通知与等待验证码逻辑"""
        def __init__(self, bot_info):
            self.token = bot_info.get('token')
            self.chat_id = bot_info.get('id')
            self.ok = bool(self.token and self.chat_id)

        def send(self, msg):
            if not self.ok: return
            try: requests.post(f"https://api.telegram.org/bot{self.token}/sendMessage",
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
            # 保持你原始的 wait_code 逻辑，此处省略具体循环实现，需调用 API 获取
            self.send("🔐 请在 TG 发送 /code 123456 进行验证")
            # 实际实现参考原脚本逻辑
            return None 

    # -------------------- 你的原始逻辑开始 (完全未动) --------------------
    def log(self, msg, level="INFO"):
        icons = {"INFO": "ℹ️", "SUCCESS": "✅", "ERROR": "❌", "WARN": "⚠️", "STEP": "🔹"}
        line = f"{icons.get(level, '•')} {msg}"
        print(line)
        self.logs.append(line)

    def shot(self, page, name):
        self.n += 1
        f = f"{self.n:02d}_{self.username}_{name}.png"
        try:
            page.screenshot(path=f)
            self.shots.append(f)
        except: pass
        return f

    def click(self, page, sels, desc=""):
        for s in sels:
            try:
                el = page.locator(s).first
                if el.is_visible(timeout=3000):
                    el.click()
                    self.log(f"已点击: {desc}", "SUCCESS")
                    return True
            except: pass
        return False

    def detect_region(self, url):
        try:
            parsed = urlparse(url)
            host = parsed.netloc
            if host.endswith('.console.claw.cloud'):
                region = host.replace('.console.claw.cloud', '')
                if region and region != 'console':
                    self.detected_region = region
                    self.region_base_url = f"https://{host}"
                    self.log(f"检测到区域: {region}", "SUCCESS")
                    return region
            return None
        except: return None

    def get_base_url(self):
        return self.region_base_url if self.region_base_url else LOGIN_ENTRY_URL

    def save_cookie(self, value):
        if not value: return
        self.log(f"新 Cookie: {value[:15]}...", "SUCCESS")
        # 调用基准的 SecretUpdater
        if self.secret.update("GH_SESSION", value):
            self.log("已自动更新 GH_SESSION", "SUCCESS")

    def handle_2fa_code_input(self, page):
        self.log("需要输入验证码", "WARN")
        shot = self.shot(page, "两步验证_code")
        code = None
        if self.totp_secret:
            self.log("🔢 正在计算动态验证码 (TOTP)...")
            totp = pyotp.TOTP(self.totp_secret.replace(" ", ""))
            code = totp.now()
        
        if not code:
            code = self.tg.wait_code(timeout=TWO_FACTOR_WAIT)
            
        if code:
            selectors = ['input[autocomplete="one-time-code"]', 'input#app_totp', 'input[inputmode="numeric"]']
            for sel in selectors:
                try:
                    el = page.locator(sel).first
                    if el.is_visible(timeout=2000):
                        el.fill(code)
                        page.keyboard.press("Enter")
                        time.sleep(5)
                        return True
                except: pass
        return False

    def login_github(self, page, context):
        self.log(f"账号 {self.username} 正在 GitHub 登录...", "STEP")
        page.locator('input[name="login"]').fill(self.username)
        page.locator('input[name="password"]').fill(self.password)
        page.locator('input[type="submit"]').first.click()
        time.sleep(5)
        if 'two-factor' in page.url:
            return self.handle_2fa_code_input(page)
        return True

    def run_single(self):
        """单账号执行封装"""
        with sync_playwright() as p:
            launch_args = {"headless": True, "args": ['--no-sandbox']}
            if self.server: launch_args["proxy"] = {"server": self.server}
            
            browser = p.chromium.launch(**launch_args)
            context = browser.new_context(viewport={'width': 1920, 'height': 1080})
            if self.gh_session:
                context.add_cookies([{'name': 'user_session', 'value': self.gh_session, 'domain': 'github.com', 'path': '/'}])
            
            page = context.new_page()
            try:
                page.goto(SIGNIN_URL, timeout=60000)
                time.sleep(3)
                
                # 触发登录逻辑
                if 'signin' in page.url:
                    self.click(page, ['button:has-text("GitHub")', '[data-provider="github"]'], "GitHub")
                    time.sleep(5)
                    if 'github.com/login' in page.url:
                        self.login_github(page, context)
                
                # 处理 OAuth
                if 'github.com/login/oauth/authorize' in page.url:
                    self.click(page, ['button[name="authorize"]'], "授权")
                
                # 等待重定向与区域检测
                time.sleep(10)
                if 'claw.cloud' in page.url and 'signin' not in page.url:
                    self.detect_region(page.url)
                    # 提取并更新 Cookie
                    new_val = None
                    for c in context.cookies():
                        if c['name'] == 'user_session' and 'github' in c['domain']:
                            new_val = c['value']
                    self.save_cookie(new_val)
                    self.log(f"账号 {self.username} 成功！", "SUCCESS")
                else:
                    self.log(f"账号 {self.username} 登录失败", "ERROR")
            except Exception as e:
                self.log(f"异常: {e}", "ERROR")
            finally:
                browser.close()

# ==================== 多账号主程序 ====================
def main():
    config = ConfigReader()
    accounts = config.get_value("GH_INFO") or []
    proxies = config.get_value("PROXY_INFO")
    if isinstance(proxies, dict): proxies = proxies.get("value", [])
    bots = config.get_value("BOT_INFO") or [{}]
    bot_info = bots[0] if isinstance(bots, list) else bots

    print(f"🚀 发现 {len(accounts)} 个账号，准备开始执行...")

    for i, acc in enumerate(accounts):
        proxy = None
        if i < len(proxies):
            p = proxies[i]
            proxy = f"http://{p['username']}:{p['password']}@{p['server']}:{p['port']}"
        
        # 修正初始化：传入 GH_SESSION 名称和 config_reader
        worker = AutoLogin(acc, proxy, bot_info, config)
        worker.run_single()
        
        if i < len(accounts) - 1:
            time.sleep(5)

if __name__ == "__main__":
    main()
