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

# ==================== 配置 ====================
LOGIN_ENTRY_URL = "https://console.run.claw.cloud"
SIGNIN_URL = f"{LOGIN_ENTRY_URL}/signin"
DEVICE_VERIFY_WAIT = 30  
TWO_FACTOR_WAIT = int(os.environ.get("TWO_FACTOR_WAIT", "120"))
STATUS_OK = "OK"
STATUS_FAIL = "FAIL"

class Telegram:
    def __init__(self):
        self.token = os.environ.get('TG_BOT_TOKEN')
        self.chat_id = os.environ.get('TG_CHAT_ID')
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

    def flush_updates(self):
        if not self.ok: return 0
        try:
            r = requests.get(f"https://api.telegram.org/bot{self.token}/getUpdates", params={"timeout": 0}, timeout=10)
            data = r.json()
            if data.get("ok") and data.get("result"):
                return data["result"][-1]["update_id"] + 1
        except: pass
        return 0

    def wait_code(self, timeout=120):
        if not self.ok: return None
        offset = self.flush_updates()
        deadline = time.time() + timeout
        pattern = re.compile(r"^/code\s+(\d{6,8})$")
        while time.time() < deadline:
            try:
                r = requests.get(f"https://api.telegram.org/bot{self.token}/getUpdates",
                                 params={"timeout": 20, "offset": offset}, timeout=30)
                data = r.json()
                if data.get("ok"):
                    for upd in data.get("result", []):
                        offset = upd["update_id"] + 1
                        msg = upd.get("message", {})
                        if str(msg.get("chat", {}).get("id")) == str(self.chat_id):
                            text = (msg.get("text") or "").strip()
                            m = pattern.match(text)
                            if m: return m.group(1)
            except: pass
            time.sleep(2)
        return None

class SecretUpdater:
    def __init__(self):
        self.token = os.environ.get('REPO_TOKEN')
        self.repo = os.environ.get('GITHUB_REPOSITORY')
        self.ok = bool(self.token and self.repo)

    def update(self, name, value):
        if not self.ok: return False
        try:
            from nacl import encoding, public
            headers = {"Authorization": f"token {self.token}", "Accept": "application/vnd.github.v3+json"}
            r = requests.get(f"https://api.github.com/repos/{self.repo}/actions/secrets/public-key", headers=headers, timeout=30)
            key_data = r.json()
            pk = public.PublicKey(key_data['key'].encode(), encoding.Base64Encoder())
            encrypted = public.SealedBox(pk).encrypt(value.encode())
            requests.put(f"https://api.github.com/repos/{self.repo}/actions/secrets/{name}",
                         headers=headers, json={"encrypted_value": base64.b64encode(encrypted).decode(), "key_id": key_data['key_id']}, timeout=30)
            return True
        except: return False

class AutoLogin:
    def __init__(self):
        self.server_list = os.environ.get('PROXY', '')
        self.username = os.environ.get('GH_USERNAME')
        self.password = os.environ.get('GH_PASSWORD')
        self.gh_session = os.environ.get('GH_SESSION', '').strip()
        self.totp_secret = os.environ.get("GH_2FA_SECRET")
        self.tg = Telegram()
        self.secret = SecretUpdater()
        self.shots = []
        self.logs = []
        self.n = 0
        self.detected_region = None
        self.region_base_url = None

    def log(self, msg, level="INFO"):
        icon = {"INFO": "ℹ️", "SUCCESS": "✅", "ERROR": "❌", "WARN": "⚠️", "STEP": "🔹"}.get(level, "•")
        line = f"{icon} {msg}"
        print(line)
        self.logs.append(line)

    def shot(self, page, name):
        self.n += 1
        f = f"{self.n:02d}_{name}.png"
        try:
            page.screenshot(path=f)
            self.shots.append(f)
        except: pass
        return f

    def pick_available_proxy(self):
        if not self.server_list:
            return None, "未配置代理，准备直连"
        proxies = [p.strip() for p in self.server_list.split(",") if p.strip()]
        for idx, s in enumerate(proxies, 1):
            proxy_url = s if s.startswith(("http", "socks5")) else f"http://{s}"
            self.log(f"测试代理 {idx}/{len(proxies)}: {proxy_url}")
            try:
                resp = requests.get("https://myip.ipip.net", proxies={"http": proxy_url, "https": proxy_url}, timeout=10)
                if resp.status_code == 200:
                    return proxy_url, f"代理可用: {resp.text.strip()}"
            except: pass
        return None, "所有代理均不可用，切换直连"

    def detect_region(self, url):
        try:
            parsed = urlparse(url)
            host = parsed.netloc
            if host.endswith('.console.claw.cloud'):
                region = host.replace('.console.claw.cloud', '')
                if region and region != 'console':
                    self.detected_region, self.region_base_url = region, f"https://{host}"
                    self.log(f"检测到区域: {region}", "SUCCESS")
                    return region
        except: pass
        return None

    def wait_device(self, page):
        self.log(f"等待设备验证 {DEVICE_VERIFY_WAIT}s...", "WARN")
        self.tg.send("⚠️ <b>需要设备验证</b>")
        for i in range(DEVICE_VERIFY_WAIT):
            time.sleep(1)
            if i % 5 == 0:
                if 'verified-device' not in page.url and 'device-verification' not in page.url:
                    self.log("设备验证通过！", "SUCCESS")
                    return True
                try: page.reload(); page.wait_for_load_state('networkidle', timeout=10000)
                except: pass
        return 'verified-device' not in page.url

    def handle_2fa(self, page):
        self.log("处理 2FA...", "WARN")
        if self.totp_secret:
            code = pyotp.TOTP(self.totp_secret).now()
            self.log(f"生成 TOTP: {code}")
        else:
            self.tg.send("🔐 <b>等待 TG 发送 /code</b>")
            code = self.tg.wait_code(TWO_FACTOR_WAIT)
        
        if not code: return False
        
        for sel in ['input[autocomplete="one-time-code"]', 'input#app_totp', 'input[name="otp"]']:
            try:
                el = page.locator(sel).first
                if el.is_visible(timeout=3000):
                    el.fill(code)
                    page.keyboard.press("Enter")
                    time.sleep(5)
                    return "two-factor" not in page.url
            except: pass
        return False

    def run(self):
        with sync_playwright() as p:
            proxy_cfg, proxy_msg = self.pick_available_proxy()
            self.log(proxy_msg, "SUCCESS" if proxy_cfg else "WARN")
            
            browser = p.chromium.launch(headless=True, args=['--no-sandbox'], proxy={"server": proxy_cfg} if proxy_cfg else None)
            context = browser.new_context(viewport={'width': 1920, 'height': 1080})
            
            if self.gh_session:
                context.add_cookies([{'name': 'user_session', 'value': self.gh_session, 'domain': 'github.com', 'path': '/'}])
            
            page = context.new_page()
            try:
                # 步骤1: 访问 ClawCloud
                page.goto(SIGNIN_URL, timeout=60000)
                page.wait_for_load_state('networkidle')
                
                # 步骤2: 点击 GitHub
                if "signin" in page.url:
                    page.locator('button:has-text("GitHub"), [data-provider="github"]').first.click()
                    page.wait_for_load_state('networkidle')
                
                # 步骤3: GitHub 登录逻辑
                if "github.com/login" in page.url:
                    page.locator('input[name="login"]').fill(self.username)
                    page.locator('input[name="password"]').fill(self.password)
                    page.locator('input[type="submit"]').click()
                    page.wait_for_load_state('networkidle')
                
                if "two-factor" in page.url:
                    self.handle_2fa(page)
                
                if "verified-device" in page.url:
                    self.wait_device(page)

                # 步骤4: 等待跳转并检测区域
                success = False
                for _ in range(30):
                    if "claw.cloud" in page.url and "signin" not in page.url:
                        success = True
                        break
                    if "authorize" in page.url:
                        page.locator('button[name="authorize"]').click()
                    time.sleep(2)
                
                if success:
                    self.detect_region(page.url)
                    # 步骤5: 保活
                    base = self.region_base_url or LOGIN_ENTRY_URL
                    page.goto(f"{base}/apps")
                    page.wait_for_load_state('networkidle')
                    self.log("登录成功且已保活", "SUCCESS")
                    
                    # 步骤6: 更新 Cookie
                    for c in context.cookies():
                        if c['name'] == 'user_session' and 'github' in c['domain']:
                            self.secret.update('GH_SESSION', c['value'])
                            break
                    self.tg.send(f"✅ ClawCloud 登录成功\n区域: {self.detected_region}")
                else:
                    self.log("登录失败", "ERROR")
                    self.tg.send("❌ ClawCloud 登录失败")
                    
            except Exception as e:
                self.log(f"异常: {str(e)}", "ERROR")
            finally:
                browser.close()

if __name__ == "__main__":
    AutoLogin().run()
