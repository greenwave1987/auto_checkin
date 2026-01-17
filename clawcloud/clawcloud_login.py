import os
import sys
import time
import requests
from playwright.sync_api import sync_playwright
from urllib.parse import urlparse

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
        def update(self, value): return False

# ==================== 配置与常量 ====================
SIGNIN_URL = "https://console.run.claw.cloud/signin"
STATUS_FAIL = "FAIL"

class ClawLoginTask:
    def __init__(self):
        self.config = ConfigReader()
        self.gh_session = os.environ.get("GH_SESSION")
        
        bots = self.config.get_value("BOT_INFO") or [{}]
        bot_info = bots[0] if isinstance(bots, list) else bots
        self.tg_token = bot_info.get('token')
        self.tg_chat_id = bot_info.get('id')
        
        self.secret_manager = SecretUpdater("CLAW_COOKIE", config_reader=self.config)
        
        self.detected_region = None
        self.region_base_url = None
        self.n = 0
        self.logs = []

    def log(self, msg, level="INFO"):
        icon = {"STEP": "🔹", "SUCCESS": "✅", "ERROR": "❌", "WARN": "⚠️"}.get(level, "ℹ️")
        line = f"{icon} {msg}"
        print(line)
        self.logs.append(line)

    def shot(self, page, name):
        self.n += 1
        path = f"{self.n:02d}_{name}.png"
        try:
            page.screenshot(path=path)
            return path
        except: return None

    def click(self, page, selectors, desc=""):
        for s in selectors:
            try:
                el = page.locator(s).first
                if el.is_visible(timeout=5000):
                    el.click()
                    self.log(f"点击成功: {desc}")
                    return True
            except: pass
        return False

    def detect_region(self, url):
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

    def keepalive(self, page):
        self.log("正在执行保活动作...", "STEP")
        base = self.region_base_url if self.region_base_url else "https://console.run.claw.cloud"
        for path in ["/", "/apps"]:
            try:
                page.goto(f"{base}{path}", timeout=30000)
                time.sleep(2)
            except: pass

    def get_session(self, context):
        cookies = context.cookies()
        claw_cookies = [f"{c['name']}={c['value']}" for c in cookies if "claw.cloud" in c['domain']]
        return "; ".join(claw_cookies) if claw_cookies else None

    def save_cookie(self, cookie_str):
        if self.secret_manager.update(cookie_str):
            self.log("CLAW_COOKIE 已保存至 Secrets", "SUCCESS")

    def notify(self, success, reason=""):
        msg = "\n".join(self.logs)
        if reason: msg += f"\n失败原因: {reason}"
        # 这里由外部 main 处理 TG 最终截图发送

    def login_github(self, page, context):
        # 简化版：仅检查 Session 是否直接通过
        # 如果跳到了登录页，说明 Session 无效
        return "github.com/login" not in page.url

    def oauth(self, page):
        self.log("正在处理 OAuth 授权...", "STEP")
        self.click(page, ['button[name="authorize"]'], "授权按钮")

    def wait_redirect(self, page, wait=60):
        """等待重定向并检测区域"""
        self.log("等待重定向...", "STEP")
        for i in range(wait):
            url = page.url
            
            # 检查是否已跳转到 claw.cloud
            if 'claw.cloud' in url and 'signin' not in url.lower()and 'callback' not in url.lower():
                self.log("重定向成功！", "SUCCESS")
                
                # 检测并记录区域
                self.detect_region(url)
                
                return True
            
            if 'github.com/login/oauth/authorize' in url:
                self.oauth(page)
            
            time.sleep(1)
            if i % 10 == 0:
                self.log(f"  等待... ({i}秒)")
        
        self.log("重定向超时", "ERROR")
        return False

    def send_final_report(self, photo_path, caption):
        if not self.tg_token or not self.tg_chat_id: return
        try:
            url = f"https://api.telegram.org/bot{self.tg_token}/sendPhoto"
            with open(photo_path, 'rb') as f:
                requests.post(url, data={'chat_id': self.tg_chat_id, 'caption': caption}, files={'photo': f}, timeout=30)
        except Exception as e:
            print(f"TG 发送失败: {e}")

    # ==================== 核心登录逻辑执行 ====================
    def run(self):
        if not self.gh_session:
            self.log("缺少 GH_SESSION 环境变量", "ERROR")
            return

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, args=['--no-sandbox'])
            context = browser.new_context(viewport={'width': 1280, 'height': 800})
            context.add_cookies([{'name': 'user_session', 'value': self.gh_session, 'domain': 'github.com', 'path': '/'}])
            page = context.new_page()
            
            last_screenshot = None
            try:
                # 1. 访问 ClawCloud 登录入口
                self.log("步骤1: 打开 ClawCloud 登录页", "STEP")
                page.goto(SIGNIN_URL, timeout=60000)
                page.wait_for_load_state('networkidle', timeout=30000)
                time.sleep(2)
                last_screenshot = self.shot(page, "clawcloud")
                
                current_url = page.url
                self.log(f"当前 URL: {current_url}")
                
                if 'signin' not in current_url.lower() and 'claw.cloud' in current_url:
                    self.log("已自动登录成功！", "SUCCESS")
                    self.detect_region(current_url)
                    self.keepalive(page)
                    new_cookie = self.get_session(context)
                    if new_cookie: self.save_cookie(new_cookie)
                    return True
                
                # 2. 点击 GitHub
                self.log("步骤2: 点击 GitHub", "STEP")
                if not self.click(page, [
                    'button:has-text("GitHub")',
                    'a:has-text("GitHub")',
                    '[data-provider="github"]'
                ], "GitHub"):
                    self.log("找不到按钮", "ERROR")
                    return False
                
                time.sleep(3)
                page.wait_for_load_state('networkidle', timeout=60000)
                last_screenshot = self.shot(page, "after_click")
                
                url = page.url
                self.log(f"当前 URL: {url}")
                
                # 3. GitHub 认证
                self.log("步骤3: GitHub 认证", "STEP")
                if 'github.com/login' in url or 'github.com/session' in url:
                    # 如果跳转到登录页，说明 Session 失效，按照你的简化要求直接退出
                    self.log("GH_SESSION 已失效，无法自动登录", "ERROR")
                    return False
                elif 'github.com/login/oauth/authorize' in url:
                    self.log("Cookie 有效，开始 OAuth", "SUCCESS")
                    self.oauth(page)
                
                # 4. 等待重定向
                self.log("步骤4: 等待重定向", "STEP")
                if not self.wait_redirect(page):
                    self.log("重定向超时失败", "ERROR")
                    last_screenshot = self.shot(page, "redirect_fail")
                    return False
                
                last_screenshot = self.shot(page, "redirect_success")
                
                # 5. 验证
                self.log("步骤5: 验证", "STEP")
                current_url = page.url
                if 'claw.cloud' not in current_url or 'signin' in current_url.lower():
                    self.log("最终验证失败，未进入控制台", "ERROR")
                    return False
                
                if not self.detected_region:
                    self.detect_region(current_url)
                
                # 保存 Cookie
                new_cookie = self.get_session(context)
                if new_cookie: self.save_cookie(new_cookie)
                
                # 6. 保活
                self.keepalive(page)
                last_screenshot = self.shot(page, "final_state")
                return True

            except Exception as e:
                self.log(f"运行异常: {str(e)}", "ERROR")
                return False
            finally:
                # 无论结果如何，发送最后一次截图和日志
                report_path = self.shot(page, "end_process") or last_screenshot
                self.send_final_report(report_path, "\n".join(self.logs))
                browser.close()

if __name__ == "__main__":
    task = ClawLoginTask()
    task.run()
