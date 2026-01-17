import os
import sys
import time
import base64
import re
import requests
import pyotp
from urllib.parse import urlparse
from playwright.sync_api import sync_playwright

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

class AutoLogin:
    def __init__(self, account_info, proxy_config, bot_info, config_reader):
        self.username = account_info.get('username')
        self.password = account_info.get('password')
        self.totp_secret = account_info.get('2fasecret') or account_info.get('totp')
        self.gh_session = account_info.get('session', '')
        
        # --- 核心修正：代理处理 ---
        self.proxy = None
        if proxy_config:
            # 确保 socks5 协议头正确
            server = proxy_config.get('server')
            port = proxy_config.get('port')
            user = proxy_config.get('username')
            pwd = proxy_config.get('password')
            # Playwright 格式: socks5://user:pass@host:port
            self.proxy = {
                "server": f"socks5://{server}:{port}",
                "username": user,
                "password": pwd
            }
        
        self.secret = SecretUpdater("GH_SESSION", config_reader=config_reader)
        self.tg_token = bot_info.get('token')
        self.tg_chat_id = bot_info.get('id')
        
        self.n = 0
        self.logs = []
        self.region_base_url = None

    def log(self, msg, level="INFO"):
        icons = {"INFO": "ℹ️", "SUCCESS": "✅", "ERROR": "❌", "WARN": "⚠️"}
        print(f"{icons.get(level, '•')} {msg}")

    # -------------------- 你的原始逻辑保持不变 --------------------
    def handle_2fa(self, page):
        if self.totp_secret:
            self.log("🔢 正在计算 TOTP...")
            code = pyotp.TOTP(self.totp_secret.replace(" ", "")).now()
            page.locator('input[autocomplete="one-time-code"], input#app_totp').first.fill(code)
            page.keyboard.press("Enter")
            time.sleep(5)
            return True
        return False

    def run_single(self):
        with sync_playwright() as p:
            # 使用修正后的代理配置
            launch_args = {
                "headless": True, 
                "args": ['--no-sandbox', '--disable-setuid-sandbox']
            }
            if self.proxy:
                launch_args["proxy"] = self.proxy
                self.log(f"使用代理: {self.proxy['server']}", "INFO")

            browser = p.chromium.launch(**launch_args)
            context = browser.new_context(viewport={'width': 1920, 'height': 1080})
            
            # 注入现有 Session
            if self.gh_session:
                context.add_cookies([{'name': 'user_session', 'value': self.gh_session, 'domain': 'github.com', 'path': '/'}])

            page = context.new_page()
            try:
                self.log(f"正在访问登录页: {self.username}")
                page.goto(SIGNIN_URL, timeout=60000)
                
                # 如果没直接进去，点击 GitHub 登录
                if "signin" in page.url:
                    page.locator('button:has-text("GitHub"), [data-provider="github"]').first.click()
                    time.sleep(5)
                    
                    if "github.com/login" in page.url:
                        page.locator('input[name="login"]').fill(self.username)
                        page.locator('input[name="password"]').fill(self.password)
                        page.locator('input[type="submit"]').click()
                        time.sleep(5)
                        
                        if "two-factor" in page.url:
                            self.handle_2fa(page)

                # 授权页处理
                if "github.com/login/oauth/authorize" in page.url:
                    page.locator('button[name="authorize"]').click()

                time.sleep(10)
                if "claw.cloud" in page.url and "signin" not in page.url:
                    self.log(f"✅ 账号 {self.username} 登录成功", "SUCCESS")
                else:
                    self.log(f"❌ 账号 {self.username} 状态异常: {page.url}", "ERROR")

            except Exception as e:
                self.log(f"❌ 运行异常: {str(e)}", "ERROR")
            finally:
                browser.close()

# ==================== 主调度 ====================
def main():
    config = ConfigReader()
    accounts = config.get_value("GH_INFO") or []
    proxies = config.get_value("PROXY_INFO")
    if isinstance(proxies, dict): proxies = proxies.get("value", [])
    bots = config.get_value("BOT_INFO") or [{}]
    bot_info = bots[0] if isinstance(bots, list) else bots

    for i, acc in enumerate(accounts):
        # 匹配当前账号的代理配置对象
        current_proxy_cfg = proxies[i] if i < len(proxies) else None
        
        worker = AutoLogin(acc, current_proxy_cfg, bot_info, config)
        worker.run_single()
        
        if i < len(accounts) - 1:
            time.sleep(5)

if __name__ == "__main__":
    main()
