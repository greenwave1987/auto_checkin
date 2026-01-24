import os
import sys
import time
import random
import base64
import datetime
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

# ==================== 路径 & 基础依赖 ====================
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)

from engine.notify import TelegramNotifier
from engine.main import ConfigReader, SecretUpdater

LOGIN_URL = "https://leaflow.net/login"
DASHBOARD_URL = "https://leaflow.net/dashboard"
BALANCE_URL = "https://leaflow.net/balance"


# ==================== 核心任务类 ====================
class LeaflowTask:
    def __init__(self):
        self.config = ConfigReader()
        self.logs = []
        self.notifier = TelegramNotifier(self.config)
        self.secret = SecretUpdater("LEAFLOW_LOCALS", config_reader=self.config)

    # ---------- 日志 ----------
    def log(self, msg, level="INFO"):
        icons = {
            "INFO": "ℹ️",
            "SUCCESS": "✅",
            "ERROR": "❌",
            "WARN": "⚠️",
            "STEP": "🔹"
        }
        line = f"{icons.get(level, '•')} {msg}"
        print(line, flush=True)
        self.logs.append(line)

    # ---------- 启动浏览器 ----------
    def open_browser(self, proxy_url=None, storage=None):
        self.log("启动 Playwright 浏览器", "STEP")

        pw = sync_playwright().start()

        launch_args = {
            "headless": True,
            "args": [
                "--no-sandbox",
                "--disable-blink-features=AutomationControlled",
                "--disable-infobars",
                "--exclude-switches=enable-automation",
            ]
        }

        if proxy_url:
            launch_args["proxy"] = {"server": proxy_url}
            self.log(f"启用代理: {proxy_url}", "INFO")
        else:
            self.log("未使用代理", "WARN")

        browser = pw.chromium.launch(**launch_args)

        context = browser.new_context(
            storage_state=storage,
            viewport={"width": 1920, "height": 1080},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/128.0.0.0 Safari/537.36"
            ),
        )

        # ---- 反检测注入 ----
        context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            Object.defineProperty(navigator, 'plugins', { get: () => [1,2,3,4,5] });
            Object.defineProperty(navigator, 'languages', { get: () => ['en-US','en'] });
            window.chrome = { runtime: {} };
            const originalQuery = window.navigator.permissions.query;
            window.navigator.permissions.query = (parameters) => (
                parameters.name === 'notifications'
                ? Promise.resolve({ state: Notification.permission })
                : originalQuery(parameters)
            );
        """)

        page = context.new_page()
        return pw, browser, context, page

    # ---------- session 校验 ----------
    def cookies_ok(self, page):
        self.log("校验 session 是否有效", "STEP")
        try:
            page.goto(DASHBOARD_URL, timeout=30000)
            page.wait_for_load_state("networkidle")
            ok = "login" not in page.url.lower()
            self.log("session 有效" if ok else "session 已失效", "SUCCESS" if ok else "WARN")
            return ok
        except Exception as e:
            self.log(f"session 校验异常: {e}", "ERROR")
            return False

    # ---------- 模拟人类登录 ----------
    def login_and_get_storage(self, page, username, password):
        self.log(f"开始登录账号 {username}", "STEP")

        page.goto(LOGIN_URL, timeout=30000)
        page.wait_for_selector("#account")

        # 输入账号
        page.fill("#account", username)
        time.sleep(random.uniform(0.3, 0.8))

        # 输入密码
        page.fill("#password", password)
        time.sleep(random.uniform(0.5, 1.2))

        # 勾选保持登录
        try:
            el = page.get_by_role("checkbox", name="保持登录状态").first
            el.wait_for(state="visible", timeout=5000)
            time.sleep(random.uniform(0.5, 1.2))
            el.hover()
            time.sleep(random.uniform(0.2, 0.4))
            el.click(force=True)
            self.log("已勾选保持登录状态", "SUCCESS")
        except PlaywrightTimeoutError:
            self.log("未找到保持登录复选框", "WARN")

        # 点击登录
        page.locator('button[type="submit"]').click()
        page.wait_for_load_state("networkidle", timeout=60000)
        time.sleep(3)

        if "login" in page.url.lower():
            raise RuntimeError("登录失败，仍停留在登录页")

        self.log("登录成功，提取 storage_state", "SUCCESS")
        return page.context.storage_state()

    # ---------- 查询余额 ----------
    def get_balance_info(self, page):
        self.log("查询余额信息", "STEP")
        page.goto(BALANCE_URL, timeout=30000)
        page.wait_for_load_state("networkidle")

        balance = page.locator('p[title="点击显示完整格式"]').text_content().strip()
        spent = page.locator('p.text-3xl.font-bold:not([title])').text_content().strip()

        msg = f"🏦余额: {balance} | 已消费: {spent}"
        self.log(msg, "INFO")
        return msg

    # ---------- 主流程 ----------
    def run(self):
        self.log("Leaflow 多账号 Session 池任务启动", "STEP")

        accounts = self.config.get_value("LF_INFO")
        proxies = self.config.get_value("PROXY_INFO")
        lf_locals = self.secret.load() or {}

        new_sessions = {}

        for idx, account in enumerate(accounts):
            username = account["username"]
            password = account["password"]

            proxy = proxies[idx] if proxies and idx < len(proxies) else None
            proxy_url = None
            if proxy:
                proxy_url = f"socks5://{proxy['username']}:{proxy['password']}@{proxy['server']}:{proxy['port']}"

            self.log(f"处理账号 {username}", "STEP")

            pw = browser = None
            try:
                storage = lf_locals.get(username)
                if storage:
                    self.log("发现已有 session，尝试注入", "INFO")

                pw, browser, ctx, page = self.open_browser(proxy_url, storage)

                if not storage or not self.cookies_ok(page):
                    storage = self.login_and_get_storage(page, username, password)
                    new_sessions[username] = storage

                balance = self.get_balance_info(page)
                self.logs.append(f"{username} {balance}")

            except Exception as e:
                self.log(f"{username} 异常: {e}", "ERROR")

            finally:
                if browser:
                    browser.close()
                if pw:
                    pw.stop()
            return
        # ---------- 回写 session ----------
        if new_sessions:
            self.log("更新 LEAFLOW_LOCALS", "STEP")
            encoded = {
                k: base64.b64encode(str(v).encode()).decode()
                for k, v in new_sessions.items()
            }
            if self.secret.update(encoded):
                self.log("Session 更新成功", "SUCCESS")
            else:
                self.log("Session 更新失败", "ERROR")

        self.log("任务完成，发送通知", "STEP")
        self.notifier.send(
            title="Leaflow 自动登录维护",
            content="\n".join(self.logs)
        )


# ==================== 入口 ====================
if __name__ == "__main__":
    LeaflowTask().run()
