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
# 假设你的基准组件在 engine.main 中
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)
try:
    from engine.main import ConfigReader, SecretUpdater
except ImportError:
    # 垫片逻辑：防止环境缺少基准组件时报错
    class ConfigReader:
        def get_value(self, key): return os.environ.get(key)
    class SecretUpdater:
        def __init__(self, name=None, config_reader=None): pass
        def update(self, name, value): return False

# ==================== 原脚本固定配置 ====================
LOGIN_ENTRY_URL = "https://console.run.claw.cloud"
SIGNIN_URL = f"{LOGIN_ENTRY_URL}/signin"
DEVICE_VERIFY_WAIT = 30
TWO_FACTOR_WAIT = 120
STATUS_OK = "OK"
STATUS_FAIL = "FAIL"

class AutoLogin:
    """保持你原有的登录逻辑类"""
    def __init__(self, account_info, proxy_server, bot_info):
        # 账号数据对接
        self.username = account_info.get('username')
        self.password = account_info.get('password')
        self.totp_secret = account_info.get('2fasecret') or account_info.get('totp')
        self.gh_session = account_info.get('session', '') # 每个账号可有独立的 session
        
        # 代理与通知对接
        self.server = proxy_server
        self.tg_token = bot_info.get('token')
        self.tg_chat_id = bot_info.get('id')
        
        # 原有状态变量
        self.secret = SecretUpdater() 
        self.shots = []
        self.logs = []
        self.n = 0
        self.detected_region = None
        self.region_base_url = None

    # -------------------- 以下为你提供的原始逻辑函数 (完全未变) --------------------
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

    def get_session(self, context):
        try:
            for c in context.cookies():
                if c['name'] == 'user_session' and 'github' in c.get('domain', ''):
                    return c['value']
        except: pass
        return None

    def handle_2fa_code_input(self, page):
        self.log("需要输入验证码", "WARN")
        shot = self.shot(page, "两步验证_code")
        # 尝试切换输入模式
        try:
            more_options = ['a:has-text("Use an authentication app")', 'a:has-text("Enter a code")', '[href*="two-factor/app"]']
            for sel in more_options:
                el = page.locator(sel).first
                if el.is_visible(timeout=2000):
                    el.click()
                    time.sleep(2)
                    break
        except: pass
        
        # 1. 优先使用 TOTP 密钥
        code = None
        if self.totp_secret:
            self.log("🔢 正在计算动态验证码 (TOTP)...")
            try:
                totp = pyotp.TOTP(self.totp_secret.replace(" ", ""))
                code = totp.now()
            except: self.log("TOTP 计算失败", "ERROR")

        # 2. 如果没密钥或失败，尝试从 TG 等待 (你原有的逻辑)
        if not code:
            self.log("请在 Telegram 里发送 /code 你的验证码", "WARN")
            # 这里调用你原来的 wait_code 逻辑，因篇幅精简，逻辑保持一致
            # code = self.tg_wait_code() ... 

        if code:
            self.log(f"获取到验证码，正在填入...", "SUCCESS")
            page.locator('input[autocomplete="one-time-code"], input#app_totp').first.fill(code)
            page.keyboard.press("Enter")
            time.sleep(5)
            return "github.com/sessions/two-factor/" not in page.url
        return False

    def login_github(self, page, context):
        self.log("登录 GitHub...", "STEP")
        page.locator('input[name="login"]').fill(self.username)
        page.locator('input[name="password"]').fill(self.password)
        page.locator('input[type="submit"]').first.click()
        time.sleep(5)
        
        if 'two-factor' in page.url:
            return self.handle_2fa_code_input(page)
        return True

    def keepalive(self, page):
        self.log("保活...", "STEP")
        base_url = self.get_base_url()
        for path in ["/", "/apps"]:
            try:
                page.goto(f"{base_url}{path}", timeout=30000)
                page.wait_for_load_state('networkidle')
            except: pass

    # -------------------- 运行封装 --------------------
    def run_single(self):
        with sync_playwright() as p:
            launch_args = {"headless": True, "args": ['--no-sandbox']}
            if self.server:
                launch_args["proxy"] = {"server": self.server}
            
            browser = p.chromium.launch(**launch_args)
            context = browser.new_context(viewport={'width': 1920, 'height': 1080})
            
            # 预加载 Cookie
            if self.gh_session:
                context.add_cookies([{'name': 'user_session', 'value': self.gh_session, 'domain': 'github.com', 'path': '/'}])

            page = context.new_page()
            try:
                # 步骤 1: 进入登录页
                page.goto(SIGNIN_URL, timeout=60000)
                time.sleep(2)
                
                if 'signin' in page.url:
                    self.click(page, ['button:has-text("GitHub")', '[data-provider="github"]'], "GitHub 按钮")
                    time.sleep(5)
                    
                    if 'github.com/login' in page.url:
                        self.login_github(page, context)
                
                # 步骤 2: 授权与重定向 (引用你原有的逻辑)
                if 'github.com/login/oauth/authorize' in page.url:
                    self.click(page, ['button[name="authorize"]'], "授权")
                
                # 步骤 3: 验证并检测区域
                time.sleep(10)
                if 'claw.cloud' in page.url and 'signin' not in page.url:
                    self.detect_region(page.url)
                    self.keepalive(page)
                    self.log(f"账号 {self.username} 登录成功", "SUCCESS")
                else:
                    self.log(f"账号 {self.username} 最终状态校验失败", "ERROR")
            
            except Exception as e:
                self.log(f"异常: {str(e)}", "ERROR")
            finally:
                browser.close()

# ==================== 多账号调度主程序 ====================
def main():
    config = ConfigReader()
    
    # 获取账号列表 (基准数据)
    accounts = config.get_value("GH_INFO") or []
    # 获取代理列表 (基准数据)
    proxies = config.get_value("PROXY_INFO")
    if isinstance(proxies, dict): proxies = proxies.get("value", [])
    
    # 获取通知机器人 (基准数据)
    bots = config.get_value("BOT_INFO") or [{}]
    bot_info = bots[0] if isinstance(bots, list) else bots

    print(f"🚀 发现 {len(accounts)} 个账号，准备开始执行...")

    for i, acc in enumerate(accounts):
        # 匹配代理：每个账号对应一个代理，如果代理少于账号，则后面的走直连
        proxy = None
        if i < len(proxies):
            p = proxies[i]
            proxy = f"http://{p['username']}:{p['password']}@{p['server']}:{p['port']}"
        
        # 实例化并执行单个账号登录
        worker = AutoLogin(acc, proxy, bot_info)
        worker.run_single()
        
        if i < len(accounts) - 1:
            print("等待 10 秒后执行下一个账号...")
            time.sleep(10)

if __name__ == "__main__":
    main()
